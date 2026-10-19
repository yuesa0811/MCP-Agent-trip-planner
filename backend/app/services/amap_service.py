"""外围地图查询：每次请求独立 MCP 会话，将供应商结果转换为原 API 模型。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date

from pydantic import ValidationError

from ..config import Settings, get_settings
from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo
from .mcp_service import (
    MCPToolError, MCPToolUnavailableError, execute_mcp_tool, open_amap_tools, safe_tool_error,
)


class AmapServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _invalid() -> AmapServiceError:
    return AmapServiceError("INVALID_TOOL_RESPONSE", "地图工具返回的数据不完整或格式错误。")


def _service_error(exc: Exception) -> AmapServiceError:
    if isinstance(exc, AmapServiceError):
        return exc
    # MCP SDK 清理会话时可能把业务异常放入 ExceptionGroup。
    if isinstance(exc, ExceptionGroup):
        errors = [_service_error(child) for child in exc.exceptions]
        return next((error for error in errors if error.status_code == 504), errors[0])
    if isinstance(exc, (MCPToolUnavailableError, FileNotFoundError)):
        return AmapServiceError("TOOL_UNAVAILABLE", "地图工具不可用，请检查 MCP 服务配置。", 503)
    if isinstance(exc, (ValidationError, KeyError, TypeError, ValueError)) and not isinstance(exc, MCPToolError):
        return _invalid()
    error = safe_tool_error(exc)
    return AmapServiceError(error.code, error.message, 504 if error.code == "TOOL_TIMEOUT" else 502)


def _text(value: object, *, required: bool = False) -> str:
    # 高德部分空字段返回 [] 或 null；必需字段不允许以空字符串蒙混通过。
    if value is None or value == []:
        value = ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise _invalid()
    return value.strip()


def _location(value: object) -> Location:
    longitude, latitude = _text(value, required=True).split(",")
    return Location(longitude=float(longitude), latitude=float(latitude))


def _items(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise _invalid()
    return value


class AmapService:
    MAX_SEARCH_RESULTS = 5

    def __init__(self, settings: Settings | None = None):
        self.settings = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def _session(self):
        if not self.settings.amap_api_key.strip():
            raise AmapServiceError("AMAP_CONFIG_ERROR", "请配置本项目的 AMAP_API_KEY。", 503)
        try:
            async with open_amap_tools(self.settings) as tools:
                yield {tool.name: tool for tool in tools}
        except Exception as exc:
            raise _service_error(exc) from None

    async def _call(self, tools, name: str, **arguments) -> dict:
        if name not in tools:
            raise AmapServiceError("TOOL_UNAVAILABLE", "MCP 服务未提供所需的地图工具。", 503)
        return await execute_mcp_tool(
            tools[name], arguments, timeout_seconds=self.settings.mcp_timeout_seconds,
        )

    async def _detail(self, tools, poi_id: str) -> dict:
        data = await self._call(tools, "maps_search_detail", id=poi_id)
        if _text(data.get("id"), required=True) != poi_id:
            raise _invalid()
        _text(data.get("name"), required=True)
        _location(data.get("location"))
        return data

    async def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> list[POIInfo]:
        async with self._session() as tools:
            data = await self._call(
                tools, "maps_text_search", keywords=keywords.strip(), city=city.strip(),
                citylimit=str(citylimit).lower(),
            )
            pois = _items(data, "pois")
            results = []
            # 0.1.11 的搜索只返回 ID/名称/地址；有界补详情取得坐标，禁止伪造 (0, 0)。
            for index, poi in enumerate(pois[:self.MAX_SEARCH_RESULTS]):
                if index:
                    # 同请求内串行补详情，避免突发查询触发高德 QPS 配额；不自动重试。
                    await asyncio.sleep(self.settings.amap_poi_detail_interval_seconds)
                poi_id = _text(poi.get("id"), required=True)
                detail = await self._detail(tools, poi_id)
                results.append(POIInfo(
                    id=poi_id, name=_text(detail.get("name"), required=True),
                    type=_text(detail.get("type") or poi.get("typecode")),
                    address=_text(detail.get("address")),
                    location=_location(detail.get("location")), tel=_text(detail.get("tel")) or None,
                ))
            return results

    async def get_weather(self, city: str) -> list[WeatherInfo]:
        async with self._session() as tools:
            data = await self._call(tools, "maps_weather", city=city.strip())
            forecasts = _items(data, "forecasts")
            # 同时兼容 MCP 的扁平预报和原始高德 API 的城市/casts 包装。
            casts = forecasts if forecasts and "date" in forecasts[0] else [
                cast for forecast in forecasts for cast in _items(forecast, "casts")
            ]
            weather = []
            for cast in casts:
                forecast_date = _text(cast.get("date"), required=True)
                if date.fromisoformat(forecast_date).isoformat() != forecast_date:
                    raise _invalid()
                weather.append(WeatherInfo(
                    date=forecast_date,
                    day_weather=_text(cast.get("dayweather"), required=True),
                    night_weather=_text(cast.get("nightweather"), required=True),
                    day_temp=_text(cast.get("daytemp"), required=True),
                    night_temp=_text(cast.get("nighttemp"), required=True),
                    wind_direction=_text(cast.get("daywind")), wind_power=_text(cast.get("daypower")),
                ))
            return weather

    async def plan_route(
        self, origin_address: str, destination_address: str,
        origin_city: str | None = None, destination_city: str | None = None,
        route_type: str = "walking",
    ) -> RouteInfo | None:
        names = {
            "walking": "maps_direction_walking_by_address",
            "driving": "maps_direction_driving_by_address",
            "transit": "maps_direction_transit_integrated_by_address",
        }
        if route_type not in names:
            raise AmapServiceError("INVALID_ROUTE_TYPE", "路线类型需为 walking、driving 或 transit。", 422)
        if route_type == "transit" and not (origin_city and destination_city):
            raise AmapServiceError("TRANSIT_CITY_REQUIRED", "公交路线需要提供起点和终点城市。", 422)
        arguments = {"origin_address": origin_address, "destination_address": destination_address}
        if origin_city:
            arguments["origin_city"] = origin_city
        if destination_city:
            arguments["destination_city"] = destination_city
        async with self._session() as tools:
            data = await self._call(tools, names[route_type], **arguments)
            route = data.get("route")
            if not isinstance(route, dict):
                raise _invalid()
            paths = _items(route, "transits" if route_type == "transit" else "paths")
            if not paths:
                return None
            path = paths[0]
            if route_type == "transit":
                # 0.1.11 只保留 route.distance，未提供每个公交方案的完整行驶距离。
                description = "公交方案；距离为服务返回的起终点距离。"
                distance = route.get("distance")
            else:
                steps = _items(path, "steps")
                description = "；".join(
                    _text(step.get("instruction")) for step in steps if step.get("instruction")
                ) or "服务未提供分步导航说明。"
                distance = path.get("distance")
            return RouteInfo(
                distance=distance, duration=path.get("duration"),
                route_type=route_type, description=description,
            )

    async def geocode(self, address: str, city: str | None = None) -> Location | None:
        async with self._session() as tools:
            arguments = {"address": address.strip()}
            if city:
                arguments["city"] = city.strip()
            data = await self._call(tools, "maps_geo", **arguments)
            locations = _items(data, "return")
            return _location(locations[0].get("location")) if locations else None

    async def get_poi_detail(self, poi_id: str) -> dict:
        async with self._session() as tools:
            return await self._detail(tools, poi_id.strip())


def get_amap_service() -> AmapService:
    """轻量工厂不连接外部服务；连接生命周期由每个异步方法管理。"""
    return AmapService()
