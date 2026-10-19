"""SQLAlchemy 基础设施：固定 SQLite 路径、短 Session 与连接级约束。"""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import BACKEND_DIR, get_settings


class Base(DeclarativeBase):
    """所有 ORM 实体共享的声明基类。"""


def _prepare_sqlite_directory(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    path = Path(url.database)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_db_engine(database_url: str | None = None) -> Engine:
    """创建引擎；测试可传临时 URL，不共享正式数据库。"""
    url = database_url or get_settings().get_database_url()
    _prepare_sqlite_directory(url)
    connect_args = {"check_same_thread": False} if make_url(url).get_backend_name() == "sqlite" else {}
    engine = create_engine(url, connect_args=connect_args)
    if make_url(url).get_backend_name() == "sqlite":
        event.listen(engine, "connect", _configure_sqlite)
    return engine


engine = create_db_engine()
SessionLocal = sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)


def init_database(target_engine: Engine | None = None) -> None:
    """空库首次建表；已有库的后续结构变更应使用显式迁移。"""
    # 导入实体后 metadata 才包含三张业务表。
    from .models import entities  # noqa: F401

    Base.metadata.create_all(bind=target_engine or engine)


def close_database() -> None:
    """应用停止时释放连接池；测试和热重载后不遗留文件句柄。"""
    engine.dispose()
