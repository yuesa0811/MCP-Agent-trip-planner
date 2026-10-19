"""管理员查看全局任务和工具过程；不提供修改或重放操作。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...models.schemas import (
    AdminTaskDetailResponse,
    AdminTaskListResponse,
    TaskStatus,
)
from ...services import task_service
from ...services.task_service import TaskNotFoundError
from ..deps import get_db, require_admin


router = APIRouter(
    prefix="/admin",
    tags=["管理员"],
    dependencies=[Depends(require_admin)],
)


@router.get("/tasks", response_model=AdminTaskListResponse)
def all_tasks(
    session: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    username: Annotated[str | None, Query(min_length=1, max_length=32, pattern=r"\S")] = None,
):
    return task_service.list_all_tasks(
        session,
        page=page,
        page_size=page_size,
        status=task_status,
        username=username,
    )


@router.get("/tasks/{task_id}", response_model=AdminTaskDetailResponse)
def task_details(
    task_id: str,
    session: Annotated[Session, Depends(get_db)],
):
    try:
        return task_service.get_admin_task(session, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
