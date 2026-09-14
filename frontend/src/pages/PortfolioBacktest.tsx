import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  cancelPortfolioBacktest,
  createPortfolioBacktest,
  fetchPortfolioBacktest,
  listBenchmarkIndices,
  listStrategies,
} from "../lib/api";
import type {
  PortfolioBacktestResponse,
  PortfolioSentimentSummary,
  SentimentGateDay,
} from "../lib/types";

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
      benchmark_symbol: "sh000300",
      start_time: start.toISOString().slice(0, 10),
      end_time: end.toISOString().slice(0, 10),
      initial_cash: 200_000,
      participationRatePct: "",
      allowPartialFill: true,
      gateEnabled: false,
      gateLagDays: 1,
      gateMinSealRate: "60",
      gateMinMaxStreak: "",
      gateMinLimitUpCount: "",
      gateOnMissing: "allow" as "allow" | "block",
      gateScaleExposure: false,
      gateMinExposure: "30",
    };
  });
  const [currentId, setCurrentId] = useState<number | null>(null);

  const { data: strategies } = useQuery({
    queryKey: ["strategies"],
    queryFn: listStrategies,
  });

  const { data: benchmarkIndices } = useQuery({
    queryKey: ["benchmark-indices"],
    queryFn: listBenchmarkIndices,
    staleTime: 60 * 60 * 1000,
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
        max_participation_rate: percentToRate(form.participationRatePct) ?? 0,
        allow_partial_fill: form.allowPartialFill,
        sentiment_gate: form.gateEnabled
          ? {
              enabled: true,
              lag_days: form.gateLagDays,
              min_seal_rate: percentToRate(form.gateMinSealRate),
              max_broken_rate: null,
              min_max_streak: countOrNull(form.gateMinMaxStreak),
              min_limit_up_count: countOrNull(form.gateMinLimitUpCount),
              on_missing: form.gateOnMissing,
              scale_exposure: form.gateScaleExposure,
              min_exposure: percentToRate(form.gateMinExposure) ?? 0.3,
            }
          : null,
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

  // 启用闸门但一个条件都没配时后端会 422，这里提前拦住并给出提示
  const gateInvalid =
    form.gateEnabled &&
    percentToRate(form.gateMinSealRate) === null &&
    countOrNull(form.gateMinMaxStreak) === null &&
    countOrNull(form.gateMinLimitUpCount) === null &&
    !form.gateScaleExposure;

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
              list="benchmark-index-options"
              placeholder="sh000300"
              value={form.benchmark_symbol}
              onChange={(e) =>
                setForm((f) => ({ ...f, benchmark_symbol: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
            <datalist id="benchmark-index-options">
              {(benchmarkIndices?.items ?? []).map((idx) => (
                <option key={idx.symbol} value={idx.symbol}>
                  {idx.name}
                </option>
              ))}
            </datalist>
            <p className="mt-1 text-xs text-slate-500">
              指数要带交易所前缀（sh / sz）；也可直接填个股代码。
            </p>
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
          <Field label="成交量参与率上限（%，留空=不限制）">
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              placeholder="不限"
              value={form.participationRatePct}
              onChange={(e) =>
                setForm((f) => ({ ...f, participationRatePct: e.target.value }))
              }
              className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
            />
            <p className="mt-1 text-xs text-slate-500">
              单笔成交量不得超过当日成交量的该比例；填 1 表示最多吃掉 1% 成交量。
              不填则沿用旧行为（视为无限容量，容量风险被隐藏）。
            </p>
          </Field>
        </div>

        <label className="mt-3 flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.allowPartialFill}
            disabled={!form.participationRatePct.trim()}
            onChange={(e) =>
              setForm((f) => ({ ...f, allowPartialFill: e.target.checked }))
            }
          />
          超出参与率上限时按可成交量部分成交（不勾选则整笔拒绝）
          {!form.participationRatePct.trim() && (
            <span className="text-xs text-slate-500">（需先填参与率上限）</span>
          )}
        </label>

        {/* 市场情绪闸门（可选） */}
        <div className="mt-4 border-t border-slate-800 pt-3">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.gateEnabled}
              onChange={(e) =>
                setForm((f) => ({ ...f, gateEnabled: e.target.checked }))
              }
            />
            启用涨停板市场情绪闸门（弱势不开新仓，SELL 不受影响）
          </label>

          {form.gateEnabled && (
            <div className="mt-3 pl-6 space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Field label="封板率下限（%）">
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={form.gateMinSealRate}
                    onChange={(e) =>
                      setForm((f) => ({ ...f, gateMinSealRate: e.target.value }))
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                  />
                </Field>
                <Field label="最高连板下限（板）">
                  <input
                    type="number"
                    min={0}
                    value={form.gateMinMaxStreak}
                    placeholder="不限"
                    onChange={(e) =>
                      setForm((f) => ({ ...f, gateMinMaxStreak: e.target.value }))
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                  />
                </Field>
                <Field label="涨停家数下限（只）">
                  <input
                    type="number"
                    min={0}
                    value={form.gateMinLimitUpCount}
                    placeholder="不限"
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        gateMinLimitUpCount: e.target.value,
                      }))
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                  />
                </Field>
                <Field label="滞后交易日">
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={form.gateLagDays}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        gateLagDays: Number(e.target.value),
                      }))
                    }
                    className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                  />
                </Field>
              </div>

              <div className="flex flex-wrap items-center gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={form.gateScaleExposure}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        gateScaleExposure: e.target.checked,
                      }))
                    }
                  />
                  按封板率缩放新开仓仓位
                </label>
                {form.gateScaleExposure && (
                  <label className="flex items-center gap-2 text-xs text-slate-400">
                    仓位下限（%）
                    <input
                      type="number"
                      min={0}
                      max={100}
                      value={form.gateMinExposure}
                      onChange={(e) =>
                        setForm((f) => ({
                          ...f,
                          gateMinExposure: e.target.value,
                        }))
                      }
                      className="w-20 bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                    />
                  </label>
                )}
                <label className="flex items-center gap-2 text-xs text-slate-400">
                  情绪缺失时
                  <select
                    value={form.gateOnMissing}
                    onChange={(e) =>
                      setForm((f) => ({
                        ...f,
                        gateOnMissing: e.target.value as "allow" | "block",
                      }))
                    }
                    className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm"
                  >
                    <option value="allow">放行</option>
                    <option value="block">拦截</option>
                  </select>
                </label>
              </div>

              <p className="text-xs text-slate-500">
                情绪取自本地「涨停板情绪」序列（东财实时抓取 + 日线离线回算）。区间内
                没有情绪数据时任务会直接失败，请先到「数据中心 → 涨停情绪」执行历史回补。
                涨停家数是绝对量、随样本面变化，建议优先用封板率与连板高度。
              </p>
              {gateInvalid && (
                <p className="text-xs text-yellow-400">
                  至少要配置一个阈值条件（封板率 / 连板高度 / 涨停家数），或打开仓位缩放。
                </p>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 mt-4">
          <button
            onClick={() => createMut.mutate()}
            disabled={createMut.isPending || gateInvalid}
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

              {/* 口径与容量（D5~D9）：这些字段决定结果能怎么被引用，必须显示 */}
              <div className="mt-4 rounded border border-slate-800 bg-slate-950/60 p-3 text-xs space-y-2">
                <div className="font-medium text-slate-300">结果口径（引用前必读）</div>
                <div className="text-slate-400">
                  复权口径：
                  <span className="text-slate-200">
                    {result.result.bars_adjust_label ?? result.result.bars_adjust ?? "未标注"}
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
                <div className="text-slate-400">
                  基准分列：同池等权{" "}
                  <span className="text-slate-200">
                    {pct(result.result.equal_weight_return ?? 0)}
                  </span>
                  {" ｜ 含现金 "}
                  <span className="text-slate-200">
                    {pct(result.result.cash_return ?? 0)}
                  </span>
                  {" ｜ 超额（对等权）"}
                  <span
                    className={
                      (result.result.excess_vs_equal_weight ?? 0) > 0
                        ? "text-green-400"
                        : "text-red-400"
                    }
                  >
                    {pct(result.result.excess_vs_equal_weight ?? 0)}
                  </span>
                </div>
                <div className="text-slate-400">
                  容量（D5）：
                  {result.result.min_capacity_multiple == null ? (
                    <span className="text-amber-400">
                      未启用参与率上限，容量风险未建模
                    </span>
                  ) : (
                    <span
                      className={
                        result.result.min_capacity_multiple < 1
                          ? "text-red-400"
                          : "text-slate-200"
                      }
                    >
                      最小容量倍数 {result.result.min_capacity_multiple.toFixed(2)}
                      {result.result.min_capacity_multiple < 1 &&
                        "（< 1：有成交超出当日可承受容量）"}
                      ，部分成交 {result.result.partial_fill_count ?? 0} 笔
                    </span>
                  )}
                </div>
                {result.result.capacity && (
                  <div className="text-slate-400">
                    规模上界（D5）：不超容量的账户规模约{" "}
                    <span className="text-slate-200">
                      ¥{Math.round(result.result.capacity.max_aum).toLocaleString("zh-CN")}
                    </span>
                    {" ｜ 最紧的一笔 "}
                    <span className="text-slate-200">
                      {result.result.capacity.binding_trade.date}
                      {result.result.capacity.binding_trade.symbol
                        ? ` ${result.result.capacity.binding_trade.symbol}`
                        : ""}
                      （倍数{" "}
                      {result.result.capacity.binding_trade.capacity_multiple.toFixed(3)}）
                    </span>
                    <div className="mt-1 text-slate-500">
                      上界 = min(成交日净值 × 容量倍数)，基于{" "}
                      {result.result.capacity.considered_trades} 笔成交。
                      {result.result.capacity.assumptions.join(" ")}
                    </div>
                  </div>
                )}
                <div className="text-slate-400">
                  尾部风险：年化波动{" "}
                  <span className="text-slate-200">
                    {pct(result.result.volatility ?? 0)}
                  </span>
                  {" ｜ VaR95 "}
                  <span className="text-slate-200">{pct(result.result.var_95 ?? 0)}</span>
                  {" ｜ CVaR95 "}
                  <span className="text-slate-200">{pct(result.result.cvar_95 ?? 0)}</span>
                  {" ｜ 最长回撤 "}
                  <span className="text-slate-200">
                    {result.result.max_drawdown_duration ?? 0} 个交易日
                  </span>
                  {" ｜ 最差单日 "}
                  <span className="text-slate-200">
                    {pct(result.result.worst_day_return ?? 0)}
                  </span>
                </div>
                {result.result.benchmark_labels &&
                  Object.values(result.result.benchmark_labels).length > 0 && (
                    <ul className="list-disc pl-4 text-slate-500">
                      {Object.values(result.result.benchmark_labels).map((t, i) => (
                        <li key={i}>{t}</li>
                      ))}
                    </ul>
                  )}
              </div>

              {result.result.sentiment && (
                <SentimentPanel summary={result.result.sentiment} />
              )}
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

function SentimentPanel({ summary }: { summary: PortfolioSentimentSummary }) {
  const config = summary.config;
  const chartData = summary.days.map((day) => ({
    date: day.date,
    封板率: day.seal_rate,
  }));
  const ranges = groupBlockedRanges(summary.days);

  return (
    <div className="mt-4 border-t border-slate-800 pt-3">
      <h3 className="font-medium mb-2">市场情绪闸门</h3>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <Stat
          label="拦截交易日"
          value={`${summary.blocked_days} / ${summary.total_days}`}
          color={summary.blocked_days > 0 ? "text-yellow-400" : undefined}
        />
        <Stat
          label="被拦截的买入信号"
          value={String(summary.blocked_buy_signals)}
          color={
            summary.blocked_buy_signals > 0 ? "text-yellow-400" : undefined
          }
        />
        <Stat
          label="平均新开仓系数"
          value={`${(summary.average_buy_exposure * 100).toFixed(1)}%`}
        />
        <Stat label="情绪序列长度" value={`${summary.series_days} 天`} />
      </div>

      <p className="text-xs text-slate-500 mt-2">
        参数：滞后 {config.lag_days} 个交易日
        {config.min_seal_rate !== null
          ? ` · 封板率下限 ${(config.min_seal_rate * 100).toFixed(0)}%`
          : ""}
        {config.min_max_streak !== null
          ? ` · 连板高度下限 ${config.min_max_streak}`
          : ""}
        {config.min_limit_up_count !== null
          ? ` · 涨停家数下限 ${config.min_limit_up_count}`
          : ""}
        {config.scale_exposure
          ? ` · 仓位下限 ${(config.min_exposure * 100).toFixed(0)}%`
          : ""}
      </p>

      {chartData.length > 0 && (
        <div className="bg-slate-950 rounded p-3 mt-3" style={{ height: 240 }}>
          <ResponsiveContainer>
            <LineChart data={chartData}>
              <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
              <XAxis dataKey="date" hide />
              <YAxis
                tick={{ fill: "#94a3b8", fontSize: 11 }}
                tickFormatter={(v) => `${(Number(v) * 100).toFixed(0)}%`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "#1e293b",
                  border: "1px solid #334155",
                }}
                labelStyle={{ color: "#cbd5e1" }}
                formatter={(value) =>
                  typeof value === "number"
                    ? `${(value * 100).toFixed(1)}%`
                    : "-"
                }
              />
              {config.min_seal_rate !== null && (
                <ReferenceLine
                  y={config.min_seal_rate}
                  stroke="#ef4444"
                  strokeDasharray="4 2"
                  label={{ value: "下限", fill: "#ef4444", fontSize: 11 }}
                />
              )}
              <Line
                type="monotone"
                dataKey="封板率"
                stroke="#38bdf8"
                dot={false}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {ranges.length > 0 && (
        <div className="mt-3 text-xs text-slate-400">
          <div className="mb-1">
            被拦截的连续区间（共 {ranges.length} 段）：
          </div>
          <div className="flex flex-wrap gap-2">
            {ranges.slice(0, 12).map((range) => (
              <span
                key={range.start}
                className="bg-slate-800 rounded px-2 py-0.5 font-mono"
              >
                {range.start}
                {range.end !== range.start ? ` ~ ${range.end}` : ""}
                {range.count > 1 ? ` (${range.count})` : ""}
              </span>
            ))}
            {ranges.length > 12 && <span>…</span>}
          </div>
        </div>
      )}

      {summary.missing_days.length > 0 && (
        <p className="text-xs text-slate-500 mt-2">
          有 {summary.missing_days.length} 个交易日没有可用的前置情绪数据，按「
          {config.on_missing === "block" ? "拦截" : "放行"}」处理。
        </p>
      )}
    </div>
  );
}

interface BlockedRange {
  start: string;
  end: string;
  count: number;
}

function groupBlockedRanges(days: SentimentGateDay[]): BlockedRange[] {
  const ranges: BlockedRange[] = [];
  let index = 0;
  while (index < days.length) {
    if (days[index].allowed) {
      index += 1;
      continue;
    }
    const startIndex = index;
    while (index + 1 < days.length && !days[index + 1].allowed) {
      index += 1;
    }
    ranges.push({
      start: days[startIndex].date,
      end: days[index].date,
      count: index - startIndex + 1,
    });
    index += 1;
  }
  return ranges;
}

function percentToRate(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  const parsed = Number(text);
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.min(1, parsed / 100));
}

function countOrNull(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}
