"""地图 API：await 服务调用；外部服务错误由主应用统一转为安全响应。"""

from fastapi import APIRouter, Depends, Query

from ...config import get_settings
from ...models.schemas import (
    GeocodeResponse, POISearchResponse, RouteRequest, RouteResponse, WeatherResponse,
)
from ...services.amap_service import AmapServiceError, get_amap_service
from ..deps import get_current_user

router = APIRouter(prefix="/map", tags=["地图服务"])


@router.get(
    "/poi", response_model=POISearchResponse, summary="搜索 POI（最多 5 条）",
    dependencies=[Depends(get_current_user)],
)
async def search_poi(
    keywords: str = Query(..., min_length=1, pattern=r"\S"),
    city: str = Query(..., min_length=1, pattern=r"\S"),
    citylimit: bool = True,
):
    pois = await get_amap_service().search_poi(keywords, city, citylimit)
    return POISearchResponse(success=True, message="POI搜索成功" if pois else "未找到匹配的POI", data=pois)


@router.get(
    "/weather", response_model=WeatherResponse, summary="查询天气预报",
    dependencies=[Depends(get_current_user)],
)
async def get_weather(city: str = Query(..., min_length=1, pattern=r"\S")):
    weather = await get_amap_service().get_weather(city)
    return WeatherResponse(success=True, message="天气查询成功" if weather else "暂无天气预报", data=weather)


@router.post(
    "/route", response_model=RouteResponse, summary="规划路线",
    dependencies=[Depends(get_current_user)],
)
async def plan_route(request: RouteRequest):
    route = await get_amap_service().plan_route(**request.model_dump())
    return RouteResponse(success=True, message="路线规划成功" if route else "未找到可用路线", data=route)


@router.get(
    "/geocode", response_model=GeocodeResponse, summary="地址转坐标",
    dependencies=[Depends(get_current_user)],
)
async def geocode(
    address: str = Query(..., min_length=1, pattern=r"\S"),
    city: str | None = Query(None, min_length=1, pattern=r"\S"),
):
    location = await get_amap_service().geocode(address, city)
    return GeocodeResponse(success=True, message="地理编码成功" if location else "未找到匹配地址", data=location)


@router.get("/health", summary="地图服务配置检查")
async def health_check():
    if not get_settings().amap_api_key.strip():
        raise AmapServiceError("AMAP_CONFIG_ERROR", "高德密钥未配置。", 503)
    return {"status": "healthy", "service": "amap", "transport": "stdio", "external_services_checked": False}
