"""无网络 MCP 测试：真实工具适配器配合假 session，不启动 uvx。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, ListToolsResult, TextContent, Tool

from app.config import Settings
from app.services import mcp_service


WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        amap_api_key="test-amap-key",
        amap_mcp_command="test-uvx",
        mcp_timeout_seconds=30,
    )


@pytest.fixture
def fake_mcp(monkeypatch):
    state = SimpleNamespace(
        connections=[],
        sessions=[],
        tools=[Tool(name="maps_weather", description="天气", inputSchema=WEATHER_SCHEMA)],
        load_error=None,
    )

    class FakeClient:
        def __init__(self, connections):
            state.connections.append(connections)

        @asynccontextmanager
        async def session(self, name):
            assert name == "amap"
            session = SimpleNamespace(
                closed=False,
                list_tools=AsyncMock(
                    return_value=ListToolsResult(tools=state.tools),
                    side_effect=state.load_error,
                ),
                call_tool=AsyncMock(
                    return_value=CallToolResult(
                        content=[TextContent(type="text", text="晴")],
                    )
                ),
            )
            state.sessions.append(session)
            try:
                yield session
            finally:
                session.closed = True

    monkeypatch.setattr(mcp_service, "MultiServerMCPClient", FakeClient)
    return state


@pytest.mark.asyncio
async def test_real_adapter_preserves_schema_and_sessions_are_isolated(settings, fake_mcp):
    async with mcp_service.open_amap_tools(settings) as first:
        assert first[0].name == "maps_weather"
        assert first[0].args_schema == WEATHER_SCHEMA
        result = await first[0].ainvoke({"city": "北京"})
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "晴"
        await first[0].ainvoke({"city": "上海"})
        assert fake_mcp.sessions[0].call_tool.await_count == 2
        async with mcp_service.open_amap_tools(settings) as second:
            assert second[0] is not first[0]
            assert not any(session.closed for session in fake_mcp.sessions)
        assert fake_mcp.sessions[1].closed
        assert not fake_mcp.sessions[0].closed
    assert all(session.closed for session in fake_mcp.sessions)


@pytest.mark.asyncio
async def test_config_mapping_and_default_settings(settings, fake_mcp, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "must-not-be-forwarded")
    monkeypatch.setattr(mcp_service, "get_settings", lambda: settings)
    async with mcp_service.open_amap_tools():
        connection = fake_mcp.connections[0]["amap"]
        assert connection["transport"] == "stdio"
        assert connection["command"] == "test-uvx"
        assert connection["args"][:5] == ["--from", "amap-mcp-server==0.1.11", "python", "-X", "utf8"]
        assert connection["args"][5].endswith("run_amap_mcp.py")
        assert connection["env"] == {"AMAP_MAPS_API_KEY": "test-amap-key"}
        assert connection["session_kwargs"] == {
            "read_timeout_seconds": timedelta(seconds=30),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "   "])
async def test_missing_key_does_not_connect(settings, fake_mcp, key):
    settings.amap_api_key = key
    with pytest.raises(ValueError, match="AMAP_API_KEY"):
        async with mcp_service.open_amap_tools(settings):
            pytest.fail("Missing credentials must not yield tools")
    assert fake_mcp.connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_discovery_failure_or_empty_list_closes_session(settings, fake_mcp, failed):
    fake_mcp.tools = []
    if failed:
        fake_mcp.load_error = RuntimeError("discovery failed")
    with pytest.raises(RuntimeError, match="discovery failed|未提供可用工具"):
        async with mcp_service.open_amap_tools(settings):
            pytest.fail("Failed discovery must not yield tools")
    assert fake_mcp.sessions[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", [False, True])
async def test_tool_errors_propagate_and_close_session(settings, fake_mcp, transport_error):
    expected = ConnectionError if transport_error else ToolException
    with pytest.raises(expected, match="tool unavailable"):
        async with mcp_service.open_amap_tools(settings) as tools:
            call_tool = fake_mcp.sessions[0].call_tool
            if transport_error:
                call_tool.side_effect = ConnectionError("tool unavailable")
            else:
                call_tool.return_value = CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text="tool unavailable")],
                )
            await tools[0].ainvoke({"city": "北京"})
    assert fake_mcp.sessions[0].closed


@pytest.mark.asyncio
async def test_cancellation_closes_session(settings, fake_mcp):
    with pytest.raises(asyncio.CancelledError):
        async with mcp_service.open_amap_tools(settings):
            raise asyncio.CancelledError
    assert fake_mcp.sessions[0].closed


@pytest.mark.asyncio
async def test_sdk_timeout_stops_unanswered_rpc():
    """验证所选 SDK 超时参数确实限制等响应时间，不需要真实服务端。"""
    server_writer, client_reader = anyio.create_memory_object_stream(10)
    client_writer, server_reader = anyio.create_memory_object_stream(10)
    async with server_writer, server_reader:
        async with ClientSession(
            client_reader,
            client_writer,
            read_timeout_seconds=timedelta(milliseconds=20),
        ) as session:
            with pytest.raises(McpError, match="Timed out"):
                await session.call_tool("maps_weather", {"city": "北京"})


@pytest.mark.parametrize("location", ["path", "beside_python", "scripts", "missing"])
def test_default_uvx_discovery(monkeypatch, tmp_path, location):
    monkeypatch.setattr(mcp_service, "sys", SimpleNamespace(executable=str(tmp_path / "python.exe")))
    monkeypatch.setattr(
        mcp_service.shutil, "which", lambda name: "bin/uvx.exe" if location == "path" else None
    )
    if location == "path":
        assert mcp_service._resolve_command("uvx") == "bin/uvx.exe"
    elif location == "missing":
        with pytest.raises(FileNotFoundError, match="AMAP_MCP_COMMAND"):
            mcp_service._resolve_command("uvx")
    else:
        executable = tmp_path / ("Scripts/uvx.exe" if location == "scripts" else "uvx.exe")
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        assert mcp_service._resolve_command("uvx") == str(executable)


POI_RESULT = {
    "status": "1", "infocode": "10000",
    "pois": [{"id": "p1", "name": "故宫", "location": "116.397,39.918"}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["maps_text_search", "maps_weather"])
async def test_request_city_overrides_wrong_model_city(tool_name):
    call = AsyncMock(return_value=POI_RESULT if tool_name == "maps_text_search" else {"forecasts": []})
    original = StructuredTool(
        name=tool_name, description="测试城市约束", coroutine=call,
        args_schema={"type": "object", "properties": {
            "city": {"type": "string"}, "citylimit": {"type": "string"},
        }, "required": ["city"]},
    )
    wrapped = mcp_service.bind_stage_tools(
        [original], "query", mcp_service.ToolRunContext(), request_city="北京",
    )[0]
    await wrapped.ainvoke({"city": "上海", "citylimit": "false"})
    assert call.await_args.kwargs["city"] == "北京"
    if tool_name == "maps_text_search":
        assert call.await_args.kwargs["citylimit"] == "true"
FORECAST_RESULT = {
    "forecasts": [{"city": "北京", "casts": [{"date": "2026-10-01", "dayweather": "晴"}]}],
}


def source_tool(result=None, *, name="maps_text_search", call=None):
    call = call if call is not None else AsyncMock(return_value=result)
    return StructuredTool(
        name=name, description="离线工具", args_schema=WEATHER_SCHEMA, coroutine=call,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["dict", "str", "blocks", "message", "mcp_result"])
async def test_stage_wrapper_decodes_evidence_and_preserves_tool_schema(form):
    text = json.dumps(POI_RESULT, ensure_ascii=False)
    raw = {
        "dict": POI_RESULT,
        "str": text,
        "blocks": [{"type": "text", "text": text}],
        "message": ToolMessage(content=text, tool_call_id="c1"),
        "mcp_result": CallToolResult(content=[TextContent(type="text", text=text)]),
    }[form]
    original = source_tool(raw)
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([original], "attraction", context)[0]

    result = await wrapped.ainvoke({"city": "北京"})

    assert wrapped.name == original.name
    assert wrapped.args_schema == original.args_schema
    assert result == POI_RESULT
    assert context.successful_results("attraction") == [POI_RESULT]
    assert context.successful_results("weather") == []
    assert context.calls[0].status == "succeeded"
    assert context.calls[0].error_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [FORECAST_RESULT, {"lives": [{"weather": "晴"}]}])
async def test_weather_evidence_is_successful_only_with_actual_weather(data):
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(data, name="maps_weather")], "weather", context,
    )[0]
    assert await wrapped.ainvoke({"city": "北京"}) == data
    assert context.successful_results("weather") == [data]


@pytest.mark.asyncio
async def test_actual_amap_server_flat_forecasts_are_normalized():
    casts = [{"date": "2026-10-01", "dayweather": "晴", "nightweather": "多云", "daytemp": "25", "nighttemp": "15"}]
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools(
        [source_tool({"city": "北京市", "forecasts": casts}, name="maps_weather")], "weather", context,
    )[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["forecasts"] == [{"city": "北京市", "casts": casts}]
    assert context.calls[0].status == "succeeded"


@pytest.mark.asyncio
async def test_actual_search_then_detail_preserves_poi_id_and_real_coordinates():
    search_data = {"pois": [{"id": "p1", "name": "故宫", "address": "北京", "typecode": "110200"}]}
    detail_call = AsyncMock(return_value={"id": "p1", "name": "故宫", "location": "116.397,39.918", "city": "北京市"})
    detail = StructuredTool(
        name="maps_search_detail", description="查详情", coroutine=detail_call,
        args_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    )
    context = mcp_service.ToolRunContext()
    search, lookup = mcp_service.bind_stage_tools([source_tool(search_data), detail], "attraction", context, request_city="北京")
    assert (await lookup.ainvoke({"id": "unsearched"}))["error"]["code"] == "UNKNOWN_POI"
    detail_call.assert_not_awaited()
    assert context.calls == []
    await search.ainvoke({"city": "上海"})
    result = await lookup.ainvoke({"id": "p1"})
    detail_call.assert_awaited_once_with(id="p1")
    assert result["location"] == "116.397,39.918"
    assert len(context.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name, data", [
    ("maps_text_search", {"pois": []}),
    ("maps_weather", {"forecasts": []}),
    ("maps_weather", {"forecasts": [{"casts": []}]}),
    ("maps_weather", {"lives": []}),
])
async def test_business_empty_results_are_not_evidence(name, data):
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([source_tool(data, name=name)], "query", context)[0]
    assert await wrapped.ainvoke({"city": "北京"}) == data
    assert context.calls[0].status == "empty"
    assert context.calls[0].result == data
    assert context.successful_results("query") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name, raw, code", [
    ("maps_text_search", {"status": "0", "info": "private-credential"}, "AMAP_BUSINESS_ERROR"),
    ("maps_text_search", {"error": "Text Search failed: private-credential"}, "AMAP_BUSINESS_ERROR"),
    ("maps_text_search", {**POI_RESULT, "infocode": "10001"}, "AMAP_BUSINESS_ERROR"),
    ("maps_text_search", {"isError": True, "content": "private-credential"}, "TOOL_EXECUTION_ERROR"),
    ("maps_text_search", "not JSON private-credential", "INVALID_TOOL_RESPONSE"),
    ("maps_text_search", {"unknown": "private-credential"}, "INVALID_TOOL_RESPONSE"),
    ("maps_text_search", {"pois": ["bad"]}, "INVALID_TOOL_RESPONSE"),
    ("maps_text_search", {"pois": [{}]}, "INVALID_TOOL_RESPONSE"),
    ("maps_weather", {"forecasts": [{"casts": [{}]}]}, "INVALID_TOOL_RESPONSE"),
    ("maps_weather", {"forecasts": [{"casts": "bad"}]}, "INVALID_TOOL_RESPONSE"),
    ("maps_weather", {"lives": [{}]}, "INVALID_TOOL_RESPONSE"),
    ("maps_weather", ToolMessage(
        content="private-credential", status="error", tool_call_id="c1",
    ), "TOOL_EXECUTION_ERROR"),
    ("maps_weather", CallToolResult(
        isError=True, content=[TextContent(type="text", text="private-credential")],
    ), "TOOL_EXECUTION_ERROR"),
])
async def test_invalid_or_failed_results_return_safe_errors(name, raw, code):
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([source_tool(raw, name=name)], "query", context)[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["error"]["code"] == code
    assert context.calls[0].status == "failed"
    assert context.calls[0].result is None
    assert context.calls[0].error_code == code
    assert context.successful_results("query") == []
    assert "private-credential" not in str(result) + repr(context.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("error, code", [
    (ToolException("private-credential"), "TOOL_EXECUTION_ERROR"),
    (ConnectionError("private-credential"), "TOOL_CONNECTION_ERROR"),
    (RuntimeError("private-credential"), "TOOL_EXECUTION_ERROR"),
    (McpError(ErrorData(code=408, message="private-credential")), "TOOL_TIMEOUT"),
])
async def test_provider_exceptions_never_expose_raw_details(error, code):
    context = mcp_service.ToolRunContext()
    raw_call = AsyncMock(side_effect=error)
    wrapped = mcp_service.bind_stage_tools([source_tool(call=raw_call)], "attraction", context)[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["error"]["code"] == code
    assert context.calls[0].error_code == code
    assert "private-credential" not in str(result) + repr(context.calls)


@pytest.mark.asyncio
async def test_failed_attempt_can_be_followed_by_successful_evidence():
    raw_call = AsyncMock(side_effect=[ToolException("failed"), POI_RESULT])
    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([source_tool(call=raw_call)], "attraction", context)[0]
    await wrapped.ainvoke({"city": "北京"})
    await wrapped.ainvoke({"city": "北京"})
    assert [call.status for call in context.calls] == ["failed", "succeeded"]
    assert context.successful_results("attraction") == [POI_RESULT]


@pytest.mark.asyncio
async def test_parallel_tool_calls_cannot_exceed_stage_limit():
    release, three_started = asyncio.Event(), asyncio.Event()
    actual_calls = []

    async def call(**arguments):
        actual_calls.append(arguments)
        if len(actual_calls) == 3:
            three_started.set()
        await release.wait()
        return POI_RESULT

    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([source_tool(call=call)], "attraction", context)[0]
    tasks = [asyncio.create_task(wrapped.ainvoke({"city": "北京"})) for _ in range(4)]
    try:
        await asyncio.wait_for(three_started.wait(), timeout=1)
        assert len(context.calls) == 3
        assert context.successful_results("attraction") == []
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(actual_calls) == 3
    assert sum(isinstance(result, mcp_service.ToolCallLimitError) for result in results) == 1
    assert all(record.status == "succeeded" for record in context.calls)


@pytest.mark.asyncio
async def test_limits_and_results_are_separate_between_contexts_and_stages():
    first, second = mcp_service.ToolRunContext(), mcp_service.ToolRunContext()
    original = source_tool(POI_RESULT)
    attraction = mcp_service.bind_stage_tools([original], "attraction", first, max_calls=1)[0]
    hotel = mcp_service.bind_stage_tools([original], "hotel", first, max_calls=1)[0]
    other_task = mcp_service.bind_stage_tools([original], "attraction", second, max_calls=1)[0]
    for wrapped in (attraction, hotel, other_task):
        await wrapped.ainvoke({"city": "北京"})
    with pytest.raises(mcp_service.ToolCallLimitError):
        await attraction.ainvoke({"city": "北京"})
    assert len(first.calls) == 2
    assert len(second.calls) == 1


@pytest.mark.asyncio
async def test_tool_deadline_records_failure_and_cancels_underlying_work():
    cancelled = asyncio.Event()

    async def never_returns(**arguments):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools(
        [source_tool(call=never_returns)], "attraction", context, timeout_seconds=0.02,
    )[0]
    result = await wrapped.ainvoke({"city": "北京"})
    assert result["error"]["code"] == "TOOL_TIMEOUT"
    assert cancelled.is_set()
    assert context.calls[0].status == "failed"
    assert context.successful_results("attraction") == []


@pytest.mark.asyncio
async def test_external_cancellation_is_not_converted_to_a_model_error():
    started = asyncio.Event()

    async def waiting(**arguments):
        started.set()
        await asyncio.Event().wait()

    context = mcp_service.ToolRunContext()
    wrapped = mcp_service.bind_stage_tools([source_tool(call=waiting)], "attraction", context)[0]
    task = asyncio.create_task(wrapped.ainvoke({"city": "北京"}))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.calls[0].status == "failed"
    assert context.calls[0].error_code == "TOOL_CANCELLED"
