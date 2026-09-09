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

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # 日志
    log_level: str = "INFO"

    # 行情数据源优先级
    market_providers: str = "tencent,akshare,mock"
    quote_poll_interval: float = 3.0
    rolling_window_size: int = 300
    signal_cooldown_seconds: float = 60.0

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


@lru_cache
def get_settings() -> Settings:
    """返回缓存的全局配置单例。"""
    return Settings()
