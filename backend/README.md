# A 股实时分析平台后端

> ⚠️ **免责声明**：本项目所有分析结果仅用于研究，不构成投资建议。

一个面向 A 股市场的实时分析平台后端，第一版聚焦于**实时行情监控 + 信号提醒 + 历史回测 + 模拟交易**，**禁止真实下单**。

## 功能特性

- A 股实时行情监控（东方财富 / 腾讯 / AKShare / QMT 多源故障转移，免费源约 3 秒轮询自选股）
- 全市场股票池（东方财富 / BaoStock / AKShare，point-in-time 不可变交易日快照）
- 东方财富板块行情、资金流与数据中心（龙虎榜、大宗交易、融资融券、沪深港通、机构调研等）
- 东方财富涨停板情绪池（涨停 / 跌停 / 炸板 / 强势 / 次新，含封板资金、连板数与所属行业）
- 自选股管理
- 技术指标计算（MA / EMA / MACD / RSI / 成交量均线 / 涨跌幅 / 振幅 / 量比）
- 策略信号生成（MA 交叉、放量突破、RSI 超买超卖、MACD 金叉死叉）
- 历史回测与组合回测（T+1、涨跌停、停牌、手续费、滑点、无未来数据）
- 全市场历史入库（日线本地缓存，东财 → 新浪 → BaoStock 回退链）
- 智能选股与样本外评估（多因子排名、RankIC、换手率、模拟调仓草案）
- 研究候选排序（全市场一次横扫，8 个触发条件打分 + 证据门禁；未通过样本外验证时禁止推荐表述）
- 模拟交易（含风控：仓位、日亏损、回撤、T+1、信号幂等）
- 每日流水线（股票池 → 历史入库 → 选股 → 模拟调仓，可恢复、可选自动调度）
- QMT 实盘通道（只读对账、一次性令牌、固定风险确认，默认关闭）
- WebSocket 实时推送（行情 + 信号）

## 技术栈

| 组件             | 版本约束                      |
| -------------- | ------------------------- |
| Python         | 3.11+                     |
| FastAPI        | >=0.115,<1.0              |
| SQLAlchemy     | >=2.0,<3.0                |
| Pydantic       | >=2.8,<3.0                |
| Alembic        | >=1.13,<2.0               |
| APScheduler    | >=3.10,<4.0               |
| pandas / numpy | 兼容范围见 requirements.txt    |
| 数据库            | 开发 SQLite / 生产 PostgreSQL |

## 项目结构

```
a-stock-platform/
├── backend/
│   ├── app/
│   │   ├── main.py                 # 应用入口
│   │   ├── config.py               # 配置
│   │   ├── logging_config.py       # 日志（含脱敏）
│   │   ├── validation.py           # 股票代码校验
│   │   ├── database/               # ORM 模型与会话
│   │   ├── api/                    # REST 接口
│   │   ├── market_data/            # 行情数据源
│   │   ├── realtime/               # 缓存/调度/WebSocket/信号引擎/当日分时/买点雷达
│   │   ├── indicators/             # 技术指标
│   │   ├── strategies/             # 交易策略
│   │   ├── backtest/               # 回测引擎
│   │   ├── paper_trading/          # 模拟交易
│   │   └── risk/                   # 风控
│   ├── migrations/                 # Alembic 迁移
│   ├── tests/                      # pytest 测试
│   ├── requirements.txt
│   └── .env.example
├── scripts/
│   ├── start_backend.ps1           # Windows 一键启动
│   └── test_backend.ps1            # Windows 一键测试
└── outputs/                        # 回测输出目录
```

## 快速开始

### Windows（一键启动）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_backend.ps1
```

服务启动后访问：<http://127.0.0.1:8000>  
API 文档：<http://127.0.0.1:8000/docs>

### 手动启动

```bash
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 运行测试

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test_backend.ps1
```

或手动：

```bash
cd backend
.venv\Scripts\python -m pytest -v
```

## 环境变量

复制 `backend/.env.example` 为 `backend/.env`，关键配置：

| 变量                        | 说明                  | 默认值                      |
| ------------------------- | ------------------- | ------------------------ |
| `HOST` / `PORT`           | 服务监听地址与端口           | `127.0.0.1` / `8000`     |
| `DEBUG` / `LOG_LEVEL`     | 调试开关与日志级别           | `false` / `INFO`         |
| `DATABASE_URL`            | 数据库连接串              | `sqlite:///./a_stock.db` |
| `AUTO_CREATE_TABLES`      | 启动时自动建表（仅测试/演示）     | `false`                  |
| `CORS_ORIGINS`            | 允许跨域的来源（逗号分隔）       | `http://localhost:3000,http://127.0.0.1:3000` |
| `E2E_USE_MOCK`            | 测试/CI/managed E2E 强制改用 mock 数据源 | `false` |
| `MARKET_PROVIDERS`        | 数据源优先级（tdx/eastmoney/tencent/akshare/qmt/mock） | `tdx,eastmoney,tencent,akshare` |
| `UNIVERSE_PROVIDERS`      | 股票池主数据源优先级（eastmoney/baostock/akshare） | `eastmoney,baostock,akshare` |
| `EASTMONEY_UNIVERSE_TIMEOUT_SECONDS` | 东方财富股票池整轮超时（秒） | `90` |
| `BAOSTOCK_UNIVERSE_TIMEOUT_SECONDS` | BaoStock 股票池超时（秒） | `300` |
| `BAOSTOCK_BJ_SUPPLEMENT_TIMEOUT_SECONDS` | AKShare BJ 子源超时（秒） | `60` |
| `AKSHARE_UNIVERSE_TIMEOUT_SECONDS` | AKShare 股票池超时（秒） | `30` |
| `UNIVERSE_MAX_RETRIES`    | 股票池同步每个 provider 的重试次数 | `2` |
| `UNIVERSE_BACKOFF_BASE_MS` | 股票池重试退避基数（毫秒）     | `50`                     |
| `QUOTE_POLL_INTERVAL`     | 轮询间隔（秒）             | `3`                      |
| `ROLLING_WINDOW_SIZE`     | 滚动窗口大小              | `300`                    |
| `SIGNAL_COOLDOWN_SECONDS` | 信号冷却时间              | `60`                     |
| `MAX_QUOTE_AGE_SECONDS`   | 行情时效阈值（超过则拒绝成交）     | `15`                     |
| `HISTORY_INGEST_MAX_CONCURRENCY` | 全市场历史入库后台并发 | `4` |
| `TENCENT_API_KEY`         | 腾讯行情密钥（免费接口一般无需）   | `YOUR_API_KEY`           |
| `AKSHARE_API_KEY`         | AKShare 密钥（免费，一般无需）  | `YOUR_API_KEY`           |
| `REAL_TRADING_ENABLED`    | 是否启用 QMT 实盘通道       | `false`                  |
| `QMT_ACCOUNT_TYPE`        | QMT 账户类型             | `STOCK`                  |
| `QMT_CALL_TIMEOUT_SECONDS` | QMT SDK 调用超时（秒）     | `10`                     |
| `DAILY_PIPELINE_AUTO_LOOKBACK_DAYS` | 每日流水线历史入库回看天数 | `365` |
| `LIMIT_UP_SENTIMENT_AUTO_ENABLED` | 收盘后自动抓取涨停板情绪池 | `true` |
| `LIMIT_UP_SENTIMENT_AUTO_HOUR` / `LIMIT_UP_SENTIMENT_AUTO_MINUTE` | 自动抓取时刻（北京时间） | `16` / `10` |
| `LIMIT_UP_SENTIMENT_BACKFILL_DAYS` | 手动回补默认回溯自然日数 | `20` |
| `SCREENER_TDX_POOL_SIZE` | 买点雷达行情并行连接数（0 表示只用 ProviderManager 回退） | `6` |
| `RATE_LIMIT_PER_MINUTE`   | 单 IP 每分钟最大请求数（0 关闭） | `300`                    |
| `WS_MAX_SUBSCRIPTIONS`    | 单 WebSocket 最大订阅数   | `200`                    |

上表为常用配置；完整变量（含 `QMT_USERDATA_PATH`、`QMT_ACCOUNT_ID`、`LIVE_TRADING_API_TOKEN`、
`LIVE_RECONCILE_INTERVAL_SECONDS`、`DAILY_PIPELINE_AUTO_*`、`RISK_*` 等）见
`backend/.env.example`，其中每一项都带有用途说明。

## 数据库迁移

