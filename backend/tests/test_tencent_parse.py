"""腾讯行情解析测试。"""
from app.market_data.tencent_provider import _parse_quote, to_tencent_symbol


def test_to_tencent_symbol():
    assert to_tencent_symbol("600000") == "sh600000"
    assert to_tencent_symbol("000001") == "sz000001"
    assert to_tencent_symbol("300750") == "sz300750"
    assert to_tencent_symbol("830799") == "bj830799"
    assert to_tencent_symbol("sh600000") == "sh600000"


def _build_payload(fields: dict[int, str], total: int = 40) -> str:
    """构造腾讯行情返回串。"""
    arr = [""] * total
    for idx, val in fields.items():
        arr[idx] = val
    return f'v_sh600000="{"~".join(arr)}";'


def test_parse_quote_valid():
    fields = {
        1: "浦发银行",
        2: "600000",
        3: "10.50",
        4: "10.40",
        5: "10.45",
        6: "12345",
        9: "10.49",
        19: "10.51",
        30: "20240101103000",
        33: "10.60",
        34: "10.30",
        37: "1234.5",
    }
    text = _build_payload(fields)
    quote = _parse_quote(text)
    assert quote is not None
    assert quote.symbol == "600000"
    assert quote.name == "浦发银行"
    assert quote.price == 10.50
    assert quote.previous_close == 10.40
    assert quote.volume == 12345 * 100  # 手转股
    assert quote.high == 10.60
    assert quote.low == 10.30
    assert quote.is_stale is False


def test_parse_quote_malformed():
    assert _parse_quote("") is None
    assert _parse_quote("no_equals_sign") is None
    assert _parse_quote("v_sh600000=\"1~2~3\";") is None  # 字段不足


def test_parse_quote_invalid_number():
    fields = {
        1: "测试",
        2: "600000",
        3: "not_a_number",  # 非法价格
        4: "10.40",
        5: "10.45",
        6: "12345",
        9: "10.49",
        19: "10.51",
        30: "20240101103000",
        33: "10.60",
        34: "10.30",
        37: "1234.5",
    }
    text = _build_payload(fields)
    assert _parse_quote(text) is None
