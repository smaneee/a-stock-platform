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
│   │   ├── realtime/               # 缓存/调度/WebSocket/信号引擎
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
| GET      | `/api/indicators`                       | 技术指标目录（22 个序列的中文名与 key）       |
| GET      | `/api/indicators/{symbol}`              | 单标的技术指标序列（MA/EMA/MACD/BOLL/KDJ/ATR/OBV/CCI/WR…） |
| GET      | `/api/quotes/{symbol}`                  | 单只行情                           |
| POST     | `/api/quotes/batch`                     | 批量行情                           |
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
| GET      | `/api/universe/snapshots/{day}/members` | 查询不可变的历史交易日成员                  |
| POST     | `/api/universe/filter`                  | 按交易日筛选可交易成员                    |
| GET/POST | `/api/daily-pipeline/runs`              | 创建/查询可恢复的每日股票池→历史入库→选股→模拟调仓流水线 |
| GET      | `/api/daily-pipeline/runs/{id}`         | 单条每日流水线任务详情（进度/阶段/错误摘要）      |
| GET      | `/api/daily-pipeline/schedule`          | 自动调度配置与下次触发时间（只读）              |
| POST     | `/api/history-ingest`                   | 创建指定交易日股票池的全市场历史入库任务          |
| GET      | `/api/history-ingest`                   | 历史入库任务列表                         |
| GET      | `/api/history-ingest/{id}`              | 历史入库任务详情（进度/覆盖率/失败明细）          |
| POST     | `/api/history-ingest/{id}/cancel`       | 取消排队中或运行中的历史入库任务               |
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
- 单位已核对：`vol` 单位是手（×100 转股）、`amount` 已是元、`servertime` 补当天日期。
- 单次请求按 80 只分块；K 线按 800 根/页分页，最多 40 页。

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

### 东方财富股票池（`universe` 首选源）

`EastmoneyUniverseProvider` 走 `clist/get`（`fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048`），
单页上限 100 条，60 页并发（信号量 4）实测约 3 秒拉完；一次同步约 5900 只（沪深 2467 / 深市 3091 /
北交所 351），并带出所属行业（`f100` → `SecurityRecord.sector`）、上市日期与名称。

两点约束：

- 只提供**当日**全市场快照；请求历史日期会显式抛错（`不支持历史快照`），由调用方回退 BaoStock，
  避免把当前成分名单回填成历史快照而产生幸存者偏差。
- 北交所旧号段（`43xxxx` / `83xxxx`）在 `clist` 中会混入可转债，已按代码段与名称（含「债」「转」）过滤。

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

指标计算在 `app/indicators/` 下按「一个指标一个文件」组织，全部只依赖标准库
（不引入 numpy / pandas），口径对齐通达信：

| 指标   | 文件                | 口径要点                                    |
| ---- | ----------------- | --------------------------------------- |
| BOLL | `boll.py`         | 中轨 MA(period)，上下轨 ± `num_std` 倍总体标准差；用 `ddof=1` |
| KDJ  | `kdj.py`          | RSV 后按 `SMA(X, N, 1)` 递推 K/D，J = 3K − 2D；无振幅时取 50 |
| ATR  | `atr.py`          | `true_range` 取三者最大，再按 Wilder 平滑          |
| OBV  | `obv.py`          | 首根记 0，收涨累加成交量、收跌累减                    |
| CCI  | `cci.py`          | AVEDEV 平均绝对偏差口径，MD 为 0 时返回 0            |
| WR   | `wr.py`           | 默认 0~100（通达信），`signed=True` 输出 −100~0     |

`GET /api/indicators` 返回目录（22 个序列的 key + 中文名 + 可用周期 + limit 边界），
`GET /api/indicators/{symbol}?period=daily&limit=250` 返回日期轴与各指标序列
（NaN 统一转 `null`，保留 4 位小数），以及 `latest_values` 便于前端做单值徽标。

数据源优先读本地 `historical_bars`（`adjust=none`），不足时经 ProviderManager
向上游补齐，返回体里的 `source` 标明本次实际来源（如 `tdx` / `local`）。
**该接口只做计算与展示，不改变任何既有信号逻辑。**

## 智能选股

选股服务严格使用指定交易日的 `UniverseSnapshot`，只读取该日及以前的日线，避免未来数据和当前成分股幸存者偏差。默认综合 20/60 日动量、20 日年化波动、60 日最大回撤和 20 日平均成交额；至少需要 61 根且最新行情不超过 10 天。结果连同配置哈希和数据指纹写入 `selection_runs`，同一快照、配置和数据重复执行会返回同一运行记录。

样本外评估严格用下一交易日开盘作为入场价、未来第 N 根日线收盘作为退出价，并汇总多批次胜率、RankIC 与换手率。模拟调仓默认要求至少 3 个已验证批次且平均收益、RankIC 均为正；仅模拟盘可显式覆盖门槛。生成的方案不会自动成交，必须在 15 分钟内由用户点击确认，重复执行会被状态机拒绝。真实下单只通过 QMT 实盘通道进行，需先完成下述"QMT 实盘安全流程"中的确认步骤。

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
