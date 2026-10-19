"""认证与三表基础测试：真实 Argon2、PyJWT、SQLAlchemy 和 FastAPI 依赖链。"""

import asyncio
import json
import sys
import threading
from datetime import timedelta

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.main import app
from app.config import Settings, get_settings
from app.database import create_db_engine, init_database
from app.models.entities import TripTask, User
from app.services import auth_service
from app.services.auth_service import create_access_token, create_user, verify_password


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "auth.db"
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    init_database(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_db
    yield engine, session_factory, path
    app.dependency_overrides.pop(deps.get_db, None)
    engine.dispose()


@pytest_asyncio.fixture
async def client(database):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client


async def register(client, username="Alice_1", password="correct-pass-123"):
    return await client.post("/api/auth/register", json={"username": username, "password": password})


async def login(client, username="alice_1", password="correct-pass-123"):
    return await client.post("/api/auth/login", json={"username": username, "password": password})


def test_database_has_three_tables_indexes_foreign_keys_and_wal(database):
    engine, session_factory, path = database
    assert path.is_file()
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {"users", "trip_tasks", "tool_calls"}
    assert {item["name"] for item in inspector.get_indexes("trip_tasks")} == {
        "ix_trip_tasks_status_created", "ix_trip_tasks_user_created",
    }
    assert {item["name"] for item in inspector.get_indexes("tool_calls")} == {
        "ix_tool_calls_task_id_id",
    }
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"

    with session_factory() as session:
        session.add(TripTask(user_id=999, request_data={"city": "北京"}))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.asyncio
async def test_register_normalizes_username_and_stores_only_argon2_hash(client, database):
    response = await register(client, "  Alice_1  ")
    assert response.status_code == 201
    assert response.json()["username"] == "alice_1"
    assert response.json()["is_admin"] is False
    assert "password" not in response.text.lower()

    _, session_factory, _ = database
    with session_factory() as session:
        user = session.scalar(select(User).where(User.username == "alice_1"))
        assert user is not None
        assert user.password_hash.startswith("$argon2")
        assert "correct-pass-123" not in user.password_hash
        assert verify_password("correct-pass-123", user.password_hash)


@pytest.mark.asyncio
async def test_duplicate_username_is_conflict_even_with_different_case(client, database):
    assert (await register(client, "Alice_1")).status_code == 201
    duplicate = await register(client, "  ALICE_1 ", "different-pass-123")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "用户名已存在。"
    _, session_factory, _ = database
    with session_factory() as session:
        assert len(session.scalars(select(User)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"username": "ab", "password": "password1"},
    {"username": "含中文", "password": "password1"},
    {"username": "bad-name", "password": "password1"},
    {"username": "valid_name", "password": "short"},
    {"username": "valid_name", "password": "x" * 129},
    {"username": "valid_name", "password": "password1", "is_admin": True},
])
async def test_invalid_registration_is_rejected_before_database(client, database, payload):
    assert (await client.post("/api/auth/register", json=payload)).status_code == 422
    _, session_factory, _ = database
    with session_factory() as session:
        assert session.scalar(select(User)) is None


@pytest.mark.asyncio
async def test_password_is_not_trimmed_and_wrong_password_is_unauthorized(client):
    password = " pass word "
    assert (await register(client, password=password)).status_code == 201
    correct = await login(client, password=password)
    wrong = await login(client, password=password.strip())
    assert correct.status_code == 200
    assert wrong.status_code == 401
    assert wrong.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_login_token_and_me_round_trip(client):
    await register(client)
    response = await login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 7200
    assert body["user"]["username"] == "alice_1"
    payload = jwt.decode(
        body["access_token"], get_settings().jwt_secret_key,
        algorithms=[auth_service.JWT_ALGORITHM],
    )
    assert payload["sub"] == str(body["user"]["id"])
    assert set(payload) == {"sub", "iat", "exp"}

    current = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert current.status_code == 200
    assert current.json() == body["user"]


@pytest.mark.asyncio
async def test_missing_malformed_expired_and_wrong_algorithm_tokens_are_401(client):
    await register(client)
    user_id = (await login(client)).json()["user"]["id"]
    expired = create_access_token(user_id, expires_delta=timedelta(seconds=-1))
    forged = jwt.encode(
        {"sub": str(user_id), "iat": 1, "exp": 4_000_000_000},
        "different-secret-with-at-least-32-characters", algorithm="HS256",
    )
    wrong_algorithm = jwt.encode(
        {"sub": str(user_id), "iat": 1, "exp": 4_000_000_000},
        get_settings().jwt_secret_key, algorithm="HS384",
    )
    tokens = [None, "not-a-jwt", expired, forged, wrong_algorithm]
    for token in tokens:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_token_for_deleted_or_unknown_user_is_401(client):
    token = create_access_token(999999)
    response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "不存在" in response.json()["detail"]


@pytest.mark.asyncio
async def test_missing_or_placeholder_jwt_key_makes_login_unavailable(client, monkeypatch):
    await register(client)
    for secret in ("", "replace-with-at-least-32-random-characters"):
        settings = Settings(_env_file=None, jwt_secret_key=secret)
        monkeypatch.setattr("app.api.routes.auth.get_settings", lambda: settings)
        monkeypatch.setattr(auth_service, "get_settings", lambda: settings)
        response = await login(client)
        assert response.status_code == 503
        assert "JWT_SECRET_KEY" in response.json()["detail"]
        registration = await register(client, username="another_user")
        assert registration.status_code == 503


def test_admin_dependency_distinguishes_normal_user_and_admin():
    normal = User(id=1, username="normal", password_hash="hash", is_admin=False)
    admin = User(id=2, username="admin", password_hash="hash", is_admin=True)
    with pytest.raises(HTTPException) as caught:
        deps.require_admin(normal)
    assert caught.value.status_code == 403
    assert deps.require_admin(admin) is admin


def test_admin_creation_uses_interactive_password_and_never_prints_it(
    database, monkeypatch, capsys,
):
    from scripts import create_admin

    _, session_factory, _ = database
    monkeypatch.setattr(create_admin, "SessionLocal", session_factory)
    monkeypatch.setattr(create_admin, "init_database", lambda: None)
    monkeypatch.setattr(create_admin, "getpass", lambda prompt: "admin-pass-123")
    monkeypatch.setattr(sys, "argv", ["create_admin.py", "  ROOT_ADMIN "])
    create_admin.main()
    output = capsys.readouterr().out
    assert "root_admin" in output
    assert "admin-pass-123" not in output
    with session_factory() as session:
        admin = session.scalar(select(User).where(User.username == "root_admin"))
        assert admin is not None and admin.is_admin
        assert verify_password("admin-pass-123", admin.password_hash)


@pytest.mark.asyncio
async def test_password_hashing_route_does_not_block_event_loop(client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def blocking_hash(password):
        started.set()
        assert release.wait(3)
        return "$argon2id$test-only-hash"

    monkeypatch.setattr(auth_service, "hash_password", blocking_hash)
    pending = asyncio.create_task(register(client, "thread_user", "thread-pass-123"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        health = await asyncio.wait_for(client.get("/health"), 1)
        assert health.status_code == 200
    finally:
        release.set()
        response = await pending
    assert response.status_code == 201
