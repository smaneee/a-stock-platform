"""技术指标计算测试。"""
import math

import pytest

from app.indicators.macd import macd
from app.indicators.moving_average import ema, ma
from app.indicators.rsi import rsi
from app.indicators.volume import (
    amplitude,
    change_percent,
    volume_ma,
    volume_ratio,
)


def test_ma_basic():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ma(values, 3)
    assert result[0] != result[0]  # NaN
    assert result[1] != result[1]
    assert result[2] == pytest.approx(2.0)
    assert result[4] == pytest.approx(4.0)


def test_ma_insufficient_data():
    values = [1.0, 2.0]
    result = ma(values, 5)
    assert all(v != v for v in result)  # 全部 NaN


def test_ema_basic():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(values, 3)
    # 前两个为 NaN，后续应单调递增
    assert result[0] != result[0]
    assert result[1] != result[1]
    assert result[4] > result[3] > result[2]


def test_macd_structure():
    values = [float(i) for i in range(1, 60)]
    dif, dea, hist = macd(values)
    assert len(dif) == len(values)
    assert len(dea) == len(values)
    assert len(hist) == len(values)
    # 后期应有有效值
    assert dif[-1] == dif[-1]
    assert dea[-1] == dea[-1]


def test_macd_invalid_periods():
    values = [1.0] * 40
    with pytest.raises(ValueError):
        macd(values, fast=26, slow=12)  # fast >= slow


def test_rsi_range():
    values = [float(i) for i in range(1, 50)]
    result = rsi(values, 14)
    valid = [v for v in result if v == v]
    assert valid
    for v in valid:
        assert 0 <= v <= 100


def test_rsi_flat_series():
    values = [10.0] * 30
    result = rsi(values, 14)
    # 无波动时 RSI 定义为 50（或 100），但不应为 NaN 或异常
    valid = [v for v in result if v == v]
    assert valid


def test_volume_indicators():
    volumes = [100.0, 200.0, 300.0, 400.0, 500.0]
    result = volume_ma(volumes, 3)
    assert result[4] == pytest.approx(400.0)

    assert change_percent(10.5, 10.0) == pytest.approx(5.0)
    assert change_percent(10.0, 0.0) == 0.0

    assert amplitude(10.2, 9.8, 10.0) == pytest.approx(4.0)
    assert volume_ratio(500.0, 100.0) == pytest.approx(5.0)
    assert volume_ratio(500.0, 0.0) == 0.0
