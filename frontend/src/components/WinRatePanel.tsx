/** 首页「赚钱率前五」：样本内历史回放胜率排名（真实数据，实时刷新）。
 *
 * 这个数字的边界必须显示在页面上：**不是未来上涨概率**、**未通过样本外验证**、**未扣费用**。
 * 后端每次调用都会用本地前复权日线重新回放，所以刷新就是重新算，不是缓存快照。
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { fetchRealtimeWinRate } from "../lib/api";

const panel = "rounded-lg border border-slate-800 bg-slate-900 p-4";

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export default function WinRatePanel() {
  const [holdDays, setHoldDays] = useState(5);
  const [poolSize, setPoolSize] = useState(20);
  const [autoMs, setAutoMs] = useState(60_000);

  const query = useQuery({
    queryKey: ["realtime-win-rate", holdDays, poolSize],
    queryFn: () => fetchRealtimeWinRate({ top_n: 5, pool_size: poolSize, hold_days: holdDays }),
    refetchInterval: autoMs > 0 ? autoMs : false,
    refetchIntervalInBackground: false,
  });

  const manual = useMutation({ mutationFn: () => query.refetch() });

  const data = query.data;
  const rows = data?.top ?? [];

  return (
    <section className={panel}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-medium">赚钱率前五（样本内历史回放 · 实时重算）</h2>
          <p className="mt-1 text-xs text-slate-500">
            对当前实时排名前 {poolSize} 名的候选，用本地前复权日线逐日回放「同类条件」，
            统计持有 {holdDays} 个交易日的正收益占比后排出前五。每次都重新计算，不是缓存快照。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
          <label className="flex items-center gap-1">
            持有
            <select
              className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5"
              value={holdDays}
              onChange={(event) => setHoldDays(Number(event.target.value))}
            >
              {[1, 3, 5, 10, 20].map((day) => (
                <option key={day} value={day}>{day} 日</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1">
            候选池
            <select
              className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5"
              value={poolSize}
              onChange={(event) => setPoolSize(Number(event.target.value))}
            >
              {[10, 20, 30, 50].map((size) => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1">
            刷新
            <select
              className="rounded border border-slate-700 bg-slate-950 px-1 py-0.5"
              value={autoMs}
              onChange={(event) => setAutoMs(Number(event.target.value))}
            >
              <option value={30_000}>30 秒</option>
              <option value={60_000}>1 分钟</option>
              <option value={300_000}>5 分钟</option>
              <option value={0}>手动</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => manual.mutate()}
            disabled={query.isFetching}
            className="rounded border border-slate-700 px-3 py-1 hover:bg-slate-800 disabled:opacity-40"
          >
            {query.isFetching ? "计算中…" : "立即重算"}
          </button>
        </div>
      </div>

      {query.isError ? (
        <p className="mt-3 text-sm text-rose-400">
          取数失败：{query.error instanceof Error ? query.error.message : "未知错误"}
          （数据源不可用时不会显示旧结果）
        </p>
      ) : null}

      {data ? (
        <>
          <div className="mt-3 overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="px-2 py-1 text-left">#</th>
                  <th className="px-2 py-1 text-left">标的</th>
                  <th className="px-2 py-1 text-right">现价</th>
                  <th className="px-2 py-1 text-right">涨跌幅</th>
                  <th className="px-2 py-1 text-right">赚钱率</th>
                  <th className="px-2 py-1 text-right">样本数</th>
                  <th className="px-2 py-1 text-right">平均收益</th>
                  <th className="px-2 py-1 text-right">中位数</th>
                  <th className="px-2 py-1 text-right">最好 / 最差</th>
                  <th className="px-2 py-1 text-right">强度分</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.symbol} className="border-t border-slate-800">
                    <td className="px-2 py-1 text-slate-500">{row.board_rank}</td>
                    <td className="px-2 py-1">
                      <span className="text-slate-200">{row.symbol}</span>
                      <span className="ml-2 text-slate-400">{row.name}</span>
                      {row.live ? (
                        <span className="ml-2 rounded bg-emerald-950/60 px-1 text-[10px] text-emerald-400">盘中</span>
                      ) : null}
                    </td>
                    <td className="px-2 py-1 text-right">{num(row.price)}</td>
                    <td className={`px-2 py-1 text-right ${(row.change_pct ?? 0) >= 0 ? "text-rose-400" : "text-emerald-400"}`}>
                      {pct((row.change_pct ?? 0) / 100)}
                    </td>
                    <td className="px-2 py-1 text-right font-medium text-amber-300">{pct(row.win_rate)}</td>
                    <td className="px-2 py-1 text-right text-slate-400">{row.samples}</td>
                    <td className={`px-2 py-1 text-right ${(row.mean_return ?? 0) >= 0 ? "text-rose-400" : "text-emerald-400"}`}>
                      {pct(row.mean_return)}
                    </td>
                    <td className="px-2 py-1 text-right text-slate-300">{pct(row.median_return)}</td>
                    <td className="px-2 py-1 text-right text-xs text-slate-500">
                      {pct(row.best)} / {pct(row.worst)}
                    </td>
                    <td className="px-2 py-1 text-right text-violet-300">{num(row.strength_score, 1)}</td>
                  </tr>
                ))}
                {rows.length === 0 ? (
                  <tr>
                    <td colSpan={10} className="px-2 py-3 text-slate-500">
                      没有候选满足「回放样本数 ≥ {data.min_samples}」的门槛，因此不给排名（缺样本不编数字）。
                      已跳过 {data.skipped_count} 只。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <div className="mt-3 space-y-1 text-xs text-slate-500">
            <div>
              数据：{data.bars_adjust} 日线 · 生成 {data.generated_at_cst} · 信号日 {data.market_session.signal_day}
              {data.market_session.live ? "（已合并最新行情）" : "（按收盘价）"} · 行情覆盖 {pct(data.coverage_ratio)}
              · 参与回放 {data.pool_size} 只 / 入榜 {data.evaluated} 只 / 跳过 {data.skipped_count} 只
            </div>
            <div>
              口径：{data.definitions.buy_timing}；回放条件 {data.definitions.conditions_used.length} 条
              （{data.definitions.conditions_used.join("、")}），与实时选股的 8 条**不完全相同**。
            </div>
            <div className="text-amber-400/80">{data.disclaimer}</div>
          </div>
        </>
      ) : query.isLoading ? (
        <p className="mt-3 text-sm text-slate-500">正在回放历史并计算…</p>
      ) : null}
    </section>
  );
}
