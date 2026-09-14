/** 分时页：单只标的的当日实时曲线。
 *
 * 曲线由两段拼成：
 * 1. ``GET /api/quotes/{symbol}/intraday`` 给整段分时（数据源 1 分钟线，
 *    任意标的都有），页面每 30 秒重拉一次做兜底；
 * 2. ``/ws/quotes`` 的实时 tick 叠在末尾，让曲线在盘中真的动起来。
 *
 * 后端只轮询自选股，所以实时推送也只覆盖自选股；非自选股会明确提示，
 * 不会让人误以为"行情卡住了"。
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import IntradayChart from "../components/IntradayChart";
import { fetchIntraday, listWatchlists } from "../lib/api";
import { formatNumber } from "../lib/format";
import type { IntradayPoint, IntradaySeries, QuoteData } from "../lib/types";
import { useWebSocket, type WsMessage } from "../lib/ws";

const REFETCH_MS = 30_000;
const DEFAULT_SYMBOL = "600000";
const panel = "bg-slate-900 rounded-lg border border-slate-800";
const inputCls =
  "bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-slate-200 numeric w-32";

const PRESETS = [
  { symbol: "600000", label: "浦发银行" },
  { symbol: "600519", label: "贵州茅台" },
  { symbol: "000001", label: "平安银行" },
  { symbol: "300750", label: "宁德时代" },
];

const SOURCE_LABEL: Record<IntradaySeries["source"], string> = {
  live: "实时缓存",
  baseline: "数据源分时",
  merged: "实时 + 数据源",
  empty: "无数据",
};

/** URL 上的 ?symbol= 只接受 6 位代码，其余按缺省处理。 */
function normalizeSymbol(raw: string | null): string | null {
  const trimmed = (raw ?? "").trim();
  return /^\d{6}$/.test(trimmed) ? trimmed : null;
}

/** 把实时 tick 叠加到分时序列末尾。
 *
 * 实时行情的成交量/成交额是**当日累计值**，而分时点的量额是**每分钟增量**；
 * 直接追加会把均价算成天文数字。这里用「累计值 − 已有点量额合计」换算成当前
 * 分钟的增量，再覆盖 / 追加该分钟，均价因此始终是真实 VWAP。
 *
 * 另外，非交易日实时源会给出「日期是今天、时间却是上一场收盘」的幽灵 tick
 * （通达信 servertime 不含日期），日期与曲线交易日不一致时直接丢弃。
 */
function applyTick(
  points: IntradayPoint[],
  quote: QuoteData | null,
  tradeDate: string | undefined,
): IntradayPoint[] {
  if (!quote) return points;
  const when = quote.market_time ?? quote.received_at;
  if (!when || when.length < 16) return points;
  if (tradeDate && when.slice(0, 10) !== tradeDate) return points;
  const minute = `${when.slice(0, 16)}:00`;

  let cumulativeVolume = 0;
  let cumulativeAmount = 0;
  for (const point of points) {
    cumulativeVolume += point.volume;
    cumulativeAmount += point.amount;
  }
  const deltaVolume = Math.max(0, quote.volume - cumulativeVolume);
  const deltaAmount = Math.max(0, quote.amount - cumulativeAmount);

  const last = points[points.length - 1];
  if (last && last.time === minute) {
    return [
      ...points.slice(0, -1),
      {
        ...last,
        price: quote.price,
        volume: last.volume + deltaVolume,
        amount: last.amount + deltaAmount,
      },
    ];
  }
  return [
    ...points,
    {
      time: minute,
      price: quote.price,
      volume: deltaVolume,
      amount: deltaAmount,
    },
  ];
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="numeric text-slate-200">{value}</div>
    </div>
  );
}

