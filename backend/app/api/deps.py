"""API 共享依赖。

通过 request.app.state 访问 main.py 中组装的共享组件。
"""
from __future__ import annotations

from fastapi import Request

from app.market_data.provider_manager import ProviderManager
from app.market_data.eastmoney_datacenter import EastmoneyDatacenterService
from app.market_data.eastmoney_limit_up import EastmoneyLimitUpService
from app.market_data.eastmoney_market import EastmoneyMarketService
from app.realtime.quote_cache import QuoteCache
from app.realtime.signal_engine import SignalEngine
from app.realtime.websocket_manager import ConnectionManager


def get_provider_manager(request: Request) -> ProviderManager:
    return request.app.state.provider_manager


def get_market_service(request: Request) -> EastmoneyMarketService:
    return request.app.state.market_service


def get_datacenter_service(request: Request) -> EastmoneyDatacenterService:
    return request.app.state.datacenter_service


def get_limit_up_service(request: Request) -> EastmoneyLimitUpService:
    return request.app.state.limit_up_service


def get_quote_cache(request: Request) -> QuoteCache:
    return request.app.state.quote_cache


def get_connection_manager(request: Request) -> ConnectionManager:
    return request.app.state.connection_manager


def get_signal_engine(request: Request) -> SignalEngine:
    return request.app.state.signal_engine
