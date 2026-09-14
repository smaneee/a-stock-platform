"""pytest 共享配置与 fixtures。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 确保 backend 目录在 sys.path 中，使 app 包可导入
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 测试环境使用内存 SQLite，避免污染真实数据
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MARKET_PROVIDERS", "mock")
os.environ.setdefault("AUTO_CREATE_TABLES", "true")
# 测试环境默认走 mock（CI 友好）；真实 AKShare 测试单独切到 akshare provider
os.environ.setdefault("UNIVERSE_PROVIDERS", "mock")
os.environ.setdefault("E2E_USE_MOCK", "true")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database.session import Base, engine as _app_engine  # noqa: E402
# 强制 import 所有模型，确保 Base.metadata 含全部表定义后再 create_all
from app.database import models as _db_models  # noqa: E402,F401
from app.universe import (  # noqa: E402,F401
    exclusion as _uni_excl,
    snapshot_service as _uni_snap,
    sync_service as _uni_sync,
    providers as _uni_prov,
)

# 测试运行开始前，在 app 自身引擎上把全部表建好（TestClient 走这个引擎）
if str(_app_engine.url).startswith("sqlite"):
    # 重建表（alembic 测试可能让 in-memory DB 状态污染）
    Base.metadata.drop_all(bind=_app_engine)
    Base.metadata.create_all(bind=_app_engine)


@pytest.fixture(autouse=True)
def _reset_app_engine_state():
    """每个测试前清空 app 引擎的表 + 重建（防止 alembic 等测试造成 in-memory
    状态污染）。TestClient 类测试依赖 app.database.session.engine 干净。
    """
    if str(_app_engine.url).startswith("sqlite"):
        Base.metadata.drop_all(bind=_app_engine)
        Base.metadata.create_all(bind=_app_engine)
    # 复权口径解析是**进程级** TTL 缓存（见 screener.resolve_bar_adjust_cached）：
    # 不清空会让「先播 none 行、后播 qfq 行」的两个用例互相串味。
    from app.realtime.screener import reset_adjust_cache

    reset_adjust_cache()
    yield
    reset_adjust_cache()


@pytest.fixture
def db_session():
    """提供独立的内存 SQLite 会话。"""
    from sqlalchemy import event

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # 每个新连接自动开启 FK 约束（不然 0009 迁移里 NO ACTION 失效，
    # 删 Security 时会静默清空历史 UniverseMember）。
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    event.listen(test_engine, "connect", _enable_fk)

    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = TestingSession()
    yield session
    session.close()
    test_engine.dispose()
