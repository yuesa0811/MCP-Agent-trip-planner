"""POI 兼容接口与图片查询。"""

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ...models.schemas import POISearchResponse
from ...services.amap_service import get_amap_service
from ...services.unsplash_service import get_unsplash_service
from ..deps import get_current_user

router = APIRouter(prefix="/poi", tags=["POI服务"])


class POIDetailResponse(BaseModel):
    success: bool
    message: str
    data: dict | None = None


@router.get(
    "/detail/{poi_id}", response_model=POIDetailResponse, summary="获取 POI 详情",
    dependencies=[Depends(get_current_user)],
)
async def get_poi_detail(poi_id: str = Path(..., min_length=1, pattern=r"\S")):
    detail = await get_amap_service().get_poi_detail(poi_id)
    return POIDetailResponse(success=True, message="POI详情获取成功", data=detail)


@router.get(
    "/search", response_model=POISearchResponse, summary="搜索 POI",
    dependencies=[Depends(get_current_user)],
)
async def search_poi(
    keywords: str = Query(..., min_length=1, pattern=r"\S"),
    city: str = Query("北京", min_length=1, pattern=r"\S"),
):
    pois = await get_amap_service().search_poi(keywords, city)
    return POISearchResponse(success=True, message="POI搜索成功" if pois else "未找到匹配的POI", data=pois)


def _find_photo(name: str) -> str | None:
    """两个同步请求都在线程池执行；仅正常空结果才尝试第二个关键词。"""
    service = get_unsplash_service()
    return service.get_photo_url(f"{name} China landmark") or service.get_photo_url(name)


@router.get(
    "/photo", summary="查询景点图片",
    dependencies=[Depends(get_current_user)],
)
async def get_attraction_photo(name: str = Query(..., min_length=1, pattern=r"\S")):
    photo_url = await run_in_threadpool(_find_photo, name.strip())
    return {
        "success": True, "message": "图片查询成功" if photo_url else "未找到匹配图片",
        "data": {"name": name.strip(), "photo_url": photo_url},
    }
