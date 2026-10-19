"""LangChain 的高德 MCP 入口；连接只在调用方的 async with 内有效。"""

import asyncio
import json
import math
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult

from ..config import Settings, get_settings
from .tool_monitor import DatabaseToolMonitor, ToolMonitorError


class MCPToolUnavailableError(RuntimeError):
    """MCP 服务没有提供所需工具。"""


def _resolve_command(command: str) -> str:
    """默认 uvx 查找当前环境；显式配置的其他命令或路径保持原样。"""
    if command != "uvx":
        return command
    if executable := shutil.which("uvx"):
        return executable

    python_dir = Path(sys.executable).parent
    for candidate in (python_dir / "uvx.exe", python_dir / "Scripts" / "uvx.exe"):
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "找不到 uvx；请在当前 Python 环境安装 uv，"
        "或通过 AMAP_MCP_COMMAND 指定 uvx 的完整路径。"
    )


@asynccontextmanager
async def open_amap_tools(
    settings: Settings | None = None,
) -> AsyncIterator[list[BaseTool]]:
    """打开独立会话并加载工具，退出时由 SDK 关闭连接和 stdio 子进程。

    工具/Agent 调用必须在此上下文内完成，不能缓存工具供退出后使用。
    超时作用于每次 MCP 请求等待响应，不代表整个任务的总超时。
    这里只管理连接；阶段编排和数据库日志由调用方负责。
    """
    settings = settings if settings is not None else get_settings()
    if not settings.amap_api_key.strip():
        raise ValueError("AMAP_API_KEY 未配置，无法连接高德 MCP 服务。")

    connection: StdioConnection = {
        "transport": "stdio",
        "command": _resolve_command(settings.amap_mcp_command),
        # 固定本步实际验证过的服务端版本，避免 uvx 自动选择新版本改变 schema。
        "args": [
            "--from", "amap-mcp-server==0.1.11", "python", "-X", "utf8",
            str(Path(__file__).resolve().parents[2] / "scripts" / "run_amap_mcp.py"),
        ],
        # SDK 继承 PATH 等必要变量；不将其他服务的密钥传给子进程。
        "env": {"AMAP_MAPS_API_KEY": settings.amap_api_key},
        # stdio 的 RPC 超时通过 ClientSession 参数设置，不能用顶层 timeout。
        "session_kwargs": {
            "read_timeout_seconds": timedelta(seconds=settings.mcp_timeout_seconds),
        },
    }
    client = MultiServerMCPClient({"amap": connection})
    async with client.session("amap") as session:
        tools = await load_mcp_tools(session, handle_tool_errors=False)
        if not tools:
            raise MCPToolUnavailableError("高德 MCP 服务未提供可用工具。")
        yield tools


class ToolCallLimitError(RuntimeError):
    """同一阶段已经用完真实工具调用次数，不能再请求外部服务。"""


@dataclass
class ToolCallRecord:
    agent_name: str
    tool_name: str
    status: Literal["succeeded", "empty", "failed"]
    result: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class ToolRunContext:
    """一次规划独有的证据；不包含用户凭证、原始异常或共享的当前任务。"""

    calls: list[ToolCallRecord] = field(default_factory=list)

    def successful_results(self, agent_name: str) -> list[dict]:
        return [
            call.result for call in self.calls
            if call.agent_name == agent_name and call.status == "succeeded"
            and call.result is not None
        ]


