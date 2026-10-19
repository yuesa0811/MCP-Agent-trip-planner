"""FastAPI主应用"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from ..services.amap_service import AmapServiceError
from ..services.task_service import recover_interrupted_tasks
from ..services.unsplash_service import UnsplashServiceError
from ..database import close_database, init_database
from ..config import get_settings, validate_config, print_config
from .routes import admin, auth, tasks, trip, poi, map as map_routes

# 获取配置
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """初始化本地资源，并在退出时释放数据库连接池。"""
    try:
        print("\n" + "="*60)
        print(f"🚀 {settings.app_name} v{settings.app_version}")
        print("="*60)

        init_database()
        interrupted_count = await run_in_threadpool(recover_interrupted_tasks)
        if interrupted_count:
            print(f"⚠️ 已将 {interrupted_count} 个重启前未完成任务标记为失败")

        print_config()
        try:
            validate_config()
            print("\n✅ 配置验证通过")
        except ValueError as exc:
            print(f"\n❌ 配置验证失败:\n{exc}")
            print("\n请检查.env文件并确保所有必要的配置项都已设置")
            raise

        print("\n" + "="*60)
        print("📚 API文档: http://localhost:8000/docs")
        print("📖 ReDoc文档: http://localhost:8000/redoc")
        print("="*60 + "\n")
        yield
    finally:
        close_database()
        print("\n" + "="*60)
        print("👋 应用正在关闭...")
        print("="*60 + "\n")


# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于 LangChain 与高德 MCP 的多智能体旅行规划 API",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 配置CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.exception_handler(AmapServiceError)
@app.exception_handler(UnsplashServiceError)
async def external_service_error_handler(request, exc):
    """外围接口只返回已脱敏的业务异常；与原规划接口的错误外层保持一致。"""
    return JSONResponse(status_code=exc.status_code, content={
        "success": False, "message": exc.message, "detail": exc.message,
        "error_code": exc.code, "data": None,
    })


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=False
    )
