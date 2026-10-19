"""配置管理模块"""

from pathlib import Path
from typing import List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 固定读取本项目 backend/.env，不受启动目录影响，也不读取相邻项目。
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """应用配置"""

    # 应用基本配置
    app_name: str = "MCP多智能体旅行规划系统"
    app_version: str = "1.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = Field(default="", repr=False)
    amap_mcp_command: str = "uvx"
    mcp_timeout_seconds: float = Field(default=30, gt=0)
    amap_poi_detail_interval_seconds: float = Field(default=0.4, ge=0, le=5)

    # Unsplash API配置
    unsplash_access_key: str = Field(default="", repr=False)
    unsplash_secret_key: str = Field(default="", repr=False)

    # 新模型入口优先使用 LLM_*，为空时逐项回退到 OPENAI_*。
    llm_api_key: str = Field(default="", repr=False)
    llm_base_url: str = ""
    llm_model_id: str = ""
    llm_timeout_seconds: float = Field(default=90, gt=0)
    planning_timeout_seconds: float = Field(default=600, gt=0)
    agent_max_tool_calls: int = Field(default=3, ge=1, le=3)
    plan_repair_attempts: int = Field(default=1, ge=0, le=1)
    openai_api_key: str = Field(default="", repr=False)
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4"

    # 本地数据库与认证。JWT 密钥没有可用默认值，需在本项目 .env 配置。
    database_url: str = Field(default="", repr=False)
    jwt_secret_key: str = Field(default="", repr=False)
    access_token_expire_minutes: int = Field(default=120, ge=1, le=1440)

    # 日志配置
    log_level: str = "INFO"

    # 显式构造参数 > 进程环境变量 > backend/.env > 字段默认值。
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')]

    def get_llm_config(self) -> dict[str, str]:
        """统一处理模型配置优先级；返回值包含密钥，不要直接打印。"""
        return {
            "api_key": self.llm_api_key or self.openai_api_key,
            "base_url": self.llm_base_url or self.openai_base_url,
            "model": self.llm_model_id or self.openai_model,
        }

    def get_database_url(self) -> str:
        """返回显式地址，或固定到 backend/data，避免随启动目录变化。"""
        if self.database_url.strip():
            return self.database_url.strip()
        database_path = (BACKEND_DIR / "data" / "trip_planner.db").as_posix()
        return f"sqlite:///{database_path}"

    def has_valid_jwt_secret(self) -> bool:
        key = self.jwt_secret_key.strip()
        return len(key) >= 32 and not key.startswith("replace-with-")


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    llm_api_key = settings.get_llm_config()["api_key"]
    if not llm_api_key:
        warnings.append("LLM_API_KEY或OPENAI_API_KEY未配置,LLM功能可能无法使用")
    if not settings.has_valid_jwt_secret():
        warnings.append("JWT_SECRET_KEY未配置为随机长字符串,认证接口将返回503")

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n⚠️  配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")

    # 检查LLM配置
    llm_config = settings.get_llm_config()

    print(f"LLM API Key: {'已配置' if llm_config['api_key'] else '未配置'}")
    print(f"LLM Base URL: {llm_config['base_url']}")
    print(f"LLM Model: {llm_config['model']}")
    print(f"JWT密钥: {'已配置' if settings.has_valid_jwt_secret() else '未配置或仍为占位值'}")
    print("数据库: 已配置（连接地址不输出）")
    print(f"日志级别: {settings.log_level}")
