"""真实 LangChain 图和 ChatOpenAI 的离线协议测试，严格限制规划输出次数。"""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from langchain_openai import ChatOpenAI

from app.agents.trip_planner_agent import MultiAgentTripPlanner, PlanningError
from app.config import Settings
from app.models.schemas import TripRequest


def completion(arguments=None):
    message = {"role": "assistant", "content": "没有提交结构化行程"}
    if arguments is not None:
        message = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call_plan", "type": "function",
                "function": {
                    "name": "TripPlan",
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                },
            }],
        }
    return {
        "id": "offline", "object": "chat.completion", "created": 0, "model": "offline",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls" if arguments is not None else "stop",
            "message": message,
        }],
    }


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    responses, requests = [], []

    def reject_network(*args, **kwargs):
        raise AssertionError("离线运行时测试禁止真实 HTTP 请求")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_network)

    def respond(request):
        requests.append(request)
        assert responses, "规划器发起了超出预算的模型调用"
        return httpx.Response(200, json=responses.pop(0))

    request = TripRequest(
        city="北京", start_date="2026-10-01", end_date="2026-10-01", travel_days=1,
        transportation="步行", accommodation="经济型酒店",
    )
    attractions = [{
        "id": "offline-poi", "name": "测试博物馆", "address": "工具返回地址",
        "location": {"longitude": 116.4, "latitude": 39.9}, "type": "博物馆",
    }]
    plan = {
        "city": request.city, "start_date": request.start_date, "end_date": request.end_date,
        "days": [{
            "date": request.start_date, "day_index": 0, "description": "参观博物馆",
            "transportation": "步行", "accommodation": "自行安排",
            "attractions": [{
                "poi_id": "offline-poi", "name": "测试博物馆", "address": "模型生成的地址",
                "location": {"longitude": 0, "latitude": 0},
                "visit_duration": 90, "description": "参观展览",
            }],
        }],
        "overall_suggestions": "费用为估算，请以实际信息为准。",
    }
    settings = Settings(_env_file=None, debug=False, plan_repair_attempts=1)
    planner = MultiAgentTripPlanner(settings)
    with httpx.Client(transport=httpx.MockTransport(respond)) as sync_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as async_client:
            model = ChatOpenAI(
                model="offline", api_key="offline", base_url="https://example.invalid/v1",
                max_retries=0, http_client=sync_client, http_async_client=async_client,
            )

            async def generate():
                return await planner._generate_plan(
                    model, request,
                    json.dumps({"request": request.model_dump(), "attractions": attractions}),
                    attractions, [], [],
                )

            try:
                yield SimpleNamespace(
                    responses=responses, requests=requests, plan=plan, generate=generate,
                )
            finally:
                await model.root_async_client.close()
                model.root_client.close()


@pytest.mark.asyncio
async def test_valid_plan_needs_only_one_actual_model_call(runtime):
    runtime.responses.append(completion(runtime.plan))
    result = await runtime.generate()
    assert len(runtime.requests) == 1
    assert result.days[0].attractions[0].address == "工具返回地址"
    assert result.days[0].attractions[0].location.longitude == 116.4
    assert result.weather_info == []


@pytest.mark.asyncio
async def test_missing_structured_output_fails_after_exactly_two_actual_model_calls(runtime):
    runtime.responses.extend([completion(), completion()])
    with pytest.raises(PlanningError) as exc:
        await runtime.generate()
    assert exc.value.code == "INVALID_PLAN"
    assert len(runtime.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_output", "malformed_json", "invalid_schema", "invalid_semantics"])
async def test_one_repair_uses_one_additional_actual_model_call(runtime, failure):
    first = completion()
    if failure == "malformed_json":
        first = completion('{"city": definitely-not-json}')
    elif failure == "invalid_schema":
        first = completion({"city": "北京"})
    elif failure == "invalid_semantics":
        invalid = copy.deepcopy(runtime.plan)
        invalid["city"] = "上海"
        first = completion(invalid)
    runtime.responses.extend([first, completion(runtime.plan)])
    result = await runtime.generate()
    assert len(runtime.requests) == 2
    assert result.city == "北京"
    second_payload = json.loads(runtime.requests[1].content)
    assert "上一次输出校验失败" in second_payload["messages"][-1]["content"]
