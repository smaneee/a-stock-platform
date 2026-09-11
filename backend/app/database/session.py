"""数据库会话管理。

使用 SQLAlchemy 2 的声明式 API，通过环境变量切换 SQLite / PostgreSQL。
业务代码不直接依赖具体数据库方言，仅通过 session 访问。
"""
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """每个新连接自动 PRAGMA foreign_keys=ON。

    SQLite 默认禁用 FK 约束（包括 ondelete / onupdate），必须每个连接开启。
    没有这一步，0009 迁移把 universe_members.security_id 改成 NO ACTION 形同虚设，
    删除 Security 时会静默级联清空历史 UniverseMember。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # History ingest uses a few short-lived writer sessions concurrently.
        # Wait briefly for the writer lock instead of failing immediately.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _make_engine():
    """根据配置创建数据库引擎。

    SQLite 需要禁用跨线程检查 + 默认开启 FK 约束；
    PostgreSQL 默认使用连接池。
    """
    url = settings.database_url
    if url.startswith("sqlite"):
        # 内存 SQLite 使用 StaticPool，保证多线程共享同一连接
        if ":memory:" in url:
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(
                url,
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
            )
        # 每个新连接自动开启 FK 约束
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        return engine
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
