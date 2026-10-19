"""真实外围 API 联调：使用本地配置和外部配额，不纳入 pytest。"""

import asyncio
from time import perf_counter

import httpx

from app.api.main import app
from app.config import get_settings
from app.models.schemas import GeocodeResponse, POISearchResponse, RouteResponse, WeatherResponse


async def main():
    failures = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def check(method, path, **kwargs):
            started = perf_counter()
            response = await client.request(method, path, **kwargs)
            payload = response.json()
            print(f"{method} {path}: HTTP {response.status_code}, {perf_counter() - started:.1f}s", flush=True)
            if response.status_code != 200:
                print(payload.get("error_code", "REQUEST_ERROR"), payload.get("message", "请求失败"))
                failures.append(path)
                return None
            return payload

        search = await check("GET", "/api/map/poi", params={"keywords": "故宫博物院", "city": "北京"})
        if search:
            pois = POISearchResponse.model_validate(search).data
            assert pois, "真实搜索未获得可验证 POI"
            print(f"POI: {len(pois)} 条，首条 {pois[0].name}", flush=True)
            detail = await check("GET", f"/api/poi/detail/{pois[0].id}")
            if detail:
                assert detail["data"]["id"] == pois[0].id
        weather = await check("GET", "/api/map/weather", params={"city": "北京"})
        if weather:
            assert WeatherResponse.model_validate(weather).data, "没有实际预报"
        geo = await check("GET", "/api/map/geocode", params={"address": "北京站", "city": "北京"})
        if geo:
            assert GeocodeResponse.model_validate(geo).data is not None
        for route_type in ("walking", "driving", "transit"):
            route = await check("POST", "/api/map/route", json={
                "origin_address": "北京站", "destination_address": "天安门",
                "origin_city": "北京", "destination_city": "北京", "route_type": route_type,
            })
            if route:
                parsed = RouteResponse.model_validate(route)
                assert parsed.data and parsed.data.distance > 0 and parsed.data.duration > 0
                print(f"{route_type}: {parsed.data.distance} 米 / {parsed.data.duration} 秒", flush=True)
        if get_settings().unsplash_access_key.strip():
            photo = await check("GET", "/api/poi/photo", params={"name": "Forbidden City Beijing"})
            if photo:
                assert photo["data"]["photo_url"], "图片服务未返回匹配图片"
                print("图片查询取得 URL", flush=True)
        else:
            response = await client.get("/api/poi/photo", params={"name": "故宫"})
            assert response.status_code == 503 and response.json()["error_code"] == "PHOTO_CONFIG_ERROR"
            print("图片未配置：已验证明确返回 PHOTO_CONFIG_ERROR / 503；未执行真实图片查询。")
    if failures:
        raise SystemExit(1)
    print("外围 API 联调完成。")


if __name__ == "__main__":
    asyncio.run(main())
