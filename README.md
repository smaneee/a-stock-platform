# A 股个人量化平台

> ⚠️ **免责声明**：本项目所有分析、选股与回测结果仅用于研究，不构成投资建议。
> 模拟盘与实盘接口默认全部关闭，任何真实下单都必须由使用者在界面上逐笔确认。

面向个人使用的 A 股量化研究工作台：从**全市场股票池 → 历史入库 → 多因子选股 →
样本外验证 → 模拟盘调仓 → 风控 → 经确认的实盘委托**形成闭环。
前端 Vite + React 18 + TypeScript，后端 FastAPI + SQLAlchemy + SQLite/PostgreSQL。

## 下载方式

三种任选其一。

**方式 A：git clone（推荐，便于后续更新）**

本仓库为**私有**仓库，clone 需要 GitHub 登录凭据：

```powershell
git clone https://github.com/smaneee/a-stock-platform.git
Set-Location a-stock-platform
```

**方式 B：网页下载 ZIP**

仓库页 → 绿色 `Code` 按钮 → `Download ZIP` → 解压即用（私有仓库需先登录对应账号）。

**方式 C：本机离线压缩包（不依赖 GitHub）**

源码压缩包（**仅代码**，1.35 MB）生成在：

```
D:\A股量化平台备份\a-stock-platform-src-<时间戳>.zip
D:\A股量化平台备份\a-stock-platform-src-<时间戳>.zip.sha256.txt
```

已生成的最新一份（可直接复制走）：

```
D:\A股量化平台备份\a-stock-platform-src-20260914_144427.zip
SHA-256  8cffc1fc052eb17b6f2397acf1e715335f6e8decbe9cf49a29ad99f4b42249f2
```

重新生成（包含当前 HEAD 的全部跟踪文件，不含数据库与密钥）：

```powershell
git archive --format=zip -o "D:\A股量化平台备份\a-stock-platform-src-$(Get-Date -Format yyyyMMdd_HHmmss).zip" HEAD
```

校验：

```powershell
Get-FileHash .\a-stock-platform-src-*.zip -Algorithm SHA256
Get-Content .\a-stock-platform-src-*.zip.sha256.txt
```

> 仓库**只放代码**：数据库（约 2.5 GB）、`.env` 密钥、Python venv、`node_modules`、
> 行情数据与研究产物全部由 `.gitignore` 排除。首次运行会自动建表，数据按需拉取。

### 把本仓库发布到 GitHub（维护者一次性操作）

本机已安装 GitHub Desktop 且已登录，最省事的方式：

1. GitHub Desktop → `File` → `Add local repository…` → 选择本目录；
2. 右上角 `Publish repository`；
3. **勾选 `Keep this code private`**（私有），仓库名填 `a-stock-platform`；
4. 点 `Publish repository`。

等价的命令行方式：

```powershell
git remote add origin https://github.com/smaneee/a-stock-platform.git
git push -u origin main
```

## 功能概览

- 实时行情与自选股监控（通达信 TDX / 东方财富 / 腾讯 / AKShare / QMT 数据源，WebSocket 推送行情与信号）
- 首页「现在买什么 · 实时排名」（看板页顶部）：直接给出全市场扫描后的**排名结果**，
  每只带建议买入区间 / 止损 / 目标 / 盈亏比 / 建议仓位与命中的触发条件，可选取前 5~20 名，
  支持 30 秒 ~ 2 分钟自动刷新或手动刷新，并标注扫描耗时、日线复权口径、市场情绪仓位系数；
  手机端自动改为单列卡片。数据全部为真实行情，**不构成投资建议**（验证结论见下方买点雷达条目）
- 实时分时曲线（`GET /api/quotes/{symbol}/intraday` + 「实时分时」页：价格 / 均价 / 每分钟成交量，
  实时缓存与数据源分钟线按分钟合并，右侧涨跌幅轴、昨收中轴、午休分隔线，支持 `/intraday?symbol=600519` 深链）
