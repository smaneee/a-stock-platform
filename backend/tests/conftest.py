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

from app.database.session import Base  # noqa: E402


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
