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
    Base.metadata.create_all(bind=_app_engine)


@pytest.fixture
def db_session():
    """提供独立的内存 SQLite 会话。"""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    session = TestingSession()
    yield session
    session.close()
    test_engine.dispose()
