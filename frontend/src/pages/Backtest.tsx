import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  cancelBacktest,
  createBacktest,
  fetchBacktest,
  listStrategies,
} from "../lib/api";
import type { BacktestResponse } from "../lib/types";

const PRESETS = [
  { label: "近 3 个月", days: 90 },
  { label: "近 6 个月", days: 180 },
  { label: "近 1 年", days: 365 },
  { label: "近 2 年", days: 730 },
];

export default function BacktestPage() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState(() => {
    const end = new Date();
    const start = new Date();
    start.setDate(start.getDate() - 180);
    return {
      symbol: "600000",
      strategy_name: "ma_cross",
      start_time: start.toISOString().slice(0, 10),
      end_time: end.toISOString().slice(0, 10),
      initial_cash: 100_000,
    };
  });
  const [currentId, setCurrentId] = useState<number | null>(null);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: listStrategies,
  });

  const createMut = useMutation({
    mutationFn: () =>
      createBacktest({
        ...form,
        start_time: new Date(form.start_time).toISOString(),
        end_time: new Date(form.end_time).toISOString(),
      }),
    onSuccess: (data) => {
      setCurrentId(data.id);
      queryClient.invalidateQueries({ queryKey: ["backtest", data.id] });
    },
  });

  const { data: result, isFetching } = useQuery({
    queryKey: ["backtest", currentId],
    queryFn: () => fetchBacktest(currentId!),
    enabled: currentId !== null,
    refetchInterval: (q) => {
      const data = q.state.data as BacktestResponse | undefined;
      return data?.status === "queued" || data?.status === "running" ? 1500 : false;
    },
  });

  const cancelMut = useMutation({
    mutationFn: () => cancelBacktest(currentId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["backtest", currentId] });
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

  return (
    <div className="max-w-5xl space-y-6">
      <h1 className="text-2xl font-semibold">历史回测</h1>

      {/* 创建表单 */}
      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
        <h2 className="font-medium mb-3">新建回测任务</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Field label="股票代码">
            <input
              type="text"
              value={form.symbol}
              onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))}
              maxLength={6}
              className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm focus:border-sky-500 outline-none"
            />
          </Field>
          <Field label="策略">
            <select
              value={form.strategy_name}
              onChange={(e) =>
                setForm((f) => ({ ...f, strategy_name: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
            >
              {strategies?.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="开始日期">
            <input
              type="date"
              value={form.start_time}
              onChange={(e) =>
                setForm((f) => ({ ...f, start_time: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
            />
          </Field>
          <Field label="结束日期">
            <input
              type="date"
              value={form.end_time}
              onChange={(e) =>
                setForm((f) => ({ ...f, end_time: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
            />
          </Field>
          <Field label="初始资金">
            <input
              type="number"
              value={form.initial_cash}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  initial_cash: Number(e.target.value) || 0,
                }))
              }
              min={1000}
              step={1000}
              className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
            />
          </Field>
          <Field label="日期范围预设">
            <div className="flex flex-wrap gap-1">
              {PRESETS.map((p) => (
                <button
                  key={p.days}
                  onClick={() => applyPreset(p.days)}
                  className="text-xs px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </Field>
        </div>
        <button
          onClick={() => createMut.mutate()}
          disabled={createMut.isPending}
          className="mt-4 px-4 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 disabled:text-slate-500 rounded text-sm"
        >
          {createMut.isPending ? "提交中..." : "开始回测"}
        </button>
        {createMut.isError && (
          <p className="text-rose-400 text-xs mt-2">
            提交失败：{(createMut.error as Error).message}
          </p>
        )}
      </div>

      {/* 结果 */}
      {result && (
        <div className="bg-slate-900 rounded-lg p-4 border border-slate-800 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">回测结果 · ID #{result.id}</h2>
            <div className="flex items-center gap-2">
              {(result.status === "queued" || result.status === "running") && (
                <button
                  onClick={() => cancelMut.mutate()}
                  disabled={cancelMut.isPending}
                  className="text-xs px-2 py-1 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 rounded"
                >
                  取消
                </button>
              )}
              <StatusBadge status={result.status} />
            </div>
          </div>

          {(result.status === "queued" || result.status === "running") && (
            <div>
              <div className="flex justify-between text-xs text-slate-400 mb-1">
                <span>
                  {result.status === "queued" ? "排队中" : "回测进行中"}
                </span>
                <span>{result.progress ?? 0}%</span>
              </div>
              <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className="h-full bg-sky-500 transition-all"
                  style={{ width: `${result.progress ?? 0}%` }}
                />
              </div>
            </div>
          )}

          {result.status === "succeeded" && result.result && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Stat label="总收益率" value={pct(result.result.total_return)} highlight />
                <Stat label="年化" value={pct(result.result.annual_return)} />
                <Stat label="最大回撤" value={pct(result.result.max_drawdown)} negative />
                <Stat label="夏普" value={result.result.sharpe_ratio.toFixed(3)} />
                <Stat label="胜率" value={pct(result.result.win_rate)} />
                <Stat
                  label="盈亏比"
                  value={
                    isFinite(result.result.profit_loss_ratio)
                      ? result.result.profit_loss_ratio.toFixed(2)
                      : "∞"
                  }
                />
                <Stat label="交易次数" value={String(result.result.trade_count)} />
                <Stat
                  label="期末资产"
                  value={
                    result.result.equity_curve[result.result.equity_curve.length - 1]
                      ? "¥" +
                        result.result.equity_curve[
                          result.result.equity_curve.length - 1
                        ].toFixed(0)
                      : "—"
                  }
                />
              </div>

              {/* 结果口径（D6/D8）：单标的结果也必须自报口径，否则无法与组合结果并列引用 */}
              <div className="mt-3 rounded border border-slate-800 bg-slate-950/60 p-3 text-xs space-y-1">
                <div className="font-medium text-slate-300">结果口径（引用前必读）</div>
                <div className="text-slate-400">
                  复权口径：
                  <span className="text-slate-200">
                    {result.result.bars_adjust_label ??
                      result.result.bars_adjust ??
                      "未标注（旧任务）"}
                  </span>
                  {result.result.limit_reference === "unadjusted_previous_close" && (
                    <>
                      {" ｜ 涨跌停判定基准："}
                      <span className="text-slate-200">未复权昨收</span>
                    </>
                  )}
                  {typeof result.result.limit_reference_missing === "number" &&
                    result.result.limit_reference_missing > 0 && (
                      <span className="ml-2 text-amber-400">
                        ⚠ 有 {result.result.limit_reference_missing} 根 K 线未能对齐未复权昨收，
                        对应交易日已跳过涨跌停判断
                      </span>
                    )}
                </div>
                {result.result.return_convention_note && (
                  <div className="text-slate-500">
                    {result.result.return_convention_note}
                  </div>
                )}
              </div>

              {result.result.equity_curve.length > 0 && (
                <div className="h-64 bg-slate-950 rounded p-2">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                      data={result.result.equity_curve.map((v, i) => ({
                        i,
                        v,
                      }))}
                    >
                      <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                      <XAxis dataKey="i" tick={{ fill: "#94a3b8", fontSize: 10 }} />
                      <YAxis
                        tick={{ fill: "#94a3b8", fontSize: 10 }}
                        domain={["auto", "auto"]}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "#0f172a",
                          border: "1px solid #334155",
                          fontSize: 12,
                        }}
                        formatter={(v: number) => "¥" + v.toFixed(2)}
                      />
                      <Line
                        type="monotone"
                        dataKey="v"
                        stroke="#38bdf8"
                        dot={false}
                        strokeWidth={1.5}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}

              {result.result.trades.length > 0 && (
                <details className="text-sm">
                  <summary className="cursor-pointer text-slate-400">
                    成交记录（{result.result.trades.length} 笔）
                  </summary>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs mt-2">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="text-left">时间</th>
                        <th className="text-left">方向</th>
                        <th className="text-right">价格</th>
                        <th className="text-right">数量</th>
                        <th className="text-right">手续费</th>
                        <th className="text-right">印花税</th>
                        <th className="text-right">盈亏</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.result.trades.map((t, i) => (
                        <tr
                          key={i}
                          className="border-b border-slate-800/50"
                        >
                          <td className="text-slate-500">
                            {new Date(t.time).toLocaleString()}
                          </td>
                          <td
                            className={
                              t.side === "BUY" ? "text-up" : "text-down"
                            }
                          >
                            {t.side}
                          </td>
                          <td className="text-right numeric">
                            {t.price.toFixed(2)}
                          </td>
                          <td className="text-right numeric">{t.quantity}</td>
                          <td className="text-right numeric">
                            {t.commission.toFixed(2)}
                          </td>
                          <td className="text-right numeric">
                            {t.stamp_tax != null ? t.stamp_tax.toFixed(2) : "—"}
                          </td>
                          <td
                            className={`text-right numeric ${
                              t.pnl > 0
                                ? "text-up"
                                : t.pnl < 0
                                  ? "text-down"
                                  : ""
                            }`}
                          >
                            {t.pnl.toFixed(2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    </table>
                  </div>
                </details>
              )}
            </>
          )}

          {result.status === "failed" && (
            <p className="text-rose-400 text-sm">
              回测失败：{result.error_message ?? "未知错误"}
            </p>
          )}

          {result.status === "cancelled" && (
            <p className="text-amber-400 text-sm">回测已取消</p>
          )}
        </div>
      )}

      {isFetching && !result && (
        <div className="text-slate-500 text-center py-4">查询中...</div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      {children}
    </label>
  );
}

function Stat({
  label,
  value,
  highlight,
  negative,
}: {
  label: string;
  value: string;
  highlight?: boolean;
  negative?: boolean;
}) {
  const color = highlight
    ? "text-sky-400"
    : negative
      ? "text-rose-400"
      : "text-slate-200";
  return (
    <div className="bg-slate-950 rounded p-3">
      <div className="text-xs text-slate-500 mb-1">{label}</div>
      <div className={`numeric text-lg ${color}`}>{value}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "succeeded"
      ? "bg-emerald-500/20 text-emerald-300"
      : status === "failed"
        ? "bg-rose-500/20 text-rose-300"
        : status === "cancelled"
          ? "bg-slate-500/20 text-slate-300"
          : "bg-amber-500/20 text-amber-300";
  const label =
    status === "queued"
      ? "排队中"
      : status === "running"
        ? "运行中"
        : status === "succeeded"
          ? "已完成"
          : status === "failed"
            ? "失败"
            : "已取消";
  return (
    <span className={`text-xs px-2 py-1 rounded ${color}`}>{label}</span>
  );
}

function pct(v: number) {
  return `${(v * 100).toFixed(2)}%`;
}
