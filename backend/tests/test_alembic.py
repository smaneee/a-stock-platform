"""Alembic 迁移集成测试。

用临时 SQLite 文件验证迁移可升级、可降级、可重复升级。
不污染全局状态：monkeypatch 在测试结束后自动还原环境变量。
"""
from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """临时数据库 URL，monkeypatch 注入到 settings，迁移结束后还原。"""
    db_file = tmp_path / "migration.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    # env.py 通过 lru_cache 缓存了 settings，必须清缓存
    from app.config import get_settings

    get_settings.cache_clear()
    yield db_url
    get_settings.cache_clear()


def _make_config() -> Config:
    cfg = Config("alembic.ini")
    # 强制 prepend_sys_path 指向 backend 目录，使 migrations/env.py 能 import app.*
    cfg.set_main_option("prepend_sys_path", os.path.dirname(os.path.abspath("alembic.ini")))
    return cfg


def test_alembic_upgrade_creates_all_tables(isolated_db):
    """升级到 head 后所有 ORM 模型对应的表都应创建。"""
    cfg = _make_config()
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(isolated_db))
    expected_tables = {
        "watchlists",
        "watchlist_symbols",
        "strategies",
        "signals",
        "paper_accounts",
        "paper_positions",
        "paper_orders",
        "paper_trades",
        "asset_records",
        "backtests",
        "trading_calendar",
        "historical_bars",
    }
    actual_tables = set(insp.get_table_names())
    assert expected_tables.issubset(actual_tables), (
        f"缺少表: {expected_tables - actual_tables}"
    )


def test_alembic_0002_creates_signal_idempotency_constraint(isolated_db):
    """0002 迁移必须为 paper_trades 添加 (account_id, signal_id) UNIQUE 约束。"""
    cfg = _make_config()
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(isolated_db))
    constraints = insp.get_unique_constraints("paper_trades")
    names = {c.get("name") for c in constraints}
    assert "uq_paper_trade_account_signal" in names, (
        f"未找到幂等约束，实际约束: {names}"
    )

    # 约束列必须包含 account_id 和 signal_id
    target = next(c for c in constraints if c.get("name") == "uq_paper_trade_account_signal")
    columns = set(target.get("column_names") or [])
    assert {"account_id", "signal_id"}.issubset(columns)


def test_alembic_0003_creates_trading_calendar(isolated_db):
    """0003 迁移必须创建 trading_calendar 表（date 主键）。"""
    cfg = _make_config()
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(isolated_db))
    assert "trading_calendar" in insp.get_table_names()

    pk = insp.get_pk_constraint("trading_calendar")
    assert "trade_date" in (pk.get("constrained_columns") or [])


def test_alembic_roundtrip(isolated_db):
    """升级 → 降级 → 升级：迁移必须可逆且可重复。"""
    cfg = _make_config()

    command.upgrade(cfg, "head")
    insp = inspect(create_engine(isolated_db))
    assert "paper_trades" in insp.get_table_names()

    command.downgrade(cfg, "base")
    insp = inspect(create_engine(isolated_db))
    assert "paper_trades" not in insp.get_table_names()

    # 再次升级，所有表应恢复
    command.upgrade(cfg, "head")
    insp = inspect(create_engine(isolated_db))
    assert "paper_trades" in insp.get_table_names()
    constraints = {c.get("name") for c in insp.get_unique_constraints("paper_trades")}
    assert "uq_paper_trade_account_signal" in constraints