- 市场行情页（东方财富：行业/概念/地域板块行情与成分股、板块与个股资金流、数据中心）
- 东方财富数据中心（龙虎榜与席位、大宗交易、融资融券、沪深港通、机构调研、股东户数、限售解禁、业绩预告、分红送配、高管持股变动、股权质押比例、可转债）
- 东方财富涨停板情绪池（涨停 / 跌停 / 炸板 / 强势 / 次新，含封板资金、连板数、封板时间与所属行业）
- 涨停板情绪因子落库（封板率 / 连板高度历史曲线，支持一键回补与收盘后自动累积，供回测做市场温度因子）
- 市场情绪闸门（组合回测可选）：按滞后交易日（默认 1 天，杜绝未来函数）的封板率 / 连板高度 /
  涨停家数拦截新开仓，或按封板率线性缩放仓位；逐日决策与拦截次数随回测结果一并返回
- 技术指标（MA / EMA / MACD / RSI / 量比 / 振幅 / BOLL / KDJ / ATR / OBV / CCI / WR）与策略信号（金叉死叉、放量突破、RSI 反转）
- 实时买点雷达（`GET /api/realtime/picks` + 「买点雷达」页）：全市场两段式扫描
  （便宜因子筛池 → 入围池精算 RSI / BOLL / KDJ / MACD / ATR），输出评分、触发条件、
  买入区间 / 止损 / 目标 / 盈亏比与建议仓位，建议仓位再乘上涨停板情绪因子给出的仓位系数。
  全市场约 5550 只一次横扫实测 3~4 秒（6 条通达信连接并行取行情），休市日不会伪造
  当日 K 线，买点按下一交易日评估
- 买点雷达样本外验证（`POST /api/realtime/picks/validation` + 页面同面板）：把同一套
  规则放到历史上逐日重放（次日开盘买入、k 日收盘卖出、扣佣金与印花税、对比同池等权
  基准），并带随机 / 最差对照组自检脚手架。**实测结论是这套规则当前没有超额收益，
  不要把「买点雷达」的候选直接用于真实资金**，详见后端 README 的样本外验证一节
- 前复权日线（`POST /api/history-adjust` + 「智能选股」页的「前复权日线准备」卡片）：
  用通达信除权除息数据（`get_xdxr_info`，实测 40~50ms/只）配合本地未复权日线，在本地
  推导复权因子并写回 `historical_bars(adjust=qfq)`，全市场 5578 只 / 132 万行实测 4~5
  分钟、幂等、可重复执行。雷达与样本外验证**优先使用前复权**，缺失时自动回退不复权
  并在返回体 `bars_adjust` 里写明；实测复权口径会直接改变验证结论（不复权下「最差组」
  看起来更好，其实是除权除息的假跳空），详见后端 README
- 单标的与组合回测（T+1、涨跌停、停牌、手续费、滑点、无未来数据）
- 回测基准指数（沪深300 / 上证指数 / 深证成指 / 创业板指 / 科创50 / 中证500 / 上证50 / 中小100），
  指数代码带 `sh` / `sz` 前缀（如 `sh000300`），日线走通达信低延迟通道
- 全市场股票池（东方财富 + BaoStock + AKShare 三源，point-in-time 不可变快照）。
  东财 `f292` 上市状态码把原始 5910 条记录拆成「正常交易 5550 / 已退市 339 / 停牌 12 /
  待上市 9」，排除规则再据此剔除；「可交易」= 通过排除规则的成员，**不等于当天能下单**
  （休市日同样成立）。「股票池」页可浏览全量标的、按市场与状态过滤并手动触发同步
- 交易日历状态（`GET /api/market/session`）：今天是否交易日、最近交易日与下一交易日；
  「股票池」页顶部据此标注「今日休市（最近交易日 X）」并提供 `day=` 参数供回看/自测
- 全市场历史入库后台队列（限并发、可取消、可恢复、逐股票覆盖证据）
- 多因子智能选股 + T+1 样本外验证（胜率 / RankIC / 换手率，覆盖率不足拒绝给出结论）
- 模拟盘（订单状态机、日终结算、调仓草案、可配置风控阈值）
- 每日流水线（股票池 → 历史 → 选股 → 模拟调仓草案），可手动触发或按交易日自动调度
- QMT 实盘通道（默认关闭；账号只存哈希指纹，需一次性确认令牌，同步超时标记 UNKNOWN 且不自动重试）
- **实时排名强度分**（2026-09-14）：`GET /api/realtime/picks` 新增 `strength_score`
  （0~100 = Σ 命中权重 × 连续强度）并作为**排名主键**，解决「前 10 名并列 86.0 分、
  排序退化成按成交额排」的问题（实测前 10 名强度取值由 4 个增至 10 个；`score` 语义不变）
