"""数据库实体；与 API 的 Pydantic 输入/输出模型分开。"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index,
    Integer, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    tasks: Mapped[list["TripTask"]] = relationship(back_populates="user")


class TripTask(Base):
    __tablename__ = "trip_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'degraded', 'failed')",
            name="ck_trip_tasks_status",
        ),
        CheckConstraint(
            "total_duration_ms IS NULL OR total_duration_ms >= 0",
            name="ck_trip_tasks_duration",
        ),
        Index("ix_trip_tasks_user_created", "user_id", "created_at"),
        Index("ix_trip_tasks_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    request_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    original_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    edited_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
    )
    total_duration_ms: Mapped[float | None] = mapped_column(Float)

    user: Mapped[User] = relationship(back_populates="tasks")
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="task", cascade="all, delete-orphan",
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'empty', 'failed')",
            name="ck_tool_calls_status",
        ),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_tool_calls_duration"),
        Index("ix_tool_calls_task_id_id", "task_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("trip_tasks.id", ondelete="CASCADE"), nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)

    task: Mapped[TripTask] = relationship(back_populates="tool_calls")
