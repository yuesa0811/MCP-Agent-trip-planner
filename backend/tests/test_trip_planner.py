"""业务编排测试：模拟模型和 MCP 连接，保留真实工具包装及结果校验。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool, ToolException
from openai import APIConnectionError

from app.agents import trip_planner_agent as module
from app.config import Settings
from app.models.schemas import TripPlan, TripRequest


def make_request(city="北京", days=1):
    return TripRequest(
        city=city, start_date="2026-10-01",
        end_date=(date(2026, 10, 1) + timedelta(days=days - 1)).isoformat(),
        travel_days=days, transportation="步行", accommodation="经济型",
        preferences=["博物馆"],
    )


def make_plan(request):
    days = []
    for index in range(request.travel_days):
        current = (date.fromisoformat(request.start_date) + timedelta(days=index)).isoformat()
        days.append({
            "date": current, "day_index": index, "description": "离线测试行程",
            "transportation": request.transportation, "accommodation": request.accommodation,
            "attractions": [{
                "poi_id": f"{request.city}-poi", "name": f"{request.city}测试博物馆",
                "address": "模型生成的错误地址", "location": {"longitude": 0, "latitude": 0},
                "visit_duration": 90, "description": "测试景点",
            }],
            "hotel": {"name": f"{request.city}测试酒店", "address": "模型生成的错误酒店地址"},
            "meals": [{"type": kind, "name": f"测试{kind}"} for kind in ("breakfast", "lunch", "dinner")],
        })
    return TripPlan.model_validate({
        "city": request.city, "start_date": request.start_date, "end_date": request.end_date,
        "days": days, "overall_suggestions": "离线测试",
        "weather_info": [{"date": day["date"], "day_weather": "模型编造的天气", "day_temp": 99} for day in days],
    })


@pytest.fixture
def settings():
    return Settings(
        _env_file=None, debug=False,
        amap_api_key="test-amap-key", amap_mcp_command="test-only-command",
        llm_api_key="test-model-key", llm_base_url="https://example.invalid/v1", llm_model_id="test-model",
        openai_api_key="", openai_base_url="https://example.invalid/v1", openai_model="unused",
        planning_timeout_seconds=5, agent_max_tool_calls=3, plan_repair_attempts=1,
    )


@pytest.fixture
def fake_runtime(monkeypatch):
    state = SimpleNamespace(
        cities=["北京"], requests={"北京": make_request()}, models=[], sessions=[],
        modes={}, calls=[], agent_tools=[], planner_inputs={}, plans={}, planner_counts={},
        weather_dates=["2026-10-01"], entered=asyncio.Event(),
    )

    def new_model(*args, **kwargs):
        model = SimpleNamespace(
            city=state.cities[len(state.models)],
            root_async_client=SimpleNamespace(close=AsyncMock()),
            root_client=SimpleNamespace(close=Mock()),
        )
        state.models.append(model)
        return model

    @asynccontextmanager
    async def open_tools(*args, **kwargs):
        session = SimpleNamespace(closed=False)
        state.sessions.append(session)

        async def search(keywords: str, city: str):
            stage = "hotel" if keywords == "酒店" else "attraction"
            mode = state.modes.get(stage)
            if mode == "error":
                raise ToolException("离线模拟的工具异常")
            if mode == "empty":
                return {"pois": []}
            hotel = stage == "hotel"
            data = {"pois": [{
                "id": f"{city}-hotel" if hotel else f"{city}-poi",
                "name": f"{city}测试酒店" if hotel else f"{city}测试博物馆",
                "address": f"{city}工具返回地址", "location": "116.4,39.9",
                "type": "住宿服务" if hotel else "风景名胜",
            }]}
            if state.modes.get("actual_server"):
                data["pois"][0].pop("location")
            return data

        async def detail(id: str):
            city = id.rsplit("-", 1)[0]
            return {
                "id": id, "name": f"{city}测试酒店" if id.endswith("hotel") else f"{city}测试博物馆",
                "location": "116.4,39.9", "address": f"{city}工具返回地址", "city": city,
            }

        async def weather(city: str):
            if state.modes.get("weather") == "error":
                raise ToolException("离线模拟的天气异常")
            dates = [] if state.modes.get("weather") == "empty" else state.weather_dates
            data = {"forecasts": [{"city": city, "casts": [{
                "date": value, "dayweather": "晴", "nightweather": "多云",
                "daytemp": "25", "nighttemp": "15", "daywind": "南", "daypower": "1-3",
            } for value in dates]}]}
            if state.modes.get("actual_server"):
                return {"city": city, "forecasts": data["forecasts"][0]["casts"]}
            return data

        async def unrelated(city: str):
            pytest.fail("路线工具不应暴露给这四个阶段")

        tools = [
            StructuredTool.from_function(coroutine=search, name="maps_text_search", description="测试 POI 搜索"),
            StructuredTool.from_function(coroutine=detail, name="maps_search_detail", description="测试 POI 详情"),
            StructuredTool.from_function(coroutine=weather, name="maps_weather", description="测试天气"),
            StructuredTool.from_function(coroutine=unrelated, name="maps_direction_walking", description="无关路线工具"),
        ]
        try:
            yield tools
        finally:
            session.closed = True

    def create_agent(model, tools=None, *, name, **kwargs):
        tools = tools or []
        state.agent_tools.append((model.city, name, {item.name for item in tools}))

        async def ainvoke(inputs, *args, **kwargs):
            state.calls.append((model.city, name))
            await asyncio.sleep(0)  # 允许并发测试交错执行。
            mode = state.modes.get(name)
            if mode == "model_error":
                raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
            if mode == "wait":
                state.entered.set()
                await asyncio.Event().wait()
            if name == "planner":
                state.planner_inputs.setdefault(model.city, []).append(
                    json.dumps(inputs, ensure_ascii=False, default=str)
                )
                attempt = state.planner_counts.get(model.city, 0)
                state.planner_counts[model.city] = attempt + 1
                candidates = state.plans.get(model.city, [make_plan(state.requests[model.city])])
                candidate = candidates[min(attempt, len(candidates) - 1)]
                if isinstance(candidate, Exception):
                    raise candidate
                return {"structured_response": candidate.model_copy(deep=True)}
            if mode != "no_call":
                args = {"city": model.city}
                if name != "weather":
                    args["keywords"] = "酒店" if name == "hotel" else "博物馆"
                await tools[0].ainvoke(args)
                if state.modes.get("actual_server") and name != "weather":
                    await tools[1].ainvoke({"id": f"{model.city}-hotel" if name == "hotel" else f"{model.city}-poi"})
            return {"messages": [AIMessage(content="模型声称已找到全部景点、酒店和天气") ]}

        return SimpleNamespace(ainvoke=ainvoke)

    monkeypatch.setattr(module, "create_langchain_llm", new_model)
    monkeypatch.setattr(module, "open_amap_tools", open_tools)
    monkeypatch.setattr(module, "create_agent", create_agent)
    return state


def assert_closed(state):
    assert state.sessions and all(session.closed for session in state.sessions)
    for model in state.models:
        model.root_async_client.close.assert_awaited_once()
        model.root_client.close.assert_called_once()


@pytest.mark.asyncio
async def test_success_runs_ordered_stages_with_minimal_tools_and_real_poi_facts(settings, fake_runtime):
    result = await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert result.status == "succeeded"
    assert not result.warnings
    assert fake_runtime.calls == [("北京", stage) for stage in ("attraction", "weather", "hotel", "planner")]
    assert fake_runtime.agent_tools == [
        ("北京", "attraction", {"maps_text_search", "maps_search_detail"}),
        ("北京", "weather", {"maps_weather"}),
        ("北京", "hotel", {"maps_text_search", "maps_search_detail"}),
        ("北京", "planner", set()),
    ]
    attraction = result.plan.days[0].attractions[0]
    assert attraction.location.longitude == 116.4
    assert attraction.address == "北京工具返回地址"
    assert result.plan.weather_info[0].day_weather == "晴"
    assert result.plan.weather_info[0].day_temp == 25
    assert_closed(fake_runtime)


@pytest.mark.asyncio
async def test_task_id_creates_one_explicit_monitor_for_all_stages(
    settings, fake_runtime, monkeypatch,
):
    events = []

    class FakeMonitor:
        def __init__(self, task_id):
            events.append(("task", task_id))

        async def set_stage(self, stage):
            events.append(("stage", stage))

        async def start(self, agent_name, tool_name, arguments):
            handle = SimpleNamespace(index=len(events))
            events.append(("start", agent_name, tool_name, arguments["city"]))
            return handle

        async def finish(self, handle, status, **kwargs):
            events.append(("finish", handle.index, status))

    monkeypatch.setattr(module, "DatabaseToolMonitor", FakeMonitor)
    result = await module.MultiAgentTripPlanner(settings).plan_trip(
        make_request(), task_id="task-123",
    )
    assert result.status == "succeeded"
    assert events[0] == ("task", "task-123")
    assert [event[1] for event in events if event[0] == "stage"] == [
        "attraction", "weather", "hotel", "planner",
    ]
    starts = [event for event in events if event[0] == "start"]
    assert [(event[1], event[2]) for event in starts] == [
        ("attraction", "maps_text_search"),
        ("weather", "maps_weather"),
        ("hotel", "maps_text_search"),
    ]
    assert all(event[3] == "北京" for event in starts)
    assert [event[2] for event in events if event[0] == "finish"] == [
        "succeeded", "succeeded", "succeeded",
    ]


@pytest.mark.asyncio
async def test_actual_server_search_detail_and_flat_weather_shapes(settings, fake_runtime):
    fake_runtime.modes["actual_server"] = True
    result = await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert result.status == "succeeded"
    assert result.plan.days[0].attractions[0].location.longitude == 116.4
    assert result.plan.days[0].hotel.location.latitude == 39.9
    assert result.plan.weather_info[0].day_temp == 25
    assert_closed(fake_runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_call", "empty", "error"])
async def test_missing_real_attractions_fails_despite_assistant_claim(settings, fake_runtime, mode):
    fake_runtime.modes["attraction"] = mode
    with pytest.raises(module.PlanningError) as exc:
        await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert exc.value.code == "NO_ATTRACTIONS"
    assert not fake_runtime.planner_counts
    assert_closed(fake_runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["weather", "hotel"])
async def test_missing_optional_evidence_degrades_and_clears_model_inventions(settings, fake_runtime, stage):
    fake_runtime.modes[stage] = "empty"
    result = await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert result.status == "degraded"
    assert result.warnings
    if stage == "weather":
        assert result.plan.weather_info == []
    else:
        assert all(day.hotel is None for day in result.plan.days)
    assert_closed(fake_runtime)


@pytest.mark.asyncio
async def test_weather_uses_only_actual_forecasts_inside_requested_dates(settings, fake_runtime):
    request = make_request(days=2)
    fake_runtime.requests["北京"] = request
    fake_runtime.weather_dates = ["2026-09-30", "2026-10-01", "2026-10-03"]
    result = await module.MultiAgentTripPlanner(settings).plan_trip(request)
    assert result.status == "degraded"
    assert result.warnings
    assert [item.date for item in result.plan.weather_info] == ["2026-10-01"]
    assert result.plan.weather_info[0].day_temp == 25


@pytest.mark.asyncio
@pytest.mark.parametrize("mistake", ["city", "dates", "days"])
async def test_inconsistent_plan_gets_one_repair_without_requerying_tools(settings, fake_runtime, mistake):
    valid = make_plan(make_request())
    invalid = valid.model_copy(deep=True)
    if mistake == "city":
        invalid.city = "上海"
    elif mistake == "dates":
        invalid.start_date = "2026-10-02"
        invalid.days[0].date = "2026-10-02"
    else:
        invalid.days = []
    fake_runtime.plans["北京"] = [invalid, valid]
    result = await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert result.plan.city == "北京"
    assert result.plan.start_date == "2026-10-01"
    assert len(result.plan.days) == 1
    assert fake_runtime.planner_counts["北京"] == 2
    assert fake_runtime.calls[:3] == [("北京", name) for name in ("attraction", "weather", "hotel")]
    assert len(fake_runtime.calls) == 5


@pytest.mark.asyncio
async def test_invalid_plan_after_one_repair_fails_without_placeholder_plan(settings, fake_runtime):
    invalid = make_plan(make_request()).model_copy(update={"city": "不存在的请求城市"})
    fake_runtime.plans["北京"] = [invalid]
    with pytest.raises(module.PlanningError) as exc:
        await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert exc.value.code == "INVALID_PLAN"
    assert fake_runtime.planner_counts["北京"] == 2
    assert_closed(fake_runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["weather", "planner"])
async def test_model_api_failure_is_fatal_including_optional_stage(settings, fake_runtime, stage):
    fake_runtime.modes[stage] = "model_error"
    with pytest.raises(module.PlanningError) as exc:
        await module.MultiAgentTripPlanner(settings).plan_trip(make_request())
    assert exc.value.code == "MODEL_UNAVAILABLE"
    assert fake_runtime.calls.count(("北京", stage)) == 1
    assert_closed(fake_runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["timeout", "cancel"])
async def test_timeout_or_cancellation_releases_task_resources(settings, fake_runtime, termination):
    fake_runtime.modes["weather"] = "wait"
    if termination == "timeout":
        settings.planning_timeout_seconds = 0.05
    task = asyncio.create_task(module.MultiAgentTripPlanner(settings).plan_trip(make_request()))
    await asyncio.wait_for(fake_runtime.entered.wait(), timeout=1)
    if termination == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(module.PlanningError) as exc:
            await task
        assert exc.value.code == "PLANNING_TIMEOUT"
    assert_closed(fake_runtime)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_models_sessions_or_evidence(settings, fake_runtime):
    fake_runtime.cities = ["北京", "上海"]
    fake_runtime.requests["上海"] = make_request("上海")
    planner = module.MultiAgentTripPlanner(settings)
    results = await asyncio.gather(planner.plan_trip(make_request()), planner.plan_trip(make_request("上海")))
    assert [result.plan.city for result in results] == ["北京", "上海"]
    assert len(fake_runtime.models) == len(fake_runtime.sessions) == 2
    assert fake_runtime.models[0] is not fake_runtime.models[1]
    for result, city, other in zip(results, ("北京", "上海"), ("上海", "北京")):
        assert result.plan.days[0].attractions[0].poi_id == f"{city}-poi"
        prompt = fake_runtime.planner_inputs[city][0]
        assert f"{city}-poi" in prompt
        assert f"{other}-poi" not in prompt
    assert_closed(fake_runtime)