- **投资研究（2026-09-14 新增，面向个人本机自用）**：
  - 全市场基本面/估值快照：`POST /api/fundamentals/refresh`（东财行情接口，60 页 /
    原始 5913 行 / 实测 51 秒入库 **5550** 只；字段含义已用 akshare 独立财务数据逐项交叉核对）
  - 覆盖统计：`GET /api/fundamentals/coverage`（多少标的、最新快照日、报告期分布、告警行数）
  - 单标的质量与置信度：`GET /api/fundamentals/{symbol}`（分行业画像打分 +
    **独立的证据置信度**；缺失指标剔除而不填 0；报告期过期直接停止判断）
  - 三情景估值与敏感性：`POST /api/fundamentals/{symbol}/valuation`（悲观 / 基准 / 乐观 +
    敏感性表 + 安全边际；**假设必须显式给出**；报告期营收自动年化并在响应里写明系数
    `revenue_caliber`；银行等金融行业直接判定「模型不适用、不给价值判断」）
  - 7 项统一输出：`POST /api/fundamentals/{symbol}/analysis`（一句话结论 + 适用期限、数据时点与
    完整性、质量 / 估值 / 市场预期 / 组合风险、3 条支持 + 3 条反对证据、三情景、失效条件与
    复核触发、可展开的计算依据；结论带措辞自检）
  - 解释层（可选，DeepSeek）：`POST /api/fundamentals/{symbol}/explain`、
    `GET /api/fundamentals/explain/status`；**只解释不计算**，数字必须能在冻结证据包中找到，
    否则整段标 `rejected`；未配置密钥时如实返回 `not_configured`，确定性结果照常展示
  - 一条命令做完整研究：`scripts\research.ps1 -Symbol 000333 -Portfolio`
    （快照 → 质量 → 三情景 → 7 项输出 → 可选解释，报告落盘 `outputs\research\`）

## 环境要求

| 组件        | 要求                                                    |
| --------- | ----------------------------------------------------- |
| 操作系统      | Windows 10/11 + PowerShell 5.1+（本项目脚本面向 Windows）        |
| Python    | >= 3.11（`scripts/start_all.ps1` 会复用/创建 `backend/.venv`） |
| Node.js   | >= 18，提供 `npm.cmd`；PATH 上没有时 `start_all.ps1` 会探测常见安装位置，也可用 `NPM_PATH` 指定 |
| 网络        | 首次安装依赖与拉取行情需要联网                                       |
| 可选（实盘）    | 券商授权的 MiniQMT / `xtquant` SDK，本机 `userdata_mini` 路径     |

## 安装与启动

```powershell
cd a-stock-platform

# 1) 后端配置（密钥只写本机 .env，已被 .gitignore 排除）
Copy-Item backend\.env.example backend\.env

# 2) 前端依赖（可选，start_all.ps1 在缺失时会自动用国内镜像安装）
cd frontend
npm install --registry=https://registry.npmmirror.com
cd ..

# 3) 一键启动（迁移 → 后端 :8000 → 前端 :5173）
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1

