"""只读探查主库结构/数据覆盖。不写入任何内容。

用法：
    <venv-311>\Scripts\python.exe work\ps_search\_probe_db.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "backend" / "a_stock.db"


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # 只读连接下 PRAGMA 只做读取
    conn.execute("PRAGMA query_only = ON")
    return conn


def q1(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return cur.fetchone()


def qa(conn, sql, params=(), limit=50):
    cur = conn.execute(sql, params)
    return cur.fetchall()[:limit]


def main() -> int:
    print(f"db={DB} exists={DB.exists()} size={DB.stat().st_size:,}")
    conn = connect_ro(DB)
    try:
        print("\n== tables ==")
        rows = qa(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            limit=200,
        )
        print(", ".join(r["name"] for r in rows))

        print("\n== limit_up_sentiment ==")
        r = q1(
            conn,
            """
            SELECT COUNT(*) AS n, MIN(trade_date) AS dmin, MAX(trade_date) AS dmax
            FROM limit_up_sentiment
            """,
        )
        print(dict(r))
        for row in qa(
            conn,
            """
            SELECT source, COUNT(*) AS n, MIN(trade_date) AS dmin, MAX(trade_date) AS dmax,
                   SUM(CASE WHEN coverage_symbols IS NULL THEN 1 ELSE 0 END) AS null_cov
            FROM limit_up_sentiment GROUP BY source ORDER BY n DESC
            """,
        ):
            print("  ", dict(row))
        for row in qa(
            conn,
            """
            SELECT COUNT(*) AS n, MIN(trade_date) AS dmin, MAX(trade_date) AS dmax
            FROM limit_up_pool_members
            """,
        ):
            print("  pool_members", dict(row))
        for row in qa(
            conn,
            """
            SELECT pool, source_of_dummy FROM (SELECT pool, COUNT(*) AS source_of_dummy
            FROM limit_up_pool_members GROUP BY pool) ORDER BY 2 DESC
            """,
            limit=30,
        ):
            print("  pool", dict(row))

        print("\n== historical_bars ==")
        for row in qa(
            conn,
            """
            SELECT period, adjust, COUNT(*) AS n, COUNT(DISTINCT symbol) AS syms,
                   MIN(trade_date) AS dmin, MAX(trade_date) AS dmax
            FROM historical_bars GROUP BY period, adjust ORDER BY n DESC
            """,
        ):
            print("  ", dict(row))
        for row in qa(
            conn,
            """
            SELECT source, COUNT(*) AS n FROM historical_bars GROUP BY source ORDER BY n DESC
            """,
            limit=20,
        ):
            print("   src", dict(row))

        print("\n== trading_calendar ==")
        print(
            dict(
                q1(
                    conn,
                    "SELECT COUNT(*) AS n, MIN(trade_date) AS dmin, MAX(trade_date) AS dmax FROM trading_calendar",
                )
            )
        )

        print("\n== universe_snapshots ==")
        print(
            dict(
                q1(
                    conn,
                    "SELECT COUNT(*) AS n, MIN(trading_day) AS dmin, MAX(trading_day) AS dmax FROM universe_snapshots",
                )
            )
        )
        print(
            dict(
                q1(
                    conn,
                    "SELECT COUNT(*) AS n, COUNT(DISTINCT symbol) AS syms FROM universe_members",
                )
            )
        )

        print("\n== securities ==")
        print(dict(q1(conn, "SELECT COUNT(*) AS n FROM securities")))

        print("\n== duplicate trade_date in limit_up_sentiment (should be 0) ==")
        print(
            dict(
                q1(
                    conn,
                    """
                    SELECT COUNT(*) AS dup_dates FROM (
                      SELECT trade_date FROM limit_up_sentiment
                      GROUP BY trade_date HAVING COUNT(*) > 1)
                    """,
                )
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
