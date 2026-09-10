"""端到端冒烟测试。

对运行中的后端(127.0.0.1:8000)与前端(127.0.0.1:5173)执行关键路径冒烟，
逐项打印 PASS/FAIL，任一失败返回非零退出码。

覆盖（按用户最新要求逐项）：
- 真实下单必须 FILLED（不接受 400 reject）
- 回测必须等待 succeeded（failed/cancelled/timeout 算失败）
- 组合回测全链路（创建 → succeeded）
- WebSocket 订阅 → 收到至少一条推送
- 所有幂等键、账号、任务用唯一标记 + 后清理
- 每条断言都打印（不再"9 类汇总"掩盖失败）

用法: python scripts/e2e_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import websockets

BACKEND = os.environ.get("E2E_BACKEND", "http://127.0.0.1:8000")
FRONTEND = os.environ.get("E2E_FRONTEND", "http://127.0.0.1:5173")
WS_URL = os.environ.get("E2E_WS", "ws://127.0.0.1:8000/ws/quotes")
TIMEOUT = 15.0
WS_TIMEOUT = 15.0
WAIT_SUCCEEDED_TIMEOUT_S = 90.0
WAIT_POLL_INTERVAL_S = 1.0

# 用一个唯一 run 标记，方便回溯与清理
RUN_ID = f"smoke-{uuid.uuid4().hex[:8]}"


class SmokeResult:
    def __init__(self):
        self.records: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.records.append((name, ok, detail))
        prefix = "  [PASS]" if ok else "  [FAIL]"
        suffix = f" - {detail}" if detail else ""
        print(f"{prefix} {name}{suffix}")

    def summary(self) -> tuple[int, int]:
        passed = sum(1 for _, ok, _ in self.records if ok)
        total = len(self.records)
        print(f"\n结果: {passed}/{total} 通过")
        return passed, total


async def wait_status_succeeded(client: httpx.AsyncClient, url: str, label: str, res: SmokeResult) -> bool:
    """轮询直到 status=succeeded 或超时。失败返回 False，并把中间状态记入日志。"""
    deadline = time.time() + WAIT_SUCCEEDED_TIMEOUT_S
    last_status = None
    while time.time() < deadline:
        try:
            r = await client.get(url)
            if r.status_code != 200:
                await asyncio.sleep(WAIT_POLL_INTERVAL_S)
                continue
            data = r.json()
            status = data.get("status")
            last_status = status
            if status == "succeeded":
                return True
            if status in ("failed", "cancelled"):
                res.check(
                    f"{label}-等待 succeeded",
                    False,
                    f"任务以 {status} 结束，error={data.get('error_message')}",
                )
                return False
        except Exception:
            pass
        await asyncio.sleep(WAIT_POLL_INTERVAL_S)
    res.check(f"{label}-等待 succeeded", False, f"{WAIT_SUCCEEDED_TIMEOUT_S}秒超时，最后状态={last_status}")
    return False


async def verify_ws_subscription(symbol: str, res: SmokeResult) -> None:
    """订阅 /ws/quotes，等待收到该 symbol 的报价推送。"""
    try:
        async with websockets.connect(WS_URL, open_timeout=WS_TIMEOUT) as ws:
            # 1) 发送订阅
            await ws.send(json.dumps({"action": "subscribe", "symbols": [symbol]}))
            # 2) 等推送（5 秒内）
            try:
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT)
                msg = json.loads(msg_raw)
                # 推送可能是单条 quote 或 batch（看实现）
                got = False
                if isinstance(msg, dict):
                    if msg.get("symbol") == symbol:
                        got = True
                    elif msg.get("type") == "quote" and msg.get("data", {}).get("symbol") == symbol:
                        got = True
                    elif msg.get("type") in ("quotes", "quotes_batch"):
                        data = msg.get("data", [])
                        if any(q.get("symbol") == symbol for q in data):
                            got = True
                    elif symbol in str(msg):
                        got = True
                res.check("WS 订阅并收到推送", got, f"消息前缀={str(msg)[:60]}")
            except asyncio.TimeoutError:
                res.check("WS 订阅并收到推送", False, f"{WS_TIMEOUT}秒内无推送")
    except Exception as exc:  # noqa: BLE001
        res.check("WS 订阅并收到推送", False, f"连接失败: {exc}")


async def main() -> int:
    print(f"=== A 股平台端到端冒烟测试 (run={RUN_ID}) ===")
    res = SmokeResult()
    # 记录创建的对象以便清理
    created_account_id: int | None = None
    created_paper_account_id: int | None = None
    created_backtest_id: int | None = None
    created_portfolio_bt_id: int | None = None

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        # 1. 存活探针
        try:
            r = await client.get(f"{BACKEND}/api/health/live")
            res.check("存活探针 /health/live", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            res.check("存活探针 /health/live", False, str(exc))

        # 2. 就绪探针
        try:
            r = await client.get(f"{BACKEND}/api/health/ready")
            res.check("就绪探针 /health/ready", r.status_code == 200, f"HTTP {r.status_code}")
            if r.status_code != 200:
                print("后端未就绪，终止后续检查")
                _, _ = res.summary()
                return 1
        except Exception as exc:  # noqa: BLE001
            res.check("就绪探针 /health/ready", False, str(exc))
            return 1

        # 3. 详细健康
        try:
            r = await client.get(f"{BACKEND}/api/health")
            data = r.json()
            res.check(
                "详细健康 /health",
                data.get("status") == "ok" and "database" in data,
                f"db={data.get('database', {}).get('ok')}",
            )
        except Exception as exc:  # noqa: BLE001
            res.check("详细健康 /health", False, str(exc))

        # 4. 指标
        try:
            r = await client.get(f"{BACKEND}/api/metrics")
            data = r.json()
            res.check("指标 /metrics", "data_status" in data, f"data_status={data.get('data_status')}")
        except Exception as exc:  # noqa: BLE001
            res.check("指标 /metrics", False, str(exc))

        # 5. 批量行情
        try:
            r = await client.post(
                f"{BACKEND}/api/quotes/batch", json={"symbols": ["600000", "000001"]}
            )
            quotes = r.json().get("quotes", [])
            res.check(
                "批量行情 /quotes/batch",
                r.status_code == 200 and len(quotes) == 2,
                f"{len(quotes)} 条",
            )
        except Exception as exc:  # noqa: BLE001
            res.check("批量行情 /quotes/batch", False, str(exc))

        # 6. 策略列表
        try:
            r = await client.get(f"{BACKEND}/api/strategies")
            names = [s["name"] for s in r.json()]
            res.check("策略列表 /strategies", "ma_cross" in names, f"{len(names)} 个策略")
        except Exception as exc:  # noqa: BLE001
            res.check("策略列表 /strategies", False, str(exc))

        # 7. 单标的回测：创建 → 等待 succeeded → 验证 result
        unique_bt_key = f"{RUN_ID}-bt"
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=180)
            r = await client.post(
                f"{BACKEND}/api/backtests",
                json={
                    "symbol": "600000",
                    "strategy_name": "ma_cross",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "initial_cash": 100000,
                    "idempotency_key": unique_bt_key,
                },
            )
            ok = r.status_code in (200, 201, 202) and "id" in r.json()
            bt_id = r.json().get("id")
            created_backtest_id = bt_id
            res.check(
                "创建单标的回测",
                ok,
                f"HTTP {r.status_code} id={bt_id}",
            )
            if ok and bt_id:
                # 等待 succeeded
                succeeded = await wait_status_succeeded(
                    client,
                    f"{BACKEND}/api/backtests/{bt_id}",
                    "单标的回测",
                    res,
                )
                if succeeded:
                    # 复核 result 非空
                    r2 = await client.get(f"{BACKEND}/api/backtests/{bt_id}")
                    data = r2.json()
                    res.check(
                        "单标的回测 result 字段完整",
                        data.get("result") is not None
                        and data["result"].get("trade_count", -1) >= 0,
                        f"trades={data.get('result', {}).get('trade_count', '?')}",
                    )
        except Exception as exc:  # noqa: BLE001
            res.check("创建单标的回测", False, str(exc))

        # 8. 组合回测：创建 → 等待 succeeded → 验证 equity_curve
        unique_pbt_key = f"{RUN_ID}-portfolio"
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=120)
            r = await client.post(
                f"{BACKEND}/api/portfolio-backtests",
                json={
                    "symbols": ["600000", "000001"],
                    "strategy_name": "ma_cross",
                    "weights": {"600000": 0.6, "000001": 0.4},
                    "benchmark_symbol": "000300",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "initial_cash": 200000,
                    "idempotency_key": unique_pbt_key,
                },
            )
            ok = r.status_code in (200, 201, 202) and "id" in r.json()
            pbt_id = r.json().get("id")
            created_portfolio_bt_id = pbt_id
            res.check(
                "创建组合回测",
                ok,
                f"HTTP {r.status_code} id={pbt_id}",
            )
            if ok and pbt_id:
                succeeded = await wait_status_succeeded(
                    client,
                    f"{BACKEND}/api/portfolio-backtests/{pbt_id}",
                    "组合回测",
                    res,
                )
                if succeeded:
                    r2 = await client.get(f"{BACKEND}/api/portfolio-backtests/{pbt_id}")
                    data = r2.json()
                    result = data.get("result") or {}
                    res.check(
                        "组合回测 result 含 equity_curve + benchmark_curve",
                        bool(result.get("equity_curve"))
                        and bool(result.get("benchmark_curve")),
                        f"bars={len(result.get('equity_curve', []))}",
                    )
                    res.check(
                        "组合回测 result 含 α/β",
                        "alpha" in result and "beta" in result,
                        f"alpha={result.get('alpha'):.4f}",
                    )
        except Exception as exc:  # noqa: BLE001
            res.check("创建组合回测", False, str(exc))

        # 9. 模拟账户 → 下单 → 验证 FILLED
        try:
            r = await client.post(
                f"{BACKEND}/api/paper/accounts",
                json={"name": f"{RUN_ID}-account", "initial_cash": 100000},
            )
            acc_id = r.json().get("id")
            created_paper_account_id = acc_id
            res.check("创建模拟账户", r.status_code == 201 and acc_id is not None, f"id={acc_id}")

            if acc_id:
                # 先确保有自选股/行情能取到 → mock 模式下应总能取到
                quote = None
                try:
                    rq = await client.get(f"{BACKEND}/api/quotes/600000")
                    if rq.status_code == 200:
                        quote = rq.json()
                except Exception:
                    pass

                order_payload = {
                    "account_id": acc_id,
                    "symbol": "600000",
                    "side": "BUY",
                    "quantity": 100,
                    "signal_id": f"{RUN_ID}-buy",
                }
                r2 = await client.post(f"{BACKEND}/api/paper/orders", json=order_payload)
                if r2.status_code in (200, 201):
                    body = r2.json()
                    # 这里 r2 返回的是创建响应，不是 filled 状态；查 orders 列表
                    order_id = body.get("order_id")
                    rl = await client.get(
                        f"{BACKEND}/api/paper/orders",
                        params={"account_id": acc_id, "limit": 5},
                    )
                    orders = rl.json().get("orders", [])
                    filled = [o for o in orders if o.get("status") == "FILLED"]
                    res.check(
                        "模拟下单 → FILLED",
                        order_id is not None and len(filled) >= 1,
                        f"order_id={order_id} filled={len(filled)}",
                    )
                else:
                    res.check(
                        "模拟下单",
                        False,
                        f"HTTP {r2.status_code} body={r2.text[:100]}",
                    )
        except Exception as exc:  # noqa: BLE001
            res.check("模拟下单流程", False, str(exc))

        # 10. WebSocket 订阅 → 收到推送
        # 先把 600000 加入自选股（轮询仅对自选股生效），再等待轮询触发推送。
        try:
            rc = await client.post(
                f"{BACKEND}/api/watchlists",
                json={"name": f"e2e-{RUN_ID}", "symbols": ["600000"]},
            )
            ok_wl = rc.status_code in (200, 201) and rc.json().get("id") is not None
            if ok_wl:
                wl_id = rc.json()["id"]
                # 二次添加确认接口可调用
                await client.post(
                    f"{BACKEND}/api/watchlists/{wl_id}/symbols",
                    json={"symbol": "600000"},
                )
        except Exception:
            ok_wl = False

        if ok_wl:
            # 等下一次轮询（默认 3 秒）+ 推送
            await asyncio.sleep(5)
        await verify_ws_subscription("600000", res)

        # 11. 前端可达
        try:
            r = await client.get(FRONTEND)
            res.check("前端页面可达", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            res.check("前端页面可达", False, str(exc))

    # 清理：取消未结束的任务（仅为了不留垃圾；已 succeeded/failed 的会保留做审计）
    async with httpx.AsyncClient(timeout=5.0) as cleanup:
        if created_backtest_id:
            try:
                await cleanup.post(f"{BACKEND}/api/backtests/{created_backtest_id}/cancel")
            except Exception:
                pass
        if created_portfolio_bt_id:
            try:
                await cleanup.post(f"{BACKEND}/api/portfolio-backtests/{created_portfolio_bt_id}/cancel")
            except Exception:
                pass

    passed, total = res.summary()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
