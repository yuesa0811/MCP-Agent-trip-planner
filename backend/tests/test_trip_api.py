"""同步兼容接口的认证、任务复用、失败响应和健康检查。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.agents.trip_planner_agent import PlanningError
from app.api import deps
from app.api.routes import trip
from app.database import create_db_engine, init_database
from app.models.entities import TripTask, User
from app.models.schemas import PlanningResult, TripPlan
from app.services import task_service
from app.services.auth_service import create_access_token


REQUEST = {
    "city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-01",
    "travel_days": 1, "transportation": "步行", "accommodation": "经济型酒店",
}


@pytest.fixture
def app_context(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'trip-api.db').as_posix()}")
    init_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(task_service, "SessionLocal", factory)

    with factory() as session:
        user = User(username="trip_user", password_hash="unused")
        session.add(user)
        session.commit()
        session.refresh(user)

    application = FastAPI()
    application.include_router(trip.router, prefix="/api")

    def override_db():
        with factory() as session:
            yield session

    application.dependency_overrides[deps.get_db] = override_db
    yield application, factory, user
    engine.dispose()


@pytest_asyncio.fixture
async def client(app_context):
    application, _, user = app_context
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test", headers=headers,
    ) as test_client:
        yield test_client


def install_planner(monkeypatch, execute):
    monkeypatch.setattr(
        task_service,
        "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=execute),
    )


@pytest.mark.asyncio
async def test_plan_requires_login_and_does_not_create_task(app_context):
    application, factory, _ = app_context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as anonymous:
        response = await anonymous.post("/api/trip/plan", json=REQUEST)
    assert response.status_code == 401
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TripTask)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "degraded"])
async def test_plan_response_remains_compatible_and_is_saved_to_history(
    client, app_context, monkeypatch, status,
):
    _, factory, user = app_context
    plan = TripPlan(
        city="北京", start_date="2026-10-01", end_date="2026-10-01",
        days=[], overall_suggestions="测试",
    )
    warnings = ["天气暂不可用"] if status == "degraded" else []
    execute = AsyncMock(return_value=PlanningResult(plan=plan, status=status, warnings=warnings))
    install_planner(monkeypatch, execute)

    response = await client.post("/api/trip/plan", json=REQUEST)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["city"] == "北京"
    assert response.json()["status"] == status
    assert response.json()["warnings"] == warnings
    execute.assert_awaited_once()
    assert execute.await_args.kwargs["task_id"]

    with factory() as session:
        task = session.scalar(select(TripTask))
        assert task.user_id == user.id
        assert task.status == status
        assert task.original_plan["city"] == "北京"


@pytest.mark.asyncio
@pytest.mark.parametrize("code, status", [
    ("NO_ATTRACTIONS", 502),
    ("INVALID_PLAN", 502),
    ("PLANNING_TIMEOUT", 504),
    ("MODEL_CONFIG_ERROR", 503),
])
async def test_failures_are_saved_and_not_returned_as_success(
    client, app_context, monkeypatch, code, status,
):
    _, factory, _ = app_context
    execute = AsyncMock(side_effect=PlanningError(code, "可理解的失败原因"))
    install_planner(monkeypatch, execute)
    response = await client.post("/api/trip/plan", json=REQUEST)
    assert response.status_code == status
    payload = response.json()
    assert payload["success"] is False and payload["data"] is None
    assert payload["status"] == "failed" and payload["error_code"] == code
    assert payload["detail"] == "可理解的失败原因"
    with factory() as session:
        task = session.scalar(select(TripTask))
        assert (task.status, task.error_code) == ("failed", code)


@pytest.mark.asyncio
async def test_invalid_dates_rejected_before_task_creation(client, app_context, monkeypatch):
    _, factory, _ = app_context
    execute = AsyncMock()
    install_planner(monkeypatch, execute)
    response = await client.post("/api/trip/plan", json={**REQUEST, "travel_days": 2})
    assert response.status_code == 422
    execute.assert_not_called()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TripTask)) == 0


@pytest.mark.asyncio
async def test_health_is_available_while_planning_and_never_creates_agents(
    client, monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_plan(_request, *, task_id):
        assert task_id
        entered.set()
        await release.wait()
        raise PlanningError("NO_ATTRACTIONS", "测试结束")

    install_planner(monkeypatch, slow_plan)
    monkeypatch.setattr(trip, "get_settings", lambda: SimpleNamespace(
        amap_api_key="test", get_llm_config=lambda: {"api_key": "test"},
    ))
    pending = asyncio.create_task(client.post("/api/trip/plan", json=REQUEST))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        monkeypatch.setattr(
            task_service, "get_trip_planner_agent",
            lambda: pytest.fail("健康检查不能创建 Agent"),
        )
        response = await asyncio.wait_for(client.get("/api/trip/health"), timeout=1)
        assert response.status_code == 200
        assert response.json()["external_services_checked"] is False
        assert not pending.done()
    finally:
        release.set()
        await pending


@pytest.mark.asyncio
async def test_unexpected_errors_do_not_expose_exception_details(
    client, app_context, monkeypatch,
):
    _, factory, _ = app_context
    execute = AsyncMock(side_effect=RuntimeError("secret-provider-token"))
    install_planner(monkeypatch, execute)
    response = await client.post("/api/trip/plan", json=REQUEST)
    assert response.status_code == 500
    assert "secret-provider-token" not in response.text
    with factory() as session:
        task = session.scalar(select(TripTask))
        assert (task.status, task.error_code) == ("failed", "INTERNAL_ERROR")
