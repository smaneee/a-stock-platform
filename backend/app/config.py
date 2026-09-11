"""应用配置模块。

所有配置通过环境变量或 .env 文件提供，密钥一律不硬编码。
"""
from functools import lru_cache

from pydantic import Field
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
    # BaoStock 超时（秒）：point-in-time 主数据源，含 query_all_stock + query_stock_basic
    # + BJ 子源 + login/logout。实机测量全市场约 70~210 秒（query_all_stock 逐行读取
    # 7382 行约 11s、query_stock_basic 逐行读取 8950 行约 36s 是主要耗时），
    # 原默认 60s 会让每次同步都判超时并丢弃已经拉好的结果。
    baostock_universe_timeout_seconds: float = 300.0
    # AKShare BJ 子源（补 BaoStock 北交所缺口）超时（秒）：该接口分 18 页拉取，
    # 实测 12~20s。
    baostock_bj_supplement_timeout_seconds: float = 60.0
    # 同步重试参数（每个 provider 内最多 max_retries+1 次尝试）
    universe_max_retries: int = 2
    universe_backoff_base_ms: int = 50
    history_ingest_max_concurrency: int = 4

    # QMT 实盘默认硬关闭；账号只从本机 .env 读取，不写数据库或日志。
    real_trading_enabled: bool = False
    qmt_userdata_path: str = ""
    qmt_account_id: str = ""
    qmt_account_type: str = "STOCK"
    qmt_call_timeout_seconds: float = 10.0
    live_trading_api_token: str = ""
    live_reconcile_interval_seconds: float = 30.0

    # ───────────── 模拟交易风控限额（RISK_*） ─────────────
    # 比例均为小数（0.20 = 20%）。只影响模拟盘/回测的风控判定，
    # 不改变券商柜台侧风控；修改后需重启后端生效。
    risk_max_position_per_symbol: float = Field(default=0.20, gt=0, le=1)
    risk_max_total_position: float = Field(default=0.80, gt=0, le=1)
    risk_max_daily_loss: float = Field(default=0.03, gt=0, le=1)
    risk_max_total_drawdown: float = Field(default=0.10, gt=0, le=1)
    risk_min_commission: float = Field(default=5.0, ge=0, le=1000)
    risk_commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    risk_stamp_tax_rate: float = Field(default=0.0005, ge=0, le=0.01)

    # ───────────── 每日流水线自动调度（可选） ─────────────
    # 开启后按 A 股交易日在北京时间 DAILY_PIPELINE_AUTO_HOUR:MINUTE 自动创建
    # 当日流水线任务；只做研究与模拟调仓，永不触发真实下单。
    daily_pipeline_auto_enabled: bool = False
    daily_pipeline_auto_hour: int = Field(default=15, ge=0, le=23)
    daily_pipeline_auto_minute: int = Field(default=35, ge=0, le=59)
    daily_pipeline_auto_paper_account_id: int = Field(default=0, ge=0)
    daily_pipeline_auto_execute_paper: bool = False
    daily_pipeline_auto_lookback_days: int = Field(default=365, ge=90, le=2000)

    # 其余数据源密钥（仅通过环境变量提供）
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
