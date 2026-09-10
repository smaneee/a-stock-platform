"""样本内/外切分与滚动前推（walk-forward）。

按交易日切分，避免不同标的 K 线数量不一致导致的错位。
"""
from __future__ import annotations

from datetime import date

from app.market_data.base import QuoteData


def _bar_date(bar: QuoteData) -> date:
    return (bar.market_time or bar.received_at).date()


def _common_dates(histories: dict[str, list[QuoteData]]) -> list[date]:
    dates: set[date] = set()
    for hist in histories.values():
        for b in hist:
            dates.add(_bar_date(b))
    return sorted(dates)


def _split_at(
    histories: dict[str, list[QuoteData]], split_date: date
) -> tuple[dict[str, list[QuoteData]], dict[str, list[QuoteData]]]:
    """按切分日把每个标的的 K 线拆成样本内（<= 切分日）与样本外（> 切分日）。"""
    in_sample: dict[str, list[QuoteData]] = {}
    out_sample: dict[str, list[QuoteData]] = {}
    for symbol, hist in histories.items():
        left, right = [], []
        for b in hist:
            (left if _bar_date(b) <= split_date else right).append(b)
        in_sample[symbol] = left
        out_sample[symbol] = right
    return in_sample, out_sample


def split_in_out_sample(
    histories: dict[str, list[QuoteData]], ratio: float = 0.7
) -> tuple[dict[str, list[QuoteData]], dict[str, list[QuoteData]]]:
    """按时间比例切分样本内/外。ratio 为样本内占比（0~1）。"""
    ratio = min(max(ratio, 0.0), 1.0)
    dates = _common_dates(histories)
    if not dates:
        return {}, {}
    split_date = dates[int(len(dates) * ratio) - 1] if len(dates) > 1 else dates[0]
    return _split_at(histories, split_date)


def walk_forward_splits(
    histories: dict[str, list[QuoteData]], n_folds: int = 3
) -> list[tuple[dict[str, list[QuoteData]], dict[str, list[QuoteData]]]]:
    """生成滚动前推的（训练、验证）切分。

    第 i 折用前 (i+1)/n_folds 作为样本内，紧随其后的 1/n_folds 作为样本外。
    """
    dates = _common_dates(histories)
    if not dates or n_folds < 1:
        return []
    n_folds = min(n_folds, len(dates))
    splits: list[tuple[dict, dict]] = []
    step = len(dates) / n_folds
    for i in range(1, n_folds):
        # 样本内终点与样本外区间
        train_end = dates[int(step * i) - 1]
        out_end = dates[min(int(step * (i + 1)) - 1, len(dates) - 1)]
        train, _ = _split_at(histories, train_end)
        # 样本外 = train_end 之后到 out_end
        out: dict[str, list[QuoteData]] = {}
        for symbol, hist in histories.items():
            out[symbol] = [
                b for b in hist if train_end < _bar_date(b) <= out_end
            ]
        splits.append((train, out))
    return splits
