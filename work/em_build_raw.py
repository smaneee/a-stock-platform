# -*- coding: utf-8 -*-
"""聚合所有实测探针结果 → outputs/handoff/eastmoney-matrix-raw.json"""
from __future__ import annotations

import json
import os
import sys

PROJ = sys.argv[1]
WORK = os.path.join(PROJ, "work")
OUT = sys.argv[2]


def load(name):
    with open(os.path.join(WORK, name), encoding="utf-8") as f:
        return json.load(f)


catalog = load("catalog.json")
probe = load("probe_all.json")
align = load("align.txt") if False else None
behavior = load("probe_behavior.json")
coverage = load("probe_coverage.json")
extra = load("probe_extra.json")
stability = load("probe_stability.json")
margin = load("probe_margin.json")
edges = load("probe_edges.json")
future = load("probe_future.json")

cat_dc = json.loads(catalog["/api/market/datacenter"]["body"])
cat_lu = json.loads(catalog["/api/market/limit-up"]["body"])
cat_ix = json.loads(catalog["/api/market/indices"]["body"])

declared_dc = {d["key"]: d for d in cat_dc["datasets"]}
declared_lu = {d["key"]: d for d in cat_lu["pools"]}


def body_of(rec):
    try:
        return json.loads(rec.get("body") or "")
    except Exception:  # noqa: BLE001
        return None


def rows_of(p):
    if not isinstance(p, dict):
        return []
    for k in ("rows", "items"):
        if isinstance(p.get(k), list):
            return p[k]
    return []


def trim(row, n=700):
    s = json.dumps(row, ensure_ascii=False)
    return json.loads(s) if len(s) <= n else {"_truncated_sample": s[:n] + "…"}


FRONTEND_DC = "frontend/src/components/DatacenterPanel.tsx:267-292（表头与单元格全部由目录 fields 驱动，逐个字段渲染）"

datasets = {}
for key, d in declared_dc.items():
    rec = probe.get("dc." + key, {})
    p = body_of(rec) or {}
    rws = rows_of(p)
    cov = coverage.get("dc." + key, {})
    dc_declared = [f["key"] for f in d["fields"]]
    actual = list(rws[0].keys()) if rws else []
    datasets[key] = {
        "label": d["label"],
        "endpoint": f"GET /api/market/datacenter/{key}",
        "params_tested": ["limit=20", "limit=200", "limit=500"],
        "http_status": rec.get("status"),
        "latency_ms_first_call_limit20": rec.get("ms"),
        "latency_ms_limit500": cov.get("ms"),
        "total_rows_upstream": p.get("total"),
        "rows_returned_limit500": cov.get("n"),
        "declared_fields": dc_declared,
        "declared_field_titles": {f["key"]: f["title"] for f in d["fields"]},
        "declared_field_kinds": {f["key"]: f["kind"] for f in d["fields"]},
        "actual_fields_first_row": actual,
        "field_diff": {"missing_in_response": [x for x in dc_declared if x not in actual],
                       "extra_in_response": [x for x in actual if x not in dc_declared]},
        "markets_limit500": cov.get("markets"),
        "date_field": cov.get("date_field"),
        "date_min_limit500": cov.get("date_min"),
        "date_max_limit500": cov.get("date_max"),
        "always_null_fields_limit200": (stability.get("nulls." + key) or {}).get("always_null"),
        "partial_null_fields_limit200": (stability.get("nulls." + key) or {}).get("partial_null"),
        "sample_row": trim(rws[0]) if rws else None,
        "frontend_consumers": FRONTEND_DC,
        "supports_date_filter": d.get("supports_date"),
        "supports_symbol_filter": d.get("supports_symbol"),
        "description": d.get("description"),
    }