# 停止
powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1
```

`start_all.ps1` 会自动创建 `backend\.venv` 并使用清华 PyPI 镜像安装
`backend\requirements.txt`；`frontend\node_modules` 缺失时同样自动用
`registry.npmmirror.com` 安装前端依赖，因此第 2 步可以跳过。

`scripts\*.ps1` 一律保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 会把无 BOM 的
UTF-8 文件按本地代码页解码，中文注释会串码，甚至直接报语法错误。

启动后：

只想起后端或只跑测试时，用 `scripts\start_backend.ps1` 与 `scripts\test_backend.ps1`：
两者都通过 `scripts\ensure_backend_venv.ps1` 复用已有 venv（`.venv` / `.venv-311` /
`.venv-312`），没有则用 `py` 启动器挑选 Python 3.13/3.12/3.11 新建，避免 PATH 上的
旧解释器（例如 3.6）被静默用来建出不可用的环境。

| 入口        | 地址                                        |
| --------- | ----------------------------------------- |
| 前端界面      | <http://127.0.0.1:5173>                   |
| 后端 API 文档 | <http://127.0.0.1:8000/docs>              |
| 就绪探针      | <http://127.0.0.1:8000/api/health/ready>  |

### 桌面启动器

不想记命令时用图形启动器 `scripts\launcher.ps1`（.NET WinForms 手写，不引入任何第三方包，
即使 Python venv 坏了也能启停服务、看日志）：

```powershell
# 直接打开启动器
powershell -ExecutionPolicy Bypass -File scripts\launcher.ps1

# 在桌面创建「A 股量化平台」快捷方式（缺图标时自动生成 scripts\launcher.ico）
powershell -ExecutionPolicy Bypass -File scripts\create_desktop_shortcut.ps1

# 只自检启动器本身，不开窗口（排查/自动化用）
powershell -ExecutionPolicy Bypass -File scripts\launcher.ps1 -SelfTest
```

启动器提供：后端 `:8000` / 前端 `:5173` 实时状态卡（端口监听 + 就绪探针双重判定，不靠退出码）、
一键启动 / 停止全部 / 重启服务（复用 `scripts\restart_all.ps1`）、打开界面 / API 文档、
查看运行日志（`.run\launcher.log` 尾部 200 行）、创建桌面快捷方式、刷新状态。
窗口每 1.5 秒自动刷新状态；子进程输出统一写入 `.run\launcher.log` 与 `.run\launcher.err.log`
（`.run/` 已被 `.gitignore` 排除）。

### 手机 / 平板访问

界面是响应式的（`lg` 断点以下折叠成抽屉导航）：390×844 视口实测各页面无横向溢出、
无控制台报错，表格可横向滚动。但**默认只监听 `127.0.0.1`，手机连不上**，真机访问要显式开启：

```powershell
# 前端改为监听 0.0.0.0；后端仍只有 127.0.0.1，经 vite 代理转发，不额外增加暴露面
powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1 -Lan

# 重启时同样可用
powershell -ExecutionPolicy Bypass -File scripts\restart_all.ps1 -Lan
```

启动日志会打印手机地址（形如 `http://192.168.x.x:5173`），同一 Wi-Fi 下用手机浏览器打开即可。
前端请求全部走相对路径（`/api`、`/ws`），由 vite 代理到后端，因此换 host 不需要改任何配置：
已在 `http://172.16.134.136:5173` 实测 5 个页面 0 报错，且 `/ws/quotes` 握手成功、
自选股页显示「已连接」。

注意事项：

- `-Lan` 会让同一局域网内**所有设备**都能打开这个界面（无登录鉴权）。用完请
  `scripts\stop_all.ps1` 关闭，或不要把端口映射到公网。
- 首次打不开多半是 Windows 防火墙。本机 `node.exe` 已有入站放行规则（`Public` 配置文件、
  TCP 任意端口），当前 WLAN 正好是 `Public`，一般不需要额外配置；若网络被改成「专用网络」，
  需用管理员 PowerShell 补一条：`New-NetFirewallRule -DisplayName "A-Stock Frontend 5173" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5173`

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
5. 「市场行情」页可查看东方财富板块行情、板块/个股资金流、涨停板情绪池（涨停/跌停/
   炸板/强势/次新），以及龙虎榜、大宗交易、融资融券、沪深港通、机构调研、股东户数、
   限售解禁、业绩预告、分红送配、高管持股变动、股权质押比例、可转债等数据中心数据集；
  龙虎榜可下钻到买卖席位。数据源不可用时页面会明确提示，而不是显示空表。
  涨停板情绪池由东财实时接口提供，只返回最近交易日快照（该接口不支持历史日期）；
  非交易日抓取会自动对齐到最近一个交易日，不会在曲线上留下与前一交易日重复的幽灵点。
