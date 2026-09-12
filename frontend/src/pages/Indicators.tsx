/** 技术指标页：单只标的的技术面体检。
 *
 * 数据来自 ``GET /api/indicators/{symbol}``（MA / EMA / MACD / RSI / BOLL /
 * KDJ / ATR / OBV / CCI / WR），后端优先读本地缓存，不足时经数据源补齐。
 * 页面只做计算与展示，规则提示是固定阈值的机械判断，**不构成投资建议**。
 */
import { useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fetchIndicators, fetchQuote } from "../lib/api";
import type { IndicatorResponse } from "../lib/types";

const PERIODS: Array<{ value: string; label: string }> = [
  { value: "daily", label: "日线" },
  { value: "weekly", label: "周线" },
  { value: "monthly", label: "月线" },
  { value: "60m", label: "60 分钟" },
  { value: "30m", label: "30 分钟" },
  { value: "15m", label: "15 分钟" },
  { value: "5m", label: "5 分钟" },
  { value: "1m", label: "1 分钟" },
];

const LIMIT_OPTIONS = [60, 120, 250, 500, 800];

const PRESETS = [
  { symbol: "600519", label: "贵州茅台" },
  { symbol: "000001", label: "平安银行" },
  { symbol: "300750", label: "宁德时代" },
  { symbol: "920819", label: "颖泰生物(北交所)" },
];

const COLORS = {
  close: "#e2e8f0",
  ma5: "#fbbf24",
  ma20: "#38bdf8",
  ma60: "#a78bfa",
  boll: "#64748b",
  dif: "#fbbf24",
  dea: "#38bdf8",
  k: "#fbbf24",
  d: "#38bdf8",
  j: "#fb7185",
  rsi6: "#fbbf24",
  rsi14: "#38bdf8",
  rsi24: "#a78bfa",
  atr: "#fb7185",
  obv: "#34d399",
  cci: "#fbbf24",
  wr: "#38bdf8",
};

const panel = "bg-slate-900 rounded-lg border border-slate-800";
const axisTick = { fill: "#94a3b8", fontSize: 11 };
const tooltipStyle = {
  background: "#0f172a",
  border: "1px solid #1e293b",
  fontSize: 12,
};
const legendStyle = { fontSize: 12 };
const selectCls =
  "bg-slate-950 border border-slate-800 rounded px-2 py-1 text-sm text-slate-300";

const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(digits);

interface Reading {
  label: string;
  value: string;
  tone: "up" | "down" | "flat";
  note: string;
}

function toneClass(tone: Reading["tone"]): string {
  if (tone === "up") return "text-rose-400";
  if (tone === "down") return "text-emerald-400";
  return "text-slate-300";
}

