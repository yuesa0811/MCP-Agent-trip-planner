"""任务提交、后台终态、历史隔离、编辑保存和重启恢复测试。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.main import app
from app.models.entities import ToolCall, TripTask, User, utc_now
from app.models.schemas import PlanningResult, TripPlan, TripRequest
from app.services import task_service
from app.services.auth_service import create_access_token
from app.agents.trip_planner_agent import PlanningError
from app.database import create_db_engine, init_database


REQUEST = {
    "city": "北京",
    "start_date": "2026-10-01",
    "end_date": "2026-10-01",
    "travel_days": 1,
    "transportation": "公共交通",
    "accommodation": "经济型酒店",
    "preferences": ["历史文化"],
}


def plan_data(*, city="北京", budget=True):
    data = {
        "city": city,
        "start_date": "2026-10-01",
        "end_date": "2026-10-01",
        "days": [{
            "date": "2026-10-01",
            "day_index": 0,
            "description": "文化一日游",
            "transportation": "公共交通",
            "accommodation": "经济型酒店",
            "attractions": [
                {
                    "name": "故宫博物院",
                    "address": "东城区景山前街4号",
                    "location": {"longitude": 116.397, "latitude": 39.918},
                    "visit_duration": 180,
                    "description": "历史建筑群",
                    "ticket_price": 10,
                },
                {
                    "name": "中国国家博物馆",
                    "address": "东城区东长安街16号",
                    "location": {"longitude": 116.407, "latitude": 39.905},
                    "visit_duration": 120,
                    "description": "综合博物馆",
                    "ticket_price": 20,
                },
            ],
            "meals": [],
        }],
        "weather_info": [],
        "overall_suggestions": "提前预约。",
    }
    if budget:
        data["budget"] = {
            "total_attractions": 30,
            "total_hotels": 100,
            "total_meals": 50,
            "total_transportation": 10,
            "total": 190,
        }
    return data


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'tasks.db').as_posix()}")
    init_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_db
    monkeypatch.setattr(task_service, "SessionLocal", factory)
    with factory() as session:
        first = User(username="first_user", password_hash="unused", is_admin=False)
        second = User(username="second_user", password_hash="unused", is_admin=False)
        session.add_all([first, second])
        session.commit()
        session.refresh(first)
        session.refresh(second)
    yield factory, first, second
    app.dependency_overrides.pop(deps.get_db, None)
    engine.dispose()


@pytest_asyncio.fixture
async def client(database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as test_client:
        yield test_client


def auth_headers(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def add_task(factory, user, *, status="queued", plan=None, created_at=None):
    with factory() as session:
        task = TripTask(
            user_id=user.id,
            status=status,
            current_stage="completed" if status in {"succeeded", "degraded"} else status,
            request_data=REQUEST,
            original_plan=plan,
            warnings=[],
            created_at=created_at or utc_now(),
            completed_at=utc_now() if status in {"succeeded", "degraded", "failed"} else None,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


@pytest.mark.asyncio
async def test_submit_requires_login_and_uses_current_user_only(client, database, monkeypatch):
    factory, first, second = database
    assert (await client.post("/api/trip/tasks", json=REQUEST)).status_code == 401
    captured = []

    async def fake_background(task_id):
        captured.append(task_id)

    monkeypatch.setattr(task_service, "run_trip_task", fake_background)
    response = await client.post(
        "/api/trip/tasks",
        json={**REQUEST, "user_id": second.id},
        headers=auth_headers(first),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert captured == [response.json()["task_id"]]
    with factory() as session:
        task = session.get(TripTask, response.json()["task_id"])
        assert task.user_id == first.id
        assert task.request_data["city"] == "北京"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_status", ["succeeded", "degraded"])
async def test_background_execution_persists_plan_and_terminal_status(
    database, monkeypatch, final_status,
):
    factory, first, _ = database
    task_id = add_task(factory, first)
    result = PlanningResult(
        plan=TripPlan.model_validate(plan_data()),
        status=final_status,
        warnings=["天气资料不完整"] if final_status == "degraded" else [],
    )

    async def plan_trip(_request, *, task_id):
        assert task_id
        return result

    monkeypatch.setattr(
        task_service, "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=plan_trip),
    )
    await task_service.run_trip_task(task_id)
    with factory() as session:
        task = session.get(TripTask, task_id)
        assert task.status == final_status
        assert task.current_stage == "completed"
        assert task.original_plan["city"] == "北京"
        assert task.edited_plan is None
        assert task.warnings == result.warnings
        assert task.started_at is not None and task.completed_at is not None
        assert task.total_duration_ms >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_code,expected_message",
    [
        (PlanningError("NO_ATTRACTIONS", "未获取可用景点资料。"), "NO_ATTRACTIONS", "未获取可用景点资料。"),
        (RuntimeError("secret-provider-token"), "INTERNAL_ERROR", "任务执行失败，请稍后重试。"),
    ],
)
async def test_background_errors_become_safe_failed_state(
    database, monkeypatch, error, expected_code, expected_message,
):
    factory, first, _ = database
    task_id = add_task(factory, first)

    async def plan_trip(_request, *, task_id):
        assert task_id
        raise error

    monkeypatch.setattr(
        task_service, "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=plan_trip),
    )
    await task_service.run_trip_task(task_id)
    with factory() as session:
        task = session.get(TripTask, task_id)
        assert (task.status, task.current_stage) == ("failed", "failed")
        assert (task.error_code, task.error_message) == (expected_code, expected_message)
        assert "secret-provider-token" not in task.error_message


@pytest.mark.asyncio
async def test_background_timeout_and_duplicate_execution_have_clear_outcomes(database, monkeypatch):
    factory, first, _ = database
    task_id = add_task(factory, first)
    calls = 0

    async def slow_plan(_request, *, task_id):
        nonlocal calls
        assert task_id
        calls += 1
        await asyncio.sleep(1)

    monkeypatch.setattr(
        task_service, "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=slow_plan),
    )
    monkeypatch.setattr(
        task_service, "get_settings",
        lambda: SimpleNamespace(planning_timeout_seconds=0.01),
    )
    await task_service.run_trip_task(task_id)
    await task_service.run_trip_task(task_id)
    with factory() as session:
        task = session.get(TripTask, task_id)
        assert task.status == "failed"
        assert task.error_code == "PLANNING_TIMEOUT"
        assert calls == 1


@pytest.mark.asyncio
async def test_cancelled_background_task_is_not_left_running(database, monkeypatch):
    factory, first, _ = database
    task_id = add_task(factory, first)
    entered = asyncio.Event()

    async def wait_forever(_request, *, task_id):
        assert task_id
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        task_service, "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=wait_forever),
    )
    running = asyncio.create_task(task_service.run_trip_task(task_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with factory() as session:
        task = session.get(TripTask, task_id)
        assert task.status == "failed"
        assert task.error_code == "TASK_CANCELLED"


@pytest.mark.asyncio
async def test_history_filter_pagination_detail_and_user_isolation(client, database):
    factory, first, second = database
    now = utc_now()
    newest_failed = add_task(factory, first, status="failed", created_at=now)
    add_task(factory, first, status="succeeded", plan=plan_data(), created_at=now - timedelta(minutes=1))
    add_task(factory, first, status="failed", created_at=now - timedelta(minutes=2))
    other_task = add_task(factory, second, status="succeeded", plan=plan_data())

    response = await client.get(
        "/api/trip/tasks?status=failed&page=1&page_size=1",
        headers=auth_headers(first),
    )
    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 2 and body["page"] == 1 and body["page_size"] == 1
    assert [item["task_id"] for item in body["items"]] == [newest_failed]

    detail = await client.get(f"/api/trip/tasks/{newest_failed}", headers=auth_headers(first))
    assert detail.status_code == 200
    assert set(detail.json()) == {
        "task_id", "status", "stage", "request", "plan", "warnings", "error",
        "tool_calls", "created_at", "started_at", "completed_at", "updated_at",
        "total_duration_ms",
    }
    assert detail.json()["tool_calls"] == []
    assert (await client.get(
        f"/api/trip/tasks/{other_task}", headers=auth_headers(first),
    )).status_code == 404
    assert (await client.patch(
        f"/api/trip/tasks/{other_task}/result",
        json=plan_data(), headers=auth_headers(first),
    )).status_code == 404


@pytest.mark.asyncio
async def test_save_edit_keeps_original_and_recalculates_budget(client, database):
    factory, first, _ = database
    original = plan_data()
    task_id = add_task(factory, first, status="succeeded", plan=original)
    edited = plan_data()
    edited["days"][0]["attractions"].pop(0)
    response = await client.patch(
        f"/api/trip/tasks/{task_id}/result",
        json=edited,
        headers=auth_headers(first),
    )
    assert response.status_code == 200
    returned = response.json()["plan"]
    assert [item["name"] for item in returned["days"][0]["attractions"]] == ["中国国家博物馆"]
    assert returned["budget"]["total_attractions"] == 20
    assert returned["budget"]["total"] == 180
    with factory() as session:
        task = session.get(TripTask, task_id)
        assert len(task.original_plan["days"][0]["attractions"]) == 2
        assert len(task.edited_plan["days"][0]["attractions"]) == 1

    no_budget_id = add_task(factory, first, status="degraded", plan=plan_data(budget=False))
    no_budget = await client.patch(
        f"/api/trip/tasks/{no_budget_id}/result",
        json=plan_data(budget=False), headers=auth_headers(first),
    )
    assert no_budget.status_code == 200
    assert no_budget.json()["plan"]["budget"] is None


@pytest.mark.asyncio
async def test_save_edit_rejects_unfinished_or_inconsistent_plan(client, database):
    factory, first, _ = database
    queued_id = add_task(factory, first)
    assert (await client.patch(
        f"/api/trip/tasks/{queued_id}/result",
        json=plan_data(), headers=auth_headers(first),
    )).status_code == 409

    completed_id = add_task(factory, first, status="succeeded", plan=plan_data())
    mismatch = await client.patch(
        f"/api/trip/tasks/{completed_id}/result",
        json=plan_data(city="上海"), headers=auth_headers(first),
    )
    assert mismatch.status_code == 422
    with factory() as session:
        assert session.get(TripTask, completed_id).edited_plan is None


@pytest.mark.asyncio
async def test_task_detail_returns_tool_calls_in_database_order(client, database):
    factory, first, _ = database
    task_id = add_task(factory, first, status="succeeded", plan=plan_data())
    with factory() as session:
        session.add_all([
            ToolCall(
                task_id=task_id,
                agent_name="attraction",
                tool_name="maps_text_search",
                status="failed",
                arguments={"city": "北京"},
                error_code="TOOL_TIMEOUT",
                error_message="工具调用超时。",
                completed_at=utc_now(),
                duration_ms=12.5,
            ),
            ToolCall(
                task_id=task_id,
                agent_name="attraction",
                tool_name="maps_text_search",
                status="succeeded",
                arguments={"city": "北京"},
                result_summary={"item_count": 1},
                completed_at=utc_now(),
                duration_ms=8.25,
            ),
        ])
        session.commit()

    response = await client.get(f"/api/trip/tasks/{task_id}", headers=auth_headers(first))
    assert response.status_code == 200
    calls = response.json()["tool_calls"]
    assert [item["status"] for item in calls] == ["failed", "succeeded"]
    assert calls[0]["error"] == {"code": "TOOL_TIMEOUT", "message": "工具调用超时。"}
    assert calls[1]["result_summary"] == {"item_count": 1}
    assert calls[1]["duration_ms"] == 8.25
    assert calls[1]["started_at"].endswith("Z")


@pytest.mark.asyncio
async def test_task_exit_closes_unfinished_tool_records(database, monkeypatch):
    factory, first, _ = database
    task_id = add_task(factory, first)
    result = PlanningResult(
        plan=TripPlan.model_validate(plan_data()), status="succeeded", warnings=[],
    )

    async def plan_trip(_request, *, task_id):
        with factory() as session:
            session.add(ToolCall(
                task_id=task_id,
                agent_name="weather",
                tool_name="maps_weather",
                status="running",
                arguments={"city": "北京"},
            ))
            session.commit()
        return result

    monkeypatch.setattr(
        task_service, "get_trip_planner_agent",
        lambda: SimpleNamespace(plan_trip=plan_trip),
    )
    await task_service.run_trip_task(task_id)
    with factory() as session:
        task = session.get(TripTask, task_id)
        call = session.scalar(select(ToolCall).where(ToolCall.task_id == task_id))
        assert task.status == "succeeded"
        assert (call.status, call.error_code) == ("failed", "TOOL_INTERRUPTED")
        assert call.completed_at is not None and call.duration_ms is None


def test_restart_marks_unfinished_tasks_and_calls_failed_without_replay(database):
    factory, first, _ = database
    queued_id = add_task(factory, first)
    running_id = add_task(factory, first, status="running")
    complete_id = add_task(factory, first, status="succeeded", plan=plan_data())
    with factory() as session:
        session.add(ToolCall(
            task_id=running_id,
            agent_name="attraction",
            tool_name="maps_text_search",
            status="running",
            arguments={"city": "北京"},
        ))
        session.commit()

    assert task_service.recover_interrupted_tasks() == 2
    with factory() as session:
        queued = session.get(TripTask, queued_id)
        running = session.get(TripTask, running_id)
        complete = session.get(TripTask, complete_id)
        call = session.scalar(select(ToolCall).where(ToolCall.task_id == running_id))
        assert queued.status == running.status == "failed"
        assert queued.error_code == running.error_code == "SERVICE_RESTARTED"
        assert queued.total_duration_ms is None and running.total_duration_ms is None
        assert complete.status == "succeeded" and complete.error_code is None
        assert call.status == "failed" and call.error_code == "SERVICE_RESTARTED"
        assert call.duration_ms is None
