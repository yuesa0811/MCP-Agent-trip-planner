"""登录用户提交、查询和编辑自己的旅行规划任务。"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...models.entities import User
from ...models.schemas import (
    TaskDetailResponse,
    TaskListResponse,
    TaskStatus,
    TaskSubmitResponse,
    TripPlan,
    TripRequest,
)
from ...services import task_service
from ...services.task_service import TaskNotFoundError, TaskPlanError, TaskStateError
from ..deps import get_current_user, get_db


router = APIRouter(prefix="/trip", tags=["旅行任务"])


@router.post(
    "/tasks",
    response_model=TaskSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_task(
    request: TripRequest,
    background_tasks: BackgroundTasks,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
):
    """先持久化任务再返回 202；后台只接收新任务 ID。"""
    task = task_service.create_task(session, user.id, request)
    background_tasks.add_task(task_service.run_trip_task, task.id)
    return TaskSubmitResponse(task_id=task.id, status=task.status)


@router.get("/tasks", response_model=TaskListResponse)
def my_tasks(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
):
    return task_service.list_tasks(
        session, user.id, page=page, page_size=page_size, status=task_status,
    )


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
def task_details(
    task_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
):
    """返回本人任务及真实工具调用记录；运行中的调用也会立即可见。"""
    try:
        return task_service.get_task(session, task_id, user.id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.patch("/tasks/{task_id}/result", response_model=TaskDetailResponse)
def update_task_result(
    task_id: str,
    plan: TripPlan,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
):
    try:
        return task_service.save_edited_result(session, task_id, user.id, plan)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except TaskStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except TaskPlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
