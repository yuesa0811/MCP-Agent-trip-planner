"""同步兼容规划接口；复用持久化任务执行链并保留原响应字段。"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models.entities import User
from ...models.schemas import TripRequest, TripPlanResponse
from ...services import task_service
from ..deps import get_current_user, get_db

router = APIRouter(prefix="/trip", tags=["旅行规划"])
logger = logging.getLogger(__name__)


def _failure(status_code: int, code: str, message: str) -> JSONResponse:
    payload = TripPlanResponse(success=False, message=message, status="failed").model_dump()
    # 原 Axios 客户端从 detail 读取非 2xx 错误；error_code 供后续任务记录使用。
    payload.update(detail=message, error_code=code)
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
async def plan_trip(
    request: TripRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_db)],
):
    """创建可回看的任务并等待完成；主要响应结构与旧客户端兼容。"""
    try:
        task = task_service.create_task(session, user.id, request)
        await task_service.run_trip_task(task.id)
        session.expire_all()
        result = task_service.get_task(session, task.id, user.id)
        if result.status not in {"succeeded", "degraded"} or result.plan is None:
            code = result.error.code if result.error else "INTERNAL_ERROR"
            message = result.error.message if result.error else "规划任务未正常完成。"
            logger.warning("旅行规划失败，code=%s", code)
            status_code = {
                "PLANNING_TIMEOUT": 504,
                "MODEL_CONFIG_ERROR": 503,
                "INTERNAL_ERROR": 500,
                "INVALID_TASK_DATA": 500,
                "TOOL_MONITOR_ERROR": 500,
            }.get(code, 502)
            return _failure(status_code, code, message)
        return TripPlanResponse(
            success=True,
            message="旅行计划已生成，部分资料缺失" if result.status == "degraded" else "旅行计划生成成功",
            data=result.plan,
            status=result.status,
            warnings=result.warnings,
        )
    except Exception as exc:
        # 数据库或供应商原始异常可能包含敏感信息，不直接返回前端。
        logger.error("旅行规划内部异常，type=%s", type(exc).__name__)
        return _failure(500, "INTERNAL_ERROR", "规划服务发生内部异常，请稍后重试")


@router.get("/health", summary="旅行规划服务健康检查")
async def health_check():
    """仅检查本地配置，不创建 Agent、不启动 MCP、不调用模型。"""
    settings = get_settings()
    if not settings.get_llm_config()["api_key"].strip() or not settings.amap_api_key.strip():
        raise HTTPException(status_code=503, detail="模型或高德密钥未配置")
    return {
        "status": "healthy", "service": "trip-planner", "framework": "langchain",
        "agents": ["attraction", "weather", "hotel", "planner"],
        "external_services_checked": False,
    }
