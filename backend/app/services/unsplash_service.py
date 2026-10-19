"""保留同步 requests 图片服务；API 层通过线程池调用。"""

import requests

from ..config import Settings, get_settings


class UnsplashServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class UnsplashService:
    def __init__(self, settings: Settings | None = None):
        self.access_key = (settings if settings is not None else get_settings()).unsplash_access_key
        self.base_url = "https://api.unsplash.com"

    def search_photos(self, query: str, per_page: int = 5) -> list[dict]:
        if not self.access_key.strip():
            raise UnsplashServiceError("PHOTO_CONFIG_ERROR", "图片服务未配置 UNSPLASH_ACCESS_KEY。", 503)
        try:
            with requests.get(
                f"{self.base_url}/search/photos",
                params={"query": query, "per_page": per_page},
                headers={"Authorization": f"Client-ID {self.access_key}"},
                timeout=10,
            ) as response:
                response.raise_for_status()
                data = response.json()
            results = data["results"]
            if not isinstance(results, list):
                raise ValueError("invalid results")
            photos = []
            for photo in results:
                url = photo["urls"]["regular"]
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ValueError("invalid image URL")
                photos.append({
                    "id": photo["id"], "url": url, "thumb": photo["urls"].get("thumb"),
                    "description": photo.get("description") or photo.get("alt_description"),
                    "photographer": photo.get("user", {}).get("name"),
                })
            return photos
        except requests.Timeout:
            raise UnsplashServiceError("PHOTO_TIMEOUT", "图片服务请求超时。", 504) from None
        except requests.RequestException:
            raise UnsplashServiceError("PHOTO_REQUEST_FAILED", "图片服务请求失败，请检查服务配置和额度。") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise UnsplashServiceError("INVALID_PHOTO_RESPONSE", "图片服务返回格式无法识别。") from None

    def get_photo_url(self, query: str) -> str | None:
        photos = self.search_photos(query, per_page=1)
        return photos[0]["url"] if photos else None


def get_unsplash_service() -> UnsplashService:
    return UnsplashService()
