"""股票代码校验测试。"""
from app.validation import sanitize_symbols, validate_symbol


def test_valid_symbols():
    assert validate_symbol("600000") is True
    assert validate_symbol("000001") is True
    assert validate_symbol("300750") is True
    assert validate_symbol("688111") is True
    assert validate_symbol("830799") is True


def test_invalid_symbols():
    assert validate_symbol("") is False
    assert validate_symbol("abc") is False
    assert validate_symbol("12345") is False  # 5 位
    assert validate_symbol("1234567") is False  # 7 位
    assert validate_symbol("abcdef") is False
    assert validate_symbol("500000") is False  # 非法前缀


def test_sanitize_symbols():
    result = sanitize_symbols(["600000", "abc", "000001", "123"])
    assert result == ["600000", "000001"]
