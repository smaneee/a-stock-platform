# A 股实时分析平台后端

> ⚠️ **免责声明**：本项目所有分析结果仅用于研究，不构成投资建议。

一个面向 A 股市场的实时分析平台后端，第一版聚焦于**实时行情监控 + 信号提醒 + 历史回测 + 模拟交易**，**禁止真实下单**。

## 功能特性

- A 股实时行情监控（免费源约 3 秒轮询自选股）
- 自选股管理
- 技术指标计算（MA / EMA / MACD / RSI / 成交量均线 / 涨跌幅 / 振幅 / 量比）
- 策略信号生成（MA 交叉、放量突破、RSI 超买超卖、MACD 金叉死叉）
- 历史回测（T+1、涨跌停、停牌、手续费、滑点、无未来数据）
- 模拟交易（含风控：仓位、日亏损、回撤、T+1、信号幂等）
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
| `DATABASE_URL`            | 数据库连接串              | `sqlite:///./a_stock.db` |
| `AUTO_CREATE_TABLES`      | 启动时自动建表（仅测试/演示）     | `false`                  |
| `MARKET_PROVIDERS`        | 数据源优先级              | `tencent,akshare`        |
| `QUOTE_POLL_INTERVAL`     | 轮询间隔（秒）             | `3`                      |
| `ROLLING_WINDOW_SIZE`     | 滚动窗口大小              | `300`                    |
| `SIGNAL_COOLDOWN_SECONDS` | 信号冷却时间              | `60`                     |
| `MAX_QUOTE_AGE_SECONDS`   | 行情时效阈值（超过则拒绝成交）     | `15`                     |
| `RATE_LIMIT_PER_MINUTE`   | 单 IP 每分钟最大请求数（0 关闭） | `300`                    |
| `WS_MAX_SUBSCRIPTIONS`    | 单 WebSocket 最大订阅数   | `200`                    |

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
| GET/POST | `/api/paper/accounts`                   | 模拟账户                           |
| POST     | `/api/paper/orders`                     | 模拟下单                           |
| GET      | `/api/paper/orders`                     | 委托单列表（含状态/拒绝原因）               |
| POST     | `/api/paper/orders/{id}/cancel`         | 取消委托                           |
| GET      | `/api/paper/positions`                  | 持仓（含已实现盈亏）                     |
| GET      | `/api/paper/trades`                     | 成交记录（含已实现盈亏）                   |
| POST     | `/api/paper/accounts/{id}/settle`       | 日终结算（T+1 解冻 + 资产快照）            |
| GET      | `/api/paper/accounts/{id}/assets`       | 账户资产与盈亏                        |
| WS       | `/ws/quotes`                            | 行情推送                           |
| WS       | `/ws/signals`                           | 信号推送                           |

## 行情数据源

| 数据源        | 用途     | 说明                               |
| ---------- | ------ | -------------------------------- |
| QMT/xtdata | 正式实时行情 | 可选，推送模式，延迟 <1s                   |
| 腾讯行情       | 免费轮询   | 默认主数据源                           |
| AKShare    | 历史数据   | 备用数据源                            |
| Mock       | 演示/测试  | 仅在 `MARKET_PROVIDERS=mock` 时显式启用 |

数据源按 `MARKET_PROVIDERS` 优先级故障转移，严禁静默混合来源。全部失败时返回缓存数据并标记 `is_stale=true`。Mock 不参与真实数据源的默认兜底，避免把随机价格误认为真实行情。

## 已知限制

1. **腾讯免费接口无历史 K 线**，历史回测依赖 AKShare（需联网）。
2. **QMT 数据源需本地 xtdata 客户端**，未登录时自动降级。
3. **日终结算需手动触发**：模拟交易通过 `POST /api/paper/accounts/{id}/settle` 解冻 T+1，暂未自动定时解冻。
4. **限流为单机内存实现**，多实例部署需改为 Redis。

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

## 验收标准（12 项真实命令）

提交前必须逐项通过：

1. `python -m compileall app tests` — 无语法错误
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

如后续复制或修改上述项目代码，将保留对应版权与许可证声明。

