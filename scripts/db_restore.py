"""SQLite 数据库恢复。

用法:
    python scripts/db_restore.py <备份文件路径>

恢复前会先将当前数据库备份一份（安全起见），再覆盖。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "backend" / "a_stock.db"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/db_restore.py <备份文件路径>")
        return 1

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"错误：备份文件不存在 {src}")
        return 1

    # 恢复前先备份当前库
    if DB_FILE.exists():
        safety = DB_FILE.with_suffix(f".pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        shutil.copy2(DB_FILE, safety)
        print(f"已备份当前库到: {safety}")

    # 校验备份文件是合法的 SQLite 库
    try:
        conn = sqlite3.connect(str(src))
        conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
    except sqlite3.DatabaseError as exc:
        print(f"错误：备份文件损坏，无法恢复: {exc}")
        return 1

    shutil.copy2(src, DB_FILE)
    print(f"恢复完成: {DB_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
