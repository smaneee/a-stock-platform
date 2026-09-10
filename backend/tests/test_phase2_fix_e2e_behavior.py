"""行为测试：实际调用 e2e_smoke.py 的函数 + 模拟失败路径。

替代 6 个「读源码找字符串」的脆弱测试。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# 让脚本可被 import
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import e2e_smoke  # noqa: E402


# ──────────────── _resolve_frontend_command ────────────────


class TestResolveFrontendCommand:
    """_resolve_frontend_command 必须 (1) 优先 npm.cmd (2) 回退本地 vite.cmd (3) 否则报错。"""

    def test_finds_npm_on_path(self, monkeypatch):
        """PATH 上有 npm.cmd 时返回其绝对路径。"""
        fake_npm = "C:\\fake\\path\\npm.cmd"
        with mock.patch.object(e2e_smoke.shutil, "which", return_value=fake_npm):
            cmd = e2e_smoke._resolve_frontend_command(
                Path("E:/tmp/frontend")
            )
        assert cmd[0] == fake_npm, f"应该返回绝对路径 {fake_npm}，实际 {cmd[0]}"
        assert "127.0.0.1" in cmd
        assert "5173" in cmd

    def test_falls_back_to_local_vite(self, tmp_path, monkeypatch):
        """PATH 上找不到 npm，但 frontend/node_modules/.bin/vite.cmd 存在时回退到本地 vite。"""
        local_vite = tmp_path / "node_modules" / ".bin" / "vite.cmd"
        local_vite.parent.mkdir(parents=True)
        local_vite.write_text("@echo off", encoding="utf-8")

        def fake_which(name):
            return None  # 模拟 PATH 没 npm

        with mock.patch.object(e2e_smoke.shutil, "which", side_effect=fake_which):
            cmd = e2e_smoke._resolve_frontend_command(tmp_path)
        assert cmd[0] == str(local_vite)
        assert "--host" in cmd
        assert "--port" in cmd

    def test_raises_when_neither_available(self, tmp_path, monkeypatch):
        """PATH 没 npm + frontend 没有 node_modules/.bin/vite.cmd → RuntimeError 明确报错。"""
        with mock.patch.object(e2e_smoke.shutil, "which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                e2e_smoke._resolve_frontend_command(tmp_path)
        # 错误信息必须对用户友好（不要把 FileNotFoundError 抛出去）
        msg = str(exc_info.value)
        assert "managed E2E 无法启动前端" in msg
        assert "npm.cmd" in msg
        assert "npm install" in msg


# ──────────────── _delete_temp_db_unconditional ────────────────


class TestUnconditionalDelete:
    """_delete_temp_db_unconditional 必须 (1) 总是尝试删除 (2) 拒绝 a_stock.db (3) 兼容 alembic 部分创建。"""

    def test_deletes_when_db_exists(self, tmp_path, monkeypatch):
        """DB 文件存在时必须删除。"""
        db = tmp_path / "e2e_smoke_partial.db"
        db.write_text("garbage", encoding="utf-8")
        monkeypatch.setattr(e2e_smoke, "TEMP_DB_PATH", str(db))

        errors = []
        e2e_smoke._delete_temp_db_unconditional(errors)
        assert errors == [], f"删除应无错，实际 {errors}"
        assert not db.exists(), "DB 文件必须被删除"

    def test_noop_when_db_does_not_exist(self, tmp_path, monkeypatch):
        """DB 不存在时直接返回，不报错也不创建。"""
        db = tmp_path / "no_such.db"
        monkeypatch.setattr(e2e_smoke, "TEMP_DB_PATH", str(db))

        errors = []
        e2e_smoke._delete_temp_db_unconditional(errors)
        assert errors == []

    def test_refuses_to_delete_production_db(self, tmp_path, monkeypatch):
        """即使 TEMP_DB_PATH 被指到 a_stock.db，也必须拒绝删除（生产 DB 保护）。"""
        # 创建一个假 a_stock.db
        a_stock = tmp_path / "a_stock.db"
        a_stock.write_text("PROD", encoding="utf-8")
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_stock)
        # 但 TEMP_DB_PATH 仍然指向 a_stock.db（模拟 bug / 配置错误）
        monkeypatch.setattr(e2e_smoke, "TEMP_DB_PATH", str(a_stock))

        errors = []
        e2e_smoke._delete_temp_db_unconditional(errors)
        # 必须报错且不能删文件
        assert len(errors) == 1
        assert "生产 DB 路径" in errors[0]
        assert a_stock.exists(), "a_stock.db 必须被保护，不能删除"
        assert a_stock.read_text(encoding="utf-8") == "PROD"

    def test_simulation_partial_migration_alembic_creates_then_fails(
        self, tmp_path, monkeypatch
    ):
        """模拟 alembic 部分创建文件后失败：db_initialized=False，但 DB 已落盘。

        即使 db_initialized 未设置（main_managed 标志位失败），DB 也必须被清。
        """
        db = tmp_path / "e2e_partial.db"
        db.write_text("", encoding="utf-8")
        monkeypatch.setattr(e2e_smoke, "TEMP_DB_PATH", str(db))

        # 不设置 db_initialized（模拟 alembic 阶段 1 抛异常）
        errors = []
        e2e_smoke._delete_temp_db_unconditional(errors)
        assert errors == []
        assert not db.exists()


# ──────────────── _verify_a_stock_db_unchanged ────────────────


class TestAStockDbUnchanged:
    """_verify_a_stock_db_unchanged 必须区分 4 种情况。"""

    def test_unchanged_when_signature_matches(self, tmp_path, monkeypatch):
        """初始存在 + 结束存在 + 大小/mtime 完全一致 → 无错误。"""
        a_db = tmp_path / "a_stock.db"
        a_db.write_text("original", encoding="utf-8")
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_db)
        # 模拟模块加载时记录
        stat0 = a_db.stat()
        monkeypatch.setattr(
            e2e_smoke,
            "A_STOCK_DB_INITIAL_SIG",
            (stat0.st_size, stat0.st_mtime),
        )

        errors = []
        e2e_smoke._verify_a_stock_db_unchanged(errors)
        assert errors == []

    def test_modified_detected(self, tmp_path, monkeypatch):
        """初始存在 + 结束大小变化 → 报错。"""
        a_db = tmp_path / "a_stock.db"
        a_db.write_text("original", encoding="utf-8")
        stat0 = a_db.stat()
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_db)
        monkeypatch.setattr(
            e2e_smoke,
            "A_STOCK_DB_INITIAL_SIG",
            (stat0.st_size, stat0.st_mtime),
        )

        # 模拟运行期间修改
        a_db.write_text("much longer content than original", encoding="utf-8")

        errors = []
        e2e_smoke._verify_a_stock_db_unchanged(errors)
        assert len(errors) == 1
        assert "被修改" in errors[0]

    def test_disappeared_after_start_detected(self, tmp_path, monkeypatch):
        """初始存在 + 结束时消失 → 报错。"""
        a_db = tmp_path / "a_stock.db"
        a_db.write_text("original", encoding="utf-8")
        stat0 = a_db.stat()
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_db)
        monkeypatch.setattr(
            e2e_smoke,
            "A_STOCK_DB_INITIAL_SIG",
            (stat0.st_size, stat0.st_mtime),
        )
        a_db.unlink()

        errors = []
        e2e_smoke._verify_a_stock_db_unchanged(errors)
        assert len(errors) == 1
        assert "消失" in errors[0]

    def test_initial_absent_then_appears_detected(self, tmp_path, monkeypatch):
        """关键修复点：初始不存在 + 运行时被意外创建 → 必须报错。"""
        # 初始：a_stock.db 不存在
        a_db = tmp_path / "a_stock.db"
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_db)
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB_INITIAL_SIG", None)

        # 运行时被创建了
        a_db.write_text("E2E created this by accident", encoding="utf-8")

        errors = []
        e2e_smoke._verify_a_stock_db_unchanged(errors)
        assert len(errors) == 1, "初始无/运行后出现 必须报错"
        assert "意外创建" in errors[0]

    def test_initial_absent_remain_absent_passes(self, tmp_path, monkeypatch):
        """初始不存在 + 结束时仍不存在 → 无错误（正常情况）。"""
        a_db = tmp_path / "a_stock.db"
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB", a_db)
        monkeypatch.setattr(e2e_smoke, "A_STOCK_DB_INITIAL_SIG", None)

        errors = []
        e2e_smoke._verify_a_stock_db_unchanged(errors)
        assert errors == []


# ──────────────── wait_for_http 503 韧性 ────────────────


class TestWaitForHttpResilience:
    """wait_for_http 必须把 503 / 502 / 504 / 连接拒绝视为「继续等」，
    而不是直接判失败 — 防止外部数据源瞬时不可用导致 e2e flake。"""

    def test_returns_true_on_immediate_200(self, monkeypatch):
        """URL 一上来就 200 → 立即返回 True。"""
        import urllib.request
        from unittest.mock import MagicMock

        fake_resp = MagicMock(status=200)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *_: False
        with mock.patch.object(urllib.request, "urlopen", return_value=fake_resp):
            assert e2e_smoke.wait_for_http("http://fake", timeout=5) is True

    def test_returns_true_after_transient_503(self, monkeypatch):
        """前 2 次 503，第 3 次 200 → 仍然返回 True（503 不算失败）。"""
        import urllib.error
        import urllib.request
        from unittest.mock import MagicMock

        responses = [
            urllib.error.HTTPError("http://fake", 503, "Service Unavailable", {}, None),
            urllib.error.HTTPError("http://fake", 503, "Service Unavailable", {}, None),
            MagicMock(status=200),  # 第 3 次成功
        ]
        with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
            assert e2e_smoke.wait_for_http("http://fake", timeout=10) is True

    def test_502_504_also_retried(self, monkeypatch):
        """502 / 504 也视为瞬时错误并重试。"""
        import urllib.error
        import urllib.request
        from unittest.mock import MagicMock

        responses = [
            urllib.error.HTTPError("http://fake", 502, "Bad Gateway", {}, None),
            urllib.error.HTTPError("http://fake", 504, "Gateway Timeout", {}, None),
            MagicMock(status=200),
        ]
        with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
            assert e2e_smoke.wait_for_http("http://fake", timeout=10) is True

    def test_connection_refused_does_not_fail(self, monkeypatch):
        """ConnectionRefusedError 应视为「继续等」，不是直接判失败。"""
        import urllib.request
        from unittest.mock import MagicMock

        responses = [
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
            MagicMock(status=200),
        ]
        with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
            assert e2e_smoke.wait_for_http("http://fake", timeout=10) is True

    def test_4xx_returns_false_immediately(self, monkeypatch):
        """4xx（如 404）不是瞬时错误，应直接返回 False 不再重试。"""
        import urllib.error
        import urllib.request

        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError(
                "http://fake", 404, "Not Found", {}, None
            ),
        ) as mck:
            result = e2e_smoke.wait_for_http("http://fake", timeout=10)
            assert result is False
            # 调用次数应该 < 5（不应该把整个 10 秒用完才返回）
            assert mck.call_count <= 5
