/** 首页研究候选排名面板。
 *
 * 数据来自 ``GET /api/realtime/picks``：后端先扫描全市场（约 5500 只）可交易 A 股，
 * 按 8 个触发条件加权打分后排名。模型计算的价格线和风险预算不在首页包装成交易建议。
 *
 * 本面板只展示量化候选，**不构成投资建议**，也不会自动下单。样本外验证显示该雷达
 * 目前仍跑输同池等权，详情与结论见「买点雷达」页的验证面板。
 */
import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ApiError, fetchRealtimePicks } from "../lib/api";
import type { ScreenerPick, ScreenerSentiment } from "../lib/types";
import { pct, trendClass } from "../lib/format";

const panel = "bg-slate-900 rounded-lg border border-slate-800";
const chip =
  "inline-block rounded bg-slate-800/70 px-1.5 py-0.5 text-[11px] leading-5 text-slate-300";
const riskChip =
  "inline-block rounded border border-amber-900 bg-amber-950/40 px-1.5 py-0.5 text-[11px] leading-5 text-amber-300";
const select =
  "bg-slate-950 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200";

const TOP_N_OPTIONS = [5, 8, 10, 15, 20];
const REFRESH_OPTIONS = [
  { value: 30_000, label: "30 秒" },
  { value: 60_000, label: "60 秒" },
  { value: 120_000, label: "2 分钟" },
  { value: 0, label: "暂停刷新" },
];

const STANCE_STYLES: Record<string, string> = {
  strong: "border-rose-900 bg-rose-950/30 text-rose-300",
  healthy: "border-amber-900 bg-amber-950/30 text-amber-300",
  neutral: "border-slate-700 bg-slate-800/40 text-slate-300",
  weak: "border-emerald-900 bg-emerald-950/30 text-emerald-300",
  unknown: "border-slate-700 bg-slate-800/40 text-slate-400",
};

/** 数值一律走这里，避免后端缺字段时 undefined.toFixed 直接崩页面。 */
const num = (value: number | null | undefined, digits = 2): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(digits);

function formatTime(raw: string | null | undefined): string {
  if (!raw) return "—";
  return raw.replace("T", " ").slice(0, 19);
}

function Field({
  label,
  value,
  className = "text-slate-100",
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div>
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`numeric whitespace-nowrap ${className}`}>{value}</div>
    </div>
  );
}

function SentimentBadge({ sentiment }: { sentiment: ScreenerSentiment }) {
  if (!sentiment.available) {
    return (
      <span className="rounded border border-slate-700 bg-slate-800/40 px-2 py-0.5 text-xs text-slate-400">
        情绪数据未就绪（仓位按 1.0× 计）
      </span>
    );
  }
  const style = STANCE_STYLES[sentiment.stance] ?? STANCE_STYLES.unknown;
  return (
    <span className={`rounded border px-2 py-0.5 text-xs ${style}`}>
      {sentiment.label} · 仓位系数 {num(sentiment.exposure, 2)}×
    </span>
  );
}

function PickCard({ pick, evidenceLabel }: { pick: ScreenerPick; evidenceLabel: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="numeric flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-sky-800 bg-sky-600/20 text-sm font-semibold text-sky-300">
            {pick.rank}
          </span>
          <div className="min-w-0">
            <div className="truncate font-medium">{pick.name}</div>
            <div className="numeric text-xs text-slate-500">
              {pick.symbol} · {pick.board_label}
            </div>
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="numeric text-lg">{num(pick.price)}</div>
          <div className={`numeric text-xs ${trendClass(pick.change_pct ?? 0)}`}>
            {pick.change_pct === null || pick.change_pct === undefined
              ? "—"
              : pct(pick.change_pct)}
          </div>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
        <span
          className="numeric rounded border border-amber-900 bg-amber-950/40 px-1.5 py-0.5 text-amber-300"
          title="命中触发条件的权重和（命中即满分）"
        >
          评分 {num(pick.score, 0)}
        </span>
        <span
          className="numeric rounded border border-violet-900 bg-violet-950/40 px-1.5 py-0.5 text-violet-300"
          title="连续强度分（0~100）：同样命中数下用它排先后，是当前排名主键"
        >
          强度 {num(pick.strength_score, 1)}
        </span>
        <span className="numeric rounded bg-slate-800/70 px-1.5 py-0.5 text-slate-300">
          模型风险预算上限 {num(pick.suggested_weight_pct, 1)}%
        </span>
        <span className="rounded bg-slate-800/70 px-1.5 py-0.5 text-slate-400">
          {pick.live ? "含盘中行情" : "按收盘价"}
        </span>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
        <Field
          label="模型观察区间"
          value={`${num(pick.entry_low)} ~ ${num(pick.entry_high)}`}
        />
        <Field
          label="失效条件"
          value={`低于 ${num(pick.stop_loss)} 或数据过期`}
          className="text-amber-300"
        />
      </div>

      <div className="numeric mt-1.5 text-[11px] text-slate-500">
        盈亏比 {num(pick.risk_reward)} · ATR {num(pick.atr_pct)}% · RSI{" "}
        {num(pick.rsi14, 1)} · 20日动量{" "}
        {pick.momentum_20 === null || pick.momentum_20 === undefined
          ? "—"
          : pct(pick.momentum_20 * 100)}
      </div>

      {pick.reasons.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {pick.reasons.slice(0, 4).map((reason) => (
            <span key={reason} className={chip}>
              {reason}
            </span>
          ))}
        </div>
      )}

      {pick.risk_labels.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {pick.risk_labels.map((label) => (
            <span key={label} className={riskChip}>
              {label}
            </span>
          ))}
        </div>
      )}
      <div className="mt-2 text-[11px] text-amber-400">证据状态：{evidenceLabel}</div>
    </div>
  );
}