export default function IntradayPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlSymbol = normalizeSymbol(searchParams.get("symbol"));
  const [symbol, setSymbol] = useState(urlSymbol ?? DEFAULT_SYMBOL);
  const [input, setInput] = useState(urlSymbol ?? DEFAULT_SYMBOL);
  const [tick, setTick] = useState<QuoteData | null>(null);
  const [tickCount, setTickCount] = useState(0);

  // 支持 /intraday?symbol=600519 深链：地址栏变了就跟着切标的
  useEffect(() => {
    if (urlSymbol && urlSymbol !== symbol) {
      setSymbol(urlSymbol);
      setInput(urlSymbol);
    }
  }, [urlSymbol, symbol]);

  const {
    data: series,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["intraday", symbol],
    queryFn: () => fetchIntraday(symbol),
    refetchInterval: REFETCH_MS,
    retry: false,
  });

  const { data: watchlists } = useQuery({
    queryKey: ["watchlists"],
    queryFn: listWatchlists,
  });

  // 换标的时清掉上一条曲线的实时尾巴，避免串图
  useEffect(() => {
    setTick(null);
    setTickCount(0);
  }, [symbol]);

  const onMessage = (message: WsMessage) => {
    if (message.type !== "quote" || message.data.symbol !== symbol) return;
    setTick(message.data);
    setTickCount((count) => count + 1);
  };
  const { connected } = useWebSocket({
    channel: "quotes",
    symbols: [symbol],
    onMessage,
  });

  const points = useMemo(
    () => applyTick(series?.points ?? [], tick, series?.trade_date),
    [series, tick],
  );

  const stats = useMemo(() => {
    if (points.length === 0) return null;
    const prices = points.map((point) => point.price);
    let volume = 0;
    let amount = 0;
    for (const point of points) {
      volume += point.volume;
      amount += point.amount;
    }
    return {
      open: prices[0],
      high: Math.max(...prices),
      low: Math.min(...prices),
      last: prices[prices.length - 1],
      volume,
      amount,
    };
  }, [points]);

  const watchSymbols = useMemo(
    () =>
      (watchlists ?? []).flatMap((list) =>
        list.symbols.map((item) => item.symbol),
      ),
    [watchlists],
  );

  const previousClose = series?.stats.previous_close ?? 0;
  const change = stats && previousClose > 0 ? stats.last - previousClose : 0;
  const changePct = previousClose > 0 ? (change / previousClose) * 100 : 0;
  const trendClass =
    change > 0 ? "text-up" : change < 0 ? "text-down" : "text-slate-300";

  const go = (next: string) => {
    const trimmed = next.trim();
    if (!/^\d{6}$/.test(trimmed)) return;
    setInput(trimmed);
    setSymbol(trimmed);
    setSearchParams({ symbol: trimmed }, { replace: true });
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    go(input);
  };

  return (
    <div className="max-w-6xl space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">实时分时</h1>
          <p className="text-xs text-slate-500 mt-1">
            {series
              ? `${series.name} · ${series.symbol} · ${series.trade_date}`
              : symbol}
            {series && (
              <span className="ml-2">
                来源：{SOURCE_LABEL[series.source]}
              </span>
            )}
          </p>
        </div>
        <form className="flex items-center gap-2" onSubmit={onSubmit}>
          <input
            className={inputCls}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="6 位代码"
            inputMode="numeric"
            aria-label="股票代码"
          />
          <button
            type="submit"
            className="px-3 py-1.5 text-sm rounded bg-sky-600 hover:bg-sky-500 text-white"
          >
            查看
          </button>
        </form>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-500">常用：</span>
        {PRESETS.map((item) => (
          <button
            key={item.symbol}
            type="button"
            onClick={() => go(item.symbol)}
            className={`px-2 py-1 rounded border ${
              symbol === item.symbol
                ? "border-sky-500 text-sky-300"
                : "border-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {item.label}
          </button>
        ))}
        {watchSymbols.length > 0 && (
          <>
            <span className="text-slate-500 ml-2">自选股：</span>
            {watchSymbols.slice(0, 12).map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => go(item)}
                className={`px-2 py-1 rounded border numeric ${
                  symbol === item
                    ? "border-sky-500 text-sky-300"
                    : "border-slate-800 text-slate-400 hover:text-slate-200"
                }`}
              >
                {item}
              </button>
            ))}
          </>
        )}
      </div>

      {isLoading ? (
        <div className="text-slate-500 text-center py-8">加载中...</div>
      ) : isError ? (
        <div className={`${panel} p-6 text-center text-sm text-slate-400`}>
          {error instanceof Error ? error.message : "加载失败"}
        </div>
      ) : !stats ? (
        <div className={`${panel} p-8 text-center text-slate-500`}>
          <p>暂无分时数据。</p>
          <p className="text-xs mt-2">
            该标的可能停牌、尚未产生成交，或数据源暂不可用。
          </p>
        </div>
      ) : (
        <div className={`${panel} p-5`}>
          <div className="flex flex-wrap items-baseline gap-x-8 gap-y-3">
            <div>
              <div className={`text-3xl font-semibold numeric ${trendClass}`}>
                {stats.last.toFixed(2)}
              </div>
              <div className={`text-sm numeric ${trendClass}`}>
                {change >= 0 ? "+" : ""}
                {change.toFixed(2)} ({changePct >= 0 ? "+" : ""}
                {changePct.toFixed(2)}%)
              </div>
            </div>
            <Stat label="今开" value={stats.open.toFixed(2)} />
            <Stat label="最高" value={stats.high.toFixed(2)} />
            <Stat label="最低" value={stats.low.toFixed(2)} />
            <Stat
              label="昨收"
              value={previousClose > 0 ? previousClose.toFixed(2) : "—"}
            />
            <Stat
              label="均价"
              value={
                stats.volume > 0 ? (stats.amount / stats.volume).toFixed(2) : "—"
              }
            />
            <Stat
              label="成交量"
              value={`${formatNumber(stats.volume / 100)}手`}
            />
            <Stat label="成交额" value={formatNumber(stats.amount)} />
          </div>

          <div className="mt-4">
            <IntradayChart points={points} previousClose={previousClose} />
          </div>

          <div className="mt-3 pt-3 border-t border-slate-800 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
            <span className="flex items-center gap-1.5">
              <span
                className={`w-2 h-2 rounded-full ${
                  tickCount > 0
                    ? "bg-emerald-500"
                    : connected
                      ? "bg-amber-500"
                      : "bg-slate-500"
                }`}
              />
              {tickCount > 0
                ? `实时推送中，已收到 ${tickCount} 笔`
                : connected
                  ? "已订阅，等待实时推送（仅自选股有推送）"
                  : "实时通道未连接"}
            </span>
            <span>
              {series?.is_live
                ? `每 ${REFETCH_MS / 1000} 秒与后端重新对齐一次`
                : `非交易时段或该标的未被轮询，展示 ${series?.trade_date} 的历史分时`}
            </span>
            <span>分析结果仅用于研究，不构成投资建议</span>
          </div>
        </div>
      )}
    </div>
  );
}
