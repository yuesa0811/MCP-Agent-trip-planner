"""真实路由/服务/工具执行器配合内存 MCP 响应，不访问外网。"""

import asyncio
import copy
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import requests
from langchain_core.tools import StructuredTool

from app.api import deps
from app.api.main import app
from app.config import Settings
from app.models.entities import User
from app.services import amap_service, unsplash_service
from app.services.mcp_service import MCPToolUnavailableError


DETAIL = {"id": "P1", "name": "故宫", "location": "116.4,39.9", "address": "北京", "type": "景点", "tel": []}
CAST = {
    "date": "2026-10-01", "dayweather": "晴", "nightweather": "多云",
    "daytemp": "23", "nighttemp": "12", "daywind": "北", "daypower": "3",
}
PATH = {"distance": "1200", "duration": "900", "steps": [{"instruction": "向北步行"}]}
ROUTE_NAMES = {
    "walking": "maps_direction_walking_by_address",
    "driving": "maps_direction_driving_by_address",
    "transit": "maps_direction_transit_integrated_by_address",
}


@pytest.fixture
def mcp(monkeypatch):
    state = SimpleNamespace(calls=[], opened=0, closed=0, open_error=None, responses={
        "maps_text_search": {"pois": [{"id": "P1", "name": "故宫", "typecode": "110000"}]},
        "maps_search_detail": copy.deepcopy(DETAIL),
        "maps_weather": {"city": "北京", "forecasts": [copy.deepcopy(CAST)]},
        "maps_geo": {"return": [{"location": "116.4,39.9"}]},
        ROUTE_NAMES["walking"]: {"route": {"paths": [copy.deepcopy(PATH)]}},
        ROUTE_NAMES["driving"]: {"route": {"paths": [copy.deepcopy(PATH)]}},
        ROUTE_NAMES["transit"]: {"route": {"distance": "4500", "transits": [{"duration": "1300"}]}},
    })

    def make_tool(name):
        async def invoke(**arguments):
            state.calls.append((name, arguments))
            value = state.responses[name]
            if isinstance(value, Exception):
                raise value
            if callable(value):
                value = await value(arguments)
            return [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
        return StructuredTool(name=name, description=name, coroutine=invoke, args_schema={
            "type": "object", "additionalProperties": True,
            "properties": {key: {"type": "string"} for key in (
                "keywords", "city", "citylimit", "id", "address", "origin_address",
                "destination_address", "origin_city", "destination_city",
            )},
        })

    @asynccontextmanager
    async def open_tools(settings):
        state.opened += 1
        try:
            if state.open_error:
                raise state.open_error
            yield [make_tool(name) for name in state.responses]
        finally:
            state.closed += 1
    monkeypatch.setattr(amap_service, "open_amap_tools", open_tools)
    return state


@pytest_asyncio.fixture
async def client(mcp):
    app.dependency_overrides[deps.get_current_user] = lambda: User(
        id=1, username="map_user", password_hash="unused",
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_current_user, None)


@pytest.mark.asyncio
async def test_map_and_poi_business_endpoints_require_login(client, mcp):
    override = app.dependency_overrides.pop(deps.get_current_user)
    try:
        for method, url in (
            ("GET", "/api/map/weather?city=北京"),
            ("GET", "/api/poi/search?keywords=故宫&city=北京"),
            ("GET", "/api/poi/photo?name=故宫"),
        ):
            response = await client.request(method, url)
            assert response.status_code == 401
        assert (await client.get("/api/map/health")).status_code == 200
        assert mcp.calls == []
    finally:
        app.dependency_overrides[deps.get_current_user] = override


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/api/map/poi", "/api/poi/search"])
async def test_search_gets_real_coordinates_in_one_session(client, mcp, url):
    response = await client.get(url, params={"keywords": "故宫", "city": "北京"})
    assert response.status_code == 200
    poi = response.json()["data"][0]
    assert poi["location"] == {"longitude": 116.4, "latitude": 39.9}
    assert poi["tel"] is None
    assert [name for name, _ in mcp.calls] == ["maps_text_search", "maps_search_detail"]
    assert mcp.calls[0][1]["citylimit"] == "true"
    assert mcp.opened == mcp.closed == 1


@pytest.mark.asyncio
async def test_search_is_bounded_and_honors_citylimit(client, mcp):
    mcp.responses["maps_text_search"]["pois"] = [{"id": f"P{i}", "name": "景点"} for i in range(20)]
    async def detail(args):
        return {**DETAIL, "id": args["id"]}
    mcp.responses["maps_search_detail"] = detail
    response = await client.get("/api/map/poi", params={"keywords": "景点", "city": "北京", "citylimit": False})
    assert len(response.json()["data"]) == 5
    assert len(mcp.calls) == 6
    assert mcp.calls[0][1]["citylimit"] == "false"


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_weather_supports_real_flat_and_nested_forecasts(client, mcp, nested):
    if nested:
        mcp.responses["maps_weather"] = {"forecasts": [{"casts": [CAST]}]}
    response = await client.get("/api/map/weather", params={"city": "北京"})
    assert response.status_code == 200
    assert response.json()["data"][0]["day_temp"] == 23
    assert response.json()["data"][0]["night_weather"] == "多云"


@pytest.mark.asyncio
@pytest.mark.parametrize("route_type", ["walking", "driving", "transit"])
async def test_route_protocol_and_units(client, mcp, route_type):
    response = await client.post("/api/map/route", json={
        "origin_address": "北京站", "destination_address": "天安门",
        "origin_city": "北京", "destination_city": "北京", "route_type": route_type,
    })
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["route_type"] == route_type
    assert data["distance"] == (4500 if route_type == "transit" else 1200)
    assert data["duration"] == (1300 if route_type == "transit" else 900)
    assert mcp.calls[0][0] == ROUTE_NAMES[route_type]


@pytest.mark.asyncio
async def test_detail_and_geocode(client, mcp):
    detail = await client.get("/api/poi/detail/P1")
    geo = await client.get("/api/map/geocode", params={"address": "故宫", "city": "北京"})
    assert detail.json()["data"]["id"] == "P1"
    assert geo.json()["data"] == {"longitude": 116.4, "latitude": 39.9}
    assert mcp.closed == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url,tool,value,expected", [
    ("/api/map/poi?keywords=无&city=北京", "maps_text_search", {"pois": []}, []),
    ("/api/map/weather?city=北京", "maps_weather", {"forecasts": []}, []),
    ("/api/map/geocode?address=无", "maps_geo", {"return": []}, None),
])
async def test_normal_empty_results_are_success(client, mcp, url, tool, value, expected):
    mcp.responses[tool] = value
    response = await client.get(url)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"] == expected
    assert len(mcp.calls) == 1


