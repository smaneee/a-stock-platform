"""日志配置与脱敏过滤器测试。"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from app import logging_config as mod


@contextmanager
def _isolated_root():
    """临时清空 root logger 的 handler。

    pytest 的日志插件会在用例执行期间给 root 挂捕获 handler，导致
    `setup_logging` 命中"已初始化"分支；这里在用例体内清空并随后还原。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    for handler in list(root.handlers):
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001 - 清理阶段吞掉关闭异常
                pass
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_mask_keeps_key_prefix():
    assert mod._mask("api_key=abcdef") == "api_key=***"
    assert mod._mask("token: secret") == "token: ***"
    assert mod._mask("bearer") == "***"


def test_sensitive_filter_masks_assignment_and_colon():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "api_key=abcdef token: ghijkl", None, None
    )
    assert mod._SensitiveFilter().filter(record) is True
    assert "abcdef" not in record.msg
    assert "ghijkl" not in record.msg
    assert record.msg.count("***") == 2


def test_sensitive_filter_masks_bearer_token():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "call with Bearer abc123.def456 now", None, None
    )
    mod._SensitiveFilter().filter(record)
    assert "abc123.def456" not in record.msg


def test_sensitive_filter_ignores_non_string_message():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, {"payload": "x"}, None, None)
    assert mod._SensitiveFilter().filter(record) is True
    assert record.msg == {"payload": "x"}


def test_setup_logging_configures_console_and_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _isolated_root() as root:
        mod.setup_logging("DEBUG")
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 2
        assert (tmp_path / "app.log").exists()


def test_setup_logging_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _isolated_root() as root:
        mod.setup_logging("INFO")
        count = len(root.handlers)
        mod.setup_logging("INFO")
        assert len(root.handlers) == count


def test_setup_logging_falls_back_on_invalid_level(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _isolated_root() as root:
        mod.setup_logging("NOT_A_LEVEL")
        assert root.level == logging.INFO


def test_secret_is_masked_in_log_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with _isolated_root() as root:
        mod.setup_logging("INFO")
        logging.getLogger("t").warning("token=supersecret dummy")
        for handler in root.handlers:
            handler.flush()
        content = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "supersecret" not in content
    assert "***" in content
