import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  cancelPortfolioBacktest,
  createPortfolioBacktest,
  fetchPortfolioBacktest,
  listStrategies,
} from "../lib/api";
import type { PortfolioBacktestResponse } from "../lib/types";

const PRESETS = [
  { label: "近 3 个月", days: 90 },
  { label: "近 6 个月", days: 180 },
  { label: "近 1 年", days: 365 },
  { label: "近 2 年", days: 730 },
];

export default function PortfolioBacktestPage() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState(() => {
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - 180);
    return {
      symbols: ["600000", "000001"],
      symbolsText: "600000, 000001",
      weightsText: "0.5, 0.5",
      strategy_name: "ma_cross",
      benchmark_symbol: "000300",
      start_time: start.toISOString().slice(0, 10),
      end_time: end.toISOString().slice(0, 10),
      initial_cash: 200_000,
    };
  });
  const [currentId, setCurrentId] = useState<number | null>(null);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: listStrategies,
  });

  const createMut = useMutation({
    mutationFn: () => {
      const symbols = form.symbolsText
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const weightsArr = form.weightsText
        .split(/[,，\s]+/)
        .map((s) => parseFloat(s.trim()))
        .filter((n) => !Number.isNaN(n));
      const weights: Record<string, number> = {};
      symbols.forEach((s, i) => {
        if (i < weightsArr.length) weights[s] = weightsArr[i];
      });
      return createPortfolioBacktest({
        symbols,
        strategy_name: form.strategy_name,
        weights: Object.keys(weights).length ? weights : null,
        benchmark_symbol: form.benchmark_symbol || null,
        start_time: new Date(form.start_time).toISOString(),
        end_time: new Date(form.end_time).toISOString(),
        initial_cash: form.initial_cash,
        idempotency_key: `m3-${currentId ?? "first"}`,
      });
    },
    onSuccess: (data) => {
      setCurrentId(data.id);
      queryClient.invalidateQueries({ queryKey: ["portfolio-backtest", data.id] });
    },
  });

  const { data: result, isFetching } = useQuery<PortfolioBacktestResponse>({
    queryKey: ["portfolio-backtest", currentId],
    queryFn: () => fetchPortfolioBacktest(currentId!),
    enabled: currentId !== null,
    refetchInterval: (q) => {
      const data = q.state.data as PortfolioBacktestResponse | undefined;
      return data?.status === "queued" || data?.status === "running" ? 1500 : false;
    },
  });

  const cancelMut = useMutation({
    mutationFn: () => cancelPortfolioBacktest(currentId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["portfolio-backtest", currentId] });
    },
  });

  const applyPreset = (days: number) => {
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - days);
    setForm((f) => ({
      ...f,
      start_time: start.toISOString().slice(0, 10),
      end_time: end.toISOString().slice(0, 10),
    }));
  };

  const chartData = useMemo(() => {
    if (!result?.result) return [];
    const dates = result.result.dates;
    const ec = result.result.equity_curve;
    const bc = result.result.benchmark_curve;
    return dates.map((d, i) => ({
      date: d,
      组合: ec[i],
      基准: bc[i],
    }));
  }, [result]);

  const statusColor = (s: string | undefined) => {
    switch (s) {
      case "succeeded":
        return "text-green-400";
      case "running":
        return "text-blue-400";
      case "queued":
        return "text-yellow-400";
      case "failed":
        return "text-red-400";
      case "cancelled":
        return "text-slate-400";
      default:
        return "text-slate-400";
    }
  };

  return (
    <div className="max-w-5xl space-y-6">
      <h1 className="text-2xl font-semibold">组合回测</h1>

      {/* 创建表单 */}
      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
        <h2 className="font-medium mb-3">新建组合回测</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Field label="股票代码（逗号分隔）">
            <input
              type="text"
              value={form.symbolsText}
              onChange={(e) =>
                setForm((f) => ({ ...f, symbolsText: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </Field>
          <Field label="权重（与代码顺序对应，留空=等权）">
            <input
              type="text"
              value={form.weightsText}
              onChange={(e) =>
                setForm((f) => ({ ...f, weightsText: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </Field>
          <Field label="策略">
            <select
              value={form.strategy_name}
              onChange={(e) =>
                setForm((f) => ({ ...f, strategy_name: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            >
              {(strategies ?? []).map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="基准代码">
            <input
              type="text"
              value={form.benchmark_symbol}
              onChange={(e) =>
                setForm((f) => ({ ...f, benchmark_symbol: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </Field>
          <Field label="初始资金">
            <input
              type="number"
              value={form.initial_cash}
              onChange={(e) =>
                setForm((f) => ({ ...f, initial_cash: Number(e.target.value) }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
          </Field>
          <Field label="日期范围">
            <div className="flex gap-2">
              <input
                type="date"
                value={form.start_time}
                onChange={(e) =>
                  setForm((f) => ({ ...f, start_time: e.target.value }))
                }
                className="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
              />
              <span className="self-center text-slate-500">至</span>
              <input
                type="date"
                value={form.end_time}
                onChange={(e) =>
                  setForm((f) => ({ ...f, end_time: e.target.value }))
                }
                className="flex-1 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
              />
            </div>
          </Field>
        </div>

        <div className="flex items-center gap-2 mt-4">
          <button
            onClick={() => createMut.mutate()}
            disabled={createMut.isPending}
            className="bg-blue-600 hover:bg-blue-500 px-4 py-1.5 rounded text-sm font-medium disabled:opacity-50"
          >
            {createMut.isPending ? "提交中…" : "提交任务"}
          </button>
          {PRESETS.map((p) => (
            <button
              key={p.days}
              onClick={() => applyPreset(p.days)}
              className="text-xs px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded"
            >
              {p.label}
            </button>
          ))}
          {createMut.error && (
            <span className="text-red-400 text-xs">
              {(createMut.error as Error).message ?? "提交失败"}
            </span>
          )}
        </div>
      </div>

      {/* 当前任务详情 */}
      {result && (
        <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-medium">
              任务 #{result.id} —{" "}
              <span className={`font-mono ${statusColor(result.status)}`}>
                {result.status}
              </span>
              {isFetching && (
                <span className="text-xs text-slate-500 ml-2">刷新中…</span>
              )}
            </h2>
            {(result.status === "queued" || result.status === "running") && (
              <button
                onClick={() => cancelMut.mutate()}
                disabled={cancelMut.isPending}
                className="text-xs px-3 py-1 bg-red-700 hover:bg-red-600 rounded"
              >
                取消
              </button>
            )}
          </div>

          {/* 进度条 */}
          <div className="mb-3">
            <div className="text-xs text-slate-400 mb-1">
              进度：{result.progress}%
            </div>
            <div className="w-full bg-slate-800 rounded-full overflow-hidden h-2">
              <div
                className="bg-blue-500 h-2 transition-all"
                style={{ width: `${result.progress}%` }}
              />
            </div>
          </div>

          {result.error_message && (
            <div className="text-red-400 text-sm mb-3">
              错误：{result.error_message}
            </div>
          )}

          {result.result && (
            <>
              {/* 净值曲线 */}
              <div className="bg-slate-950 rounded p-3 mb-4" style={{ height: 320 }}>
                <ResponsiveContainer>
                  <LineChart data={chartData}>
                    <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                    <XAxis dataKey="date" hide />
                    <YAxis
                      tick={{ fill: "#94a3b8", fontSize: 11 }}
                      tickFormatter={(v) => `${(v / 10000).toFixed(0)}万`}
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "#1e293b",
                        border: "1px solid #334155",
                      }}
                      labelStyle={{ color: "#cbd5e1" }}
                    />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey="组合"
                      stroke="#10b981"
                      dot={false}
                      strokeWidth={2}
                    />
                    <Line
                      type="monotone"
                      dataKey="基准"
                      stroke="#94a3b8"
                      dot={false}
                      strokeDasharray="4 2"
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>

              {/* 关键指标 */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                <Stat label="总收益" value={pct(result.result.total_return)} />
                <Stat label="年化" value={pct(result.result.annual_return)} />
                <Stat
                  label="最大回撤"
                  value={pct(result.result.max_drawdown)}
                  color="text-red-400"
                />
                <Stat label="夏普" value={result.result.sharpe_ratio.toFixed(2)} />
                <Stat label="胜率" value={pct(result.result.win_rate)} />
                <Stat
                  label="盈亏比"
                  value={result.result.profit_loss_ratio.toFixed(2)}
                />
                <Stat
                  label="换手率"
                  value={pct(result.result.turnover)}
                />
                <Stat label="集中度" value={pct(result.result.concentration)} />
                <Stat label="基准收益" value={pct(result.result.benchmark_return)} />
                <Stat
                  label="超额收益"
                  value={pct(result.result.excess_return)}
                  color={
                    result.result.excess_return > 0
                      ? "text-green-400"
                      : "text-red-400"
                  }
                />
                <Stat
                  label="Alpha"
                  value={result.result.alpha.toFixed(4)}
                />
                <Stat label="Beta" value={result.result.beta.toFixed(2)} />
                <Stat
                  label="信息比率"
                  value={result.result.information_ratio.toFixed(2)}
                />
                <Stat
                  label="跟踪误差"
                  value={pct(result.result.tracking_error)}
                />
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs text-slate-400 mb-1 block">{label}</span>
      {children}
    </label>
  );
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="bg-slate-950 rounded px-3 py-2">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`text-base font-mono ${color ?? "text-slate-100"}`}>
        {value}
      </div>
    </div>
  );
}

function pct(v: number): string {
  return `${(v * 100).toFixed(2)}%`;
}
