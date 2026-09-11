"""应用配置模块。

所有配置通过环境变量或 .env 文件提供，密钥一律不硬编码。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用运行配置。

    字段与 .env.example 中的变量一一对应。
    """

    # 数据库
    database_url: str = "sqlite:///./a_stock.db"
    auto_create_tables: bool = False

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # 日志
    log_level: str = "INFO"

    # 行情数据源优先级
    # Mock 只能由测试或演示环境显式启用，禁止真实行情失败时返回随机价格。
    market_providers: str = "tencent,akshare"
    # e2e_smoke 启用：用 mock 作为最高优先级（无需 AKShare 网络）
    e2e_use_mock: bool = False
    quote_poll_interval: float = 3.0
    rolling_window_size: int = 300
    signal_cooldown_seconds: float = 60.0
    max_quote_age_seconds: float = 15.0

    # ───────────── Universe（股票池）Provider 配置 ─────────────
    # 生产环境默认值：BaoStock 历史时点主源，AKShare 当前全市场备援。
    # 测试/CI/managed E2E 模式：通过 E2E_USE_MOCK=true 强制改为 mock；
    # 也可以显式设 universe_providers=mock 走测试样本（26 条）。
    # 多 provider 用逗号分隔，按顺序尝试，任一成功即止；全部失败 → 503。
    # 严禁在生产默认列表里包含 mock — 否则真实源失败会静默退回假数据。
    universe_providers: str = "baostock,akshare"
    # AKShare 超时（秒）：超过这个时间就算失败，让 SyncService 切下一个 provider。
    akshare_universe_timeout_seconds: float = 30.0
    # BaoStock 超时（秒）：point-in-time 主数据源，含 query_all_stock + query_stock_basic + login/logout
    baostock_universe_timeout_seconds: float = 60.0
    # 同步重试参数（每个 provider 内最多 max_retries+1 次尝试）
    universe_max_retries: int = 2
    universe_backoff_base_ms: int = 50

    # 各数据源密钥（仅通过环境变量提供）
    qmt_api_key: str = "YOUR_API_KEY"
    qmt_api_secret: str = "YOUR_API_KEY"
    tencent_api_key: str = "YOUR_API_KEY"
    akshare_api_key: str = "YOUR_API_KEY"

    # CORS
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 限流
    rate_limit_per_minute: int = 300
    ws_max_subscriptions: int = 200

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的 CORS 来源转换为列表。"""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def provider_list(self) -> list[str]:
        """将逗号分隔的数据源优先级转换为列表。"""
        return [p.strip().lower() for p in self.market_providers.split(",") if p.strip()]

    @property
    def universe_provider_list(self) -> list[str]:
        """股票池 provider 列表。生产默认 baostock→akshare；CI 显式切 mock。

        强约束：
        - 必须非空（空字符串 → 抛 RuntimeError）
        - 不允许在生产环境默认值中包含 mock（除非 e2e_use_mock=True 显式声明测试模式）
        - 不在列表中的 provider → 启动时抛错
        """
        items = [p.strip().lower() for p in self.universe_providers.split(",") if p.strip()]
        if not items:
            raise RuntimeError(
                "UNIVERSE_PROVIDERS 不能为空；至少需要配置一个 provider（akshare 或 mock）"
            )
        # 防止真实场景里静默退回 mock
        if "mock" in items and not self.e2e_use_mock:
            import os
            # 仅当用户通过 ENV 显式传入时才允许 mock（CI/managed e2e）
            if os.environ.get("UNIVERSE_PROVIDERS") or os.environ.get("E2E_USE_MOCK"):
                pass  # 显式声明 OK
            else:
                raise RuntimeError(
                    "UNIVERSE_PROVIDERS 含 mock 但未显式声明 E2E_USE_MOCK=true；"
                    "生产环境禁止默认走 mock — 设置 UNIVERSE_PROVIDERS=baostock,akshare"
                )
        return items


@lru_cache
def get_settings() -> Settings:
    """返回缓存的全局配置单例。"""
    return Settings()
