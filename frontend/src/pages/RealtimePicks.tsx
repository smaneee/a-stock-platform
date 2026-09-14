/** 研究候选排序：全市场扫描模型观察优先级。
 *
 * 数据来自 ``GET /api/realtime/picks``：后端先用便宜因子把全市场（约 5500 只）
 * 筛到 refine_pool，再对入围池精算 RSI / BOLL / KDJ / MACD / ATR 并打分，最后
 * 给出模型观察区间、风险线与情景线（仅供复核，不是交易参数）。
 *
 * 页面只做展示：策略参数由后端固定口径给出，**不构成投资建议**。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchRealtimePicks } from "../lib/api";
import type { ScreenerPick, ScreenerSentiment } from "../lib/types";
import { formatNumber, pct, trendClass } from "../lib/format";
import RadarValidationPanel from "../components/RadarValidationPanel";

const panel = "bg-slate-900 rounded-lg border border-slate-800";
const th =
  "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap align-top";
const chip =
  "inline-block rounded bg-slate-800/70 px-1.5 py-0.5 text-[11px] leading-5 text-slate-300";
const MAX_REASON_CHIPS = 4;
const input =
  "bg-slate-950 border border-slate-800 rounded px-3 py-2 text-sm text-slate-200";

const REFRESH_OPTIONS = [
  { value: 15_000, label: "15 秒" },
  { value: 30_000, label: "30 秒" },
  { value: 60_000, label: "60 秒" },
  { value: 0, label: "暂停刷新" },
];

const STANCE_STYLES: Record<string, string> = {
  strong: "border-rose-900 bg-rose-950/30 text-rose-300",
  healthy: "border-amber-900 bg-amber-950/30 text-amber-300",
  neutral: "border-slate-700 bg-slate-800/40 text-slate-300",
  weak: "border-emerald-900 bg-emerald-950/30 text-emerald-300",
  unknown: "border-slate-700 bg-slate-800/40 text-slate-400",
};

const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(digits);

function formatTime(raw: string | null | undefined): string {
  if (!raw) return "—";
  return raw.replace("T", " ").slice(0, 19);
}

function SentimentCard({ sentiment }: { sentiment: ScreenerSentiment }) {
  const style = STANCE_STYLES[sentiment.stance] ?? STANCE_STYLES.unknown;
  const sealRate =
    sentiment.seal_rate === null ? "—" : `${(sentiment.seal_rate * 100).toFixed(1)}%`;
  return (
    <div className={`${panel} p-4`}>
      <div className="flex items-center justify-between">
        <div className="text-sm text-slate-400">市场情绪（涨停板因子）</div>
        <div className={`rounded border px-2 py-0.5 text-xs ${style}`}>
          {sentiment.label}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div>
          <div className="text-xs text-slate-500">封板率</div>
          <div className="numeric text-slate-100">{sealRate}</div>
        </div>
        <div>
          <div className="text-xs text-slate-500">连板高度</div>
          <div className="numeric text-slate-100">
            {sentiment.max_streak ?? "—"}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">涨停家数</div>
          <div className="numeric text-slate-100">
            {sentiment.limit_up_count ?? "—"}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">仓位系数</div>
          <div className="numeric text-slate-100">
            {sentiment.exposure.toFixed(2)}×
          </div>
        </div>
      </div>
      <div className="mt-2 text-xs text-slate-500">
        情绪日 {sentiment.sentiment_date ?? "—"} · {sentiment.note}
      </div>
    </div>
  );
}

function ChipList({ items, max }: { items: string[]; max: number }) {
  if (items.length === 0) return <span className="text-slate-600">—</span>;
  const shown = items.slice(0, max);
  const hidden = items.length - shown.length;
  return (
    <div className="flex max-w-[17rem] flex-wrap gap-1">
      {shown.map((item) => (
        <span key={item} className={chip} title={item}>
          {item}
        </span>
      ))}
      {hidden > 0 ? (
        <span className="text-[11px] leading-5 text-slate-500" title="悬浮查看全部">
          +{hidden}
        </span>
      ) : null}
    </div>
  );
}

function PickRow({ pick }: { pick: ScreenerPick }) {
  return (
    <tr className="border-t border-slate-800 hover:bg-slate-800/40">
      <td className={`${td} text-slate-500`}>{pick.rank}</td>
      <td className={td}>
        <span className="text-slate-200">{pick.symbol}</span>
        <span className="ml-2 text-slate-400">{pick.name}</span>
        {pick.live ? (
          <span className="ml-2 rounded bg-emerald-950/60 px-1 text-[10px] text-emerald-400">
            盘中
          </span>
        ) : null}
      </td>
      <td className={`${td} text-slate-400`}>{pick.board_label}</td>
      <td className={`${td} numeric text-slate-200`}>{num(pick.price)}</td>
      <td className={`${td} numeric ${trendClass(pick.change_pct)}`}>
        {pct(pick.change_pct)}
      </td>
      <td className={`${td} numeric text-sky-300`}>{num(pick.score, 0)}</td>
      <td
        className={`${td} numeric text-violet-300`}
        title="连续强度分 = Σ 命中条件权重 × 连续强度（0~100），排名主键"
      >
        {num(pick.strength_score, 1)}
      </td>
      <td className={td} title={pick.reasons.join(" · ")}>
        <ChipList items={pick.reasons} max={MAX_REASON_CHIPS} />
      </td>
      <td className={`${td} numeric text-slate-300`}>
        {num(pick.entry_low)} ~ {num(pick.entry_high)}
      </td>
      <td className={`${td} numeric text-emerald-400`}>{num(pick.stop_loss)}</td>
      <td className={`${td} numeric text-rose-400`}>{num(pick.target_price)}</td>
      <td className={`${td} numeric text-slate-300`}>
        {num(pick.risk_reward)}
      </td>
      <td className={`${td} numeric text-slate-200`}>
        {num(pick.suggested_weight_pct)}%
      </td>
      <td className={td} title={pick.risk_labels.join(" · ")}>
        <ChipList items={pick.risk_labels} max={MAX_REASON_CHIPS} />
      </td>
    </tr>
  );
}

export default function RealtimePicksPage() {
  const [topN, setTopN] = useState(10);
  const [minTriggers, setMinTriggers] = useState(3);
  const [minAmountYi, setMinAmountYi] = useState(0.5);
  const [excludeSt, setExcludeSt] = useState(true);
  const [refreshMs, setRefreshMs] = useState(30_000);

  const query = useQuery({
    queryKey: ["realtime-picks", topN, minTriggers, minAmountYi, excludeSt],
    queryFn: () =>
      fetchRealtimePicks({
        top_n: topN,
        min_triggers: minTriggers,
        min_amount_20: Math.round(minAmountYi * 1e8),
        exclude_st: excludeSt,
      }),
    refetchInterval: refreshMs > 0 ? refreshMs : false,
    // 扫描一次 3~7 秒，避免切页/焦点变化时重复触发
    refetchOnWindowFocus: false,
    retry: false,
  });

  const data = query.data;
  const errorMessage = query.error instanceof Error ? query.error.message : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">研究候选排序</h1>
          <p className="mt-1 text-sm text-slate-400">
            全市场两段式扫描：先用便宜因子筛池，再对入围池精算 RSI / BOLL / KDJ /
            MACD / ATR 并打分。排序不是买入建议，证据状态以后端门禁为准。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2 text-slate-400">
            候选数
            <select
              className={input}
              value={topN}
              onChange={(event) => setTopN(Number(event.target.value))}
            >
              {[5, 10, 20, 30].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-slate-400">
            最少触发
            <select
              className={input}
              value={minTriggers}
              onChange={(event) => setMinTriggers(Number(event.target.value))}
            >
              {[0, 1, 2, 3, 4, 5, 6].map((value) => (
                <option key={value} value={value}>
                  {value} 项
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-slate-400">
            20 日日均额≥
            <select
              className={input}
              value={minAmountYi}
              onChange={(event) => setMinAmountYi(Number(event.target.value))}
            >
              {[0, 0.2, 0.5, 1, 2, 5].map((value) => (
                <option key={value} value={value}>
                  {value} 亿
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-slate-400">
            <input
              type="checkbox"
              checked={excludeSt}
              onChange={(event) => setExcludeSt(event.target.checked)}
            />
            剔除 ST
          </label>
          <label className="flex items-center gap-2 text-slate-400">
            自动刷新
            <select
              className={input}
              value={refreshMs}
              onChange={(event) => setRefreshMs(Number(event.target.value))}
            >
              {REFRESH_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 text-slate-200 hover:bg-slate-800"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            {query.isFetching ? "扫描中…" : "立即扫描"}
          </button>
        </div>
      </div>

      {errorMessage ? (
        <div className={`${panel} border-amber-900 bg-amber-950/20 p-4 text-sm text-amber-300`}>
          扫描失败：{errorMessage}
          <div className="mt-1 text-xs text-amber-400/80">
            数据源不可用时后端返回 503；股票池为空时先到「股票池」页点一次同步。
          </div>
        </div>
      ) : null}

      {/* 样本外验证独立于实时扫描：即使行情不可用也能看历史验证结果 */}
      <RadarValidationPanel />

      {data ? (
        <>
          <div className={`${panel} border-amber-800 bg-amber-950/30 p-4`}>
            <div className="text-sm font-medium text-amber-200">{data.evidence.label}</div>
            <div className="mt-1 text-xs text-amber-300/80">{data.evidence.summary}</div>
            <div className="mt-1 text-[11px] text-amber-400/70">
              证据来源：{data.evidence.evidence_source}
            </div>
          </div>
          <div className="grid gap-3 lg:grid-cols-3">
            <SentimentCard sentiment={data.sentiment} />
            <div className={`${panel} p-4 lg:col-span-2`}>
              <div className="text-sm text-slate-400">本次扫描</div>
              <div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
                <div>
                  <div className="text-xs text-slate-500">行情交易日</div>
                  <div className="text-slate-100">{data.session_day}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">买点信号日</div>
                  <div className="text-slate-100">{data.signal_day}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">候选 / 入围</div>
                  <div className="numeric text-slate-100">
                    {data.screened} / {data.refined}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">取到行情</div>
                  <div className="numeric text-slate-100">
                    {data.quoted} / {data.universe_size}
                  </div>
                </div>
              </div>
              <div className="mt-3 text-xs text-slate-500">
                生成于 {formatTime(data.generated_at)} · 耗时{" "}
                {data.scan_seconds.toFixed(1)} 秒 · 日线{" "}
                {data.bars_adjust === "qfq" ? "前复权" : "不复权"} · 窗口{" "}
                {data.config.lookback_days} 根 · 情绪滞后{" "}
                {data.config.sentiment_lag_days} 个交易日
                {data.live ? " · 已并入盘中行情" : " · 按最近交易日收盘评估"}
              </div>
            </div>
          </div>

          <ul className="space-y-1 text-xs text-slate-500">
            {data.notes.map((note) => (
              <li key={note}>· {note}</li>
            ))}
          </ul>

          <div className={`${panel} overflow-x-auto`}>
            <table className="min-w-full">
              <thead className="bg-slate-950/60">
                <tr>
                  <th className={th}>#</th>
                  <th className={th}>代码 / 名称</th>
                  <th className={th}>板块</th>
                  <th className={th}>现价</th>
                  <th className={th}>涨跌幅</th>
                  <th className={th}>评分</th>
                  <th className={th} title="连续强度分（0~100）：同样命中数下用它排先后">
                    强度分
                  </th>
                  <th className={th}>触发条件</th>
                  <th className={th}>模型观察区间</th>
                  <th className={th}>风险观察线</th>
                  <th className={th}>上行情景线</th>
                  <th className={th}>盈亏比</th>
                  <th className={th}>模型风险预算</th>
                  <th className={th}>风险提示</th>
                </tr>
              </thead>
              <tbody>
                {data.picks.length === 0 ? (
                  <tr>
                    <td className={`${td} text-slate-500`} colSpan={14}>
                      当前参数下没有符合条件的候选，可降低「最少触发」或流动性下限。
                    </td>
                  </tr>
                ) : (
                  data.picks.map((pick) => (
                    <PickRow key={pick.symbol} pick={pick} />
                  ))
                )}
              </tbody>
            </table>
          </div>

          <div className={`${panel} p-4 text-xs text-slate-500`}>
            模型风险预算 = 单笔可承受风险 {data.config.risk_budget_pct}% ÷
            下行风险距离，上限 {data.config.max_weight_pct}%，再乘以情绪系数{" "}
            {data.sentiment.exposure.toFixed(2)}×。20 日日均成交额门槛{" "}
            {formatNumber(data.config.min_amount_20)} 元，股价区间{" "}
            {data.config.min_price}~{data.config.max_price} 元。
            <div className="mt-2 text-amber-400/80">
              ⚠️ 上述价格线和预算是模型诊断字段，不是挂单、止损、目标或仓位建议。
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
