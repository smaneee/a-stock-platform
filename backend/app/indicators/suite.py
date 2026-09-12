"""技术指标套件：把一段 OHLCV 序列一次性算成可直接消费的指标字典。

面向 ``GET /api/indicators/{symbol}`` 与回测因子提取：

- 输入是高/低/收/量四个等长序列（价格单位元，成交量单位股）。
- 输出是「指标名 -> 与输入等长的序列」，不足处为 NaN。
- 参数取国内行情软件默认值（MA 5/10/20/60、MACD 12/26/9、RSI 6/14/24、
  BOLL 20/2、KDJ 9/3/3、ATR 14、CCI 14、WR 14、量比 5 日），不选用非默认
  参数以免与用户看到的行情软件对不上。

本模块只做计算，不做任何网络或数据库访问。
"""
from __future__ import annotations

from app.indicators.atr import atr
from app.indicators.boll import boll
from app.indicators.cci import cci
from app.indicators.kdj import kdj
from app.indicators.macd import macd
from app.indicators.moving_average import ema, latest_valid, ma
from app.indicators.obv import obv
from app.indicators.rsi import rsi
from app.indicators.volume import amplitude_series, volume_ratio_series
from app.indicators.wr import wr

# 默认参数：与通达信 / 同花顺默认值一致
MA_PERIODS = (5, 10, 20, 60)
EMA_PERIODS = (12, 26)
MACD_PARAMS = (12, 26, 9)
RSI_PERIODS = (6, 14, 24)
BOLL_PARAMS = (20, 2.0)
KDJ_PARAMS = (9, 3, 3)
ATR_PERIOD = 14
CCI_PERIOD = 14
WR_PERIOD = 14
VOLUME_RATIO_PERIOD = 5

# 指标名 -> 中文标题，供前端渲染表头与图例
SERIES_TITLES: dict[str, str] = {
    "ma5": "MA5",
    "ma10": "MA10",
    "ma20": "MA20",
    "ma60": "MA60",
    "ema12": "EMA12",
    "ema26": "EMA26",
    "macd_dif": "MACD DIF",
    "macd_dea": "MACD DEA",
    "macd_hist": "MACD 柱",
    "rsi6": "RSI6",
    "rsi14": "RSI14",
    "rsi24": "RSI24",
    "boll_upper": "BOLL 上轨",
    "boll_middle": "BOLL 中轨",
    "boll_lower": "BOLL 下轨",
    "kdj_k": "KDJ K",
    "kdj_d": "KDJ D",
    "kdj_j": "KDJ J",
    "atr14": "ATR14",
    "obv": "OBV",
    "cci14": "CCI14",
    "wr14": "WR14",
    "amplitude": "振幅(%)",
    "volume_ratio": "量比(5日)",
}


def compute_indicators(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> dict[str, list[float]]:
    """计算全部技术指标，返回「指标名 -> 等长序列」。"""
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("最高价/最低价/收盘价/成交量序列长度必须一致")

    result: dict[str, list[float]] = {}
    for period in MA_PERIODS:
        result[f"ma{period}"] = ma(closes, period)
    for period in EMA_PERIODS:
        result[f"ema{period}"] = ema(closes, period)

    dif, dea, hist = macd(closes, *MACD_PARAMS)
    result["macd_dif"] = dif
    result["macd_dea"] = dea
    result["macd_hist"] = hist

    for period in RSI_PERIODS:
        result[f"rsi{period}"] = rsi(closes, period)

    upper, middle, lower = boll(closes, *BOLL_PARAMS)
    result["boll_upper"] = upper
    result["boll_middle"] = middle
    result["boll_lower"] = lower

    k, d, j = kdj(highs, lows, closes, *KDJ_PARAMS)
    result["kdj_k"] = k
    result["kdj_d"] = d
    result["kdj_j"] = j

    result["atr14"] = atr(highs, lows, closes, ATR_PERIOD)
    result["obv"] = obv(closes, volumes)
    result["cci14"] = cci(highs, lows, closes, CCI_PERIOD)
    result["wr14"] = wr(highs, lows, closes, WR_PERIOD)
    result["amplitude"] = amplitude_series(highs, lows, closes)
    result["volume_ratio"] = volume_ratio_series(volumes, VOLUME_RATIO_PERIOD)
    return result


def latest_values(series: dict[str, list[float]]) -> dict[str, float | None]:
    """取每条序列最后一个有效值，作为「当前指标读数」。"""
    return {key: latest_valid(values) for key, values in series.items()}


def to_json_series(series: dict[str, list[float]]) -> dict[str, list[float | None]]:
    """把 NaN 换成 None（NaN 不是合法 JSON 字面量），并保留 4 位小数。"""
    return {
        key: [None if value != value else round(value, 4) for value in values]
        for key, values in series.items()
    }
