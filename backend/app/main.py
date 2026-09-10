"""应用入口。

组装数据源、缓存、WebSocket、信号引擎与调度器，注册路由与生命周期。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api.backtests import router as backtests_router
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.paper_accounts import router as paper_router
from app.api.portfolio_backtests import router as portfolio_backtests_router
from app.api.quotes import router as quotes_router
from app.api.signals import router as signals_router
from app.api.strategies import ensure_strategies, router as strategies_router
from app.api.watchlists import router as watchlists_router
from app.config import get_settings
from app.database import models  # noqa: F401 - 注册模型
from app.database.session import Base, SessionLocal, engine
from app.logging_config import setup_logging
from app.market_data.akshare_provider import AkshareProvider
from app.market_data.mock_provider import MockProvider
from app.market_data.provider_manager import ProviderManager
from app.market_data.qmt_provider import QmtProvider
from app.market_data.tencent_provider import TencentProvider
from app.paper_trading.scheduler import SettlementScheduler, ensure_calendar_ready
from app.realtime.quote_cache import QuoteCache
from app.realtime.quote_scheduler import QuoteScheduler
from app.realtime.signal_engine import SignalEngine
from app.realtime.websocket_manager import ConnectionManager
from app.strategies import registry
from app.tasks.portfolio_worker import PortfolioBacktestWorker
from app.tasks.worker import BacktestWorker
from app.validation import sanitize_symbols

settings = get_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """简单的内存滑动窗口限流（按客户端 IP）。"""

    def __init__(self, app, max_per_minute: int):
        super().__init__(app)
        self.max_per_minute = max_per_minute
        self._hits: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if self.max_per_minute > 0:
            client = request.client.host if request.client else "unknown"
            now = time.time()
            window = self._hits[client]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= self.max_per_minute:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后再试"},
                )
            window.append(now)
        return await call_next(request)


def _build_providers() -> list:
    """根据配置优先级构建数据源列表。"""
    factory = {
        "tencent": TencentProvider,
        "akshare": AkshareProvider,
        "qmt": QmtProvider,
        "mock": MockProvider,
    }
    providers = []
    for name in settings.provider_list:
        cls = factory.get(name)
        if cls:
            providers.append(cls())
    # e2e_use_mock 启用时把 MockProvider 插到队首（满足 e2e_smoke 无网环境）
    if settings.e2e_use_mock and providers and not isinstance(
        providers[0], MockProvider
    ):
        providers.insert(0, MockProvider())
    # 兜底：始终保留一个可用数据源
    if not providers:
        providers.append(MockProvider())
    return providers


def _get_enabled_strategies():
    """从数据库读取启用的策略并映射为策略实例。"""
    from sqlalchemy import select

    from app.database.models import Strategy

    db = SessionLocal()
    try:
        enabled_names = [
            s.name
            for s in db.scalars(select(Strategy).where(Strategy.enabled.is_(True))).all()
        ]
    finally:
        db.close()
    result = []
    for name in enabled_names:
        strategy = registry.get_strategy(name)
        if strategy:
            result.append(strategy)
    return result


def _get_watch_symbols() -> list[str]:
    """从数据库读取所有自选股代码（去重）。"""
    from sqlalchemy import select

    from app.database.models import WatchlistSymbol

    db = SessionLocal()
    try:
        symbols = list(
            set(db.scalars(select(WatchlistSymbol.symbol)).all())
        )
    finally:
        db.close()
    return symbols


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化数据库、调度器与交易日历。"""
    # 仅测试或一次性演示环境允许 create_all；正常启动必须使用 Alembic。
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)

    # 同步策略表
    db = SessionLocal()
    try:
        ensure_strategies(db)
        # 启动时若本地交易日历为空，触发多源同步
        calendar_info = ensure_calendar_ready()
        logger.info(
            "交易日历启动同步: source=%s total=%s ready=%s",
            calendar_info.get("source"),
            calendar_info.get("total"),
            calendar_info.get("ready"),
        )
    finally:
        db.close()

    # 启动行情调度器
    scheduler: QuoteScheduler = app.state.scheduler
    scheduler.start()

    # 启动日终结算调度器
    settlement_scheduler: SettlementScheduler = app.state.settlement_scheduler
    settlement_scheduler.start()

    # 启动后台回测任务 worker（含遗留 running 任务恢复）
    worker: BacktestWorker = app.state.backtest_worker
    await worker.start()

    # 启动组合回测任务 worker
    portfolio_worker: PortfolioBacktestWorker = app.state.portfolio_backtest_worker
    await portfolio_worker.start()

    logger.info("A 股实时分析平台后端已启动")
    try:
        yield
    finally:
        await worker.stop()
        await portfolio_worker.stop()
        await settlement_scheduler.stop()
        scheduler.shutdown()
        await app.state.connection_manager.close()
        await app.state.provider_manager.close()
        logger.info("后端已关闭")


