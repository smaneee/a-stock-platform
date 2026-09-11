# A 股个人量化平台

> ⚠️ **免责声明**：本项目所有分析、选股与回测结果仅用于研究，不构成投资建议。
> 模拟盘与实盘接口默认全部关闭，任何真实下单都必须由使用者在界面上逐笔确认。

面向个人使用的 A 股量化研究工作台：从**全市场股票池 → 历史入库 → 多因子选股 →
样本外验证 → 模拟盘调仓 → 风控 → 经确认的实盘委托**形成闭环。
前端 Vite + React 18 + TypeScript，后端 FastAPI + SQLAlchemy + SQLite/PostgreSQL。

## 功能概览

- 实时行情与自选股监控（腾讯/ AKShare / QMT 数据源，WebSocket 推送行情与信号）
- 技术指标（MA / EMA / MACD / RSI / 量比 / 振幅）与策略信号（金叉死叉、放量突破、RSI 反转）
- 单标的与组合回测（T+1、涨跌停、停牌、手续费、滑点、无未来数据）
- 全市场股票池（BaoStock + AKShare 双源，point-in-time 不可变快照，含 ST/新股/退市/停牌排除）
- 全市场历史入库后台队列（限并发、可取消、可恢复、逐股票覆盖证据）
- 多因子智能选股 + T+1 样本外验证（胜率 / RankIC / 换手率，覆盖率不足拒绝给出结论）
- 模拟盘（订单状态机、日终结算、调仓草案、可配置风控阈值）
- 每日流水线（股票池 → 历史 → 选股 → 模拟调仓草案），可手动触发或按交易日自动调度
- QMT 实盘通道（默认关闭；账号只存哈希指纹，需一次性确认令牌，同步超时标记 UNKNOWN 且不自动重试）

## 环境要求

| 组件        | 要求                                                    |
| --------- | ----------------------------------------------------- |
| 操作系统      | Windows 10/11 + PowerShell 5.1+（本项目脚本面向 Windows）        |
| Python    | >= 3.11（`scripts/start_all.ps1` 会复用/创建 `backend/.venv`） |
| Node.js   | >= 18，提供 `npm.cmd`                                     |
| 网络        | 首次安装依赖与拉取行情需要联网                                       |
| 可选（实盘）    | 券商授权的 MiniQMT / `xtquant` SDK，本机 `userdata_mini` 路径     |

## 安装与启动

```powershell
cd a-stock-platform

# 1) 后端配置（密钥只写本机 .env，已被 .gitignore 排除）
Copy-Item backend\.env.example backend\.env

# 2) 前端依赖（国内镜像）
cd frontend
npm install --registry=https://registry.npmmirror.com
cd ..

# 3) 一键启动（迁移 → 后端 :8000 → 前端 :5173）
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1

# 停止
powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1
```

`start_all.ps1` 会自动创建 `backend\.venv` 并使用清华 PyPI 镜像安装
`backend\requirements.txt`。启动后：

只想起后端或只跑测试时，用 `scripts\start_backend.ps1` 与 `scripts\test_backend.ps1`：
两者都通过 `scripts\ensure_backend_venv.ps1` 复用已有 venv（`.venv` / `.venv-311` /
`.venv-312`），没有则用 `py` 启动器挑选 Python 3.13/3.12/3.11 新建，避免 PATH 上的
旧解释器（例如 3.6）被静默用来建出不可用的环境。

| 入口        | 地址                                        |
| --------- | ----------------------------------------- |
| 前端界面      | <http://127.0.0.1:5173>                   |
| 后端 API 文档 | <http://127.0.0.1:8000/docs>              |
| 就绪探针      | <http://127.0.0.1:8000/api/health/ready>  |

## 日常使用

1. 打开「智能选股」页，选择交易日，点「运行每日流水线」——它会依次同步股票池、
   创建全市场历史入库任务、等待入库完成后选股，并在指定模拟账户时生成调仓草案。
2. 想在收盘后自动跑，把 `backend\.env` 中 `DAILY_PIPELINE_AUTO_ENABLED` 设为
   `true`，并配置 `DAILY_PIPELINE_AUTO_PAPER_ACCOUNT_ID` /
   `DAILY_PIPELINE_AUTO_EXECUTE_PAPER`；调度配置与下次触发时间可在
   `GET /api/daily-pipeline/schedule` 查询。
3. 模拟盘阈值（单票仓位、总仓位、日亏损、回撤、费率）通过 `RISK_*` 环境变量调整，
   详见 `backend/README.md`。
4. 实盘默认关闭；开启前必须完成 `backend/README.md` 中「QMT 实盘安全流程」的全部步骤。

## 项目结构

```
a-stock-platform/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # REST 路由（行情/选股/股票池/模拟盘/实盘/流水线…）
│   │   ├── market_data/     # 行情数据源（mock/腾讯/AKShare/QMT）
│   │   ├── universe/        # 全市场股票池（provider/sync/exclusion/snapshot）
│   │   ├── history/         # 历史行情与数据质量
│   │   ├── selection/       # 多因子选股与样本外评估
│   │   ├── paper_trading/   # 模拟盘：券商、组合、结算、调仓
│   │   ├── live_trading/    # QMT 实盘适配、调仓、只读对账
│   │   ├── risk/            # 可配置风控限额与风控管理器
│   │   ├── tasks/           # 后台任务 worker 与每日流水线
│   │   └── ...
│   ├── migrations/          # Alembic 迁移
│   ├── tests/               # pytest 测试
│   └── README.md            # 后端详细文档（配置、验收、实盘流程）
├── frontend/                # Vite + React 18 + TS 界面
│   └── src/{pages,components,lib}
├── scripts/                 # 启动/停止/备份/端到端冒烟
└── outputs/                 # 运行日志等产物（已 gitignore）
```

## 测试与验收

```powershell
# 后端全量测试
cd backend
.\.venv-311\Scripts\python.exe -m pytest -q      # 或 .venv\Scripts\python.exe

# 前端类型检查与生产构建
cd ..\frontend
npm run type-check
npm run build

# 端到端冒烟（自动迁移临时库 → 起服务 → 19 项断言 → 清理）
cd ..
$env:E2E_MODE = "managed"
backend\.venv-311\Scripts\python.exe scripts\e2e_smoke.py
```

回归基线：后端 490 项测试全绿、前端类型检查与生产构建零错误、managed E2E 19/19。

## 第三方参考与许可证

本项目自行实现，未直接复制第三方代码；架构上参考了以下开源项目，若后续引用其代码
将保留对应版权与许可证声明：

| 项目 | 许可证 | 参考点 |
| --- | --- | --- |
| [AKShare](https://github.com/akfamily/akshare) | MIT | 当前全市场与北交所数据源 |
| [BaoStock](https://github.com/baostock/baostock) | BSD | point-in-time 股票池与交易日历 |
| [Qlib](https://github.com/microsoft/qlib) | MIT | 滚动评估、样本外与 RankIC |
| [RQAlpha](https://github.com/ricequant/rqalpha) | Apache-2.0 | next-bar 撮合与交易前风控 |
| [vn.py](https://github.com/vnpy/vnpy) | MIT | 可恢复任务与风险模块边界 |

详细依赖版本约束见 `backend/requirements.txt` 与 `frontend/package.json`。