项目使用 [Alembic](https://alembic.sqlalchemy.org/) 管理 schema 变更：

```bash
# 升级到最新版本
cd backend
.venv\Scripts\alembic upgrade head

# 降级一个版本
.venv\Scripts\alembic downgrade -1

# 完整回滚
.venv\Scripts\alembic downgrade base
```

生产环境**不会**自动建表，必须先跑迁移；只有把 `AUTO_CREATE_TABLES=true`（测试/演示场景）才会在启动时 `Base.metadata.create_all`。

迁移脚本位于 `backend/migrations/versions/`，已包含：

- `0001_initial.py`：全部 10 张表
- `0002_paper_trade_idempotency.py`：为 `paper_trades` 加 `(account_id, signal_id)` UNIQUE 约束，从数据库层杜绝同一信号重复成交
- `0003_trading_calendar.py`：交易日历表
- `0004_historical_bars.py`：历史行情本地缓存表（含复权类型）
- `0005_backtest_tasks.py`：回测任务可恢复字段（进度/幂等键/时间戳/错误摘要）
- `0006_paper_order_state_machine.py`：订单状态机与盈亏字段（拒绝原因、已实现盈亏）

## 健康检查

按 K8s 探针语义拆分：

| 端点                      | 用途                  | 失败含义      |
| ----------------------- | ------------------- | --------- |
| `GET /api/health/live`  | 进程存活探针，永远 200       | 进程崩溃      |
| `GET /api/health/ready` | 就绪探针：DB 可用 + 调度器已启动 | 503（不接流量） |
| `GET /api/health`       | 详细状态：DB、调度器、各数据源健康  | 监控/排障用    |

## API 概览

| 方法       | 路径                                      | 说明                             |
| -------- | --------------------------------------- | ------------------------------ |
| GET      | `/api/health`                           | 详细健康检查（DB + 调度器 + 数据源）         |
| GET      | `/api/health/live`                      | 进程存活探针（K8s liveness）           |
| GET      | `/api/health/ready`                     | 就绪探针（K8s readiness，503 表示不接流量） |
| GET      | `/api/metrics`                          | 可观测性指标（数据源/WebSocket/任务）        |
| GET      | `/api/market/providers`                 | 数据源状态                          |
| GET      | `/api/market/indices`                   | 可做基准的指数列表（本地登记表，不依赖外部数据源）    |
| GET      | `/api/market/session`                   | 今天是否交易日 / 最近交易日 / 下一交易日（`day=` 可回看历史；日历为空或覆盖不足返回 503） |
| GET      | `/api/market/boards`                    | 东方财富板块行情（行业/概念/地域）             |
| GET      | `/api/market/boards/{code}/constituents` | 板块成分股                          |
| GET      | `/api/market/fund-flow/boards`          | 板块资金流排行                        |
| GET      | `/api/market/fund-flow/stocks`          | 个股资金流排行                        |
| GET      | `/api/market/fund-flow/stocks/{symbol}` | 个股资金流历史                        |
| GET      | `/api/market/datacenter`                | 数据中心数据集目录与字段说明                 |
| GET      | `/api/market/datacenter/{dataset}`      | 数据中心数据集查询（支持日期区间与股票代码）        |
| GET      | `/api/market/datacenter/dragon-tiger/{symbol}/seats` | 龙虎榜买入/卖出席位明细          |
| GET      | `/api/market/limit-up`                  | 涨停板情绪池目录（涨停/跌停/炸板/强势/次新）    |
| GET      | `/api/market/limit-up/{pool}`           | 单个情绪池快照（支持 `trade_date` / `limit` / `page` / `order`） |
| GET      | `/api/market/limit-up/sentiment`        | 涨停板情绪因子历史曲线（封板率 / 连板高度 / 连板梯队） |
| POST     | `/api/market/limit-up/capture`          | 抓取并落库情绪池（幂等；`backfill_days` 一键回补） |
| GET      | `/api/indicators`                       | 技术指标目录（24 个序列的中文名与 key）       |
| GET      | `/api/indicators/{symbol}`              | 单标的技术指标序列（MA/EMA/MACD/BOLL/KDJ/ATR/OBV/CCI/WR…） |
| GET      | `/api/realtime/picks`                   | 研究候选排序（全市场扫描，返回候选与证据门禁） |
| GET      | `/api/realtime/picks/validation`        | 候选排序样本外验证的状态与最近一次报告 |
| POST     | `/api/realtime/picks/validation`        | 启动样本外验证（历史逐日重放，后台执行，立即返回 202） |
| GET      | `/api/quotes/{symbol}`                  | 单只行情                           |
| POST     | `/api/quotes/batch`                     | 批量行情                           |
| GET      | `/api/quotes/{symbol}/intraday`         | 当日分时曲线（价格/均价/分钟量额；`limit` 30-2000，实时窗口与数据源分钟线按分钟合并） |
| GET/POST | `/api/watchlists`                       | 自选股列表                          |
| POST     | `/api/watchlists/{id}/symbols`          | 添加自选股                          |
| DELETE   | `/api/watchlists/{id}/symbols/{symbol}` | 删除自选股                          |
| GET      | `/api/signals`                          | 信号列表                           |
| GET      | `/api/strategies`                       | 策略列表                           |
| POST     | `/api/strategies/{id}/enable`           | 启用策略                           |
| POST     | `/api/strategies/{id}/disable`          | 禁用策略                           |
| POST     | `/api/backtests`                        | 创建回测任务（异步队列，支持幂等键）          |
| GET      | `/api/backtests`                        | 回测任务列表                         |
| GET      | `/api/backtests/{id}`                   | 回测任务详情（进度/结果/错误摘要）            |
| POST     | `/api/backtests/{id}/cancel`            | 取消回测任务                         |
| POST     | `/api/portfolio-backtests`              | 创建组合回测任务（异步队列，支持幂等键）         |
| GET      | `/api/portfolio-backtests`              | 组合回测任务列表                        |
| GET      | `/api/portfolio-backtests/{id}`         | 组合回测任务详情（进度/结果/错误摘要）          |
| POST     | `/api/portfolio-backtests/{id}/cancel`  | 取消组合回测任务                        |
| GET/POST | `/api/paper/accounts`                   | 模拟账户                           |
| POST     | `/api/paper/orders`                     | 模拟下单                           |
| GET      | `/api/paper/orders`                     | 委托单列表（含状态/拒绝原因）               |
| POST     | `/api/paper/orders/{id}/cancel`         | 取消委托                           |
| GET      | `/api/paper/positions`                  | 持仓（含已实现盈亏）                     |
| GET      | `/api/paper/trades`                     | 成交记录（含已实现盈亏）                   |
| POST     | `/api/paper/accounts/{id}/settle`       | 日终结算（T+1 解冻 + 资产快照）            |
| GET      | `/api/paper/accounts/{id}/assets`       | 账户资产与盈亏                        |
| POST     | `/api/universe/sync`                    | 同步全市场股票池并原子生成交易日快照           |
| GET      | `/api/universe/status`                  | 股票池数据源健康与最近一次同步信息            |
| GET      | `/api/universe/snapshots`               | 历史股票池快照列表                        |
| GET      | `/api/universe/snapshots/{day}/members` | 查询不可变的历史交易日成员（`status=included/excluded/all`，默认仅可交易） |
| POST     | `/api/universe/filter`                  | 按交易日筛选可交易成员                    |
| GET/POST | `/api/daily-pipeline/runs`              | 创建/查询可恢复的每日股票池→历史入库→选股→模拟调仓流水线 |
| GET      | `/api/daily-pipeline/runs/{id}`         | 单条每日流水线任务详情（进度/阶段/错误摘要）      |
| GET      | `/api/daily-pipeline/schedule`          | 自动调度配置与下次触发时间（只读）              |
| POST     | `/api/history-ingest`                   | 创建指定交易日股票池的全市场历史入库任务          |
| GET      | `/api/history-ingest`                   | 历史入库任务列表                         |
| GET      | `/api/history-ingest/{id}`              | 历史入库任务详情（进度/覆盖率/失败明细）          |
| POST     | `/api/history-ingest/{id}/cancel`       | 取消排队中或运行中的历史入库任务               |
| POST     | `/api/history-adjust`                   | 启动全市场前复权日线回填（通达信除权除息 + 本地未复权日线，后台执行，立即返回 202） |
| GET      | `/api/history-adjust`                   | 前复权回填任务状态与最近一次汇总               |
| GET      | `/api/selections`                       | 历史选股运行列表                         |
| POST     | `/api/selections/rank`                  | 基于历史快照生成并保存多因子候选排名           |
| GET      | `/api/selections/{run_id}`              | 读取可复现的选股运行及因子值                 |
| POST     | `/api/selections/{run_id}/evaluate`     | 按 T+1 开盘和未来 N 日收盘验证候选收益         |
| GET      | `/api/selections/evaluations/summary`   | 汇总样本外收益、胜率、RankIC 与换手率          |
| GET/POST | `/api/paper/rebalance-plans`            | 查询/生成选股驱动的模拟调仓草案                |
| POST     | `/api/paper/rebalance-plans/{id}/execute` | 用户确认后执行模拟调仓                     |
| POST     | `/api/paper/rebalance-plans/{id}/cancel`  | 取消未执行的模拟调仓草案                    |
| GET      | `/api/live/status`                       | QMT 实盘只读就绪状态                      |
| GET/POST | `/api/live/rebalance-plans`              | 查询/生成基于真实账户资产的实盘草案             |
| POST     | `/api/live/rebalance-plans/{id}/approve` | 固定风险确认文本换取 5 分钟一次性令牌            |
| POST     | `/api/live/rebalance-plans/{id}/execute` | 携带一次性令牌最终提交 QMT 限价委托             |
| POST     | `/api/live/rebalance-plans/{id}/reconcile` | 只读刷新 QMT 当日委托状态（不会补单/撤单）      |
| WS       | `/ws/quotes`                            | 行情推送                           |
| WS       | `/ws/signals`                           | 信号推送                           |

## 行情数据源

| 数据源           | 用途     | 说明                                                         |
| ------------- | ------ | ---------------------------------------------------------- |
| QMT/xtdata    | 正式实时行情 | 可选，推送模式，延迟 <1s                                             |
| 通达信（TDX）     | 低延迟实时行情 | 默认首选轮询源：`tdxpy` 直连通达信行情服务器，覆盖沪 / 深 / 北交所，实测单批 31–39ms、日线 266 根 43ms |
| 东方财富（Eastmoney） | 免费轮询 + 股票池 | 默认首选：实时行情 `ulist.np`、分时 `trends2`、日/周/月 K 线 `kline`（K 线受本机路径限流，见「已知限制」8）；股票池 `clist/get` |
| 东方财富数据中心   | 横截面研究数据 | 龙虎榜与席位、大宗交易、融资融券、沪深港通、机构调研、股东户数、限售解禁、业绩预告、分红送配、高管持股变动、股权质押比例、可转债 |
| 东方财富涨停板行情 | 情绪与打板数据 | `push2ex.eastmoney.com`：涨停/跌停/炸板/强势/次新 5 个池，含封板资金、连板数、封板时间 |
| 腾讯行情          | 免费轮询   | 备援实时行情；东财被限流熔断时接管                                          |
| AKShare       | 历史数据   | 东财 / 新浪双通道（东财通道与 EastmoneyProvider 同源）                     |
| BaoStock      | 股票池备援  | 按交易日成员、上市日期与当日停牌状态；支持历史交易日快照，东财股票池的回退目标              |
| Mock          | 演示/测试  | 仅在 `MARKET_PROVIDERS=mock` 时显式启用                           |

数据源按 `MARKET_PROVIDERS` 优先级故障转移，严禁静默混合来源。全部失败时返回缓存数据并标记 `is_stale=true`。Mock 不参与真实数据源的默认兜底，避免把随机价格误认为真实行情。

东财接口对同一 IP 有突发限流：短时间请求过多会直接断连（`RemoteDisconnected`）。因此
`EastmoneyProvider` 只做 2 次尝试，并在连续 3 次失败后熔断 5 分钟（期间行情/历史请求立即返回空，
由管理器回退到腾讯），到期自动恢复。`akshare` 与 `eastmoney` 的 `upstream` 同为 `eastmoney`，
ProviderManager 在一次请求内不会对同一上游重复请求。

### 通达信 TDX（`MARKET_PROVIDERS` 默认首选）

`TdxProvider`（`app/market_data/tdx_provider.py`）用 [tdxpy](https://github.com/mootdx/tdxpy)
直接连通达信行情服务器（TCP 7709），比走 HTTP 的东财/腾讯快一个量级，实测：

| 场景                | 耗时            |
| ----------------- | ------------- |
| 单批行情（含首次探活）       | 246ms         |
| 稳态单批行情（80 只/批）    | **31–39ms**   |
| 日线 266 根           | 43ms          |
| 5 分钟线 240 根        | 50ms          |

设计要点：

- tdxpy 是同步库，统一用 `asyncio.to_thread` 调用，并用 `asyncio.Lock` 串行化，
  避免多协程并发穿过同一个连接。
- 维护主机池 + 探活 + 冷却：每台主机 300 秒 TTL 内复用，失败进入 120 秒冷却；
  **全部主机失败时返回 `{}` 而不是抛异常**，交给 ProviderManager 正常回退。
- 市场号映射：沪 = 1、深 = 0、**北交所 = 3**（`4` / `8` / `920` 号段都在内）。
  北交所的请求市场号是 3，而服务端回包里的 `market` 字段是 2，这一处不对称是
  实测出来的（传 2 会返回空）。沪深京混批可以在同一次 `get_security_quotes`
  里拿全，不会被拆成两次请求。
- 单位已核对：实时行情与**日线**K 线的 `vol` 单位是手（×100 转股）、`amount` 已是元、
  `servertime` 补当天日期；**分钟线（1/5/15/30/60 分钟）的 `vol` 已经是股**，不能再乘 100
  （实测 600000：日线 653,272 手，同日 1 分钟线合计 65,327,300 股，两者成交额完全一致）。
  协议会把「无数据」解码成 `5.877e-39` 这类非规格化小数，出口统一归零。
- 单次请求按 80 只分块；K 线按 800 根/页分页，最多 40 页。
- 服务端掐断连接（`WinError 10038` / 接收数据异常）是常态：此时换一条新连接重试一次，
  仍然失败才返回空，避免把「连接被掐」误判成「这只标的没有数据」而掉进慢速回退链。

不需要 TDX 时把 `MARKET_PROVIDERS` 里的 `tdx` 去掉即可，其余逻辑无感知。

### 历史日线回退链

日线入库（`HistoricalDataService`）在实时数据源之外还有一条独立回退链，按顺序尝试：

1. AKShare 东财通道 `stock_zh_a_hist`（含 ProviderManager 的 akshare provider）；
2. AKShare 新浪通道 `stock_zh_a_daily`；
3. BaoStock `query_history_k_data_plus`（显式设置 20 秒 socket 超时，避免 `next()` 无限阻塞）。

判定规则：任一源返回非空即采用，条目的 `HistoricalBar.source` 记录实际来源（`akshare` /
`akshare_sina` / `baostock`）；**全部源抛错**才向上报错，由 `get_history` 回退本地缓存；
**全部源返回空**视为该标的无数据（停牌 / 退市），不报错。

实测东财通道（`push2his.eastmoney.com`）经常 `RemoteDisconnected`，回退链是历史入库能跑通的关键。
若 `MARKET_PROVIDERS` 已包含 `akshare` 或 `eastmoney`，回退链会跳过第一个东财源，避免对同一上游重复请求
（全市场入库时这只重复调用会让耗时翻倍）。

实机验收（9 只样本：沪 / 深 / 创业板 / 科创板 / 北交所各覆盖）：东财通道每次失败，新浪通道
全部命中，批次 `succeeded`、`coverage_ratio=1.0`、沪深京各 52 根日线。

#### 全市场入库吞吐（2026-09 实测）

回补近一年日线（全市场 5849 只）时有两处决定性开销，都已修掉：

- **确定性空结果不再退避重试**：所有源都明确返回空（停牌 / 退市 / 未上市）只打一轮。
  这类标的不是瞬时故障，但旧策略仍按 2s + 4s 退避重试 3 次，每只白等约 16 秒；
  只有「源抛异常」才继续重试。
- **假期缺口合并**：按交易日历定位缺口时，相邻缺失交易日跨度 ≤ 30 天即并入同一段。
  整年回补会被国庆（9 天）与春节（11 天）拆成 4 段，合并后单只标的网络调用降到 1 次。

两项合计把实测吞吐从 6 只/分提升到 171 只/分（约 28×），5849 只约 30 分钟跑完。
取不到数据的标的（退市 / 长期停牌）会进入 `failed_symbols` 并计入 `failed_count`，
不会静默跳过，也不会拖慢整批。

### 东方财富股票池（`universe` 首选源）

`EastmoneyUniverseProvider` 走 `clist/get`（`fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048`），
单页上限 100 条，60 页并发（信号量 4）实测约 3 秒拉完；一次同步 5910 条（沪 2467 / 深 3091 /
北交所 352），并带出所属行业（`f100` → `SecurityRecord.sector`）、上市日期与名称。
经上市状态排除后落成 5550 只「可交易」（沪 2311 / 深 2896 / 北交所 343）。

两点约束：

- 只提供**当日**全市场快照；请求历史日期会显式抛错（`不支持历史快照`），由调用方回退 BaoStock，
  避免把当前成分名单回填成历史快照而产生幸存者偏差。
- 北交所旧号段（`43xxxx` / `83xxxx`）在 `clist` 中会混入可转债，已按代码段与名称（含「债」「转」）过滤。

#### 上市状态（`f292`）与「可交易」口径

`clist` 返回的是「东财登记过的全部 A 股代码」，其中包含大量已退市 / 停牌 / 待上市的陈旧记录，
不能直接当成可交易池。因此多取一个 `f292`（上市状态码），实测全量 5910 条只出现 4 个取值，
并逐类用通达信日线交叉验证（正常股最后一根日线 = 最近交易日；停牌股停在停牌前；退市 / 待上市
整段无日线）：

| `f292` | `Security.trading_status` | 含义 | 实测条数 |
| ------ | ------------------------- | ---- | -------- |
| 13     | `active`                  | 正常交易 | 5550 |
| 6      | `suspended`               | 停牌（含停牌重组） | 12 |
| 7      | `delisted`                | 已退市 | 339 |
| 9      | `pending_listing`         | 已分配代码、尚未挂牌 | 9 |

未列出的取值一律按 `active` 处理 —— 宁可多留，也不静默剔除真实标的。
名称启发式（`退市…` / `…退` / `PT…`）优先于 `f292`：退市整理期的简称一定带「退」，
而状态码偶尔晚一拍。`ExclusionEngine` 把 `delisted` / `suspended` / `pending_listing`
分别落成 `delisted` / `suspended` / `not_listed_yet` 三种 `exclude_reason`。

**「可交易」= 通过排除规则的成员，不等于「当天能下单」**：休市日池子不会变成不可交易，
「今天能不能成交」由 `GET /api/market/session` 单独回答（前端在「股票池」页顶部标注
「今日休市」）。快照交易日只会落在交易日，非交易日同步经
`TradingCalendar.last_trading_day_on_or_before()` 归一到最近一个交易日。

### 东方财富数据中心（`/api/market/datacenter*`）

数据中心走 `datacenter-web.eastmoney.com/api/data/v1/get`，以 `reportName` 选报表。12 个数据集共用
一套声明式字段映射（输出名 / 东财列名 / 解析方式 / 中文表头），前端表头直接由
`GET /api/market/datacenter` 的返回驱动，新增数据集无需改前端。

| dataset             | 报表                                   | 说明                       |
| ------------------- | ------------------------------------ | ------------------------ |
| `dragon-tiger`      | `RPT_DAILYBILLBOARD_DETAILSNEW`      | 龙虎榜每日详情（含上榜后 1/5/10/20 日涨跌幅） |
| `dragon-tiger-seats` | `RPT_BILLBOARD_DAILYDETAILSBUY/SELL` | 龙虎榜买卖席位（专用下钻接口）          |
| `block-trade`       | `RPT_DATA_BLOCKTRADE`                | 大宗交易明细                   |
| `margin`            | `RPTA_WEB_RZRQ_GGMX`                 | 融资融券个股明细                 |
| `northbound`        | `RPT_MUTUAL_DEAL_HISTORY`            | 沪深港通各通道成交与资金             |
| `org-survey`        | `RPT_ORG_SURVEYNEW`                  | 机构调研记录                   |
| `holder-number`     | `RPT_HOLDERNUMLATEST`                | 股东户数（最新一期）               |
| `restricted-release` | `RPT_LIFT_STAGE`                    | 限售解禁（含未来日期，建议配合 `date_from`） |
| `earnings-forecast` | `RPT_PUBLIC_OP_NEWPREDICT`           | 业绩预告                     |
| `dividend`          | `RPT_SHAREBONUS_DET`                 | 分红送配                     |
| `executive-hold`    | `RPT_EXECUTIVE_HOLD_DETAILS`         | 高管持股变动（职务、均价、变动金额与变动比例）  |
| `pledge`            | `RPT_CSDC_LIST`                      | 股权质押比例（质押股数/市值按万股、万元）    |
| `convertible-bond`  | `RPT_BOND_CB_LIST`                   | 可转债发行与条款（评级、规模、票面利率、转股价） |

实现细节：

- `date` / `date_from` / `date_to` / `symbol` 先经严格正则校验再拼进东财 `filter` 表达式，
  不会把用户输入原样送进远端查询串。
- 东财用 `code=9201` 表示「查询成功但数据为空」，按空列表返回；其余失败码（9501 参数错误、
  9701 数据繁忙）触发主机切换。
- `datacenter-web` 与 `datacenter` 两台主机互为备份，复用行情侧的 `EastmoneyHostPool`。
- 金额统一换算为元：沪深港通报表的金额列东财以**百万元**计（沪股通 `142256.09` ≈ 1422.6 亿元，
  与十大成交股合计约 171 亿元一致），服务端乘 `1e6` 后返回；`HOLD_MARKET_CAP` 本身是元。
- 上游若改名或删列，解析时会立即抛错（`EastmoneyDataError`），不会静默返回一堆 0。
- 按上游口径返回、不做二次换算的数据集：`pledge` 的质押股数是**万股**、质押市值是**万元**
  （实查 000001 为 2438.48 万股 / 28627.76 万元，折合股价 11.74 元，与当日行情一致）；
  `convertible-bond` 的发行规模是**亿元**。可转债报表没有可靠的逐行交易日，因此不支持
  `date` 过滤，只按转债代码过滤；赎回/回售条款原文长达数百字，未列入表格。

### 东方财富涨停板情绪池（`/api/market/limit-up*`）

涨停板行情走 `push2ex.eastmoney.com`，共 5 个池：涨停股池 `getTopicZTPool`、
跌停股池 `getTopicDTPool`、炸板股池 `getTopicZBPool`、强势股池 `getTopicQSPool`、
次新股池 `getTopicCXPool`。`GET /api/market/limit-up` 返回池目录与字段声明（前端表头据此渲染），
`GET /api/market/limit-up/{pool}` 返回某池的最近交易日快照，支持 `limit`（≤200）、`page`
、`order`（排序字段由各池声明，如涨停池按首次封板时间升序、跌停池按封单资金降序）
与 `trade_date`（显式指定交易日，见下）。

口径与局限：

- 价格字段是「元 × 1000」（`13880` = 13.88 元），已逐只与腾讯行情核对（000993 13.88 /
  002161 8.04 / 002790 9.31）。上市首日等「无涨跌幅限制」场景上游给 `1e9` 占位，
  超出合理价格区间时返回 `null`，不会显示成 1000000.00 元。
- `fbt` / `lbt` 是 HHMMSS 整数（`92500` → `09:25:00`）；`zttj` 是 `{days, ct}` 对象，
  统一输出为「3天3板」。
- **`date` 参数对明细生效，但上游回传的 `qdate` 不可信**：实测传 20260908 返回 73 只、
  20260909 返回 48 只、20260911 返回 40 只涨停，股票列表完全不同，而 `qdate` 始终是
  最新交易日。因此传入 `trade_date` 时以**传入值**为准，不传才回落到北京时间当天。
- **上游只保留最近约 15 个交易日**：实测 20260824 起有数据、20260601 起返回空。
  过期数据无法再回补，历史情绪曲线必须靠每日累积。

### 涨停板情绪因子落库（`/api/market/limit-up/sentiment*`）

把上述情绪池每天落一次库，形成可直接用于回测的**市场情绪因子**：

- 汇总表 `limit_up_sentiment`：每个交易日一行，含 `seal_rate`（封板率 = 涨停 /
  (涨停 + 炸板)）、`broken_rate`、`max_streak`（最高连板）、`first_board_count` 与
  `streak_2_count` … `streak_5plus_count`（连板梯队）、`total_seal_amount`（封板资金）。
- 明细表 `limit_up_pool_member`：当日各池成员，供「打板 / 连板接力」类个股策略回测。

抓取按 `(交易日, 池)` 幂等覆盖，重复抓取不产生重复行；当天全部池无数据（非交易日或
超出上游保留窗口）时跳过，不写空行以免污染曲线。接口：

- `GET /api/market/limit-up/sentiment`：按 `start` / `end` / `limit` 返回升序曲线，
  `start` / `end` 为**实际返回数据的首尾交易日**；`latest` 恒为库内最新一行。
- `POST /api/market/limit-up/capture`：`{"trade_date": "2026-09-11"}` 抓指定日；
  `{"backfill_days": 20}` 一键回补最近 N 个自然日（自动跳过周末与无数据日期）。
- 前端「市场行情 → 情绪曲线」页签直接消费这两个接口，含封板率/涨停炸板家数与
  连板高度/连板梯队两张图。

自动累积：`LIMIT_UP_SENTIMENT_AUTO_ENABLED`（默认 `true`）开启后，后端在北京时间
每个交易日 `LIMIT_UP_SENTIMENT_AUTO_HOUR`:`LIMIT_UP_SENTIMENT_AUTO_MINUTE`（默认 16:10）
抓取当日情绪池。该任务只读行情并写本地库，不涉及任何交易动作。

## 技术指标（`/api/indicators*`）

指标计算在 `app/indicators/` 下按「一个指标一个文件」组织，与新老模块一致基于
pandas 实现（项目已有的硬依赖），口径对齐通达信：

| 指标   | 文件                | 口径要点                                    |
| ---- | ----------------- | --------------------------------------- |
| BOLL | `boll.py`         | 中轨 MA(period)，上下轨 ± `num_std` 倍总体标准差；用 `ddof=1` |
| KDJ  | `kdj.py`          | RSV 后按 `SMA(X, N, 1)` 递推 K/D，J = 3K − 2D；无振幅时取 50 |
| ATR  | `atr.py`          | `true_range` 取三者最大，再按 Wilder 平滑          |
| OBV  | `obv.py`          | 首根记 0，收涨累加成交量、收跌累减                    |
| CCI  | `cci.py`          | AVEDEV 平均绝对偏差口径，MD 为 0 时返回 0            |
| WR   | `wr.py`           | 默认 0~100（通达信），`signed=True` 输出 −100~0     |
| 振幅   | `volume.py`       | (最高 − 最低) / 昨收 × 100；首根无昨收记 NaN            |
| 量比   | `volume.py`       | 成交量 / 过去 5 根平均成交量；窗口未满记 NaN              |

`GET /api/indicators` 返回目录（24 个序列的 key + 中文名 + 可用周期 + limit 边界），
`GET /api/indicators/{symbol}?period=daily&limit=250` 返回日期轴与各指标序列
（NaN 统一转 `null`，保留 4 位小数），以及 `latest_values` 便于前端做单值徽标。

数据源优先读本地 `historical_bars`（`adjust=none`），不足时经 ProviderManager
向上游补齐，返回体里的 `source` 标明本次实际来源（如 `tdx` / `local`）。
**该接口只做计算与展示，不改变任何既有信号逻辑。**

## 智能选股

选股服务严格使用指定交易日的 `UniverseSnapshot`，只读取该日及以前的日线，避免未来数据和当前成分股幸存者偏差。默认综合 20/60 日动量、20 日年化波动、60 日最大回撤和 20 日平均成交额；至少需要 61 根且最新行情不超过 10 天。结果连同配置哈希和数据指纹写入 `selection_runs`，同一快照、配置和数据重复执行会返回同一运行记录。

全市场一次排名要读约 127 万根日线。原先把日线 hydrate 成 ORM 实体是主要耗时
（2026-09 实测 5350 只，`cProfile` 36.9s，其中 32.3s 花在 `_load_bars` 的实体构造上）。
改为只 `SELECT symbol/trade_date/close/amount/fetched_at` 五列、用轻量 `_BarRow`
替代实体后，同一快照排名降至 **12.9s**（约 2.9×），Top 5 候选的排名、评分与全部因子
逐字段与优化前一致（已用库内历史运行做等价比对）。

样本外评估严格用下一交易日开盘作为入场价、未来第 N 根日线收盘作为退出价，并汇总多批次胜率、RankIC 与换手率。模拟调仓默认要求至少 3 个已验证批次且平均收益、RankIC 均为正；仅模拟盘可显式覆盖门槛。生成的方案不会自动成交，必须在 15 分钟内由用户点击确认，重复执行会被状态机拒绝。真实下单只通过 QMT 实盘通道进行，需先完成下述"QMT 实盘安全流程"中的确认步骤。

## 研究候选排序（`/api/realtime/picks`）

`GET /api/realtime/picks` 一次扫描整个可交易 A 股池（当前约 5550 只），返回按模型
分数排序的研究候选清单。响应中的 `evidence` 是后端证据门禁：验证缺失、运行失败、
主口径超额不为正，或仅有尚未达到多折独立发布门槛的结果时，
`recommendation_allowed` 均为 `false`，调用方不得把候选包装为买入推荐。

### 两段式流程

1. **粗筛**（向量化）并行拉全市场行情、在独立线程读本地日线，按
   `min_amount_20` / `min_price` / `max_price` / `min_change_pct` / `max_change_pct`
   过滤（默认 20 日均额 ≥ 5000 万、价格 2~1000 元、当日涨跌幅 −7%~7%）。
2. **精算**对成交额前 `refine_pool`（默认 200）只计算 RSI/KDJ/MACD/BOLL/ATR，
   命中触发器打分，产出 `top_n`（默认 10）只候选。

### 8 个触发条件（权重合计 100，`score` 即命中权重之和）

| key                  | 条件                                       | 权重 |
| -------------------- | ---------------------------------------- | -- |
| `above_ma20`         | 现价 ≥ MA20                                | 14 |
| `ma20_above_ma60`    | MA20 ≥ MA60                               | 12 |
| `momentum_positive`  | 20 日动量 > 0                               | 10 |
| `rsi_rebound`        | 35 ≤ RSI14 ≤ 65                           | 12 |
| `macd_hist_turn`     | MACD 柱状线转强且不低于 −1% 现价                     | 14 |
| `boll_pullback`      | 现价 ≤ 中轨 ×1.02 且布林位置 ≤ 0.65               | 14 |
| `stable_volatility`  | 20 日年化波动率 ≤ 45%                          | 10 |
| `volume_expand`      | 20 日量比 ≥ 1.2                             | 14 |

至少命中 `min_triggers`（默认 3）个才会进入候选。

### 模型诊断价格线与风险预算

- 模型观察区间：`现价 − 0.5×ATR14` ~ `现价 + 0.2×ATR14`
- 下行风险线：`min(参考价 − 2×ATR14, 20 日低点×0.99)`，最宽不超过参考价的 30%
- 上行情景线：`参考价 + 3×ATR14`；盈亏比 < 1 会被打上 `poor_risk_reward`
- 模型风险预算上限：`min(max_weight_pct, risk_budget_pct / 下行风险距离%) × 情绪系数`

上述字段用于研究诊断和规则复核，不是挂单、止损、目标价或仓位建议。

### 风险标签（只提示，不计分）

`near_limit_up`（距涨停 2% 以内）、`high_volatility`（ATR/现价 > 6%）、
`overbought`（RSI14 > 75）、`extended`（20 日涨幅 > 30%）、
`thin_liquidity`（20 日均额 < 2 倍门槛）、`poor_risk_reward`（盈亏比 < 1）。

### 市场情绪系数

风险预算系数取本地涨停板情绪因子的**封板率**（滞后 `sentiment_lag_days` 个交易日，默认 1），
按封板率线性映射到 `[0.3, 1]`；档位 ≥0.75 强势 / ≥0.60 健康 / ≥0.45 中性 / 其余偏弱。
本地没有情绪数据时系数取 1，并在 `sentiment.available=false` 里标注。

### 实时性与口径

- 行情：自建 `SCREENER_TDX_POOL_SIZE`（默认 6）条独立通达信连接并行分段抓取，
  失败时回退 `ProviderManager`；行情抓取与本地日线读取并行执行。
- 实测（2026-09-12，全市场 5550 只）：扫描缓存为空时约 8 秒（本次行情 2.0s +
  日线 6.8s，日线为本地库冷读），命中缓存约 3 秒。
- 指标窗口默认 120 根日线，**优先前复权**（`adjust=qfq`），库里的前复权数据缺失
  或落后于不复权时自动回退到 `adjust=none`；返回体 `bars_adjust` 会写明本次实际
  用了哪一种。与「技术指标」页 250 根口径实测偏差 RSI < 0.1、ATR < 0.1%。
- **休市不伪造当日 K 线**：行情与本地日线逐字段一致时判定为休市（`live=false`），
  按最近交易日收盘评估，`signal_day` 指向下一交易日；交易日判定见
  `GET /api/market/session`。
- 返回体 `notes` 会写清本次是休市还是盘中、股票池快照日、各阶段耗时，
  并始终带上免责声明。

行情合并规则（避免同一根 K 线被算两次）：行情 `is_stale`、价格或成交量为 0 时直接忽略；
成交量差在 1 手（100 股）以内、且成交额与价格一致时，视为序列已包含当天，原样返回；
否则同日替换、新日追加。

参数非法（如 `lookback_days` 越界、`min_price ≥ max_price`）返回 422；
本地交易日历、股票池或行情全部缺失时返回 503。

### 样本外验证（`/api/realtime/picks/validation`）

雷达的触发器与权重是人工设定的，**必须先证明它在样本外有超额收益，再决定能不能用**。
`POST` 把同一套规则放到历史上逐日重放：

- 打分只用 `<= t` 的日线，且直接复用生产的 `_pass1_factors` / `_passes_filters` /
  `_prescreen_key` / `_refine`，不重写一份「看起来一样」的规则（重写会掩盖真实差异）；
- 成交按 `t+1` 开盘买入、`t+1+k` 收盘卖出，扣除佣金 / 印花税 / 滑点（默认往返 0.21%）；
- 基准 = 同日通过同一套硬性过滤的全部标的等权、同规则收益，用来剥离市场 beta；
- 次日停牌或退市（没有 K 线）、以及「次日开盘价即触及涨停价」都按买不到处理；
- 按等权评估 `top_n`，不叠加情绪仓位系数（仓位系数是风险预算，不是收益预测）。

`control=none` 按打分选股；`control=random` 取同池等量的确定性随机样本，用来衡量
**噪声底噪**（其超额收益应接近 0，明显偏离说明验证脚手架本身可疑）；`control=worst`
取同池打分最差的同样数量标的，用来检查分数是否具备区分度。

计算在后台线程执行（60 个评估日实测约 100~125 秒），接口返回 202 与任务状态，前端轮询
`GET /api/realtime/picks/validation`。报告只缓存在进程内，重启后需要重算。

**实测结论（2026-09-12；评估窗口 2026-06-04 ~ 2026-08-27，60 个评估日，日均同池 3854 只，
日线为前复权 `adjust=qfq`）**

| 组 | 1 日 | 3 日 | 5 日 | 10 日 |
| --- | --- | --- | --- | --- |
| 雷达 top 10 超额 | +0.07%（t=0.19） | −0.45%（t=−1.16） | −1.11%（t=−2.24） | −1.74%（t=−2.74） |
| 随机对照 10 只超额 | −0.00%（t=−0.15） | +0.14%（t=0.45） | +0.05%（t=0.01） | +0.18%（t=0.24） |
| 最差 10 只超额 | −0.52%（t=−1.22） | −0.35%（t=−0.53） | −0.24%（t=−0.36） | +0.69%（t=0.69） |

随机对照组接近 0，说明验证脚手架没有系统性偏差；雷达候选在这段历史上**仍然跑输**
同池等权（3 / 5 / 10 日都为负），只是幅度比不复权口径小。

**复权口径对结论的影响（同一窗口、同一套规则，只换日线口径）**

| 口径 | 雷达 3 日 | 雷达 10 日 | 最差 10 只 10 日 |
| --- | --- | --- | --- |
| 不复权 `none` | −0.57% | −2.30% | **+2.38%（t=2.83）** |
| 前复权 `qfq` | −0.45% | −1.74% | +0.69%（t=0.69） |

关键差别在「最差组」：不复权口径下它比基准明显更好，看起来像**排序方向被反转**；
换成前复权后它回到基准附近，说明此前那 2.38% 主要来自**除权除息造成的假跳空**，
而不是分数里含反向信息。这条对照也是「验证必须用复权价」的直接证据。

这意味着：

- **不要把当前这套触发器用于真实资金**；页面顶部的验证面板会按结论给出明确提示；
- 结论只覆盖单一窗口，并存在两类已量化说明的偏差：窗口重叠（t 值偏高）与幸存者偏差
  （本地只有一份「当前」股票池快照），接口返回的 `caveats` 会逐条列出；
- 下一步应补齐更长历史（当前只有 1 年）与 point-in-time 股票池，在多个窗口上重新验证
  后再判断。

### 权重 / 阈值寻优（`scripts/param_search.py`）

上面的接口只能测**当前**那一套权重。要回答「换个权重 / 阈值能不能跑赢」，用仓库根目录的
`scripts/param_search.py`：它先把同一套规则在历史上逐日重放一次并缓存（171 个评估日实测约
6 分钟，缓存约 15MB，落在 `.cache/`，已在 .gitignore 里），然后在**训练段**上搜索权重与阈值，
再在**留出段**上只跑一次，最后用 30 组随机对照给出「同样本量下纯噪声能刷出多高的 t 值」。
中间的隔离带按最长持有期留出，避免持有期重叠造成的信息泄漏；分数与排序键直接复用生产口径
（`score = Σ weights[命中条件]`、按 `(-score, -amount_20, symbol)`），不重写一份「看起来一样」
的规则。引擎在 `app/realtime/param_search.py`，纯函数有单测（`tests/test_param_search.py`）。

```powershell
cd <项目根>
backend\.venv-311\Scripts\python.exe scripts\param_search.py            # 训练 100 天 / 留出 60 天
backend\.venv-311\Scripts\python.exe scripts\param_search.py --reuse    # 复用缓存，只重跑网格
```

**实测（2026-09-12；训练 2025-12-15 ~ 2026-05-19，隔离带 11 天，留出 2026-06-04 ~ 2026-08-27，
主口径 3 日，前复权 `qfq`）**

| 方案（3 日超额） | 训练段 | 留出段 |
| --- | --- | --- |
| 生产权重（现役） | −0.36%（t=−1.84） | −0.45%（t=−1.16） |
| 等权 | −0.15%（t=−0.90） | −0.52%（t=−1.25） |
| 训练段最优（按训练段单因子超额加权，top 5） | **+0.83%（t=1.92）** | **−1.15%（t=−1.42）** |
| 反向加权（只加权训练段为负的条件） | −0.61%（t=−3.10） | −0.26%（t=−0.68） |
| 只用池内可区分的条件等权 | −0.14%（t=−0.85） | −0.07%（t=−0.19） |
| 随机对照（30 个盐值，同池等量） | t ∈ [−2.13, 1.41] | t ∈ [−2.22, 1.81] |
| 最差 10 只对照 | +0.11%（t=0.27） | −0.35%（t=−0.53） |

如实记录结论，不美化：

- **跑不赢**。任何权重方案 × `min_triggers`（1~4）× `top_n`（5/10/20）的组合，在留出段
  都没有正超额，最好的也只是一串接近 0 的负数；
- 训练段「最优」的 t=1.92 **落在随机对照的噪声带里**（纯随机抽样在这段历史上就能刷到
  1.41 ~ 2.13），它在留出段直接翻成 −1.15%，这是过拟合的标准形态，不是可用的 alpha；
- 分数本身没有区分度：同一候选池内「打分最高 10 只 − 最低 10 只」的价差在留出段为
  −0.60%（t=−1.38）；按「命中条件个数」分层也不单调（4 → 8 个条件分别是 −1.01% / −1.06% /
  −1.20% / +0.08% / −0.56%），条件越多并不越好；
- 8 个触发器里有 3 个（`above_ma20`、`ma20_above_ma60`、`rsi_rebound`，合计 36 分）在
  候选池内命中率约等于 1.00：候选池本身已按趋势结构排序，这几个条件**按定义就无法区分**
  池内标的。真正参与排序的是 `macd_hist_turn`、`boll_pullback`、`stable_volatility`、
  `volume_expand`、`momentum_positive`，而这 5 个的训练段符号在留出段全部反转或消失。

因此**没有改动生产权重**：在证据表明「调权重只是在拟合噪声」时改权重，只会让页面看起来
更好、实盘更差。要真正判断有没有 alpha，得先补齐 3~5 年历史与 point-in-time 股票池，再做
滚动重估（walk-forward）与多重检验校正，而不是在 1 年数据上继续调参。

**分析结果仅用于研究，不构成投资建议。**

### 多年历史与跨年份样本外验证

单一切分（训练 100 天 / 留出 60 天）的结论很容易被一段市场风格主导；本地日线原本
只有 1 年、股票池只有「当前」一份快照，这两个硬伤会让「到底能不能跑赢」无法回答。
为此补齐三件事。

**1）多年日线**（`scripts/backfill_history.py`）

先把交易日历铺到区间起点（否则增量逻辑会认为「历史已完整」而跳过），再按标的增量
抓取：只补缺口，失败逐只列进报告，**不写任何合成 / 插值数据**。

```powershell
python scripts/backfill_history.py --start 2021-03-25 --extend-calendar `
  --providers tdx,eastmoney,tencent --concurrency 4
```

实测（2026-09-12）：5550 只、2425 秒、6676452 根、**失败 0**；区间
2021-03-25 ~ 2026-09-11 共 1328 个交易日。中断可安全重跑（增量补缺口）。

数据质量复核：这 1328 天里有 879 只标的存在「内部缺口」，用通达信原始日线逐日对齐后
确认**全部是真实停牌**（本地缺 0 天，本地甚至比远端多 1~2 天），不是抓漏。

**2）前复权铺到同样长度**（`scripts/backfill_qfq.py`）

复权口径不同会改变验证结论，所以前复权必须跟不复权一样长，否则验证会自动回退。

实测：5580 只 / 6646925 行 / 1147 秒，应用 22822 个除权除息事件；26 只因并发写锁
失败（`database is locked`），把这 26 只单独重跑即 0 失败 —— 脚本幂等，重跑安全。

**3）跨年份 walk-forward**（`scripts/walk_forward.py`）

把缓存铺满多年，按 `[训练 250 天] + [隔离带 10 天] + [留出 60 天]` 向前滚动切多折：
每折在训练段挑「最好」的方案，留出段只跑一次且绝不回头改参数，并给出 30 组随机对照的
噪声带，用来判断「某个 t 值」是不是只是运气。

```powershell
python scripts/walk_forward.py --train 250 --hold 60 --gap 10 --step 90 --max-folds 6
python scripts/walk_forward.py --reuse      # 复用缓存，只重跑搜索
```

为支持跨年窗口，`ValidationConfig` 新增 `end_day`（窗口右端，None = 取最近 N 天）
与 `validate(max_eval_days=...)`；生产与 API 路径仍保持 `MAX_EVAL_DAYS = 240`。

**历史时点股票池快照**（`scripts/backfill_universe.py`）

快照的成员必须来自「当日实际在场清单」，不能来自当前 `securities` 全表 —— 否则
2021 年的快照里会混进 2024 年才上市的标的（未来函数），而 2021 年存在、后来退市的
标的又被漏掉（幸存者偏差）。为此新增
`UniverseSnapshotService.get_or_create_snapshot_from_records()`：
成员只取传入的当日记录，`trading_status` 用**当日状态**，并且对 `securities` 表
**只补缺、不覆盖**，历史状态绝不污染生产主数据（5 项单元测试覆盖）。

实测：2021-06-30 锚点成功（当日清单 4387 条，入选 4359，其中含后来退市的标的）。

**已知限制**：2021-11-15 之后的锚点暂不可用 —— BaoStock 的 `query_all_stock(day)`
对这些历史日不返回北交所成分，被 `validate_market_coverage` 的 BJ 门槛硬拦
（`BJ 覆盖 0 < 50 / 100`）。这是数据源缺口，不是脚本问题；宁可失败也不放宽门槛，
因为「少一个交易所」的快照会被当成完整历史用。

### 前复权日线（`/api/history-adjust`）

本地日线来自通达信原始行情，是**不复权**价：持仓窗口里一旦发生除权除息，分红 / 送转
会被算成下跌。上文的复权口径对照说明这会直接改变验证结论，因此补齐了前复权数据。

为什么不直接抓现成的前复权序列：

- 东方财富 K 线接口（`stock_zh_a_hist` / `push2his`）在本机稳定复现
  `RemoteDisconnected`（见「已知限制」9），不能作为全市场主通道；
- 新浪日线能给出前复权序列，但实测约 4~5 秒/只，全市场一轮要数小时，只适合做小样本
  交叉验证；
- 通达信 `get_xdxr_info`（除权除息）实测 40~50 毫秒/只且字段齐全，配合本地已有的
  全市场不复权日线，可以直接把复权因子推出来。

口径与公式（`app/history/adjust.py`，与通达信 / 同花顺 / 新浪一致）：

```
参考价 = (前收盘价 - 每股派现 + 每股配股 × 配股价) / (1 + 每股送转股 + 每股配股)
因子 k = 参考价 / 前收盘价
前复权价 = 原价 × ∏{事件日 > 该 K 线日期} k
```

通达信 `fenhong` / `songzhuangu` / `peigu` 都是**每 10 股**口径，代码里统一换算成每股。
前复权以最新交易日为基准，因此**最新一根 K 线的因子恒为 1.0**，越早的交易日因子越小。

实测校验：300750 于 2026-08-10 每 10 股派 14.11 元，除权前最后一个交易日收 394.40，
k = (394.40 − 1.411) / 394.40 = 0.996423 → 前复权价 392.9893；新浪前复权序列同一天
给 392.98（截断到分）。4 只样本与新浪逐日比对，最大相对偏差 1.4e-4（0.01 元取整误差）。

回填约束：

- 只写 `historical_bars` 里 `adjust='qfq'` 的行，按标的「先删后插」整体重写，可重复执行；
- **不改动** `adjust='none'` 的任何数据，不动其它表；
- 单只标的拿不到除权除息数据时**跳过写库**并记入报告的失败列表 —— 宁可不写，也不能用
  「全 1.0 因子」把不复权数据伪装成前复权；
- 全市场实测（2026-09-12，5578 只 / 132 万行）：4~5 分钟，应用 4616 个除权除息事件，
  1 只失败（并发写锁）重跑即补齐。

```powershell
# 全市场回填（脚本形式，与 API 等价）
python scripts/backfill_qfq.py --workers 6
# 只回填指定标的
python scripts/backfill_qfq.py 600519 000001
# 或走 API（后台任务 + 状态轮询）
curl -X POST "http://127.0.0.1:8000/api/history-adjust"
curl "http://127.0.0.1:8000/api/history-adjust"
```

## 已知限制

1. **腾讯免费接口无历史 K 线**，历史回测依赖上文的 AKShare/BaoStock 回退链（需联网）；
   三个来源都不可达时只能读本地缓存，并会被标记为不完整。
2. **QMT 数据源需本地 xtdata 客户端**，未登录时自动降级。
3. **QMT 实盘需本机授权环境**：默认 `REAL_TRADING_ENABLED=false`。启用前必须安装券商授权的 MiniQMT/xtquant，在本机 `.env` 配置 `QMT_USERDATA_PATH` 和 `QMT_ACCOUNT_ID`；开发测试无法代替券商柜台验收。
4. **限流为单机内存实现**，多实例部署需改为 Redis。
5. **BaoStock 不覆盖北交所历史成员**：北交所开市后的历史快照若缺 BJ 覆盖会明确失败；仅当前同步允许用 AKShare 当前 BJ 名单补充，禁止把当前名单回填过去。
6. **北交所日线依赖 AKShare 新浪通道**：BaoStock 只认 `sh.` / `sz.` 前缀（`bj.` 返回
   10004011），东财通道又经常不可达。当前北交所代码统一为 `920xxx`（交易所清单 343 只），
   新浪通道可拉到完整历史；已停用的旧 `43xxxx` / `83xxxx` 号段没有可用的历史接口。
7. **通达信数据源的边界**：只映射沪深京 A 股（沪 1 / 深 0 / 北交所 3），B 股、
   基金、债券等代码段不会命中，会由 ProviderManager 回退到东财。`tdxpy` 不返回
   证券名称，已由 `main.py` 从本地 `securities` 表批量补齐。北交所的请求市场号
   与服务端回包 `market` 不一致（**3 进 2 出**），这是实测结论，改错会拿不到数据。
8. **交易日历兜底依赖 exchange_calendars 4.x**：该版本只有 `sessions_in_range`（无
   `valid_days`），且 XSHG 日历边界为 2006-09-11 ~ 2026-12-31，超出边界的区间会被裁剪。
9. **东财限流按「主机 + 路径」生效**：实测本机 `push2his.eastmoney.com` 的
   `/api/qt/stock/kline/get`、`/api/qt/stock/trends2/get` 会被直接断连（curl 返回 `000`），
   而同一主机的 `/api/qt/ulist.np/get` 正常。分时不受影响：故障转移后由
   `push2delay.eastmoney.com` 提供，实测 600519 当日 241 根 1 分钟线（09:30–15:00）。
   但 `push2delay` **没有历史 K 线库**（`rc=0` 且 `dktotal=0`、`klines=[]`），因此东财
   **日/周/月/5m 及以上周期的 K 线暂时取不到**：`kline_available()` 会判定该主机没有这份
   数据并继续换主机，两台都拿不到时熔断 300 秒，由 ProviderManager 回退到 `MARKET_PROVIDERS`
   里的下一顺位（默认 tdx → 腾讯），历史入库回退 AKShare/新浪与 BaoStock。加上第 7 条，
   这类 K 线请求实际上多由通达信兜住。路径恢复后无需改配置即可自动命中。行情批量、分时、板块/资金流
   与数据中心不受影响。

10. **研究候选排序依赖本地数据**：需要本地已有交易日历、股票池快照和至少
    `lookback_days`（默认 120）根日线，任一缺失都会返回 503；首次扫描要冷读本地
    SQLite（实测约 7 秒），命中缓存后约 3 秒。休市日不伪造当日 K 线，`live=false`
    时按最近交易日收盘评估，`signal_day` 指向下一交易日。
11. **候选排序目前没有可用的超额收益**：见上文「样本外验证」实测结论，候选在
    2026-06-04 ~ 2026-08-27 的样本外跑输同池等权，前复权口径下 10 日平均 −1.74%
    （t=−2.74）、5 日 −1.11%（t=−2.24）、3 日 −0.45%（t=−1.16）。当前实现只把规则
    算清楚、并可复现地验证，**不代表可以实盘使用**。
12. **前复权数据需要重跑回填**：`historical_bars` 里的 `adjust='qfq'` 行不会随行情
    入库自动更新，新增交易日或出现新的除权除息后需要重跑 `POST /api/history-adjust`
    （幂等，全市场约 4~5 分钟）。未回填时雷达与验证自动回退不复权口径，并在返回体
    `bars_adjust` / 报告 `bars_adjust` 里写明。

## 运维脚本

```powershell
# 一键启动（迁移优先：alembic upgrade -> 后端 -> 前端）
powershell -ExecutionPolicy Bypass -File scripts/start_all.ps1

# 按 PID 停止全部服务
powershell -ExecutionPolicy Bypass -File scripts/stop_all.ps1

# 数据库备份 / 恢复（SQLite 在线安全备份）
python scripts/db_backup.py
python scripts/db_restore.py backups/a_stock_YYYYmmdd_HHMMSS.db

# 端到端冒烟测试（需先启动服务）
python scripts/e2e_smoke.py

# 全市场前复权日线回填（通达信除权除息 + 本地未复权日线，幂等）
python scripts/backfill_qfq.py --workers 6
```

## 验收标准（11 项真实命令）

提交前必须逐项通过：

1. `python -m compileall app tests migrations` — 无语法错误
2. `python -m pytest -q` — 全部测试通过
3. `python -m pytest --cov=app` — 覆盖率报告
4. `alembic upgrade head`（空库 + 从 0002 升级）— 迁移可用
5. `npm run type-check` — 前端类型零错误
6. `npm run build` — 前端可构建
7. `scripts/start_all.ps1` — 一键启动
8. `python scripts/e2e_smoke.py` — 端到端冒烟通过
9. `scripts/stop_all.ps1` — 停止后无残留进程
10. `git diff --check` — 无空白错误
11. `git status` — 工作区干净

## 第三方代码与许可证

本项目自行实现，未直接复制第三方代码。参考了以下开源项目的架构思路：

| 项目                                                | 许可证         |
| ------------------------------------------------- | ----------- |
| [Quanti](https://github.com/coo-moon/quanti)      | MIT         |
| [StockPro](https://github.com/Shadowell/StockPro) | MIT         |
| [InStock](https://github.com/myhhub/stock)        | Apache-2.0  |
| [AKShare](https://github.com/akfamily/akshare)    | MIT（作为可选依赖） |
| [BaoStock](https://github.com/baostock/baostock) | BSD（作为直接依赖）  |
| [tdxpy](https://github.com/mootdx/tdxpy)          | MIT（作为直接依赖，通达信行情协议） |
| [Qlib](https://github.com/microsoft/qlib)        | MIT（参考滚动评估与 RankIC） |
| [RQAlpha](https://github.com/ricequant/rqalpha) | Apache-2.0（参考撮合与交易前风控） |
| [vn.py](https://github.com/vnpy/vnpy)           | MIT（参考组合与风险模块边界） |

如后续复制或修改上述项目代码，将保留对应版权与许可证声明。

## QMT 实盘安全流程

实盘使用本机 QMT 官方 SDK，不把账号写入数据库或返回前端；数据库只保存账号哈希指纹。所有实盘读写接口还要求 `LIVE_TRADING_API_TOKEN`（至少 32 个随机字符），前端只在当前内存会话中保存。系统先读取真实资金和持仓生成草案，且不允许绕过“同参数至少 3 个样本外批次、平均收益和 RankIC 为正”的门槛。用户输入固定风险确认后获得仅在当前前端内存保存的 5 分钟一次性令牌，最终提交前再次读取账户与盘口；现金、持仓或可用数量变化会强制重新审核。同步委托超时标记为 `UNKNOWN` 并停止后续订单，禁止自动重试。

服务启动后会运行只读的实盘委托对账 worker；仅当 `REAL_TRADING_ENABLED=true` 且 QMT 路径/账号配置完整时，它才会按 `LIVE_RECONCILE_INTERVAL_SECONDS` 轮询 QMT 当日委托，把 `SUBMITTED` / `PARTIAL` / `UNKNOWN` 的本地方案刷新为券商状态。该 worker 不会自动补单、撤单或重试未知委托。

可在 PowerShell 生成实盘访问密钥：`[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))`，然后只把结果写入本机 `.env` 的 `LIVE_TRADING_API_TOKEN`。

## 每日流水线

`/api/daily-pipeline/runs` 用于把日常研究流程持久化：同步交易日股票池，创建全市场历史入库任务，等待入库完成后生成选股结果，并在配置了模拟账户时生成模拟调仓草案。流水线状态会落库，服务重启后遗留 `running` 会恢复为 `queued`，`waiting_history` 会在历史入库任务完成后继续推进。默认不会自动执行模拟成交；只有请求里显式 `auto_execute_paper=true` 且指定模拟账户时才会执行，真实 QMT 永远不由该流水线自动下单。

### 自动调度（可选）

设置 `DAILY_PIPELINE_AUTO_ENABLED=true` 后，后端会在每个 A 股交易日按
`DAILY_PIPELINE_AUTO_HOUR`:`DAILY_PIPELINE_AUTO_MINUTE`（北京时间，默认 15:35）
自动创建当日流水线任务，非交易日跳过。`DAILY_PIPELINE_AUTO_PAPER_ACCOUNT_ID`
指定联动的模拟账户；只有同时设置 `DAILY_PIPELINE_AUTO_EXECUTE_PAPER=true` 才会
自动执行模拟调仓（未指定账户时该开关会被忽略并写告警日志）。该调度只创建研究
与模拟任务，永不下真实订单；`GET /api/daily-pipeline/schedule` 返回当前配置与
下次触发时间。

## 模拟盘风控阈值

模拟成交与风控判定共用一组可配置阈值（`RISK_*`，比例均为小数）：

| 变量                            | 默认     | 含义                    |
| ----------------------------- | ------ | --------------------- |
| `RISK_MAX_POSITION_PER_SYMBOL` | 0.20   | 单只股票最大仓位              |
| `RISK_MAX_TOTAL_POSITION`      | 0.80   | 总仓位上限                 |
| `RISK_MAX_DAILY_LOSS`          | 0.03   | 单日亏损达到该比例后禁止新增买入      |
| `RISK_MAX_TOTAL_DRAWDOWN`      | 0.10   | 总回撤达到该比例后禁止新增仓位       |
| `RISK_MIN_COMMISSION`          | 5.0    | 单笔最低佣金（元）             |
| `RISK_COMMISSION_RATE`         | 0.0003 | 佣金费率                  |
| `RISK_STAMP_TAX_RATE`          | 0.0005 | 印花税（仅卖出）              |

阈值只影响本系统的模拟盘与风控判定，不改变券商柜台侧风控；修改后需重启后端。