def create_app() -> FastAPI:
    """创建并组装 FastAPI 应用。"""
    app = FastAPI(
        title="A 股实时分析平台后端",
        description="分析结果仅用于研究，不构成投资建议。",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 限流
    app.add_middleware(RateLimitMiddleware, max_per_minute=settings.rate_limit_per_minute)

    # 组装共享组件并挂载到 app.state
    providers = _build_providers()
    provider_manager = ProviderManager(providers)
    quote_cache = QuoteCache()
    connection_manager = ConnectionManager()
    signal_engine = SignalEngine(
        quote_cache=quote_cache,
        connection_manager=connection_manager,
        get_enabled_strategies=_get_enabled_strategies,
        cooldown_seconds=settings.signal_cooldown_seconds,
    )
    scheduler = QuoteScheduler(
        provider_manager=provider_manager,
        quote_cache=quote_cache,
        connection_manager=connection_manager,
        get_symbols=_get_watch_symbols,
        on_quotes=signal_engine.process_quotes,
    )
    backtest_worker = BacktestWorker(provider_manager=provider_manager)
    settlement_scheduler = SettlementScheduler()
    portfolio_backtest_worker = PortfolioBacktestWorker(
        provider_manager=provider_manager
    )

    app.state.provider_manager = provider_manager
    app.state.quote_cache = quote_cache
    app.state.connection_manager = connection_manager
    app.state.signal_engine = signal_engine
    app.state.scheduler = scheduler
    app.state.backtest_worker = backtest_worker
    app.state.portfolio_backtest_worker = portfolio_backtest_worker
    app.state.settlement_scheduler = settlement_scheduler

    # 注册路由
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(quotes_router)
    app.include_router(watchlists_router)
    app.include_router(signals_router)
    app.include_router(strategies_router)
    app.include_router(backtests_router)
    app.include_router(portfolio_backtests_router)
    app.include_router(paper_router)

    # 行情数据源状态接口
    @app.get("/api/market/providers")
    async def market_providers():
        status = await provider_manager.health_check()
        return {"providers": [p.name for p in providers], "status": status}

    # WebSocket 行情推送
    @app.websocket("/ws/quotes")
    async def ws_quotes(websocket: WebSocket):
        await connection_manager.connect(websocket, channel="quotes")
        try:
            while True:
                data = await websocket.receive_json()
                action = data.get("action")
                if action == "subscribe":
                    symbols = sanitize_symbols(data.get("symbols", []))
                    await connection_manager.subscribe(websocket, symbols)
        except WebSocketDisconnect:
            await connection_manager.disconnect(websocket)
        except Exception:  # noqa: BLE001
            await connection_manager.disconnect(websocket)

    # WebSocket 信号推送
    @app.websocket("/ws/signals")
    async def ws_signals(websocket: WebSocket):
        await connection_manager.connect(websocket, channel="signals")
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await connection_manager.disconnect(websocket)
        except Exception:  # noqa: BLE001
            await connection_manager.disconnect(websocket)

    return app


app = create_app()