/** 固定阈值的机械判读，仅用于快速定位，不构成任何买卖建议。 */
function buildReadings(latest: Record<string, number | null>, close: number): Reading[] {
  const get = (key: string) => latest[key] ?? null;
  const out: Reading[] = [];

  const ma5 = get("ma5");
  const ma20 = get("ma20");
  const ma60 = get("ma60");
  if (ma5 !== null && ma20 !== null && ma60 !== null) {
    const bull = ma5 > ma20 && ma20 > ma60;
    const bear = ma5 < ma20 && ma20 < ma60;
    out.push({
      label: "均线排列",
      value: bull ? "多头排列" : bear ? "空头排列" : "均线纠缠",
      tone: bull ? "up" : bear ? "down" : "flat",
      note: `MA5 ${num(ma5)} / MA20 ${num(ma20)} / MA60 ${num(ma60)}`,
    });
  }

  const dif = get("macd_dif");
  const dea = get("macd_dea");
  if (dif !== null && dea !== null) {
    const above = dif > dea;
    out.push({
      label: "MACD",
      value: above ? "DIF 在 DEA 上方" : "DIF 在 DEA 下方",
      tone: above ? "up" : "down",
      note: `DIF ${num(dif, 3)} / DEA ${num(dea, 3)} / 柱 ${num(dif - dea, 3)}`,
    });
  }

  const k = get("kdj_k");
  const d = get("kdj_d");
  const j = get("kdj_j");
  if (k !== null && d !== null) {
    const tone: Reading["tone"] = j !== null && j > 100 ? "up" : j !== null && j < 0 ? "down" : "flat";
    const state = j !== null && j > 100 ? "超买" : j !== null && j < 0 ? "超卖" : k > d ? "偏强" : "偏弱";
    out.push({
      label: "KDJ",
      value: state,
      tone,
      note: `K ${num(k, 1)} / D ${num(d, 1)} / J ${num(j, 1)}`,
    });
  }

  const rsi = get("rsi14");
  if (rsi !== null) {
    out.push({
      label: "RSI14",
      value: rsi > 70 ? "超买" : rsi < 30 ? "超卖" : "中性区间",
      tone: rsi > 70 ? "up" : rsi < 30 ? "down" : "flat",
      note: `当前 ${num(rsi, 1)}（>70 超买 / <30 超卖）`,
    });
  }

  const upper = get("boll_upper");
  const lower = get("boll_lower");
  const middle = get("boll_middle");
  if (upper !== null && lower !== null && middle !== null && upper > lower) {
    const pos = (close - lower) / (upper - lower);
    out.push({
      label: "BOLL 位置",
      value: pos > 1 ? "突破上轨" : pos < 0 ? "跌破下轨" : `通道内 ${(pos * 100).toFixed(0)}%`,
      tone: pos > 1 ? "up" : pos < 0 ? "down" : "flat",
      note: `上轨 ${num(upper)} / 中轨 ${num(middle)} / 下轨 ${num(lower)}`,
    });
  }

  const cci = get("cci14");
  if (cci !== null) {
    out.push({
      label: "CCI14",
      value: cci > 100 ? "超买" : cci < -100 ? "超卖" : "常态区间",
      tone: cci > 100 ? "up" : cci < -100 ? "down" : "flat",
      note: `当前 ${num(cci, 1)}（±100 为界）`,
    });
  }

  const wr = get("wr14");
  if (wr !== null) {
    out.push({
      label: "WR14",
      value: wr < 20 ? "超买" : wr > 80 ? "超卖" : "常态区间",
      tone: wr < 20 ? "up" : wr > 80 ? "down" : "flat",
      note: `当前 ${num(wr, 1)}（0 最强 / 100 最弱，方向与 KDJ 相反）`,
    });
  }

  const atr = get("atr14");
  if (atr !== null && close > 0) {
    const ratio = (atr / close) * 100;
    out.push({
      label: "ATR14 波动",
      value: `${ratio.toFixed(2)}%`,
      tone: "flat",
      note: ratio > 5 ? "日内振幅偏大，注意仓位" : ratio < 2 ? "波动较小" : "波动中等",
    });
  }

  const ratio = get("volume_ratio");
  if (ratio !== null) {
    out.push({
      label: "量比(5日)",
      value: ratio >= 1.5 ? "明显放量" : ratio < 0.8 ? "明显缩量" : "量能平稳",
      tone: ratio >= 1.5 ? "up" : ratio < 0.8 ? "down" : "flat",
      note: `当前 ${num(ratio, 2)}（>1.5 放量 / <0.8 缩量）`,
    });
  }

  const amplitude = get("amplitude");
  if (amplitude !== null) {
    out.push({
      label: "振幅",
      value: `${num(amplitude, 2)}%`,
      tone: "flat",
      note: "当日 (最高 − 最低) / 昨收",
    });
  }

  return out;
}

interface ChartPoint {
  label: string;
  close: number | null;
  ma5: number | null;
  ma20: number | null;
  ma60: number | null;
  bollUpper: number | null;
  bollMiddle: number | null;
  bollLower: number | null;
  dif: number | null;
  dea: number | null;
  hist: number | null;
  k: number | null;
  d: number | null;
  j: number | null;
  rsi6: number | null;
  rsi14: number | null;
  rsi24: number | null;
  atr: number | null;
  obv: number | null;
  cci: number | null;
  wr: number | null;
  amplitude: number | null;
  volumeRatio: number | null;
}

