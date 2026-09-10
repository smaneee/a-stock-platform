"""端到端冒烟测试 - 严格版。

对运行中的后端与前端执行关键路径冒烟，逐项打印 PASS/FAIL。
任一失败返回非零退出码；清理失败也必须使测试失败。

强制约束（fix(validation)）：
1) 数据源 mock 模式下，data_status 必须 = simulated
2) 组合回测必须 succeeded，且 executed_symbols == requested_symbols
3) 单标的/组合回测结果必须含有效数据（trade_count > 0 / bars > 0）
4) 临时 SQLite 数据库：启动前创建、结束后删除；不污染 a_stock.db
5) 清理失败必须使测试失败（try/except 不能吞异常）
6) 关闭后端/前端进程后必须确认端口释放
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import websockets

BACKEND = os.environ.get("E2E_BACKEND", "http://127.0.0.1:8000")
FRONTEND = os.environ.get("E2E_FRONTEND", "http://127.0.0.1:5173")
WS_URL = os.environ.get("E2E_WS", "ws://127.0.0.1:8000/ws/quotes")
TIMEOUT = 15.0
WS_TIMEOUT = 15.0
WAIT_SUCCEEDED_TIMEOUT_S = 90.0
WAIT_POLL_INTERVAL_S = 1.0
USE_MOCK = os.environ.get("E2E_USE_MOCK", "true").lower() in ("1", "true", "yes")

# 临时 SQLite 数据库路径（专用、不污染 a_stock.db）
TEMP_DB_PATH = os.environ.get(
    "E2E_DB_PATH",
    str(Path(tempfile.gettempdir()) / f"e2e_smoke_{uuid.uuid4().hex[:8]}.db"),
)
A_STOCK_DB = Path("E:/workbuddy work/2026-09-09-15-34-54/a-stock-platform/backend/a_stock.db")

# Run 标记
RUN_ID = f"smoke-{uuid.uuid4().hex[:8]}"


class SmokeResult:
    def __init__(self):
        self.records: list[tuple[str, bool, str]] = []
        self.cleanup_errors: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.records.append((name, ok, detail))
        prefix = "  [PASS]" if ok else "  [FAIL]"
        suffix = f" - {detail}" if detail else ""
        print(f"{prefix} {name}{suffix}")

    def summary(self) -> tuple[int, int]:
        passed = sum(1 for _, ok, _ in self.records if ok)
        total = len(self.records)
        print(f"\n结果: {passed}/{total} 通过")
        if self.cleanup_errors:
            print(f"\n[FAIL] {len(self.cleanup_errors)} 个清理错误（使测试失败）：")
            for err in self.cleanup_errors:
                print(f"  - {err}")
        return passed, total


async def wait_status_succeeded(
    client: httpx.AsyncClient, url: str, label: str, res: SmokeResult
) -> bool:
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
    res.check(
        f"{label}-等待 succeeded",
        False,
        f"{WAIT_SUCCEEDED_TIMEOUT_S}秒超时，最后状态={last_status}",
    )
    return False


async def verify_ws_subscription(symbol: str, res: SmokeResult) -> None:
    try:
        async with websockets.connect(WS_URL, open_timeout=WS_TIMEOUT) as ws:
            await ws.send(json.dumps({"action": "subscribe", "symbols": [symbol]}))
            try:
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=WS_TIMEOUT)
                msg = json.loads(msg_raw)
                got = False
                if isinstance(msg, dict):
                    if msg.get("symbol") == symbol:
                        got = True
                    elif msg.get("type") == "quote" and msg.get("data", {}).get(
                        "symbol"
                    ) == symbol:
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
    except Exception as exc:
        res.check("WS 订阅并收到推送", False, f"连接失败: {exc}")


# ──────────────── Lifecycle: 启动后端 + 前端（专用临时 DB） ────────────────


def start_backend_with_temp_db(backend_dir: Path) -> subprocess.Popen:
    """启动后端，使用专用临时 SQLite DB，避免污染 a_stock.db。"""
    log_path = Path(tempfile.gettempdir()) / f"e2e_backend_{RUN_ID}.log"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{TEMP_DB_PATH}"
    env["E2E_USE_MOCK"] = "true" if USE_MOCK else "false"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            str(backend_dir / ".venv" / "Scripts" / "python.exe"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(backend_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )
    print(f"  后端 PID={proc.pid}, log={log_path}")
    return proc


def start_frontend(frontend_dir: Path) -> subprocess.Popen:
    log_path = Path(tempfile.gettempdir()) / f"e2e_frontend_{RUN_ID}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["npm.cmd", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"],
        cwd=str(frontend_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    print(f"  前端 PID={proc.pid}, log={log_path}")
    return proc


def init_temp_db(backend_dir: Path) -> None:
    """运行 alembic upgrade head 在临时 DB 上建表。"""
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{TEMP_DB_PATH}"
    print(f"  初始化临时数据库：{TEMP_DB_PATH}")
    result = subprocess.run(
        [str(backend_dir / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade 失败: {result.stderr}")


def wait_for_http(url: str, timeout: float = 60.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = urllib.request.urlopen(url, timeout=2)
            if r.status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ──────────────── 清理：失败必须使测试失败 ────────────────


def kill_process(proc: subprocess.Popen, name: str, errors: list[str]) -> None:
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception as exc:
                errors.append(f"{name} 强制 kill 失败: {exc}")
        except Exception as exc:
            errors.append(f"{name} 终止失败: {exc}")


def delete_temp_db(errors: list[str]) -> None:
    p = Path(TEMP_DB_PATH)
    if p.exists():
        try:
            p.unlink()
            print(f"  已删除临时数据库：{TEMP_DB_PATH}")
        except Exception as exc:
            errors.append(f"删除临时 DB 失败: {exc}")


def verify_no_residue(errors: list[str]) -> None:
    """E2E 结束后确认端口释放 + 进程退出 + 临时 DB 已删除。"""
    import socket

    for port in (8000, 5173):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    errors.append(f"端口 {port} 仍被占用")
        except Exception:
            pass

    if Path(TEMP_DB_PATH).exists():
        errors.append(f"临时 DB 仍存在：{TEMP_DB_PATH}")


# ──────────────── 主流程 ────────────────


async def run_checks(res: SmokeResult) -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        # 1. 存活探针
        try:
            r = await client.get(f"{BACKEND}/api/health/live")
            res.check("存活探针 /health/live", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:
            res.check("存活探针 /health/live", False, str(exc))

        # 2. 就绪探针
        try:
            r = await client.get(f"{BACKEND}/api/health/ready")
            if r.status_code != 200:
                res.check("就绪探针 /health/ready", False, f"HTTP {r.status_code}")
                return
            res.check("就绪探针 /health/ready", True, "HTTP 200")
        except Exception as exc:
            res.check("就绪探针 /health/ready", False, str(exc))
            return

        # 3. 详细健康
        try:
            r = await client.get(f"{BACKEND}/api/health")
            data = r.json()
            res.check(
                "详细健康 /health",
                data.get("status") == "ok" and "database" in data,
                f"db={data.get('database', {}).get('ok')}",
            )
        except Exception as exc:
            res.check("详细健康 /health", False, str(exc))

        # 4. 指标 + data_status 严格匹配
        try:
            r = await client.get(f"{BACKEND}/api/metrics")
            data = r.json()
            expected_status = "simulated" if USE_MOCK else "real-time"
            actual = data.get("data_status", "missing")
            res.check(
                f"指标 /metrics data_status={expected_status}",
                actual == expected_status,
                f"actual={actual}, active={data.get('active_providers')}",
            )
        except Exception as exc:
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
        except Exception as exc:
            res.check("批量行情 /quotes/batch", False, str(exc))

        # 6. 策略列表
        try:
            r = await client.get(f"{BACKEND}/api/strategies")
            names = [s["name"] for s in r.json()]
            res.check("策略列表 /strategies", "ma_cross" in names, f"{len(names)} 个策略")
        except Exception as exc:
            res.check("策略列表 /strategies", False, str(exc))

        # 7. 单标的回测 → succeeded → result 含 trade_count > 0
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
            bt_id = r.json().get("id") if r.status_code in (200, 201, 202) else None
            res.check(
                "创建单标的回测",
                bt_id is not None,
                f"HTTP {r.status_code} id={bt_id}",
            )
            if bt_id:
                succeeded = await wait_status_succeeded(
                    client, f"{BACKEND}/api/backtests/{bt_id}", "单标的回测", res
                )
                if succeeded:
                    r2 = await client.get(f"{BACKEND}/api/backtests/{bt_id}")
                    data = r2.json()
                    result = data.get("result") or {}
                    trades = result.get("trade_count", 0)
                    res.check(
                        "单标的回测 result 含 trade_count > 0",
                        trades > 0,
                        f"trades={trades}",
                    )
        except Exception as exc:
            res.check("创建单标的回测", False, str(exc))

        # 8. 组合回测 → succeeded → executed_symbols == requested_symbols
        unique_pbt_key = f"{RUN_ID}-portfolio"
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=120)
            requested = ["600000", "000001"]
            r = await client.post(
                f"{BACKEND}/api/portfolio-backtests",
                json={
                    "symbols": requested,
                    "strategy_name": "ma_cross",
                    "weights": {"600000": 0.6, "000001": 0.4},
                    "benchmark_symbol": "000300",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "initial_cash": 200000,
                    "idempotency_key": unique_pbt_key,
                },
            )
            pbt_id = (
                r.json().get("id") if r.status_code in (200, 201, 202) else None
            )
            res.check(
                "创建组合回测",
                pbt_id is not None,
                f"HTTP {r.status_code} id={pbt_id}",
            )
            if pbt_id:
                succeeded = await wait_status_succeeded(
                    client,
                    f"{BACKEND}/api/portfolio-backtests/{pbt_id}",
                    "组合回测",
                    res,
                )
                if succeeded:
                    r2 = await client.get(
                        f"{BACKEND}/api/portfolio-backtests/{pbt_id}"
                    )
                    data = r2.json()
                    result = data.get("result") or {}
                    # 关键断言：executed == requested == 2
                    executed = result.get("executed_symbols", [])
                    res.check(
                        "组合回测 executed_symbols 完整（==2）",
                        sorted(executed) == sorted(requested),
                        f"requested={len(requested)} executed={executed}",
                    )
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
        except Exception as exc:
            res.check("创建组合回测", False, str(exc))

        # 9. 模拟账户 → 下单 → FILLED
        try:
            r = await client.post(
                f"{BACKEND}/api/paper/accounts",
                json={"name": f"{RUN_ID}-account", "initial_cash": 100000},
            )
            acc_id = r.json().get("id") if r.status_code == 201 else None
            res.check("创建模拟账户", acc_id is not None, f"id={acc_id}")
            if acc_id:
                order_payload = {
                    "account_id": acc_id,
                    "symbol": "600000",
                    "side": "BUY",
                    "quantity": 100,
                    "signal_id": f"{RUN_ID}-buy",
                }
                r2 = await client.post(
                    f"{BACKEND}/api/paper/orders", json=order_payload
                )
                order_id = r2.json().get("order_id") if r2.status_code in (200, 201) else None
                rl = await client.get(
                    f"{BACKEND}/api/paper/orders",
                    params={"account_id": acc_id, "limit": 5},
                )
                filled = [
                    o
                    for o in rl.json().get("orders", [])
                    if o.get("status") == "FILLED"
                ]
                res.check(
                    "模拟下单 → FILLED",
                    order_id is not None and len(filled) >= 1,
                    f"order_id={order_id} filled={len(filled)}",
                )
        except Exception as exc:
            res.check("模拟下单流程", False, str(exc))

        # 10. WS 推送
        try:
            rc = await client.post(
                f"{BACKEND}/api/watchlists",
                json={"name": f"e2e-{RUN_ID}", "symbols": ["600000"]},
            )
            if rc.status_code in (200, 201) and rc.json().get("id"):
                wl_id = rc.json()["id"]
                await client.post(
                    f"{BACKEND}/api/watchlists/{wl_id}/symbols",
                    json={"symbol": "600000"},
                )
                await asyncio.sleep(5)
        except Exception:
            pass

        await verify_ws_subscription("600000", res)

        # 11. 前端可达
        try:
            r = await client.get(FRONTEND)
            res.check("前端页面可达", r.status_code == 200, f"HTTP {r.status_code}")
        except Exception as exc:
            res.check("前端页面可达", False, str(exc))


async def main_managed() -> int:
    """完整生命周期：启动服务 → 运行测试 → 关闭服务 → 清理 DB（清理失败即失败）。"""
    print(f"=== A 股平台端到端冒烟测试 (run={RUN_ID}) ===")
    print(f"  E2E_USE_MOCK={USE_MOCK}, DB={TEMP_DB_PATH}")

    backend_dir = Path("E:/workbuddy work/2026-09-09-15-34-54/a-stock-platform/backend")
    frontend_dir = Path("E:/workbuddy work/2026-09-09-15-34-54/a-stock-platform/frontend")

    # 保护：不能使用 a_stock.db
    if TEMP_DB_PATH == str(A_STOCK_DB):
        raise RuntimeError("E2E 拒绝使用 a_stock.db，必须使用专用临时 DB")

    init_temp_db(backend_dir)

    backend = start_backend_with_temp_db(backend_dir)
    frontend = start_frontend(frontend_dir)

    res = SmokeResult()
    try:
        # 等待 readiness
        backend_ok = wait_for_http(f"{BACKEND}/api/health/ready", timeout=60)
        frontend_ok = wait_for_http(FRONTEND, timeout=30)
        if not backend_ok:
            res.check("后端启动就绪", False, "60 秒内未通过 /api/health/ready")
            return 1
        if not frontend_ok:
            res.check("前端启动就绪", False, "30 秒内未就绪")
            return 1

        await run_checks(res)

    finally:
        # 关闭服务
        kill_process(backend, "后端", res.cleanup_errors)
        kill_process(frontend, "前端", res.cleanup_errors)
        # 等待端口释放
        time.sleep(2)
        # 删除临时 DB
        delete_temp_db(res.cleanup_errors)
        # 残留检查
        verify_no_residue(res.cleanup_errors)

    passed, total = res.summary()
    cleanup_ok = len(res.cleanup_errors) == 0
    return 0 if (passed == total and cleanup_ok) else 1


# 兼容旧调用方式（外部已启动服务）
async def main_external() -> int:
    print(f"=== A 股平台端到端冒烟测试 (run={RUN_ID}) ===")
    res = SmokeResult()
    await run_checks(res)
    passed, total = res.summary()
    return 0 if passed == total else 1


if __name__ == "__main__":
    mode = os.environ.get("E2E_MODE", "external")
    if mode == "managed":
        sys.exit(asyncio.run(main_managed()))
    sys.exit(asyncio.run(main_external()))