@pytest.mark.asyncio
async def test_no_route_is_normal_empty(client, mcp):
    mcp.responses[ROUTE_NAMES["walking"]] = {"route": {"paths": []}}
    response = await client.post("/api/map/route", json={"origin_address": "甲", "destination_address": "乙"})
    assert response.status_code == 200 and response.json()["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value,status,code", [
    ({"error": "key=SECRET"}, 502, "AMAP_BUSINESS_ERROR"),
    ({"error": "CUQPS_HAS_EXCEEDED_THE_LIMIT SECRET"}, 502, "AMAP_RATE_LIMITED"),
    ({"status": "0", "info": "SECRET"}, 502, "AMAP_BUSINESS_ERROR"),
    ({"unexpected": []}, 502, "INVALID_TOOL_RESPONSE"),
    ({"pois": "invalid"}, 502, "INVALID_TOOL_RESPONSE"),
    (TimeoutError("SECRET"), 504, "TOOL_TIMEOUT"),
    (ConnectionError("SECRET"), 502, "TOOL_CONNECTION_ERROR"),
])
async def test_tool_failure_never_becomes_empty_or_leaks_error(client, mcp, value, status, code):
    mcp.responses["maps_text_search"] = value
    response = await client.get("/api/map/poi?keywords=故宫&city=北京")
    assert response.status_code == status
    assert response.json()["error_code"] == code
    assert response.json()["success"] is False
    assert "SECRET" not in response.text
    assert mcp.opened == mcp.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,value,url", [
    ("maps_search_detail", {**DETAIL, "location": "181,39"}, "/api/poi/detail/P1"),
    ("maps_search_detail", {**DETAIL, "id": "P2"}, "/api/poi/detail/P1"),
    ("maps_weather", {"forecasts": [{**CAST, "daytemp": "unknown"}]}, "/api/map/weather?city=北京"),
    ("maps_weather", {"forecasts": [{**CAST, "date": "invalid"}]}, "/api/map/weather?city=北京"),
    ("maps_weather", {"lives": [{"weather": "晴"}]}, "/api/map/weather?city=北京"),
    ("maps_geo", {"return": [{}]}, "/api/map/geocode?address=故宫"),
])
async def test_invalid_required_data_fails(client, mcp, tool, value, url):
    mcp.responses[tool] = value
    response = await client.get(url)
    assert response.status_code == 502
    assert response.json()["error_code"] == "INVALID_TOOL_RESPONSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status", [
    (MCPToolUnavailableError("SECRET"), 503),
    (FileNotFoundError("SECRET"), 503),
    (ExceptionGroup("SECRET", [TimeoutError("SECRET")]), 504),
])
async def test_session_failure_is_safe(client, mcp, error, status):
    mcp.open_error = error
    response = await client.get("/api/map/weather?city=北京")
    assert response.status_code == status and "SECRET" not in response.text