/** 把「等长序列」转成 recharts 需要的行式数据。 */
function buildPoints(data?: IndicatorResponse): ChartPoint[] {
  if (!data) return [];
  const at = (key: string, index: number): number | null =>
    data.series[key]?.[index] ?? null;
  return data.dates.map((label, index) => ({
    label,
    close: data.close[index] ?? null,
    ma5: at("ma5", index),
    ma20: at("ma20", index),
    ma60: at("ma60", index),
    bollUpper: at("boll_upper", index),
    bollMiddle: at("boll_middle", index),
    bollLower: at("boll_lower", index),
    dif: at("macd_dif", index),
    dea: at("macd_dea", index),
    hist: at("macd_hist", index),
    k: at("kdj_k", index),
    d: at("kdj_d", index),
    j: at("kdj_j", index),
    rsi6: at("rsi6", index),
    rsi14: at("rsi14", index),
    rsi24: at("rsi24", index),
    atr: at("atr14", index),
    obv: at("obv", index),
    cci: at("cci14", index),
    wr: at("wr14", index),
    amplitude: at("amplitude", index),
    volumeRatio: at("volume_ratio", index),
  }));
}

/** 分钟线的时间戳含空格，X 轴只显示最后一段，避免标签互相挤压。 */
const shortLabel = (value: string) =>
  value.includes(" ") ? value.split(" ")[1] : value.slice(5);

function ChartCard({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <div className={`${panel} p-4`}>
      <div className="text-sm text-slate-300 mb-2">
        {title}
        {note && <span className="text-xs text-slate-500 ml-2">{note}</span>}
      </div>
      <div className="h-64">{children}</div>
    </div>
  );
}

