"""SQLite 数据库安全备份（在线备份，服务器运行中也可安全执行）。

用法:
    python scripts/db_backup.py [输出目录]

默认备份到 <项目根>/backups/a_stock_<时间戳>.db
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "backend" / "a_stock.db"


def main() -> int:
    if not DB_FILE.exists():
        print(f"错误：未找到数据库文件 {DB_FILE}")
        return 1

    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else (ROOT / "backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = out_dir / f"a_stock_{timestamp}.db"

    # SQLite 在线备份 API：即使服务器运行中也能得到一致快照
    source = sqlite3.connect(str(DB_FILE))
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    print(f"备份完成: {dest} ({dest.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
