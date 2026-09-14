"""只读探查 #2：情绪行的 captured_at / 覆盖、指数是否落库、qfq 价格可用性。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "backend" / "a_stock.db"


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def main() -> int:
    conn = connect_ro(DB)
    try:
        print("== limit_up_sentiment captured_at by source ==")
        for r in conn.execute(
            """
            SELECT source, substr(captured_at,1,10) AS cap_day, COUNT(*) AS n
            FROM limit_up_sentiment GROUP BY source, cap_day ORDER BY source, cap_day
            """
        ):
            print("  ", dict(r))

        print("\n== derived: coverage_symbols stats ==")
        for r in conn.execute(
            """
            SELECT source, COUNT(*) n, MIN(coverage_symbols) cmin, MAX(coverage_symbols) cmax,
                   CAST(AVG(coverage_symbols) AS INT) cavg,
                   MIN(seal_rate) srmin, MAX(seal_rate) srmax,
                   MIN(max_streak) msmin, MAX(max_streak) msmax,
                   MIN(limit_up_count) lmin, MAX(limit_up_count) lmax
            FROM limit_up_sentiment GROUP BY source
            """
        ):
            print("  ", dict(r))

        print("\n== index-like symbols in historical_bars ==")
        for r in conn.execute(
            """
            SELECT symbol, period, adjust, COUNT(*) n, MIN(trade_date) dmin, MAX(trade_date) dmax
            FROM historical_bars
            WHERE symbol LIKE 'sh000%' OR symbol LIKE 'sz399%' OR symbol LIKE '000300%'
               OR symbol LIKE '399%' OR symbol LIKE 'sh000300' OR length(symbol) < 6
            GROUP BY symbol, period, adjust ORDER BY n DESC LIMIT 30
            """
        ):
            print("  ", dict(r))

        print("\n== qfq sanity on recent window ==")
        for r in conn.execute(
            """
            SELECT COUNT(*) n,
                   SUM(CASE WHEN open IS NULL OR open<=0 THEN 1 ELSE 0 END) bad_open,
                   SUM(CASE WHEN close IS NULL OR close<=0 THEN 1 ELSE 0 END) bad_close
            FROM historical_bars
            WHERE period='daily' AND adjust='qfq'
              AND trade_date >= '2025-09-01' AND trade_date <= '2026-09-11'
            """
        ):
            print("  qfq", dict(r))
        for r in conn.execute(
            """
            SELECT COUNT(*) n,
                   SUM(CASE WHEN open IS NULL OR open<=0 THEN 1 ELSE 0 END) bad_open,
                   SUM(CASE WHEN close IS NULL OR close<=0 THEN 1 ELSE 0 END) bad_close
            FROM historical_bars
            WHERE period='daily' AND adjust='none'
              AND trade_date >= '2025-09-01' AND trade_date <= '2026-09-11'
            """
        ):
            print("  none", dict(r))

        print("\n== bars per day in the sentiment window (qfq) ==")
        for r in conn.execute(
            """
            SELECT trade_date, COUNT(*) n FROM historical_bars
            WHERE period='daily' AND adjust='qfq'
              AND trade_date >= '2025-09-10' AND trade_date <= '2026-09-11'
            GROUP BY trade_date ORDER BY trade_date LIMIT 5
            """
        ):
            print("  ", dict(r))
        for r in conn.execute(
            """
            SELECT trade_date, COUNT(*) n FROM historical_bars
            WHERE period='daily' AND adjust='qfq'
              AND trade_date >= '2025-09-10' AND trade_date <= '2026-09-11'
            GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5
            """
        ):
            print("  ", dict(r))

        print("\n== trading_calendar tail ==")
        for r in conn.execute(
            "SELECT trade_date FROM trading_calendar WHERE trade_date >= '2026-08-20' AND trade_date <= '2026-10-15' ORDER BY trade_date"
        ):
            print("  ", r["trade_date"], end="")
        print()

        print("\n== securities board distribution ==")
        for r in conn.execute(
            "SELECT board, COUNT(*) n FROM securities GROUP BY board ORDER BY n DESC"
        ):
            print("  ", dict(r))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