export default function IndicatorsPage() {
  const [input, setInput] = useState("600519");
  const [symbol, setSymbol] = useState("600519");
  const [period, setPeriod] = useState("daily");
  const [limit, setLimit] = useState(250);

  const indicators = useQuery({
    queryKey: ["indicators", symbol, period, limit],
    queryFn: () => fetchIndicators(symbol, period, limit),
    enabled: /^\d{6}$/.test(symbol),
  });
  const quote = useQuery({
    queryKey: ["quote", symbol],
    queryFn: () => fetchQuote(symbol),
    enabled: /^\d{6}$/.test(symbol),
    retry: false,
  });

  const data = indicators.data;
  const latest = data?.latest ?? {};
  const close = data?.close?.[data.close.length - 1] ?? 0;
  const readings = useMemo(() => buildReadings(latest, close), [latest, close]);
  const points = useMemo(() => buildPoints(data), [data]);

  const changePct =
    quote.data && quote.data.previous_close > 0
      ? ((quote.data.price - quote.data.previous_close) / quote.data.previous_close) * 100
      : null;

  const error = indicators.error ?? quote.error ?? null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end gap-3">
        <div className="mr-auto">
          <h1 className="text-xl font-semibold text-slate-100">技术指标</h1>
          <p className="text-xs text-slate-500 mt-1">
            MA / EMA / MACD / RSI / BOLL / KDJ / ATR / OBV / CCI / WR ·
            参数取行情软件默认值，口径与通达信一致
          </p>
        </div>

        <label className="text-xs text-slate-400 flex items-center gap-2">
          周期
          <select
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
            className={selectCls}
          >
            {PERIODS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label className="text-xs text-slate-400 flex items-center gap-2">
          根数
          <select
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
            className={selectCls}
          >
            {LIMIT_OPTIONS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>

        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const next = input.trim();
            if (/^\d{6}$/.test(next)) setSymbol(next);
          }}
        >
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="6 位代码"
            className={`${selectCls} w-28 numeric`}
          />
          <button
            type="submit"
            className="px-3 py-1.5 rounded text-sm bg-sky-500/20 text-sky-300 border border-sky-500/40"
          >
            查询
          </button>
        </form>
      </header>

      <div className="flex flex-wrap gap-2 text-xs">
        {PRESETS.map((item) => (
          <button
            key={item.symbol}
            type="button"
            onClick={() => {
              setInput(item.symbol);
              setSymbol(item.symbol);
            }}
            className={`px-2 py-1 rounded border transition-colors ${
              symbol === item.symbol
                ? "bg-slate-800 text-slate-100 border-slate-700"
                : "bg-slate-900 text-slate-400 border-slate-800 hover:text-slate-200"
            }`}
          >
            {item.label} <span className="numeric opacity-60">{item.symbol}</span>
          </button>
        ))}
      </div>

      {error && (
        <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
          加载失败：{error instanceof Error ? error.message : String(error)}
        </div>
      )}

      {indicators.isLoading && (
        <div className={`${panel} p-6 text-sm text-slate-500`}>加载中…</div>
      )}

      {data && (
        <div className={`${panel} px-4 py-3 flex flex-wrap items-baseline gap-x-6 gap-y-2`}>
          <div>
            <div className="text-xs text-slate-500">
              {quote.data?.name ?? symbol}
              <span className="ml-2 numeric">{symbol}</span>
            </div>
            <div className="flex items-baseline gap-3">
              <span className="numeric text-2xl text-slate-100">
                {quote.data ? quote.data.price.toFixed(2) : num(close)}
              </span>
              {changePct !== null && (
                <span
                  className={`numeric text-sm ${
                    changePct > 0
                      ? "text-rose-400"
                      : changePct < 0
                        ? "text-emerald-400"
                        : "text-slate-400"
                  }`}
                >
                  {changePct >= 0 ? "+" : ""}
                  {changePct.toFixed(2)}%
                </span>
              )}
            </div>
          </div>
          <div className="text-xs text-slate-500">
            <div>
              K 线 {data.count} 根 · 数据源{" "}
              <span className="text-slate-300">{data.source}</span>
            </div>
            <div>
              区间 {data.dates[0] ?? "—"} ~ {data.dates[data.dates.length - 1] ?? "—"}
            </div>
          </div>
        </div>
      )}

      {readings.length > 0 && (
        <div className={`${panel} p-4`}>
          <div className="text-sm text-slate-300 mb-3">
            指标读数
            <span className="text-xs text-slate-500 ml-2">
              固定阈值机械判读，仅用于快速定位，不构成买卖建议
            </span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            {readings.map((item) => (
              <div
                key={item.label}
                className="bg-slate-950/60 border border-slate-800 rounded px-3 py-2"
              >
                <div className="text-xs text-slate-500">{item.label}</div>
                <div className={`text-base ${toneClass(item.tone)}`}>
                  {item.value}
                </div>
                <div className="text-xs text-slate-500 mt-1">{item.note}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {points.length > 0 && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <ChartCard title="价格 · 均线 · 布林带" note="MA5/20/60 与 BOLL(20,2)">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis domain={["auto", "auto"]} tick={axisTick} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <Line type="monotone" dataKey="bollUpper" name="BOLL 上轨" stroke={COLORS.boll} strokeWidth={1} dot={false} connectNulls />
                <Line type="monotone" dataKey="bollLower" name="BOLL 下轨" stroke={COLORS.boll} strokeWidth={1} dot={false} connectNulls />
                <Line type="monotone" dataKey="bollMiddle" name="BOLL 中轨" stroke={COLORS.boll} strokeDasharray="4 2" strokeWidth={1} dot={false} connectNulls />
                <Line type="monotone" dataKey="ma5" name="MA5" stroke={COLORS.ma5} strokeWidth={1.4} dot={false} connectNulls />
                <Line type="monotone" dataKey="ma20" name="MA20" stroke={COLORS.ma20} strokeWidth={1.4} dot={false} connectNulls />
                <Line type="monotone" dataKey="ma60" name="MA60" stroke={COLORS.ma60} strokeWidth={1.4} dot={false} connectNulls />
                <Line type="monotone" dataKey="close" name="收盘" stroke={COLORS.close} strokeWidth={2} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="MACD(12,26,9)" note="柱 = DIF − DEA，翻红/翻绿看动能">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis tick={axisTick} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <ReferenceLine y={0} stroke="#334155" />
                <Bar dataKey="hist" name="MACD 柱" fill="#475569" />
                <Line type="monotone" dataKey="dif" name="DIF" stroke={COLORS.dif} strokeWidth={1.6} dot={false} connectNulls />
                <Line type="monotone" dataKey="dea" name="DEA" stroke={COLORS.dea} strokeWidth={1.6} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="KDJ(9,3,3)" note="J 值 >100 超买 / <0 超卖">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis tick={axisTick} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <ReferenceLine y={80} stroke="#334155" strokeDasharray="3 3" />
                <ReferenceLine y={20} stroke="#334155" strokeDasharray="3 3" />
                <Line type="monotone" dataKey="k" name="K" stroke={COLORS.k} strokeWidth={1.6} dot={false} connectNulls />
                <Line type="monotone" dataKey="d" name="D" stroke={COLORS.d} strokeWidth={1.6} dot={false} connectNulls />
                <Line type="monotone" dataKey="j" name="J" stroke={COLORS.j} strokeWidth={1.6} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="RSI(6,14,24)" note=">70 超买 / <30 超卖">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis domain={[0, 100]} tick={axisTick} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <ReferenceLine y={70} stroke="#7f1d1d" strokeDasharray="3 3" />
                <ReferenceLine y={30} stroke="#064e3b" strokeDasharray="3 3" />
                <Line type="monotone" dataKey="rsi6" name="RSI6" stroke={COLORS.rsi6} strokeWidth={1.5} dot={false} connectNulls />
                <Line type="monotone" dataKey="rsi14" name="RSI14" stroke={COLORS.rsi14} strokeWidth={1.8} dot={false} connectNulls />
                <Line type="monotone" dataKey="rsi24" name="RSI24" stroke={COLORS.rsi24} strokeWidth={1.5} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="ATR14 与 OBV" note="波动幅度与量能方向（双轴）">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 0, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis yAxisId="atr" tick={axisTick} />
                <YAxis yAxisId="obv" orientation="right" tick={axisTick} width={54} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <Line yAxisId="atr" type="monotone" dataKey="atr" name="ATR14" stroke={COLORS.atr} strokeWidth={1.6} dot={false} connectNulls />
                <Line yAxisId="obv" type="monotone" dataKey="obv" name="OBV" stroke={COLORS.obv} strokeWidth={1.6} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="CCI14 与 WR14" note="CCI ±100 为界；WR 越小越强（方向与 KDJ 相反）">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 0, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis yAxisId="cci" tick={axisTick} />
                <YAxis yAxisId="wr" orientation="right" domain={[0, 100]} tick={axisTick} width={40} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <ReferenceLine yAxisId="cci" y={100} stroke="#7f1d1d" strokeDasharray="3 3" />
                <ReferenceLine yAxisId="cci" y={-100} stroke="#064e3b" strokeDasharray="3 3" />
                <Line yAxisId="cci" type="monotone" dataKey="cci" name="CCI14" stroke={COLORS.cci} strokeWidth={1.6} dot={false} connectNulls />
                <Line yAxisId="wr" type="monotone" dataKey="wr" name="WR14" stroke={COLORS.wr} strokeWidth={1.6} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>

          <ChartCard title="振幅与量比" note="振幅(%) 与 5 日量比（双轴）">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={points} margin={{ top: 8, right: 0, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} minTickGap={28} />
                <YAxis yAxisId="amp" tick={axisTick} />
                <YAxis yAxisId="vr" orientation="right" tick={axisTick} width={44} />
                <Tooltip contentStyle={tooltipStyle} />
                <Legend wrapperStyle={legendStyle} />
                <ReferenceLine yAxisId="vr" y={1} stroke="#334155" strokeDasharray="3 3" />
                <Bar yAxisId="amp" dataKey="amplitude" name="振幅(%)" fill="#475569" />
                <Line yAxisId="vr" type="monotone" dataKey="volumeRatio" name="量比(5日)" stroke={COLORS.ma5} strokeWidth={1.8} dot={false} connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          </ChartCard>
        </div>
      )}

      {data && (
        <div className={`${panel} p-4`}>
          <div className="text-sm text-slate-300 mb-3">
            全部指标读数
            <span className="text-xs text-slate-500 ml-2">
              共 {Object.keys(data.titles).length} 条序列，取最后一根 K 线的有效值
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-2">
            {Object.entries(data.titles).map(([key, title]) => (
              <div
                key={key}
                className="bg-slate-950/60 border border-slate-800 rounded px-3 py-2"
              >
                <div className="text-xs text-slate-500">{title}</div>
                <div className="numeric text-slate-200">
                  {num(latest[key] ?? null, 3)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="text-xs text-slate-500">
        指标计算结果仅供参考研究，不构成投资建议。
      </div>
    </div>
  );
}