6. 「股票池」页可确认本地池子到底有多少只标的（沪深京三市原始约 5910 条，其中正常交易
   约 5550 只）、最近一次同步时间、哪些被剔除（退市 / 停牌 / 待上市），并手动触发同步。
   页面上的「可交易」是**排除规则的结果**（退市 / 停牌 / 待上市不入选），不是「今天能下单」——
   非交易日（周末、节假日）池子不会变成不可交易，页面顶部会另外标注「今日休市」；
   快照交易日只会落在交易日，非交易日同步会自动归到最近一个交易日。
   「技术指标」页可查任意标的的
   MA/EMA/MACD/RSI/BOLL/KDJ/ATR/OBV/CCI/WR/振幅/量比 序列与机械读数。
7. 「情绪因子」页可一键回补整年情绪曲线：用本地不复权日线 + 涨跌停规则离线回算
   （`source=derived`）。这里**必须用不复权价**——涨跌停是按当天真实成交价判定的，
   换成复权价会算错；与东财实抓数据（`source=eastmoney`）逐日核对，封板率偏差约 ±0.05。
8. 「组合回测」页可勾选市场情绪闸门：设定封板率下限、连板高度下限、涨停家数下限与滞后
   交易日，闸门只拦新开仓、绝不拦卖出；配合「按封板率缩放仓位」后，弱情绪日会按比例
   降低新开仓规模。基准填 `sh000300` 这类带前缀的指数代码即可。
9. 「实时分时」页看单只标的的当日曲线：均价由「累计成交额 ÷ 累计成交量」现场计算（不依赖
   数据源是否给均价字段），成交量按分钟增量画柱；自选股走 WebSocket 实时叠加、每 30 秒与
   后端重新对齐，非自选股明确提示「仅自选股有实时推送」而不是让人以为行情卡住。
   任意标的都能看（数据源 1 分钟线覆盖全市场），入口在「自选股」与「股票池」列表里点代码。
10. 「买点雷达」页随时回答「现在该买什么」：默认剔除 ST、要求 20 日日均成交额不低于
    5000 万、至少命中 3 个触发条件；表里给出买入区间、止损、目标、盈亏比与建议仓位，
    并可 15 / 30 / 60 秒自动刷新。仓位系数来自涨停板情绪因子的封板率（滞后 1 个交易日，
    杜绝未来函数）；休市日页面会写明「按最近交易日收盘数据评估、买点针对下一交易日」。
    页面底部固定标注「分析结果仅用于研究，不构成投资建议」。
11. 同一页顶部是「样本外验证」面板：点「运行验证（60 日）」会在后台把雷达规则逐日重放
    （约 2 分钟，可轮询进度），给出各持有期的净收益、基准、超额、胜率与 t 值，并按结论
    给出红 / 黄 / 绿三档提示；点「随机对照自检」可确认这套验证本身没有系统性偏差
    （随机组的超额收益应接近 0）。面板底部会写明本次用的日线是「前复权」还是「不复权」。
    **当前实测为负超额，面板会明确劝阻实盘。**
12. 「智能选股」页的「前复权日线准备」卡片可一键回填全市场前复权日线（约 4~5 分钟，
    幂等，失败标的会明确列出且**不会写入假前复权数据**）。新增交易日或出现新的除权
    除息后重跑一次即可。
13. 想验证「换一套触发器权重 / 阈值能不能跑赢」，用仓库根目录的参数寻优脚本：它先逐日
    重放一次全市场（约 6 分钟，缓存到 `.cache/`），在**训练段**搜索权重与阈值，再在
    **留出段**只跑一次，并用 30 组随机对照给出噪声带。留出段没有正超额就不会改生产权重。

    ```powershell
    backend\.venv-311\Scripts\python.exe scripts\param_search.py           # 训练 100 天 / 留出 60 天
    backend\.venv-311\Scripts\python.exe scripts\param_search.py --reuse   # 复用缓存，秒级重跑
    ```

    实测结论（2026-09-12）是**跑不赢**：训练段最优方案在留出段翻负，且其 t 值落在随机
    对照的噪声带内，所以生产权重保持不变。完整表格与因子诊断见 `backend/README.md`。

