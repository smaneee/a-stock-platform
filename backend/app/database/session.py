"""数据库会话管理。

使用 SQLAlchemy 2 的声明式 API，通过环境变量切换 SQLite / PostgreSQL。
业务代码不直接依赖具体数据库方言，仅通过 session 访问。
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _make_engine():
    """根据配置创建数据库引擎。

    SQLite 需要禁用跨线程检查；PostgreSQL 默认使用连接池。
    """
    url = settings.database_url
    if url.startswith("sqlite"):
        # 内存 SQLite 使用 StaticPool，保证多线程共享同一连接
        if ":memory:" in url:
            return create_engine(
                url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供数据库会话，请求结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
