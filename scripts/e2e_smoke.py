"""端到端冒烟测试。

对运行中的后端(127.0.0.1:8000)与前端(127.0.0.1:5173)执行关键路径冒烟，
逐项打印 PASS/FAIL，任一失败返回非零退出码。

用法: python scripts/e2e_smoke.py
"""
from __future__ import annotations

import sys
import time

import httpx

BACKEND = "http://127.0.0.1:8000"
FRONTEND = "http://127.0.0.1:5173"
TIMEOUT = 10.0

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main() -> int:
    print("=== A 股平台端到端冒烟测试 ===")
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        # 1. 存活探针
        try:
            r = client.get(f"{BACKEND}/api/health/live")
            check("存活探针 /health/live", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            check("存活探针 /health/live", False, str(exc))

        # 2. 就绪探针
        try:
            r = client.get(f"{BACKEND}/api/health/ready")
            check("就绪探针 /health/ready", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            check("就绪探针 /health/ready", False, str(exc))

        # 3. 详细健康
        try:
            r = client.get(f"{BACKEND}/api/health")
            data = r.json()
            check("详细健康 /health", "database" in data and "providers" in data)
        except Exception as exc:  # noqa: BLE001
            check("详细健康 /health", False, str(exc))

        # 4. 可观测性指标
        try:
            r = client.get(f"{BACKEND}/api/metrics")
            data = r.json()
            check("指标 /metrics", "data_status" in data, f"data_status={data.get('data_status')}")
        except Exception as exc:  # noqa: BLE001
            check("指标 /metrics", False, str(exc))

        # 5. 批量行情
        try:
            r = client.post(f"{BACKEND}/api/quotes/batch", json={"symbols": ["600000", "000001"]})
            quotes = r.json().get("quotes", [])
            check("批量行情 /quotes/batch", r.status_code == 200 and len(quotes) == 2, f"{len(quotes)} 条")
        except Exception as exc:  # noqa: BLE001
            check("批量行情 /quotes/batch", False, str(exc))

        # 6. 策略列表
        try:
            r = client.get(f"{BACKEND}/api/strategies")
            names = [s["name"] for s in r.json()]
            check("策略列表 /strategies", "ma_cross" in names, f"{len(names)} 个策略")
        except Exception as exc:  # noqa: BLE001
            check("策略列表 /strategies", False, str(exc))

        # 7. 回测任务（创建 + 查询）
        try:
            r = client.post(
                f"{BACKEND}/api/backtests",
                json={
                    "symbol": "600000",
                    "strategy_name": "ma_cross",
                    "start_time": "2026-01-01T00:00:00",
                    "end_time": "2026-01-31T00:00:00",
                    "initial_cash": 100000,
                },
            )
            ok = r.status_code == 202 and "id" in r.json()
            bt_id = r.json().get("id")
            check("创建回测任务", ok, f"HTTP {r.status_code} id={bt_id}")
            if ok:
                r2 = client.get(f"{BACKEND}/api/backtests/{bt_id}")
                check("查询回测任务", r2.status_code == 200)
        except Exception as exc:  # noqa: BLE001
            check("创建回测任务", False, str(exc))

        # 8. 模拟账户（创建 + 下单）
        try:
            r = client.post(f"{BACKEND}/api/paper/accounts", json={"name": "冒烟账户", "initial_cash": 100000})
            acc_id = r.json().get("id")
            check("创建模拟账户", r.status_code == 201 and acc_id is not None, f"id={acc_id}")
            if acc_id:
                r2 = client.post(
                    f"{BACKEND}/api/paper/orders",
                    json={"account_id": acc_id, "symbol": "600000", "side": "BUY", "quantity": 100},
                )
                check("模拟下单", r2.status_code in (200, 400), f"HTTP {r2.status_code}")
        except Exception as exc:  # noqa: BLE001
            check("创建模拟账户", False, str(exc))

        # 9. 前端静态资源
        try:
            r = client.get(FRONTEND)
            check("前端页面可达", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            check("前端页面可达", False, str(exc))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