14. 想做「跨多年、跨风格」的样本外验证，用仓库根目录的 walk-forward 脚本（先把日线
    铺长，见 `backend/README.md` 的「多年历史与跨年份样本外验证」）。它按
    `[训练 250 天] + [隔离带 10 天] + [留出 60 天]` 滚动切多折，每折都在训练段挑最优、
    留出段只跑一次，并给出 30 组随机对照的噪声带 —— 结论只看留出段，训练段再好也不算。

    ```powershell
    backend\.venv-311\Scripts\python.exe scripts\walk_forward.py --max-folds 6
    backend\.venv-311\Scripts\python.exe scripts\walk_forward.py --reuse   # 复用缓存
    ```

## 项目结构

```
a-stock-platform/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── api/             # REST 路由（行情/选股/股票池/模拟盘/实盘/流水线…）
│   │   ├── market_data/     # 行情数据源（东方财富/腾讯/AKShare/QMT/mock）
│   │   ├── universe/        # 全市场股票池（provider/sync/exclusion/snapshot）
│   │   ├── history/         # 历史行情、数据质量与前复权（adjust/qfq_backfill/qfq_service）
│   │   ├── realtime/        # 行情缓存、WebSocket 推送、当日分时（实时曲线）
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
├── scripts/                 # 启动/停止/备份/端到端冒烟/雷达参数寻优
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

# 端到端冒烟（自动迁移临时库 → 起服务 → 20 项断言 → 清理）
# 需要独占 :8000 / :5173，端口被占用会立刻中止并提示先停服务
cd ..
$env:E2E_MODE = "managed"
backend\.venv-311\Scripts\python.exe scripts\e2e_smoke.py
```

回归基线（2026-09-14 实测）：后端 **1469 passed / 1 skipped / 0 failed**、
前端类型检查与生产构建零错误。历史条目中的 977 项为早期基线，已被本次全量结果替代。

其中 `tests/test_api.py::test_paper_payload_matches_frontend_contract` 是前后端字段契约的
回归测试：`/api/paper/{trades,positions,orders}` 必须返回前端表格直接消费的全部字段
（曾因成交记录漏返 `realized_pnl` 导致模拟交易页白屏）。新增接口或收窄返回字段时，
需同步更新该测试与 `frontend/src/lib/types.ts`。

## 第三方参考与许可证

本项目自行实现，未直接复制第三方代码；架构上参考了以下开源项目，若后续引用其代码
将保留对应版权与许可证声明：

| 项目 | 许可证 | 参考点 |
| --- | --- | --- |
| [AKShare](https://github.com/akfamily/akshare) | MIT | 当前全市场与北交所数据源 |
| [tdxpy](https://github.com/mootdx/tdxpy) | MIT | 通达信行情协议解析（作为直接依赖，低延迟实时行情） |
| [BaoStock](https://github.com/baostock/baostock) | BSD | point-in-time 股票池与交易日历 |
| [Qlib](https://github.com/microsoft/qlib) | MIT | 滚动评估、样本外与 RankIC |
| [RQAlpha](https://github.com/ricequant/rqalpha) | Apache-2.0 | next-bar 撮合与交易前风控 |
| [vn.py](https://github.com/vnpy/vnpy) | MIT | 可恢复任务与风险模块边界 |

详细依赖版本约束见 `backend/requirements.txt` 与 `frontend/package.json`。

## 数据来源与合规（重要）

- 本仓库**不包含任何行情、财务或研究数据文件**：数据库、`outputs/` 产物与备份目录均由
  `.gitignore` 排除，需要由使用者在**本机**自行抓取。
- 行情/财务数据来自公开接口（东方财富、腾讯、通达信、AKShare、BaoStock）与可选的本机 QMT 通道。
  这些数据的再分发受各数据源条款约束，**仅限本机内部研究使用，不要再分发**。
- 项目定位为研究与模拟：`REAL_TRADING_ENABLED` 默认 `false`，实盘通道需独立配置并由使用者在界面上
  逐笔确认；截至最新交接报告，**没有任何策略通过样本外验证**（`production_ready_count = 0`），
  策略证据页会如实展示负结果。
- 使用本软件产生的任何结论均由使用者自行负责，不构成投资建议。