pools = {}
for key, d in declared_lu.items():
    rec = probe.get("lu." + key, {})
    p = body_of(rec) or {}
    rws = rows_of(p)
    cov = coverage.get("lu." + key, {})
    dc_declared = [f["key"] for f in d["fields"]]
    actual = list(rws[0].keys()) if rws else []
    pools[key] = {
        "label": d["label"],
        "endpoint": f"GET /api/market/limit-up/{key}",
        "params_tested": ["limit=20", "limit=200", "trade_date=2026-09-11", "trade_date=2026-06-01", "trade_date=2099-01-01", "page=2"],
        "http_status": rec.get("status"),
        "latency_ms": rec.get("ms"),
        "trade_date_returned": cov.get("trade_date"),
        "total": cov.get("total"),
        "rows_returned_limit200": cov.get("n"),
        "declared_fields": dc_declared,
        "declared_field_titles": {f["key"]: f["title"] for f in d["fields"]},
        "declared_field_kinds": {f["key"]: f["kind"] for f in d["fields"]},
        "actual_fields_first_row": actual,
        "field_diff": {"missing_in_response": [x for x in dc_declared if x not in actual],
                       "extra_in_response": [x for x in actual if x not in dc_declared]},
        "markets_limit200": cov.get("markets"),
        "sample_row": trim(rws[0]) if rws else None,
        "frontend_consumers": "frontend/src/components/LimitUpPanel.tsx:124-138（表头与单元格全部由目录 fields 驱动，逐个字段渲染）",
    }

