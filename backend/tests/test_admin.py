"""管理员全局任务列表、详情和服务端权限测试。"""

from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.main import app
from app.database import create_db_engine, init_database
from app.models.entities import ToolCall, TripTask, User, utc_now
from app.services.auth_service import create_access_token


def request_data(city: str) -> dict:
    return {
        "city": city,
        "start_date": "2026-10-01",
        "end_date": "2026-10-01",
        "travel_days": 1,
        "transportation": "公共交通",
        "accommodation": "经济型酒店",
        "preferences": [],
    }


@pytest.fixture
def database(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'admin.db').as_posix()}")
    init_database(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_db
    with factory() as session:
        admin = User(username="root_admin", password_hash="unused", is_admin=True)
        alice = User(username="alice_user", password_hash="unused", is_admin=False)
        bob = User(username="bob_user", password_hash="unused", is_admin=False)
        session.add_all([admin, alice, bob])
        session.commit()
        for user in (admin, alice, bob):
            session.refresh(user)
    yield factory, admin, alice, bob
    app.dependency_overrides.pop(deps.get_db, None)
    engine.dispose()


@pytest_asyncio.fixture
async def client(database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as test_client:
        yield test_client


def headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def add_task(factory, user: User, city: str, status: str, *, age_minutes: int) -> str:
    timestamp = utc_now() - timedelta(minutes=age_minutes)
    with factory() as session:
        task = TripTask(
            user_id=user.id,
            status=status,
            current_stage="weather" if status == "running" else (
                "completed" if status in {"succeeded", "degraded"} else status
            ),
            request_data=request_data(city),
            warnings=[],
            created_at=timestamp,
            updated_at=timestamp,
            completed_at=timestamp if status in {"succeeded", "degraded", "failed"} else None,
            total_duration_ms=120 if status != "running" else None,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


@pytest.mark.asyncio
async def test_admin_routes_require_database_admin(client, database):
    factory, admin, alice, _ = database
    task_id = add_task(factory, alice, "北京", "succeeded", age_minutes=1)
    assert (await client.get("/api/admin/tasks")).status_code == 401
    assert (await client.get("/api/admin/tasks", headers=headers(alice))).status_code == 403
    assert (
        await client.get(f"/api/admin/tasks/{task_id}", headers=headers(alice))
    ).status_code == 403
    assert (await client.get("/api/admin/tasks", headers=headers(admin))).status_code == 200


@pytest.mark.asyncio
async def test_admin_list_has_all_owners_filters_and_pagination(client, database):
    factory, admin, alice, bob = database
    oldest = add_task(factory, alice, "北京", "succeeded", age_minutes=3)
    failed = add_task(factory, bob, "上海", "failed", age_minutes=2)
    newest = add_task(factory, alice, "广州", "running", age_minutes=1)

    first_page = await client.get(
        "/api/admin/tasks?page=1&page_size=2", headers=headers(admin),
    )
    assert first_page.status_code == 200
    body = first_page.json()
    assert body["total"] == 3
    assert [item["task_id"] for item in body["items"]] == [newest, failed]
    assert [(item["username"], item["city"]) for item in body["items"]] == [
        ("alice_user", "广州"), ("bob_user", "上海"),
    ]
    assert "password_hash" not in first_page.text

    second_page = await client.get(
        "/api/admin/tasks?page=2&page_size=2", headers=headers(admin),
    )
    assert [item["task_id"] for item in second_page.json()["items"]] == [oldest]

    status_filter = await client.get(
        "/api/admin/tasks?status=failed", headers=headers(admin),
    )
    assert [item["task_id"] for item in status_filter.json()["items"]] == [failed]

    owner_filter = await client.get(
        "/api/admin/tasks?username=ALICE_", headers=headers(admin),
    )
    assert owner_filter.json()["total"] == 2
    assert {item["username"] for item in owner_filter.json()["items"]} == {"alice_user"}


@pytest.mark.asyncio
async def test_admin_detail_includes_owner_and_real_tool_durations(client, database):
    factory, admin, _, bob = database
    task_id = add_task(factory, bob, "上海", "succeeded", age_minutes=1)
    with factory() as session:
        task = session.get(TripTask, task_id)
        task.total_duration_ms = 900
        session.add_all([
            ToolCall(
                task_id=task_id,
                agent_name="attraction",
                tool_name="maps_text_search",
                status="succeeded",
                arguments={"city": "上海"},
                result_summary={"item_count": 2},
                completed_at=utc_now(),
                duration_ms=25.5,
            ),
            ToolCall(
                task_id=task_id,
                agent_name="weather",
                tool_name="maps_weather",
                status="failed",
                arguments={"city": "上海"},
                error_code="TOOL_TIMEOUT",
                error_message="工具调用超时。",
                completed_at=utc_now(),
                duration_ms=40,
            ),
        ])
        session.commit()

    response = await client.get(f"/api/admin/tasks/{task_id}", headers=headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert (body["user_id"], body["username"]) == (bob.id, "bob_user")
    assert body["status"] == "succeeded" and body["stage"] == "completed"
    assert body["total_duration_ms"] == 900
    assert [call["status"] for call in body["tool_calls"]] == ["succeeded", "failed"]
    assert body["tool_calls"][0]["duration_ms"] == 25.5
    assert body["tool_calls"][1]["duration_ms"] == 40
    assert (
        await client.get("/api/admin/tasks/not-found", headers=headers(admin))
    ).status_code == 404
