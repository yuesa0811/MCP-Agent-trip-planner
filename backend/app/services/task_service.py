"""旅行规划任务的持久化、后台执行和用户结果保存。"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..agents.trip_planner_agent import PlanningError, get_trip_planner_agent
from ..config import get_settings
from ..database import SessionLocal
from ..models.entities import ToolCall, TripTask, User, utc_now
from ..models.schemas import (
    AdminTaskDetailResponse,
    AdminTaskListItem,
    AdminTaskListResponse,
    PlanningResult,
    TaskDetailResponse,
    TaskError,
    TaskListItem,
    TaskListResponse,
    TaskToolCall,
    TripPlan,
    TripRequest,
)


logger = logging.getLogger(__name__)
EDITABLE_STATUSES = {"succeeded", "degraded"}


class TaskNotFoundError(LookupError):
    """任务不存在，或不属于当前用户。"""


class TaskStateError(RuntimeError):
    """当前任务状态不允许保存编辑结果。"""


class TaskPlanError(ValueError):
    """编辑后的行程与原请求不一致。"""


def create_task(session: Session, user_id: int, request: TripRequest) -> TripTask:
    task = TripTask(
        user_id=user_id,
        status="queued",
        current_stage="queued",
        request_data=request.model_dump(mode="json"),
        warnings=[],
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def _owned_task(session: Session, task_id: str, user_id: int) -> TripTask:
    task = session.scalar(
        select(TripTask).where(TripTask.id == task_id, TripTask.user_id == user_id),
    )
    if task is None:
        # 对不存在和无权访问返回同一种结果，避免泄露其他用户的任务 ID。
        raise TaskNotFoundError("任务不存在。")
    return task


def _task_error(task: TripTask) -> TaskError | None:
    if not task.error_code and not task.error_message:
        return None
    return TaskError(
        code=task.error_code or "UNKNOWN_ERROR",
        message=task.error_message or "任务执行失败。",
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 取回的时间可能没有 tzinfo；库中时间约定始终按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tool_call_schema(call: ToolCall) -> TaskToolCall:
    error = None
    if call.error_code or call.error_message:
        error = TaskError(
            code=call.error_code or "UNKNOWN_ERROR",
            message=call.error_message or "工具调用失败。",
        )
    return TaskToolCall(
        id=call.id,
        agent_name=call.agent_name,
        tool_name=call.tool_name,
        status=call.status,
        arguments=call.arguments,
        result_summary=call.result_summary,
        error=error,
        started_at=_as_utc(call.started_at),
        completed_at=_as_utc(call.completed_at),
        duration_ms=call.duration_ms,
    )


def task_detail(task: TripTask) -> TaskDetailResponse:
    raw_plan = task.edited_plan if task.edited_plan is not None else task.original_plan
    return TaskDetailResponse(
        task_id=task.id,
        status=task.status,
        stage=task.current_stage,
        request=TripRequest.model_validate(task.request_data),
        plan=TripPlan.model_validate(raw_plan) if raw_plan is not None else None,
        warnings=list(task.warnings or []),
        error=_task_error(task),
        tool_calls=[_tool_call_schema(item) for item in sorted(task.tool_calls, key=lambda item: item.id)],
        created_at=_as_utc(task.created_at),
        started_at=_as_utc(task.started_at),
        completed_at=_as_utc(task.completed_at),
        updated_at=_as_utc(task.updated_at),
        total_duration_ms=task.total_duration_ms,
    )


def get_task(session: Session, task_id: str, user_id: int) -> TaskDetailResponse:
    return task_detail(_owned_task(session, task_id, user_id))


def list_tasks(
    session: Session,
    user_id: int,
    *,
    page: int,
    page_size: int,
    status: str | None,
) -> TaskListResponse:
    filters = [TripTask.user_id == user_id]
    if status is not None:
        filters.append(TripTask.status == status)
    total = session.scalar(select(func.count()).select_from(TripTask).where(*filters)) or 0
    tasks = session.scalars(
        select(TripTask)
        .where(*filters)
        .order_by(TripTask.created_at.desc(), TripTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size),
    ).all()
    items = []
    for task in tasks:
        request = TripRequest.model_validate(task.request_data)
        items.append(TaskListItem(
            task_id=task.id,
            status=task.status,
            stage=task.current_stage,
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            created_at=_as_utc(task.created_at),
            updated_at=_as_utc(task.updated_at),
            completed_at=_as_utc(task.completed_at),
            total_duration_ms=task.total_duration_ms,
        ))
    return TaskListResponse(items=items, total=total, page=page, page_size=page_size)


def list_all_tasks(
    session: Session,
    *,
    page: int,
    page_size: int,
    status: str | None,
    username: str | None,
) -> AdminTaskListResponse:
    """管理员全局列表；一次联表查询返回任务所属用户。"""
    filters = []
    if status is not None:
        filters.append(TripTask.status == status)
    if username:
        filters.append(User.username.contains(username.strip().lower(), autoescape=True))
    total = session.scalar(
        select(func.count()).select_from(TripTask).join(User).where(*filters),
    ) or 0
    rows = session.execute(
        select(TripTask, User.username)
        .join(User)
        .where(*filters)
        .order_by(TripTask.created_at.desc(), TripTask.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size),
    ).all()
    items = []
    for task, owner_name in rows:
        request = TripRequest.model_validate(task.request_data)
        items.append(AdminTaskListItem(
            task_id=task.id,
            user_id=task.user_id,
            username=owner_name,
            status=task.status,
            stage=task.current_stage,
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            created_at=_as_utc(task.created_at),
            updated_at=_as_utc(task.updated_at),
            completed_at=_as_utc(task.completed_at),
            total_duration_ms=task.total_duration_ms,
        ))
    return AdminTaskListResponse(items=items, total=total, page=page, page_size=page_size)


def get_admin_task(session: Session, task_id: str) -> AdminTaskDetailResponse:
    row = session.execute(
        select(TripTask, User.username).join(User).where(TripTask.id == task_id),
    ).first()
    if row is None:
        raise TaskNotFoundError("任务不存在。")
    task, username = row
    detail = task_detail(task)
    return AdminTaskDetailResponse(
        **detail.model_dump(),
        user_id=task.user_id,
        username=username,
    )


def _validate_edited_plan(plan: TripPlan, request: TripRequest) -> TripPlan:
    edited = plan.model_copy(deep=True)
    if (edited.city, edited.start_date, edited.end_date) != (
        request.city, request.start_date, request.end_date,
    ):
        raise TaskPlanError("城市和起止日期必须与原规划请求一致。")
    if len(edited.days) != request.travel_days:
        raise TaskPlanError("每日行程数量必须与原规划天数一致。")
    start = date.fromisoformat(request.start_date)
    for index, day in enumerate(edited.days):
        expected_date = (start + timedelta(days=index)).isoformat()
        if day.date != expected_date or day.day_index != index:
            raise TaskPlanError("每日日期必须连续，且 day_index 从 0 开始。")
    if edited.budget is not None:
        edited.budget.total_attractions = sum(
            max(attraction.ticket_price, 0)
            for day in edited.days
            for attraction in day.attractions
        )
        edited.budget.total = (
            edited.budget.total_attractions
            + edited.budget.total_hotels
            + edited.budget.total_meals
            + edited.budget.total_transportation
        )
    return edited


def save_edited_result(
    session: Session,
    task_id: str,
    user_id: int,
    plan: TripPlan,
) -> TaskDetailResponse:
    task = _owned_task(session, task_id, user_id)
    if task.status not in EDITABLE_STATUSES:
        raise TaskStateError("只有已成功或已降级完成的任务可以保存修改。")
    request = TripRequest.model_validate(task.request_data)
    edited = _validate_edited_plan(plan, request)
    task.edited_plan = edited.model_dump(mode="json")
    task.updated_at = utc_now()
    session.commit()
    session.refresh(task)
    return task_detail(task)


def _claim_task(task_id: str) -> TripRequest | None:
    """原子领取 queued 任务；后台只凭 task_id 重新读取请求。"""
    now = utc_now()
    with SessionLocal() as session:
        row = session.execute(
            update(TripTask)
            .where(TripTask.id == task_id, TripTask.status == "queued")
            .values(status="running", current_stage="planning", started_at=now, updated_at=now)
            .returning(TripTask.request_data),
        ).first()
        session.commit()
    if row is None:
        return None
    return TripRequest.model_validate(row[0])


def _complete_task(task_id: str, result: PlanningResult, duration_ms: float) -> None:
    now = utc_now()
    with SessionLocal() as session:
        session.execute(
            update(TripTask)
            .where(TripTask.id == task_id, TripTask.status == "running")
            .values(
                status=result.status,
                current_stage="completed",
                original_plan=result.plan.model_dump(mode="json"),
                warnings=list(result.warnings),
                error_code=None,
                error_message=None,
                completed_at=now,
                updated_at=now,
                total_duration_ms=duration_ms,
            ),
        )
        session.commit()


def _fail_task(task_id: str, code: str, message: str, duration_ms: float) -> None:
    now = utc_now()
    with SessionLocal() as session:
        session.execute(
            update(TripTask)
            .where(TripTask.id == task_id, TripTask.status.in_(("queued", "running")))
            .values(
                status="failed",
                current_stage="failed",
                error_code=code,
                error_message=message,
                completed_at=now,
                updated_at=now,
                total_duration_ms=duration_ms,
            ),
        )
        session.commit()


def _close_running_tool_calls(task_id: str) -> None:
    """任务退出后收拢异常遗留的调用；未知实际耗时不作推测。"""
    with SessionLocal() as session:
        session.execute(
            update(ToolCall)
            .where(ToolCall.task_id == task_id, ToolCall.status == "running")
            .values(
                status="failed",
                error_code="TOOL_INTERRUPTED",
                error_message="任务已结束，但工具调用未正常写入终态。",
                completed_at=utc_now(),
                duration_ms=None,
            ),
        )
        session.commit()


async def _persist_failure(task_id: str, code: str, message: str, duration_ms: float) -> None:
    try:
        await run_in_threadpool(_fail_task, task_id, code, message, duration_ms)
    except Exception as exc:
        logger.error(
            "无法写入任务失败状态: task_id=%s type=%s", task_id, type(exc).__name__,
        )


async def run_trip_task(task_id: str) -> None:
    """后台执行入口；不接收请求 Session、用户对象或前端传入的归属信息。"""
    started = perf_counter()
    claimed = False
    try:
        request = await run_in_threadpool(_claim_task, task_id)
        if request is None:
            return
        claimed = True
        settings = get_settings()
        result = await asyncio.wait_for(
            get_trip_planner_agent().plan_trip(request, task_id=task_id),
            timeout=settings.planning_timeout_seconds,
        )
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await run_in_threadpool(_complete_task, task_id, result, duration_ms)
    except asyncio.CancelledError:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await _persist_failure(task_id, "TASK_CANCELLED", "规划任务已取消。", duration_ms)
        raise
    except asyncio.TimeoutError:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await _persist_failure(
            task_id, "PLANNING_TIMEOUT", "规划超过时间上限，请缩短行程后重试。", duration_ms,
        )
    except PlanningError as exc:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await _persist_failure(task_id, exc.code, exc.message, duration_ms)
    except ValidationError:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        await _persist_failure(task_id, "INVALID_TASK_DATA", "任务数据无法解析。", duration_ms)
    except Exception as exc:
        duration_ms = round((perf_counter() - started) * 1000, 3)
        logger.error(
            "任务执行出现未预期异常: task_id=%s type=%s", task_id, type(exc).__name__,
        )
        await _persist_failure(task_id, "INTERNAL_ERROR", "任务执行失败，请稍后重试。", duration_ms)
    finally:
        if claimed:
            try:
                await run_in_threadpool(_close_running_tool_calls, task_id)
            except Exception as exc:
                logger.error(
                    "无法收拢未完成工具记录: task_id=%s type=%s",
                    task_id, type(exc).__name__,
                )


def recover_interrupted_tasks() -> int:
    """启动时结束遗留任务；不自动重放，避免重复调用模型与外部工具。"""
    now = utc_now()
    with SessionLocal() as session:
        task_ids = session.scalars(
            select(TripTask.id).where(TripTask.status.in_(("queued", "running"))),
        ).all()
        if task_ids:
            session.execute(
                update(TripTask)
                .where(TripTask.id.in_(task_ids))
                .values(
                    status="failed",
                    current_stage="failed",
                    error_code="SERVICE_RESTARTED",
                    error_message="服务重启导致任务中断，请重新提交。",
                    completed_at=now,
                    updated_at=now,
                    total_duration_ms=None,
                ),
            )
        session.execute(
            update(ToolCall)
            .where(ToolCall.status == "running")
            .values(
                status="failed",
                error_code="SERVICE_RESTARTED",
                error_message="服务重启导致工具调用中断。",
                completed_at=now,
                duration_ms=None,
            ),
        )
        session.commit()
        return len(task_ids)