raw = {
    "meta": {
        "报告": "东方财富功能矩阵逐项实测（只读）",
        "生成时间": extra.get("generated_hint") or "",
        "base_url": "http://127.0.0.1:8000",
        "服务模式": "prod（后端托管 frontend/dist，前后端同端口 8000）",
        "主库": "backend/a_stock.db（本次验收全程只读；未调用任何写接口）",
        "写接口调用": "无（未调用 capture / backfill / sync / POST 类接口）",
        "市场会话": json.loads(extra.get("session") or "{}"),
        "单位口径": {
            "涨跌幅/占比类": "百分数（如 change_pct=-18.3246 表示 -18.32%），非小数",
            "金额类": "元（东财百万元/万元列已在后端换算，见 eastmoney_datacenter.py:21-27）",
            "limit-up 价格": "元（上游为 元×1000，已除以 1000，见 eastmoney_limit_up.py:16-19、PRICE_SCALE:57）",
            "limit-up 封板时间": "HH:MM:SS 字符串（上游 HHMMSS 整数，见 eastmoney_limit_up.py:20）",
            "limit_up_stat": "「N天M板」字符串（上游 zttj 对象，见 eastmoney_limit_up.py:21）",
        },
    },
    "catalogs": {
        "datacenter": {"endpoint": "GET /api/market/datacenter", "count": cat_dc["count"],
                       "datasets": [d["key"] for d in cat_dc["datasets"]]},
        "limit_up": {"endpoint": "GET /api/market/limit-up", "count": cat_lu["count"],
                     "pools": [d["key"] for d in cat_lu["pools"]]},
        "indices": {"endpoint": "GET /api/market/indices", "count": cat_ix["count"],
                    "default": cat_ix.get("default"),
                    "items": cat_ix.get("items")},
    },
    "datasets": datasets,
    "limit_up_pools": pools,
    "other_endpoints": {
        "boards": {k: coverage.get("boards." + k) for k in ("industry", "concept", "region")},
        "constituents": {k: coverage.get("constituents." + k) for k in ("BK1592", "BK0976", "BK0153")},
        "fund_flow": {
            "boards": {k: coverage.get("ff.boards." + k) for k in ("industry", "concept", "region")},
            "stocks": coverage.get("ff.stocks"),
            "stocks_history": probe.get("fundflow.history.600519"),
        },
        "dragon_tiger_seats": {
            "probe": probe.get("dc.dragon-tiger-seats"),
            "crosscheck": stability.get("seats_crosscheck"),
        },
        "quotes": {k: probe.get("quote." + k) for k in ("600519", "000001", "430047", "688981")},
        "frontend_assets": {k: v for k, v in extra.items() if k.startswith("asset:") or k == "index_refs"},
    },
    "error_behavior": {
        "probe_all": {k: {"path": v["path"], "status": v["status"], "ms": v["ms"],
                          "body": (v.get("body") or "")[:300]}
                      for k, v in probe.items() if k.startswith("err.")},
        "edge_cases": edges,
        "stack_leak_detected": any(v.get("stack_leak") for k, v in edges.items()
                                   if isinstance(v, dict) and k != "page2_vs_page3"),
        "http_500_observed": False,
    },
    "pagination": {
        "dragon_tiger_page1_vs_page2": behavior.get("page.distinct"),
        "margin_page1_vs_page2_overlap": stability.get("margin_paging"),
        "margin_page2_vs_page3": edges.get("page2_vs_page3"),
        "margin_page_probe": {k: v for k, v in margin.items() if k.startswith("page")},
    },
    "future_trade_date_defect": future,
    "behavior_checks": behavior,
    "rate_limit_cache_failover_evidence": [
        {"类型": "全局限流", "位置": "backend/app/main.py:87-108（RateLimitMiddleware，按客户端 IP 60s 滑动窗口）",
         "参数": "rate_limit_per_minute=300（backend/app/config.py:141），挂载 backend/app/main.py:374",
         "实测": "本轮共发出约 130 次请求，未触发 429"},
        {"类型": "HTTP 超时", "位置": "backend/app/market_data/eastmoney_datacenter.py:634（10.0s）",
         "参数": "EastmoneyDatacenterService(timeout=10.0)", "实测": "最慢单次 5257ms（margin limit=500）"},
        {"类型": "HTTP 超时", "位置": "backend/app/market_data/eastmoney_limit_up.py:370（10.0s）", "参数": "同上", "实测": "最慢 236ms"},
        {"类型": "HTTP 超时", "位置": "backend/app/market_data/eastmoney_market.py:292（8.0s）", "参数": "板块/资金流", "实测": "最慢 834ms"},
        {"类型": "HTTP 超时", "位置": "backend/app/market_data/eastmoney_provider.py:384（5.0s）", "参数": "行情", "实测": "—"},
        {"类型": "重试", "位置": "backend/app/market_data/eastmoney_provider.py:352-366",
         "参数": "max_retries=1（datacenter:635 / market:293 / limit_up:371 / provider:333）",
         "实测": "max_retries=1 时 :364 的退避条件 (attempt+1<max_retries) 恒假 → 退避分支不生效，每台主机只试 1 次"},
        {"类型": "主机故障转移+冷却", "位置": "backend/app/market_data/eastmoney_provider.py:99-143（EastmoneyHostPool）",
         "参数": "cooldown_seconds=120.0（:99、:104）；ordered():114-119；mark_down():124-137；mark_up():140-143",
         "实测": "错误体出现「datacenter.eastmoney.com 未返回所需数据」，说明按主机记录并切换"},
        {"类型": "主机清单", "位置": "backend/app/market_data/eastmoney_provider.py:56-57（QUOTE_HOSTS/HISTORY_HOSTS 各 2 台）、eastmoney_datacenter.py:44（2 台）、eastmoney_limit_up.py:47（仅 1 台）",
         "参数": "push2/push2delay、push2his/push2delay、datacenter-web/datacenter、push2ex",
         "实测": "limit-up 只有 1 台主机 → 无实际备用"},
        {"类型": "缓存 TTL（后端）", "位置": "无。eastmoney_*.py 中 grep 'cache|ttl' 无命中 → 数据中心/板块/资金流/涨停池每次请求都直连上游",
         "参数": "—", "实测": "同一 URL 连续请求均走网络（margin limit=500 每次约 5.2s）"},
        {"类型": "缓存（实时行情）", "位置": "backend/app/realtime/quote_cache.py:18（QuoteCache）", "参数": "内存窗口缓存", "实测": "本轮未越权验证内部 TTL"},
        {"类型": "缓存（前端 react-query）", "位置": "frontend/src/components/DatacenterPanel.tsx:87（目录 staleTime 10min）、frontend/src/components/LimitUpPanel.tsx:41（情绪池 staleTime 60s）",
         "参数": "staleTime", "实测": "前端消费点已确认"},
        {"类型": "并发控制", "位置": "backend/app/universe/eastmoney_universe.py:178、backend/app/tasks/history_ingest_worker.py:115（asyncio.Semaphore）",
         "参数": "仅股票池同步/历史补数使用", "实测": "数据中心/板块/涨停池路径无并发上限"},
        {"类型": "上游错误码语义", "位置": "backend/app/market_data/eastmoney_datacenter.py:50-51（9201=空结果）、:16-17（9501 参数错误/9701 数据繁忙 按错误处理并切主机）", "参数": "—", "实测": "空结果返回 HTTP 200 + rows=[]"},
    ],
    "license_and_usage": {
        "使用方式": "仅只读调用东财公开 Web 接口（push2*/clist、push2ex、datacenter-web/api/data/v1/get）",
        "账号密码": "未使用；代码中无登录/账号/密码（docs/研发计划-2026Q4.md:62「不做东方财富账号密码登录、抓取或自动下单」；:178、:320「不使用东方财富密码」）",
        "登录/验证码/付费墙": "未绕过；未使用 Cookie 登录态，仅带公开页固定 User-Agent/Referer 与上游页面公开的 ut 参数（非用户凭据）",
        "官方接口文档": "未取得 datacenter-web/api/data/v1/get 的官方文档，属未核实项（东财对外文档化的授权通道是 Choice 数据量化接口 https://quantapi.eastmoney.com/Manual?from=web ，本项目未使用）",
        "官方使用条款（已核实原文）": {
            "URL": "https://about.eastmoney.com/home/protocol",
            "生效日期": "2025-07-18",
            "第六条 2 原文": "未经交易所事先书面同意，您不得对行情数据进行复制或向任何机构或个人提供全部或部分行情数据，不得将全部或部分行情数据用于开发衍生品。",
            "第六条 1 原文": "未经东方财富或相关权利人事先书面许可，任何人不得以任何形式进行使用或创造相关衍生作品。",
            "第八条 2 原文": "东方财富均不保证其真实性、完整性和准确性，且相关信息和数据不作为任何投资建议或其他实际操作的建议。",
            "对本项目的含义": "本项目把行情/数据中心数据落库并据此计算因子、回测与评分，属于条款第六条限制的「复制/衍生品开发」范围；未取得交易所或东财书面授权，属未核实/存在约束项。",
        },
        "结论": "仅可用于内部研究；不得再分发；不构成投资建议",
    },
    "issues": [
        {"编号": "EM-01", "严重度": "高", "数据集": "融资融券(margin)",
         "现象": "分页重复：page=2 与 page=3 返回 500/500 完全相同，且 page=1/2/3 的市场分布完全相同（沪 338/其他 162，深市 0），page=1 与 page=2 交集 499/500",
         "影响": "「下一页」看不到新内容；默认排序下前 1500 行无深市标的，用户无法通过翻页触达深市/北证融资融券数据（深市数据本身存在，按 symbol 可查）",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/margin?limit=500&page=1\" ; curl \"...&page=2\" ; curl \"...&page=3\" —— 对比 rows[].symbol",
         "根因定位": "margin 的 sort_column=DATE（eastmoney_datacenter.py:253）单列排序，2026-09-11 单日 682 万行存在大量并列；上游对同并列键的分页偏移不稳定/被截断",
         "建议": "默认排序追加稳定次级键（如 SECURITY_CODE），或用 date+symbol 过滤而非大页翻页"},
        {"编号": "EM-02", "严重度": "高", "数据集": "涨停/跌停/炸板/强势/次新（5 个池）",
         "现象": "请求未来 trade_date 时返回最新快照但回显请求日期，数据被错误标注。实测 trade_date=2099-01-01 与 2026-12-31 均返回 40 只且与 2026-09-11 快照 40/40 完全相同，响应体 trade_date 却回显 2099-01-01 / 2026-12-31",
         "影响": "调用方/落库任务会把 2026-09-11 的数据当成未来交易日数据；前端 LimitUpPanel.tsx:75 直接展示该字段",
         "复现": "curl \"http://127.0.0.1:8000/api/market/limit-up/limit-up?trade_date=2099-01-01&limit=200\" 与 \"...?trade_date=2026-09-11&limit=200\" 对比 items[].symbol 与 trade_date",
         "根因定位": "eastmoney_limit_up.py:434-437 —— 传了 trade_date 就用调用方入参回显，不校验上游实际返回的 qdate（:25-27 已自述 qdate 不可信）",
         "备注": "过去且超上游保留窗口的日期（2026-06-01）返回空，行为正确；仅未来/非交易日会冒名顶替。前端未暴露该参数，因此前端不受影响"},
        {"编号": "EM-03", "严重度": "中", "数据集": "全数据集",
         "现象": "非法但符合正则的日期返回 503「数据源暂不可用」而非 422。实测 date=2026-13-45 与 2026-02-30 均 HTTP 503（658ms）",
         "影响": "调用方误以为上游故障并触发重试/熔断；实际是本地入参未做日历合法性校验",
         "复现": "curl -i \"http://127.0.0.1:8000/api/market/datacenter/dragon-tiger?date=2026-13-45&limit=5\"",
         "根因定位": "eastmoney_datacenter.py:568-572 _require_date 只做 ^\\d{4}-\\d{2}-\\d{2}$ 正则匹配"},
        {"编号": "EM-04", "严重度": "中", "数据集": "沪深港通(northbound)",
         "现象": "18 个声明字段中 fund_inflow、quota_balance 在 limit=200 抽样中 100% 为 null（恒空）；buy_amount/sell_amount/net_deal_amount 在 3 个北向通道上恒空（101/200 行有值，即仅南向通道有值）",
         "影响": "前端 18 列表格中 5 列长期显示「—」；其中北向买卖额缺失与 2024-08 起交易所停止披露北向实时买卖金额的公开事实一致，属上游约束而非抓取错误",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/northbound?date=2026-09-11&limit=50\" → 观察 6 个通道行的空值分布",
         "建议": "前端/目录按通道标注可用字段，或把恒空字段从声明中移除"},
        {"编号": "EM-05", "严重度": "中", "数据集": "分红送配(dividend)",
         "现象": "bonus_ratio 在 limit=200 抽样中 100% 为 null（恒空）；it_ratio 198/200 为 null（仅 2 行有值）",
         "影响": "16 列表格中「送股比例」「转增比例」几乎不可用（A 股半年报期以现金分红为主，与上游数据有关）",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/dividend?limit=200\" → 统计 bonus_ratio / it_ratio 空值数"},
        {"编号": "EM-06", "严重度": "中", "数据集": "龙虎榜(dragon-tiger)",
         "现象": "20 个字段中 change_10d_pct、change_20d_pct 恒空（500/500），change_5d_pct 336/500 空、change_1d_pct 79/500 空",
         "影响": "「上榜后 1/5/10/20 日涨跌幅」4 列大部分或全部显示「—」，短期上榜后表现不可用",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/dragon-tiger?limit=500\" → 统计上述 4 个字段空值数"},
        {"编号": "EM-07", "严重度": "中", "数据集": "限售解禁(restricted-release)",
         "现象": "默认排序（FREE_DATE 降序，eastmoney_datacenter.py:370）返回 2028-10-09~2035-10-29 的远端未来解禁；order=asc 返回 2010-01-04 起的历史解禁。两种默认视角都没有「最近将要解禁」",
         "影响": "用户打开面板看到的是 2035 年的解禁，口径易被误读为「即将解禁」",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/restricted-release?limit=10\" （末位 2032 年）与 \"...&order=asc&limit=10\"（首位 2010-01-04）",
         "建议": "默认按 free_date 升序且 date_from=今天"},
        {"编号": "EM-08", "严重度": "低", "数据集": "股东户数(holder-number)",
         "现象": "默认排序 HOLDER_NUM 降序（eastmoney_datacenter.py:346），首页 end_date 跨 2014-06-30~2026-09-10，混合了 12 年的历史截面",
         "影响": "「股东户数」默认视图不是最新期，容易被读成当期数据",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/holder-number?limit=500\" → 观察 end_date 分布"},
        {"编号": "EM-09", "严重度": "低", "数据集": "龙虎榜席位(dragon-tiger-seats)",
         "现象": "接口返回 12 个字段，前端 SeatsTable 只渲染 4 个（seat_name/buy_amount/sell_amount/net_amount，DatacenterPanel.tsx:43-63）；buy_ratio、sell_ratio、close、change_pct、accum_amount、reason、trade_date、symbol 未展示",
         "影响": "席位占比与上榜原因等已正确返回但用户看不到",
         "复现": "curl \"http://127.0.0.1:8000/api/market/datacenter/dragon-tiger/300808/seats?trade_date=2026-09-11\""},
        {"编号": "EM-10", "严重度": "低", "数据集": "板块(boards)",
         "现象": "kind 参数大小写不敏感，但响应体 kind 原样回显。实测 kind=INDUSTRY 返回 items[].kind=\"INDUSTRY\"，与目录/前端类型 BoardKind(\"industry\"|\"concept\"|\"region\") 不一致",
         "影响": "类型契约外取值；前端只传小写，暂无实际影响",
         "复现": "curl \"http://127.0.0.1:8000/api/market/boards?kind=INDUSTRY&limit=5\""},
        {"编号": "EM-11", "严重度": "低", "数据集": "板块成分股(constituents)",
         "现象": "未知板块代码 BK9999 返回 HTTP 503「东方财富数据源暂不可用: Server disconnected without sending a response.」，而非法格式 XX 返回 422",
         "影响": "不存在的板块被报成上游故障；且该次请求触发上游断连",
         "复现": "curl -i \"http://127.0.0.1:8000/api/market/boards/BK9999/constituents?limit=5\""},
        {"编号": "EM-12", "严重度": "低", "数据集": "行情(quotes)",
         "现象": "休市日 is_stale 仍为 false。系统时间 2026-09-13（周日，/api/market/session 返回 is_trading_day=false、phase=non_trading_day），但 /api/quotes/600519 返回 market_time=2026-09-13T15:17:46、is_stale=false，且 source=tdx/tencent（非东财）",
         "影响": "前端若用 is_stale 判断时效会误判为实时；另注：market_providers 默认 \"tdx,eastmoney,tencent,akshare\"（config.py:39），东财行情通道本轮未被命中",
         "复现": "curl \"http://127.0.0.1:8000/api/quotes/600519\" 与 \"http://127.0.0.1:8000/api/market/session\""},
        {"编号": "EM-13", "严重度": "低", "数据集": "基础设施",
         "现象": "退避重试分支在默认配置下不可达：eastmoney_provider.py:352-366 中 max_retries 默认 1，range(1) 只跑 attempt=0，(attempt+1<max_retries) 恒假 → 0.3s 退避与 continue 重试永不执行；TransportError 直接 break 换主机",
         "影响": "上游瞬时错误没有指数/线性退避，只有换主机；单主机数据集（limit-up，push2ex）无重试",
         "复现": "读 backend/app/market_data/eastmoney_provider.py:350-373 与各服务 max_retries=1 默认值（datacenter:635 / market:293 / limit_up:371）"},
    ],
    "unverified": [
        {"项目": "官方接口文档与授权范围", "原因": "检索未取得 datacenter-web/api/data/v1/get 的官方文档；仅取得东财用户服务协议（不授权数据复制/衍生品开发）。属未核实项，不得推断为「已获授权」"},
        {"项目": "上游限流阈值与配额", "原因": "东财未公开限流规则；本轮仅观测到未触发限流，不能据此断言「不限流」"},
        {"项目": "上游数据许可的实际授权状态（交易所书面同意）", "原因": "项目内无授权文件；服务协议第六条要求交易所事先书面同意，是否取得未核实"},
        {"项目": "盘中（交易时段）时效与延迟", "原因": "本轮实测时间为 2026-09-13（周日）休市，无法验证盘中刷新与延迟"},
        {"项目": "历史覆盖起点（各数据集完整历史）", "原因": "仅实测单页上限内样本；接口返回的 total 是上游计数，未逐条核验历史完整性"},
        {"项目": "沪深港通 fund_inflow / quota_balance 恒空是否为永久上游约束", "原因": "仅单日 6 行样本，未做多日/多时段验证"},
        {"项目": "前端在浏览器中的实际渲染结果（截图/DOM）", "原因": "本轮以静态代码消费点 + HTTP 静态资源可达（/assets/index-wZlFnCnz.js 200/170302B）验证，未做浏览器端 DOM 断言"},
        {"项目": "大宗交易中「其他」市场代码（如 508017/508020 基金/REITs）的统计口径", "原因": "33/500 行为非沪深京股票代码，未核实是否应计入股票口径"},
    ],
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False, indent=1)
print("WROTE", OUT, "size", os.path.getsize(OUT))