class MCPToolError(ValueError):
    """统一工具执行层的安全错误，不包含供应商异常原文。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid_response() -> MCPToolError:
    return MCPToolError("INVALID_TOOL_RESPONSE", "工具返回格式无法识别。")


def _decode_payload(value: object) -> dict:
    """只接受工具的 JSON 数据；不从任意错误文本中猜测或截取 JSON。"""
    if isinstance(value, ToolMessage):
        if value.status == "error":
            raise MCPToolError("TOOL_EXECUTION_ERROR", "MCP 工具执行失败。")
        value = value.content
    if isinstance(value, CallToolResult):
        if value.isError:
            raise MCPToolError("TOOL_EXECUTION_ERROR", "MCP 工具执行失败。")
        value = value.structuredContent or [block.model_dump() for block in value.content]
    if isinstance(value, dict) and value.get("isError"):
        raise MCPToolError("TOOL_EXECUTION_ERROR", "MCP 工具执行失败。")
    if isinstance(value, dict) and "content" in value and "isError" in value:
        value = value.get("structuredContent") or value["content"]
    if isinstance(value, list):
        if not value or not all(
            isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) for block in value
        ):
            raise _invalid_response()
        value = "".join(block["text"] for block in value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise _invalid_response() from None
    if not isinstance(value, dict):
        raise _invalid_response()
    if value.get("isError"):
        raise MCPToolError("TOOL_EXECUTION_ERROR", "MCP 工具执行失败。")
    return value


def decode_tool_result(raw: object) -> dict:
    """Agent 与独立地图接口共用的协议解码及高德业务错误检查。"""
    data = _decode_payload(raw)
    if "CUQPS_HAS_EXCEEDED_THE_LIMIT" in str(data.get("error", "")):
        raise MCPToolError("AMAP_RATE_LIMITED", "高德查询频率超限，请稍后重试。")
    if "error" in data or ("status" in data and str(data["status"]) != "1") or (
        "infocode" in data and str(data["infocode"]) != "10000"
    ):
        raise MCPToolError("AMAP_BUSINESS_ERROR", "高德服务返回业务错误。")
    return data


def safe_tool_error(error: Exception) -> MCPToolError:
    """将直接异常或 SDK ExceptionGroup 变为固定错误码与安全消息。"""
    if isinstance(error, MCPToolError):
        return error
    children = getattr(error, "exceptions", ())
    if children:
        errors = [safe_tool_error(child) for child in children if isinstance(child, Exception)]
        return next((item for item in errors if item.code == "TOOL_TIMEOUT"), errors[0])
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)) or (
        isinstance(error, McpError) and error.error.code == 408
    ):
        return MCPToolError("TOOL_TIMEOUT", "工具调用超时。")
    if isinstance(error, (ConnectionError, OSError)):
        return MCPToolError("TOOL_CONNECTION_ERROR", "MCP 工具连接失败。")
    return MCPToolError("TOOL_EXECUTION_ERROR", "MCP 工具执行失败。")


async def execute_mcp_tool(
    tool: BaseTool, arguments: dict, *, timeout_seconds: float = 30,
    config: RunnableConfig | None = None,
) -> dict:
    """执行一次真实 MCP 调用；不重试、不吞业务错误、不缓存连接。"""
    try:
        raw = await asyncio.wait_for(tool.ainvoke(arguments, config=config), timeout_seconds)
        return decode_tool_result(raw)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise safe_tool_error(exc) from None


def _classify_result(tool_name: str, raw: object) -> tuple[str, dict]:
    data = decode_tool_result(raw)

    if tool_name == "maps_text_search":
        pois = data.get("pois")
        if not isinstance(pois, list) or not all(
            isinstance(poi, dict) and isinstance(poi.get("name"), str)
            and poi["name"].strip() for poi in pois
        ):
            raise _invalid_response()
        return ("succeeded" if pois else "empty"), data

    if tool_name == "maps_search_detail":
        if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("id", "name", "location")):
            raise _invalid_response()
        return "succeeded", data

    if "forecasts" in data:
        forecasts = data["forecasts"]
        if not isinstance(forecasts, list):
            raise _invalid_response()
        # amap-mcp-server 0.1.11 将 casts 提升为 forecasts，原始高德 HTTP API 则有外层城市对象。
        if forecasts and isinstance(forecasts[0], dict) and "date" in forecasts[0]:
            data = {**data, "forecasts": [{"city": data.get("city", ""), "casts": forecasts}]}
            forecasts = data["forecasts"]
        casts = []
        for forecast in forecasts:
            if not isinstance(forecast, dict) or not isinstance(forecast.get("casts"), list):
                raise _invalid_response()
            casts.extend(forecast["casts"])
        if not all(
            isinstance(cast, dict) and isinstance(cast.get("date"), str)
            and cast["date"].strip()
            and any(isinstance(cast.get(key), str) and cast[key].strip()
                    for key in ("dayweather", "nightweather"))
            for cast in casts
        ):
            raise _invalid_response()
        return ("succeeded" if casts else "empty"), data
    if "lives" in data:
        lives = data["lives"]
        if not isinstance(lives, list) or not all(
            isinstance(live, dict) and isinstance(live.get("weather"), str)
            and live["weather"].strip() for live in lives
        ):
            raise _invalid_response()
        return ("succeeded" if lives else "empty"), data
    raise _invalid_response()


def bind_stage_tools(
    tools: list[BaseTool],
    stage: str,
    context: ToolRunContext,
    *,
    monitor: DatabaseToolMonitor | None = None,
    max_calls: int = 3,
    timeout_seconds: float = 30,
    request_city: str | None = None,
) -> list[BaseTool]:
    """保留工具协议，在真实执行边界记录结果，并限制补查次数和等待时间。"""
    if max_calls < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("工具调用次数和超时必须为正数。")

    def wrap(original: BaseTool) -> BaseTool:
        if original.name not in {"maps_text_search", "maps_search_detail", "maps_weather"}:
            raise ValueError("阶段工具仅支持 POI 搜索、详情和天气。")

        async def invoke(config: RunnableConfig, **arguments) -> dict:
            # 城市由已校验的用户请求决定，不能采用模型误选的其他城市。
            if request_city is not None and original.name != "maps_search_detail":
                arguments["city"] = request_city
                if original.name == "maps_text_search" and "citylimit" in original.args:
                    arguments["citylimit"] = "true"
            if original.name == "maps_search_detail":
                searched_ids = {
                    poi.get("id") for call in context.calls
                    if call.agent_name == stage and call.tool_name == "maps_text_search"
                    and call.status == "succeeded" and call.result
                    for poi in call.result.get("pois", [])
                }
                if arguments.get("id") not in searched_ids:
                    return {"error": {"code": "UNKNOWN_POI", "message": "请先搜索，再查询已返回的 POI ID。"}}
            # 此段不 await，ToolNode 并发执行也会先占用名额，再访问外部服务。
            if sum(call.agent_name == stage for call in context.calls) >= max_calls:
                raise ToolCallLimitError(f"{stage} 阶段已达到工具调用次数上限。")
            record = ToolCallRecord(
                stage, original.name, "failed", error_code="TOOL_CALL_IN_PROGRESS",
                error_message="工具调用尚未完成。",
            )
            context.calls.append(record)
            handle = None
            if monitor is not None:
                try:
                    # running 必须先提交，外部 MCP 调用才允许开始。
                    handle = await monitor.start(stage, original.name, arguments)
                except ToolMonitorError:
                    record.error_code = "TOOL_MONITOR_ERROR"
                    record.error_message = "工具调用状态记录失败。"
                    return {"error": {
                        "code": record.error_code,
                        "message": record.error_message,
                    }}
            try:
                raw = await execute_mcp_tool(
                    original, arguments, config=config, timeout_seconds=timeout_seconds,
                )
                record.status, record.result = _classify_result(original.name, raw)
            except asyncio.CancelledError:
                record.error_code, record.error_message = "TOOL_CANCELLED", "工具调用已取消。"
                if monitor is not None and handle is not None:
                    try:
                        await monitor.finish(
                            handle, "failed",
                            error_code=record.error_code,
                            error_message=record.error_message,
                        )
                    except ToolMonitorError:
                        # 任务退出时还会统一收拢未结束的 running 记录。
                        pass
                raise
            except MCPToolError as exc:
                code, message = exc.code, exc.message
            else:
                if monitor is not None and handle is not None:
                    try:
                        await monitor.finish(handle, record.status, result=record.result)
                    except ToolMonitorError:
                        record.status, record.result = "failed", None
                        record.error_code = "TOOL_MONITOR_ERROR"
                        record.error_message = "工具调用状态记录失败。"
                        return {"error": {
                            "code": record.error_code,
                            "message": record.error_message,
                        }}
                record.error_code = record.error_message = None
                return record.result

            if monitor is not None and handle is not None:
                try:
                    await monitor.finish(
                        handle, "failed", error_code=code, error_message=message,
                    )
                except ToolMonitorError:
                    code, message = "TOOL_MONITOR_ERROR", "工具调用状态记录失败。"
            record.error_code, record.error_message = code, message
            return {"error": {"code": code, "message": message}}

        return StructuredTool(
            name=original.name, description=original.description,
            args_schema=original.args_schema, coroutine=invoke,
            metadata=original.metadata, tags=original.tags,
        )

    return [wrap(tool) for tool in tools]
