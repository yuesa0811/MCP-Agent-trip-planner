"""真实 MCP 调用的数据库监控；每次写入都使用独立短 Session。"""

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from sqlalchemy import update
from starlette.concurrency import run_in_threadpool

from ..database import SessionLocal
from ..models.entities import ToolCall, TripTask, utc_now


FinalToolStatus = Literal["succeeded", "empty", "failed"]
_SENSITIVE_KEY_PARTS = (
    "api_key", "apikey", "authorization", "credential", "password", "secret", "token",
)
_MAX_ITEMS = 10
_MAX_STRING_LENGTH = 300
_MAX_PREVIEW_LENGTH = 1500


class ToolMonitorError(RuntimeError):
    """监控记录无法写入；消息固定，不包含数据库原始异常。"""


@dataclass(frozen=True)
class ToolCallHandle:
    call_id: int
    tool_name: str
    started_monotonic: float


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _safe_json(value: object, *, depth: int = 0) -> object:
    """生成有界、可 JSON 化的摘要，不保存认证字段。"""
    if depth >= 4:
        return "[已省略深层内容]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_LENGTH:
            return value
        return value[:_MAX_STRING_LENGTH] + "…"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["_omitted"] = len(value) - _MAX_ITEMS
                break
            name = str(key)[:80]
            result[name] = "[已脱敏]" if _is_sensitive_key(key) else _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_safe_json(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
        if len(value) > _MAX_ITEMS:
            result.append(f"[另有 {len(value) - _MAX_ITEMS} 项已省略]")
        return result
    return f"[{type(value).__name__}]"


def summarize_arguments(arguments: dict) -> dict:
    summary = _safe_json(arguments)
    return summary if isinstance(summary, dict) else {}


def summarize_result(tool_name: str, result: dict) -> dict:
    """保留排查所需的数量和短预览，不把完整 MCP 响应写入数据库。"""
    summary: dict[str, object] = {
        "tool_name": tool_name,
        "keys": [str(key)[:80] for key in list(result)[:_MAX_ITEMS]],
    }
    if isinstance(result.get("pois"), list):
        summary["item_count"] = len(result["pois"])
    elif isinstance(result.get("forecasts"), list):
        forecasts = result["forecasts"]
        summary["item_count"] = sum(
            len(item.get("casts", []))
            for item in forecasts
            if isinstance(item, dict) and isinstance(item.get("casts"), list)
        )
    elif isinstance(result.get("lives"), list):
        summary["item_count"] = len(result["lives"])

    safe_result = _safe_json(result)
    preview = json.dumps(safe_result, ensure_ascii=False, separators=(",", ":"))
    summary["preview"] = (
        preview if len(preview) <= _MAX_PREVIEW_LENGTH
        else preview[:_MAX_PREVIEW_LENGTH] + "…"
    )
    return summary


def _set_stage(task_id: str, stage: str) -> None:
    now = utc_now()
    with SessionLocal() as session:
        changed = session.execute(
            update(TripTask)
            .where(TripTask.id == task_id, TripTask.status == "running")
            .values(current_stage=stage, updated_at=now),
        ).rowcount
        session.commit()
    if changed != 1:
        raise ToolMonitorError("无法更新任务执行阶段。")


def _start_call(task_id: str, agent_name: str, tool_name: str, arguments: dict) -> int:
    with SessionLocal() as session:
        task = session.get(TripTask, task_id)
        if task is None or task.status != "running":
            raise ToolMonitorError("任务不存在或已不在运行中。")
        call = ToolCall(
            task_id=task_id,
            agent_name=agent_name,
            tool_name=tool_name,
            status="running",
            arguments=arguments,
            started_at=utc_now(),
        )
        session.add(call)
        session.commit()
        return call.id


def _finish_call(
    task_id: str,
    call_id: int,
    status: FinalToolStatus,
    duration_ms: float,
    result_summary: dict | None,
    error_code: str | None,
    error_message: str | None,
) -> None:
    with SessionLocal() as session:
        changed = session.execute(
            update(ToolCall)
            .where(
                ToolCall.id == call_id,
                ToolCall.task_id == task_id,
                ToolCall.status == "running",
            )
            .values(
                status=status,
                result_summary=result_summary,
                error_code=error_code,
                error_message=error_message,
                completed_at=utc_now(),
                duration_ms=max(duration_ms, 0),
            ),
        ).rowcount
        session.commit()
    if changed != 1:
        raise ToolMonitorError("无法更新工具调用终态。")


class DatabaseToolMonitor:
    """显式绑定一个任务；不使用全局“当前任务”或上下文变量。"""

    def __init__(self, task_id: str):
        self.task_id = task_id

    async def set_stage(self, stage: str) -> None:
        try:
            await run_in_threadpool(_set_stage, self.task_id, stage)
        except ToolMonitorError:
            raise
        except Exception:
            raise ToolMonitorError("无法更新任务执行阶段。") from None

    async def start(self, agent_name: str, tool_name: str, arguments: dict) -> ToolCallHandle:
        try:
            call_id = await run_in_threadpool(
                _start_call,
                self.task_id,
                agent_name,
                tool_name,
                summarize_arguments(arguments),
            )
        except ToolMonitorError:
            raise
        except Exception:
            raise ToolMonitorError("无法创建工具调用记录。") from None
        # 数据库确认 running 已提交后再开始计量真实外部调用耗时。
        return ToolCallHandle(
            call_id=call_id,
            tool_name=tool_name,
            started_monotonic=perf_counter(),
        )

    async def finish(
        self,
        handle: ToolCallHandle,
        status: FinalToolStatus,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        duration_ms = round((perf_counter() - handle.started_monotonic) * 1000, 3)
        output = summarize_result(handle.tool_name, result) if result is not None else None
        try:
            await run_in_threadpool(
                _finish_call,
                self.task_id,
                handle.call_id,
                status,
                duration_ms,
                output,
                error_code if status == "failed" else None,
                error_message if status == "failed" else None,
            )
        except ToolMonitorError:
            raise
        except Exception:
            raise ToolMonitorError("无法更新工具调用终态。") from None
