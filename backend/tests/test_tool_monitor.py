"""真实工具调用监控：先写 running，再按校验结果写终态。"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.tools import StructuredTool, ToolException
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.database import create_db_engine, init_database
from app.models.entities import ToolCall, TripTask, User
from app.services import mcp_service, tool_monitor


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "api_key": {"type": "string"},
    },
    "required": ["city"],
}
POI_RESULT = {
    "pois": [{"id": "p1", "name": "故宫", "location": "116.397,39.918"}],
}


def source_tool(call, name="maps_text_search"):
    schema = TOOL_SCHEMA
    if name == "maps_search_detail":
        schema = {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        }
    return StructuredTool(
        name=name,
        description="离线监控测试工具",
        args_schema=schema,
        coroutine=call,
    )


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'monitor.db').as_posix()}")
    init_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(tool_monitor, "SessionLocal", factory)
    with factory() as session:
        first = User(username="monitor_a", password_hash="unused")
        second = User(username="monitor_b", password_hash="unused")
        session.add_all([first, second])
        session.flush()
        tasks = [
            TripTask(
                user_id=user.id,
                status="running",
                current_stage="planning",
                request_data={"city": city},
                warnings=[],
            )
            for user, city in ((first, "北京"), (second, "上海"))
        ]
        session.add_all(tasks)
        session.commit()
        task_ids = [item.id for item in tasks]
    yield factory, task_ids
    engine.dispose()


@pytest.mark.asyncio
async def test_running_record_is_visible_before_external_call_finishes(database):
    factory, (task_id, _) = database
    started, release = asyncio.Event(), asyncio.Event()

    async def blocking_call(**_arguments):
        started.set()
        await release.wait()
        return POI_RESULT

    monitor = tool_monitor.DatabaseToolMonitor(task_id)
    await monitor.set_stage("attraction")
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(blocking_call)],
        "attraction",
        context,
        monitor=monitor,
        request_city="北京",
    )[0]
    pending = asyncio.create_task(wrapped.ainvoke({"city": "错误城市"}))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        with factory() as session:
            task = session.get(TripTask, task_id)
            call = session.scalar(select(ToolCall).where(ToolCall.task_id == task_id))
            assert task.current_stage == "attraction"
            assert call.status == "running"
            assert call.arguments["city"] == "北京"
            assert call.completed_at is None and call.duration_ms is None
    finally:
        release.set()
        result = await pending

    assert result == POI_RESULT
    with factory() as session:
        call = session.scalar(select(ToolCall).where(ToolCall.task_id == task_id))
        assert call.status == "succeeded"
        assert call.completed_at is not None and call.duration_ms >= 0
        assert call.result_summary["item_count"] == 1
        assert "故宫" in call.result_summary["preview"]


@pytest.mark.asyncio
async def test_empty_result_and_repeated_call_create_separate_records(database):
    factory, (task_id, _) = database
    raw_call = AsyncMock(side_effect=[{"pois": []}, POI_RESULT])
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(raw_call)],
        "attraction",
        context,
        monitor=tool_monitor.DatabaseToolMonitor(task_id),
    )[0]
    await wrapped.ainvoke({"city": "北京"})
    await wrapped.ainvoke({"city": "北京"})
    with factory() as session:
        calls = session.scalars(
            select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.id),
        ).all()
        assert [item.status for item in calls] == ["empty", "succeeded"]
        assert [item.result_summary["item_count"] for item in calls] == [0, 1]
        assert calls[0].id != calls[1].id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_call,expected_code",
    [
        (AsyncMock(side_effect=ToolException("private-credential")), "TOOL_EXECUTION_ERROR"),
        (
            AsyncMock(return_value=CallToolResult(
                isError=True,
                content=[TextContent(type="text", text="private-credential")],
            )),
            "TOOL_EXECUTION_ERROR",
        ),
        (AsyncMock(return_value={"status": "0", "info": "private-credential"}), "AMAP_BUSINESS_ERROR"),
        (AsyncMock(return_value={"unexpected": "private-credential"}), "INVALID_TOOL_RESPONSE"),
    ],
)
async def test_errors_are_persisted_with_safe_codes(database, raw_call, expected_code):
    factory, (task_id, _) = database
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(raw_call)],
        "attraction",
        mcp_service.ToolRunContext(),
        monitor=tool_monitor.DatabaseToolMonitor(task_id),
    )[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["error"]["code"] == expected_code
    with factory() as session:
        call = session.scalar(select(ToolCall).where(ToolCall.task_id == task_id))
        assert call.status == "failed"
        assert call.error_code == expected_code
        assert call.result_summary is None
        assert "private-credential" not in str(call.arguments) + str(call.error_message)


@pytest.mark.asyncio
async def test_timeout_and_cancellation_are_distinct_terminal_failures(database):
    factory, (timeout_task, cancel_task) = database
    cancelled = asyncio.Event()

    async def never_returns(**_arguments):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    timeout_wrapper = mcp_service.bind_stage_tools(
        [source_tool(never_returns)],
        "weather",
        mcp_service.ToolRunContext(),
        monitor=tool_monitor.DatabaseToolMonitor(timeout_task),
        timeout_seconds=0.01,
    )[0]
    timeout_result = await timeout_wrapper.ainvoke({"city": "北京"})
    assert timeout_result["error"]["code"] == "TOOL_TIMEOUT"
    assert cancelled.is_set()

    started = asyncio.Event()

    async def wait_for_cancel(**_arguments):
        started.set()
        await asyncio.Event().wait()

    cancel_wrapper = mcp_service.bind_stage_tools(
        [source_tool(wait_for_cancel)],
        "weather",
        mcp_service.ToolRunContext(),
        monitor=tool_monitor.DatabaseToolMonitor(cancel_task),
    )[0]
    pending = asyncio.create_task(cancel_wrapper.ainvoke({"city": "上海"}))
    await asyncio.wait_for(started.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    with factory() as session:
        timeout_call = session.scalar(select(ToolCall).where(ToolCall.task_id == timeout_task))
        cancel_call = session.scalar(select(ToolCall).where(ToolCall.task_id == cancel_task))
        assert (timeout_call.status, timeout_call.error_code) == ("failed", "TOOL_TIMEOUT")
        assert (cancel_call.status, cancel_call.error_code) == ("failed", "TOOL_CANCELLED")
        assert timeout_call.duration_ms >= 0 and cancel_call.duration_ms >= 0


@pytest.mark.asyncio
async def test_arguments_and_result_summaries_are_bounded_and_redacted(database):
    factory, (task_id, _) = database
    monitor = tool_monitor.DatabaseToolMonitor(task_id)
    handle = await monitor.start(
        "attraction",
        "maps_text_search",
        {
            "city": "北京",
            "api_key": "private-api-key",
            "nested": {"Authorization": "Bearer private-token"},
            "note": "x" * 1000,
        },
    )
    await monitor.finish(
        handle,
        "succeeded",
        result={
            "pois": [{"name": "故宫", "secret": "private-result"}],
            "large": "y" * 5000,
        },
    )
    with factory() as session:
        call = session.scalar(select(ToolCall).where(ToolCall.task_id == task_id))
        serialized = str(call.arguments) + str(call.result_summary)
        assert "private-api-key" not in serialized
        assert "private-token" not in serialized
        assert "private-result" not in serialized
        assert "[已脱敏]" in serialized
        assert len(call.result_summary["preview"]) <= 1501


@pytest.mark.asyncio
async def test_concurrent_tasks_never_share_monitor_records(database):
    factory, task_ids = database

    async def run_one(task_id, city):
        wrapped = mcp_service.bind_stage_tools(
            [source_tool(AsyncMock(return_value=POI_RESULT))],
            "attraction",
            mcp_service.ToolRunContext(),
            monitor=tool_monitor.DatabaseToolMonitor(task_id),
            request_city=city,
        )[0]
        await wrapped.ainvoke({"city": "模型误选城市"})

    await asyncio.gather(run_one(task_ids[0], "北京"), run_one(task_ids[1], "上海"))
    with factory() as session:
        calls = session.scalars(select(ToolCall).order_by(ToolCall.id)).all()
        assert len(calls) == 2
        by_task = {item.task_id: item.arguments["city"] for item in calls}
        assert by_task == {task_ids[0]: "北京", task_ids[1]: "上海"}


@pytest.mark.asyncio
async def test_guarded_or_rejected_calls_do_not_create_fake_records(database):
    factory, (task_id, _) = database
    monitor = tool_monitor.DatabaseToolMonitor(task_id)
    detail = source_tool(AsyncMock(return_value=POI_RESULT), name="maps_search_detail")
    wrapped = mcp_service.bind_stage_tools(
        [detail], "attraction", mcp_service.ToolRunContext(), monitor=monitor,
    )[0]
    result = await wrapped.ainvoke({"id": "never-searched"})
    assert result["error"]["code"] == "UNKNOWN_POI"
    with factory() as session:
        assert session.scalar(select(ToolCall).where(ToolCall.task_id == task_id)) is None


@pytest.mark.asyncio
async def test_external_call_does_not_run_when_monitor_cannot_start(database):
    factory, (task_id, _) = database
    with factory() as session:
        task = session.get(TripTask, task_id)
        task.status = "failed"
        task.current_stage = "failed"
        session.commit()
    raw_call = AsyncMock(return_value=POI_RESULT)
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(raw_call)],
        "attraction",
        mcp_service.ToolRunContext(),
        monitor=tool_monitor.DatabaseToolMonitor(task_id),
    )[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["error"]["code"] == "TOOL_MONITOR_ERROR"
    raw_call.assert_not_awaited()
    with factory() as session:
        assert session.scalar(select(ToolCall).where(ToolCall.task_id == task_id)) is None
