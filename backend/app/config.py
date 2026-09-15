"""应用配置模块。

所有配置通过环境变量或 .env 文件提供，密钥一律不硬编码。
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


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
    # 通达信（tdx）走二进制 TCP 协议，实测一次批量行情约 50ms，比东财 HTTP 快
    # 一个数量级且不会被单 IP 突发限流，作为实时行情首选；东财为备援，腾讯兜底。
    # akshare 的历史接口与东财同源，管理器在一次请求内不会对同一上游重复请求
    # （见 ProviderManager.upstream）。tdx 不支持北交所代码，这类请求会自动回退。
    market_providers: str = "tdx,eastmoney,tencent,akshare"
    # e2e_smoke 启用：用 mock 作为最高优先级（无需 AKShare 网络）
    e2e_use_mock: bool = False
    quote_poll_interval: float = 3.0
    rolling_window_size: int = 300
    signal_cooldown_seconds: float = 60.0
    max_quote_age_seconds: float = 15.0

    # ───────────── 实时买点雷达 ─────────────
    # 扫描时并行的通达信连接数。实测全市场 5550 只：3 条约 4.1s、5 条约 3.0s、
    # 6 条约 2.5s、8 条约 2.5s（8 条收益已不明显）。6 条是速度与连接数的折中；
    # 设为 0 表示只用 ProviderManager 的顺序回退链（最慢但连接最少）。
    screener_tdx_pool_size: int = Field(default=6, ge=0, le=16)

    # ───────────── Universe（股票池）Provider 配置 ─────────────
    # 生产环境默认值：东方财富当前全市场主源（含所属行业），BaoStock 历史时点备援，
    # AKShare 当前全市场兜底。
    # 测试/CI/managed E2E 模式：通过 E2E_USE_MOCK=true 强制改为 mock；
    # 也可以显式设 universe_providers=mock 走测试样本（26 条）。
    # 多 provider 用逗号分隔，按顺序尝试，任一成功即止；全部失败 → 503。
    # 严禁在生产默认列表里包含 mock — 否则真实源失败会静默退回假数据。
    universe_providers: str = "eastmoney,baostock,akshare"
    # 东方财富股票池超时（秒）：60 页并发拉取，实测 3~10 秒
    eastmoney_universe_timeout_seconds: float = 90.0
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
    # 模拟盘单边滑点。默认与回测引擎 ExecutionConfig.slippage 同值（0.0005），
    # 使模拟盘与回测共用一套成交假设；不再出现「回测扣滑点、模拟盘不扣」的系统性偏乐观。
    risk_slippage: float = Field(default=0.0005, ge=0, le=0.1)
    # 过户费（双边）。与回测 ExecutionConfig.transfer_fee_rate 同值。
    risk_transfer_fee_rate: float = Field(default=0.00001, ge=0, le=0.01)

    # ───────────── 每日流水线自动调度（可选） ─────────────
    # 开启后按 A 股交易日在北京时间 DAILY_PIPELINE_AUTO_HOUR:MINUTE 自动创建
    # 当日流水线任务；只做研究与模拟调仓，永不触发真实下单。
    daily_pipeline_auto_enabled: bool = False
    daily_pipeline_auto_hour: int = Field(default=15, ge=0, le=23)
    daily_pipeline_auto_minute: int = Field(default=35, ge=0, le=59)
    daily_pipeline_auto_paper_account_id: int = Field(default=0, ge=0)
    daily_pipeline_auto_execute_paper: bool = False
    daily_pipeline_auto_lookback_days: int = Field(default=365, ge=90, le=2000)

    # ───────────── 涨停板情绪池落库（LIMIT_UP_SENTIMENT_*） ─────────────
    # 东财涨停板行情只保留最近若干个交易日，情绪曲线（封板率 / 连板高度）
    # 必须每个交易日收盘后累积一次，否则历史会永久缺失。
    # 只读行情 + 写本地数据库，不涉及任何交易动作，因此默认开启。
    limit_up_sentiment_auto_enabled: bool = True
    limit_up_sentiment_auto_hour: int = Field(default=16, ge=0, le=23)
    limit_up_sentiment_auto_minute: int = Field(default=10, ge=0, le=59)
    # 手动回补时默认回溯的自然日数（上游只保留最近若干个交易日）
    limit_up_sentiment_backfill_days: int = Field(default=20, ge=1, le=90)

    # 其余数据源密钥（仅通过环境变量提供）
    tencent_api_key: str = "YOUR_API_KEY"
    akshare_api_key: str = "YOUR_API_KEY"

    # ───────────── 研究报告解释层（DeepSeek，单用户本机） ─────────────
    # 定位：**只做解释**，不参与任何计算、不写库、不碰策略门禁与下单。
    # 数字一律来自确定性代码；模型只能引用证据包里有 id 的事实，引用不合格就整段拒收。
    #
    # 模型名**故意不给默认值**：2026-09-14 尝试核对官方文档时本机 web 检索工具不可用
    # （firecrawl 403），无法确认当前型号与费率，所以按「不写死未经确认的信息」处理 ——
    # 必须由使用者自己填 DEEPSEEK_MODEL，否则解释层返回 not_configured。
    explain_enabled: bool = False
    # 显式开启后，可复用本机 DeepSeek Harness 的回环 API；令牌只在运行时读取，
    # 不复制到项目配置或数据库。找不到本地服务时仍按未配置处理。
    explain_local_harness_enabled: bool = False
    deepseek_api_key: str = ""
    deepseek_model: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    # 单次解释的最大输出 token（成本上限；不按金额计价，因为费率未核实）
    deepseek_max_output_tokens: int = Field(default=1200, ge=128, le=8000)

    # ───────────── 研究复核提醒（阶段 2） ─────────────
    # 收盘后扫描每个标的的最新研究记录，把触发的复核条件写进待办列表。
    # 只读快照 + 只写平台自己的提醒表，不涉及任何交易动作，因此默认开启；
    # 需要关闭时设置 RESEARCH_REMINDER_AUTO_ENABLED=false。
    research_reminder_auto_enabled: bool = True
    # 日终结算在 15:30，提醒扫描排在 15:45（北京时间）
    research_reminder_auto_hour: int = Field(default=15, ge=0, le=23)
    research_reminder_auto_minute: int = Field(default=45, ge=0, le=59)
    # 一次扫描最多处理多少个标的（每个标的取最新一条研究记录）
    research_reminder_scan_limit: int = Field(default=200, ge=1, le=2000)

    # CORS
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # ───────────── 局域网访问控制（P0-04） ─────────────
    # require_auth=true 时，除 /api/health 与 /api/auth 外的所有 /api/* 都需要会话令牌；
    # 本机回环浏览器通过 /api/auth/local-token 免密取令牌，手机等局域网设备必须用
    # ACCESS_PASSWORD 登录。access_password 为空而 require_auth=true 会在启动时直接失败
    # （fail closed），不会出现「以为开了鉴权、实际裸奔」。
    require_auth: bool = False
    access_password: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = Field(default=12 * 3600, ge=60, le=30 * 24 * 3600)
    # 正式部署：由后端直接托管 frontend/dist 静态产物（不再依赖 Vite 开发服务）
    serve_static: bool = False
    static_dir: str = ""
    # 会话 cookie 是否仅走 HTTPS（局域网通常无 TLS，默认关闭）
    cookie_secure: bool = False

    # ───────────── 回测复权口径（D6） ─────────────
    # 策略/指标/账户净值使用的日线口径。默认前复权（qfq）：除权除息跳空不应计入收益。
    # 涨跌停判断始终改用未复权昨收（见 app/history/limit_reference.py），因此这里
    # 改成 qfq 不会让板价判断失真。设为 none 可一键回退到改造前的行为。
    #
    # 两个开关分开是刻意的：组合回测与单标的回测是两条独立的取数链路，分开回退
    # 才能做「只退一条链路」的受控对照。两条都必须是 qfq 才满足计划 §7.4 的口径要求。
    portfolio_bars_adjust: str = "qfq"
    # 单标的回测（/api/backtests + BacktestEngine）用的同一口径
    backtest_bars_adjust: str = "qfq"

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
    def bind_is_loopback(self) -> bool:
        """监听地址是否仅本机可访问。"""
        return self.host.strip() in {"127.0.0.1", "localhost", "::1"}

    @property
    def static_path(self) -> Path:
        """正式部署的前端静态产物目录（默认 frontend/dist）。"""
        if self.static_dir:
            candidate = Path(self.static_dir)
            return candidate if candidate.is_absolute() else (PROJECT_DIR / candidate)
        return PROJECT_DIR / "frontend" / "dist"

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