export default function NowBuyPanel() {
  const [topN, setTopN] = useState(8);
  const [refreshMs, setRefreshMs] = useState(60_000);

  const query = useQuery({
    queryKey: ["realtime-picks-home", topN],
    queryFn: () => fetchRealtimePicks({ top_n: topN }),
    refetchInterval: refreshMs === 0 ? false : refreshMs,
    placeholderData: keepPreviousData,
    staleTime: 15_000,
    retry: 1,
  });

  const data = query.data;
  const error = query.error as ApiError | null;
  const unavailable = error instanceof ApiError && error.status === 503;

  return (
    <div className={`${panel} p-4`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">研究候选排名</h2>
          <p className="text-xs text-slate-500">
            全市场约 {data ? data.universe_size : 5500} 只 A 股扫描，仅表示模型观察优先级
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-slate-500">
            取前
            <select
              className={select}
              value={topN}
              onChange={(e) => setTopN(Number(e.target.value))}
            >
              {TOP_N_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            名
          </label>
          <label className="flex items-center gap-1 text-xs text-slate-500">
            自动刷新
            <select
              className={select}
              value={refreshMs}
              onChange={(e) => setRefreshMs(Number(e.target.value))}
            >
              {REFRESH_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button
            onClick={() => query.refetch()}
            disabled={query.isFetching}
            className="rounded bg-slate-800 px-3 py-1 text-xs hover:bg-slate-700 disabled:opacity-50"
          >
            {query.isFetching ? "扫描中…" : "立即刷新"}
          </button>
        </div>
      </div>

      {query.isLoading && (
        <div className="py-8 text-center text-sm text-slate-500">
          正在扫描全市场并精算指标，首次约需 10~35 秒…
          <span className="block text-xs text-slate-600 mt-1">
            实测中位数：非交易日 11.4 秒 / 盘前 13.2 秒 / 盘中（缓存预热后）8.9 秒；
            冷启动与行情源慢时可达 30 秒以上。此处数字来自本机延迟采样，非估算。
          </span>
        </div>
      )}

      {!query.isLoading && query.isError && (
        <div className="mt-3 rounded border border-rose-900 bg-rose-950/30 p-3 text-sm text-rose-300">
          {unavailable
            ? "行情数据源当前不可用，无法生成实时排名。请稍后重试，或在「看板」检查数据源状态。"
            : `扫描失败：${error?.message ?? "未知错误"}`}
        </div>
      )}

      {data && (
        <>
          <div className="mt-3 rounded border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-200">
            <div className="font-medium">{data.evidence.label}</div>
            <div className="mt-1 text-xs text-amber-300/80">{data.evidence.summary}</div>
            <div className="mt-1 text-[11px] text-amber-400/70">
              证据来源：{data.evidence.evidence_source}
            </div>
          </div>
          {data.coverage_ok === false ? (
            <div className="mt-3 rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              行情覆盖率不足：仅 {data.quoted} / {data.universe_size} 只取到行情（
              {num((data.coverage_ratio ?? 0) * 100, 1)}%）。本次候选排序<strong>不构成研究依据</strong>，
              请先排查数据源可用性（盘前或数据源故障时常见）。
            </div>
          ) : null}
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <SentimentBadge sentiment={data.sentiment} />
            <span>
              生成于 {formatTime(data.generated_at_cst || data.generated_at)}（北京时间）· 耗时{" "}
              {num(data.scan_seconds, 1)} 秒 ·
              入围 {data.refined} / 初筛 {data.screened} / 全市场 {data.universe_size} · 覆盖率{" "}
              {num((data.coverage_ratio ?? 0) * 100, 1)}% · 日线
              {data.bars_adjust === "qfq" ? "前复权" : "不复权"}
              {data.live ? " · 已并入盘中行情" : " · 按最近交易日收盘评估"}
            </span>
          </div>

          {data.picks.length === 0 ? (
            <div className="mt-3 rounded border border-slate-800 bg-slate-950/40 p-4 text-sm text-slate-500">
              当前市场状态下没有满足全部触发条件的候选。这本身是一种结论：宁可空仓也不要硬凑。
            </div>
          ) : (
            <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {data.picks.map((pick) => (
                <PickCard
                  key={pick.symbol}
                  pick={pick}
                  evidenceLabel={data.evidence.label}
                />
              ))}
            </div>
          )}

          <div className="mt-3 space-y-1 text-[11px] text-slate-500">
            {data.notes.map((note) => (
              <div key={note}>· {note}</div>
            ))}
            <div>
              · 排名 = 8 个触发条件的权重和；模型风险预算 = 单笔风险{" "}
              {data.config.risk_budget_pct}% ÷ 下行风险距离，上限{" "}
              {data.config.max_weight_pct}%，再乘情绪仓位系数。
            </div>
            <div className="text-amber-400/80">
              · 后端证据门禁当前禁止把排名解释为买入推荐；{" "}
              <Link to="/realtime-picks" className="underline hover:text-amber-300">
                查看验证结论
              </Link>
              。⚠️ 分析结果仅用于研究，不构成投资建议。
            </div>
          </div>
        </>
      )}
    </div>
  );
}
