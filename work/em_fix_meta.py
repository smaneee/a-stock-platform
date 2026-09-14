# -*- coding: utf-8 -*-
"""把只读佐证与服务收尾状态补入聚合 JSON。"""
from __future__ import annotations

import json
import sys

p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    d = json.load(f)

d["meta"]["只读佐证"] = {
    "a_stock.db_LastWriteTime": "2026-09-13 20:31:28",
    "服务启动时间": "2026-09-13 20:38:32",
    "a_stock.db_LastAccessTime": "2026-09-13 20:47:29",
    "wal_shm残留": "无",
    "结论": "主库在本次会话期间未被写入；会话只产生读访问",
}
d["meta"]["服务收尾"] = {
    "启动方式": "游离进程 start_all.ps1 -Prod（验收开始时 8000 未监听，非复用他人服务）",
    "后端PID": 67108,
    "停止方式": "stop_all.ps1",
    "停止后复验": "netstat 无 8000 监听；/api/health/ready 不可达；停止前 3 次采样无 ESTABLISHED 连接",
    "当前状态": "已停止",
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
print("OK; meta keys:", list(d["meta"].keys()))
