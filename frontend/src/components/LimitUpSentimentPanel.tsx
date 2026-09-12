/** 涨停板情绪曲线面板：封板率 / 连板高度的历史曲线。
 *
 * 数据来自涨停板情绪池的每日落库结果（GET /api/market/limit-up/sentiment）。
 * 东财只保留最近若干个交易日，所以本地还没有数据时可用「回补」按钮一键补齐；
 * 之后的每个交易日由后端定时任务自动累积。
 *
 * 该因子供回测判断市场温度，仅用于研究，不构成投资建议。
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { captureLimitUpSentiment, fetchLimitUpSentiment } from "../lib/api";
import type { LimitUpSentimentRow } from "../lib/types";

const SEAL = "#38bdf8";
const STREAK = "#fbbf24";
const LIMIT_UP = "#fb7185";
const BROKEN = "#34d399";

const panel = "bg-slate-900 rounded-lg border border-slate-800";
const axisTick = { fill: "#94a3b8", fontSize: 11 };
const tooltipStyle = {
  background: "#0f172a",
  border: "1px solid #1e293b",
  fontSize: 12,
};

const BACKFILL_OPTIONS = [10, 20, 30];

interface ChartPoint {
  date: string;
  seal: number | null;
  streak: number;
  limitUp: number;
  broken: number;
  firstBoard: number;
  boards2: number;
  boards3: number;
  boards4: number;
  boards5: number;
}

function toPoints(rows: LimitUpSentimentRow[]): ChartPoint[] {
  return rows.map((row) => ({
    date: row.trade_date.slice(5),
    seal: row.seal_rate === null ? null : Number((row.seal_rate * 100).toFixed(2)),
    streak: row.max_streak,
    limitUp: row.limit_up_count,
    broken: row.broken_board_count,
    firstBoard: row.first_board_count,
    boards2: row.streak_2_count,
    boards3: row.streak_3_count,
    boards4: row.streak_4_count,
    boards5: row.streak_5plus_count,
  }));
}

const asPercent = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;

const asMoney = (value: number) =>
  Math.abs(value) >= 1e8 ? `${(value / 1e8).toFixed(1)} 亿` : `${value.toFixed(0)} 元`;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-slate-950/60 border border-slate-800 rounded px-3 py-2">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="numeric text-slate-100 text-lg">{value}</div>
    </div>
  );
}

export default function LimitUpSentimentPanel() {
  const [days, setDays] = useState(20);
  const queryClient = useQueryClient();

  const curve = useQuery({
    queryKey: ["market", "limit-up-sentiment"],
    queryFn: () => fetchLimitUpSentiment(250),
    staleTime: 5 * 60 * 1000,
  });

  const capture = useMutation({
    mutationFn: (backfillDays: number) =>
      captureLimitUpSentiment(
        backfillDays > 0 ? { backfill_days: backfillDays } : {},
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["market", "limit-up-sentiment"],
      });
    },
  });

  const rows = curve.data?.items ?? [];
  const points = toPoints(rows);
  const latest = curve.data?.latest ?? null;
  const error = curve.error ?? capture.error;

  return (
    <div className="space-y-4">
      <div className={`${panel} p-4 space-y-3`}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-slate-300">情绪因子已落库</span>
          <span className="numeric text-slate-100">{rows.length}</span>
          <span className="text-sm text-slate-400">个交易日</span>
          <select
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="ml-auto bg-slate-950 border border-slate-800 rounded px-2 py-1 text-sm text-slate-300"
          >
            {BACKFILL_OPTIONS.map((value) => (
              <option key={value} value={value}>
                最近 {value} 天
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => capture.mutate(0)}
            disabled={capture.isPending}
            className="px-3 py-1.5 rounded text-sm bg-slate-900 text-slate-300 border border-slate-800 disabled:opacity-40"
          >
            抓取最近交易日
          </button>
          <button
            type="button"
            onClick={() => capture.mutate(days)}
            disabled={capture.isPending}
            className="px-3 py-1.5 rounded text-sm bg-sky-500/20 text-sky-300 border border-sky-500/40 disabled:opacity-40"
          >
            {capture.isPending ? "抓取中…" : `回补最近 ${days} 天`}
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <Stat label="最新交易日" value={latest?.trade_date ?? "—"} />
          <Stat label="涨停家数" value={latest ? String(latest.limit_up_count) : "—"} />
          <Stat label="封板率" value={asPercent(latest?.seal_rate)} />
          <Stat label="最高连板" value={latest ? `${latest.max_streak} 板` : "—"} />
          <Stat
            label="封板资金合计"
            value={latest ? asMoney(latest.total_seal_amount) : "—"}
          />
        </div>
        {capture.data && !capture.isPending && (
          <div className="text-xs text-emerald-300">
            已落库 {capture.data.count} 个交易日
          </div>
        )}
      </div>

      {error && (
        <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
          加载失败：{error instanceof Error ? error.message : String(error)}
        </div>
      )}

      {curve.isLoading && (
        <div className={`${panel} p-6 text-sm text-slate-500`}>加载中…</div>
      )}

      {!curve.isLoading && rows.length === 0 && (
        <div className={`${panel} p-6 text-sm text-slate-400 space-y-2`}>
          <div>本地还没有情绪数据。</div>
          <div className="text-slate-500">
            东财涨停板接口只保留最近若干个交易日，过期即无法回补；点击上方
            「回补最近 {days} 天」即可立即生成曲线，之后每个交易日收盘后会自动累积。
          </div>
        </div>
      )}

      {rows.length > 0 && (
        <div className={`${panel} p-4`}>
          <div className="text-sm text-slate-300 mb-2">
            封板率与涨停 / 炸板家数
            <span className="text-xs text-slate-500 ml-2">
              封板率 = 涨停家数 / (涨停家数 + 炸板家数)
            </span>
          </div>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={axisTick} />
                <YAxis yAxisId="count" tick={axisTick} />
                <YAxis
                  yAxisId="rate"
                  orientation="right"
                  unit="%"
                  tick={axisTick}
                />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar yAxisId="count" dataKey="limitUp" name="涨停家数" fill={LIMIT_UP} />
                <Bar yAxisId="count" dataKey="broken" name="炸板家数" fill={BROKEN} />
                <Line
                  yAxisId="rate"
                  type="monotone"
                  dataKey="seal"
                  name="封板率(%)"
                  stroke={SEAL}
                  strokeWidth={2}
                  dot={false}
                  connectNulls
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {rows.length > 0 && (
        <div className={`${panel} p-4`}>
          <div className="text-sm text-slate-300 mb-2">
            连板高度与连板梯队
            <span className="text-xs text-slate-500 ml-2">
              最高连板衡量赚钱效应，梯队断档说明情绪转弱
            </span>
          </div>
          <div className="h-72">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={axisTick} />
                <YAxis yAxisId="count" tick={axisTick} />
                <YAxis
                  yAxisId="max"
                  orientation="right"
                  allowDecimals={false}
                  tick={axisTick}
                />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar yAxisId="count" dataKey="firstBoard" name="首板" stackId="tier" fill="#475569" />
                <Bar yAxisId="count" dataKey="boards2" name="2板" stackId="tier" fill="#64748b" />
                <Bar yAxisId="count" dataKey="boards3" name="3板" stackId="tier" fill="#94a3b8" />
                <Bar yAxisId="count" dataKey="boards4" name="4板" stackId="tier" fill="#cbd5e1" />
                <Bar yAxisId="count" dataKey="boards5" name="5板及以上" stackId="tier" fill="#f8fafc" />
                <Line
                  yAxisId="max"
                  type="monotone"
                  dataKey="streak"
                  name="最高连板"
                  stroke={STREAK}
                  strokeWidth={2}
                  dot={{ r: 2 }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      <div className="text-xs text-slate-500">
        情绪因子仅供参考研究，不构成投资建议。
      </div>
    </div>
  );
}
