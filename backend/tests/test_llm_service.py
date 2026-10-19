"""使用内存 HTTP 服务，离线验证配置、标准工具调用和结构化行程。"""

import json

import httpx
import langchain_openai
import pytest
import pytest_asyncio
from langchain.agents import create_agent
from langchain.agents.structured_output import StructuredOutputValidationError, ToolStrategy
from langchain_core.tools import tool

from app.config import Settings
from app.models.schemas import TripPlan
from app.services import llm_service


def make_settings(**overrides):
    return Settings(_env_file=None, **{
        "debug": False,
        "llm_api_key": "test-primary-key",
        "llm_base_url": "https://primary.example.invalid/v1",
        "llm_model_id": "offline-primary",
        "llm_timeout_seconds": 12,
        "openai_api_key": "test-legacy-key",
        "openai_base_url": "https://legacy.example.invalid/v1",
        "openai_model": "offline-legacy",
        **overrides,
    })


def completion(name, arguments, call_id="call_plan"):
    """返回标准 chat.completions tool_calls 响应，不模拟 LangChain 内部对象。"""
    return {
        "id": "chatcmpl-offline", "object": "chat.completion",
        "created": 0, "model": "offline-model",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
        }],
    }


@pytest_asyncio.fixture
async def provider(monkeypatch):
    responses, requests = [], []
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")

    def reject_network(*args, **kwargs):
        raise AssertionError("离线测试禁止真实 HTTP 请求")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)

    def respond(request):
        requests.append(request)
        assert responses, "模型发生了未预期的额外调用"
        return httpx.Response(200, json=responses.pop(0))

    # factory 仍创建真实 ChatOpenAI 和 OpenAI SDK，仅注入内存 HTTP transport。
    real_model = langchain_openai.ChatOpenAI
    with httpx.Client(transport=httpx.MockTransport(respond)) as sync_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as async_client:
            monkeypatch.setattr(
                langchain_openai, "ChatOpenAI",
                lambda **kwargs: real_model(
                    **kwargs, http_client=sync_client, http_async_client=async_client,
                ),
            )
            yield responses, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides, host, key, model", [
    ({}, "primary.example.invalid", "test-primary-key", "offline-primary"),
    (
        {"llm_api_key": "", "llm_base_url": "", "llm_model_id": ""},
        "legacy.example.invalid", "test-legacy-key", "offline-legacy",
    ),
    (
        {"llm_api_key": "", "llm_model_id": ""},
        "primary.example.invalid", "test-legacy-key", "offline-legacy",
    ),
    (
        {"llm_base_url": "https://api.deepseek.com/v1"},
        "api.deepseek.com", "test-primary-key", "offline-primary",
    ),
])
async def test_factory_tool_round_trip_and_trip_plan(provider, monkeypatch, overrides, host, key, model):
    responses, requests = provider
    tool_inputs = []

    @tool
    def maps_text_search(keywords: str, city: str) -> dict:
        """查询测试城市中的景点。"""
        tool_inputs.append((keywords, city))
        return {"pois": [{"id": "offline-poi", "name": "测试博物馆"}]}

    plan_data = {
        "city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-01",
        "days": [{
            "date": "2026-10-01", "day_index": 0, "description": "离线测试行程",
            "transportation": "步行", "accommodation": "测试酒店",
            "attractions": [{
                "name": "测试博物馆", "address": "测试地址",
                "location": {"longitude": 116.4, "latitude": 39.9},
                "visit_duration": 60, "description": "测试景点", "poi_id": "offline-poi",
            }],
        }],
        "weather_info": [{"date": "2026-10-01", "day_temp": "25°C"}],
        "overall_suggestions": "仅用于离线测试",
    }
    responses.extend([
        completion("maps_text_search", {"keywords": "博物馆", "city": "北京"}, "call_search"),
        completion("TripPlan", plan_data),
    ])
    monkeypatch.setattr(llm_service, "get_settings", lambda: make_settings(**overrides))
    agent = create_agent(
        model=llm_service.create_langchain_llm(),
        tools=[maps_text_search],
        response_format=ToolStrategy(TripPlan, handle_errors=False),
    )

    result = await agent.ainvoke({"messages": [{"role": "user", "content": "查询景点并规划一天行程"}]})

    assert len(requests) == 2
    assert tool_inputs == [("博物馆", "北京")]
    payloads = [json.loads(request.content) for request in requests]
    for request, payload in zip(requests, payloads):
        assert request.url.host == host
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {key}"
        assert payload["model"] == model
        assert request.extensions["timeout"]["read"] == 12
        if host == "api.deepseek.com":
            assert payload["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in payload
    assert {entry["function"]["name"] for entry in payloads[0]["tools"]} == {"maps_text_search", "TripPlan"}
    tool_messages = [message for message in payloads[1]["messages"] if message["role"] == "tool"]
    assert tool_messages[0]["tool_call_id"] == "call_search"
    assert "offline-poi" in tool_messages[0]["content"]
    plan = result["structured_response"]
    assert isinstance(plan, TripPlan)
    assert plan.days[0].attractions[0].poi_id == "offline-poi"
    assert plan.weather_info[0].day_temp == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_key", ["", "   "])
async def test_missing_key_fails_before_provider_request(provider, missing_key):
    _, requests = provider
    with pytest.raises(ValueError, match="LLM_API_KEY.*OPENAI_API_KEY"):
        llm_service.create_langchain_llm(make_settings(llm_api_key=missing_key, openai_api_key=""))
    assert requests == []


@pytest.mark.asyncio
async def test_invalid_trip_plan_does_not_start_model_repair_loop(provider):
    responses, requests = provider
    responses.append(completion("TripPlan", {"city": "北京"}))
    agent = create_agent(
        model=llm_service.create_langchain_llm(make_settings()),
        tools=[], response_format=ToolStrategy(TripPlan, handle_errors=False),
    )
    with pytest.raises(StructuredOutputValidationError, match="TripPlan"):
        await agent.ainvoke({"messages": [{"role": "user", "content": "生成行程"}]})
    assert len(requests) == 1