@pytest.mark.asyncio
async def test_missing_tool_is_explicit(client, mcp):
    del mcp.responses["maps_weather"]
    response = await client.get("/api/map/weather?city=北京")
    assert response.status_code == 503
    assert response.json()["error_code"] == "TOOL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_local_health_and_missing_config(client, mcp, monkeypatch):
    response = await client.get("/api/map/health")
    assert response.json()["external_services_checked"] is False
    assert mcp.opened == 0
    monkeypatch.setattr(amap_service, "get_settings", lambda: Settings(_env_file=None, amap_api_key=""))
    response = await client.get("/api/map/weather?city=北京")
    assert response.status_code == 503
    assert mcp.opened == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"origin_address": "甲", "destination_address": "乙", "route_type": "flying"},
    {"origin_address": "甲", "destination_address": "乙", "route_type": "transit"},
    {"origin_address": " ", "destination_address": "乙"},
])
async def test_invalid_route_rejected_before_tools(client, mcp, body):
    assert (await client.post("/api/map/route", json=body)).status_code == 422
    assert mcp.opened == 0


@pytest.mark.asyncio
async def test_async_map_does_not_block_and_cancellation_closes_session(client, mcp):
    started, release = asyncio.Event(), asyncio.Event()
    async def wait_for_release(args):
        started.set()
        await release.wait()
        return {"forecasts": []}
    mcp.responses["maps_weather"] = wait_for_release
    pending = asyncio.create_task(client.get("/api/map/weather?city=北京"))
    await asyncio.wait_for(started.wait(), 1)
    assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert mcp.closed == 1


@pytest.fixture
def photos(monkeypatch):
    state = SimpleNamespace(data={"results": []}, error=None, calls=[])
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            if state.error:
                raise state.error
        def json(self):
            return state.data
    def get(url, **kwargs):
        state.calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr(unsplash_service.requests, "get", get)
    monkeypatch.setattr(unsplash_service, "get_settings", lambda: Settings(
        _env_file=None, unsplash_access_key="TEST-PHOTO-KEY",
    ))
    return state


@pytest.mark.asyncio
async def test_photo_success_and_normal_empty_fallback(client, photos):
    empty = await client.get("/api/poi/photo?name=故宫")
    assert empty.status_code == 200 and empty.json()["data"]["photo_url"] is None
    assert [call[1]["params"]["query"] for call in photos.calls] == ["故宫 China landmark", "故宫"]
    photos.data = {"results": [{"id": "photo1", "urls": {"regular": "https://images.unsplash.com/test"}}]}
    result = await client.get("/api/poi/photo?name=故宫")
    assert result.json()["data"]["photo_url"] == "https://images.unsplash.com/test"
    assert "client_id" not in photos.calls[-1][1]["params"]
    assert photos.calls[-1][1]["timeout"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("error,status,code", [
    (requests.Timeout("SECRET"), 504, "PHOTO_TIMEOUT"),
    (requests.HTTPError("SECRET"), 502, "PHOTO_REQUEST_FAILED"),
])
async def test_photo_failures_do_not_retry_or_leak(client, photos, error, status, code):
    photos.error = error
    response = await client.get("/api/poi/photo?name=故宫")
    assert response.status_code == status and response.json()["error_code"] == code
    assert len(photos.calls) == 1
    assert "SECRET" not in response.text and "TEST-PHOTO-KEY" not in response.text


@pytest.mark.asyncio
async def test_photo_missing_key_and_invalid_format(client, photos, monkeypatch):
    photos.data = {"unexpected": []}
    response = await client.get("/api/poi/photo?name=故宫")
    assert response.status_code == 502 and response.json()["error_code"] == "INVALID_PHOTO_RESPONSE"
    monkeypatch.setattr(unsplash_service, "get_settings", lambda: Settings(_env_file=None, unsplash_access_key=""))
    response = await client.get("/api/poi/photo?name=故宫")
    assert response.status_code == 503
    assert len(photos.calls) == 1


@pytest.mark.asyncio
async def test_sync_photo_runs_off_event_loop(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def blocking(query):
        started.set()
        assert release.wait(3)
        return "https://images.unsplash.com/test"
    monkeypatch.setattr(unsplash_service.UnsplashService, "get_photo_url", lambda self, query: blocking(query))
    pending = asyncio.create_task(client.get("/api/poi/photo?name=故宫"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert (await asyncio.wait_for(client.get("/health"), 1)).status_code == 200
    finally:
        release.set()
        response = await pending
    assert response.status_code == 200
