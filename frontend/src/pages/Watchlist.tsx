import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  addWatchlistSymbol,
  createWatchlist,
  listWatchlists,
  removeWatchlistSymbol,
} from "../lib/api";
import type { QuoteData } from "../lib/types";
import { useWebSocket, type WsMessage } from "../lib/ws";

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const [newWatchlistName, setNewWatchlistName] = useState("");
  const [addSymbol, setAddSymbol] = useState({ watchlistId: 0, symbol: "", name: "" });
  // 实时行情缓存：symbol -> QuoteData
  const [liveQuotes, setLiveQuotes] = useState<Record<string, QuoteData>>({});

  const { data: watchlists, isLoading } = useQuery({
    queryKey: ["watchlists"],
    queryFn: listWatchlists,
  });

  // 收集所有自选股代码，供 WebSocket 订阅
  const allSymbols = useMemo(() => {
    if (!watchlists) return [];
    const set = new Set<string>();
    watchlists.forEach((wl) => wl.symbols.forEach((s) => set.add(s.symbol)));
    return Array.from(set);
  }, [watchlists]);

  const onWsMessage = (msg: WsMessage) => {
    if (msg.type === "quote") {
      setLiveQuotes((prev) => ({ ...prev, [msg.data.symbol]: msg.data }));
    }
  };

  const { connected } = useWebSocket({
    channel: "quotes",
    symbols: allSymbols,
    onMessage: onWsMessage,
  });

  const createMutation = useMutation({
    mutationFn: (name: string) => createWatchlist(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  const addSymbolMutation = useMutation({
    mutationFn: ({
      watchlistId,
      symbol,
      name,
    }: {
      watchlistId: number;
      symbol: string;
      name?: string;
    }) => addWatchlistSymbol(watchlistId, symbol, name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  const removeSymbolMutation = useMutation({
    mutationFn: ({ watchlistId, symbol }: { watchlistId: number; symbol: string }) =>
      removeWatchlistSymbol(watchlistId, symbol),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  // 默认第一个列表是新增目标
  useEffect(() => {
    if (watchlists && watchlists.length > 0 && addSymbol.watchlistId === 0) {
      setAddSymbol((s) => ({ ...s, watchlistId: watchlists[0].id }));
    }
  }, [watchlists, addSymbol.watchlistId]);

  return (
    <div className="max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">自选股</h1>
        <div className="flex items-center gap-2 text-xs">
          <span
            className={`w-2 h-2 rounded-full ${
              connected ? "bg-emerald-500" : "bg-slate-500"
            }`}
          />
          <span className="text-slate-400">
            实时推送 {connected ? "已连接" : "未连接"}
          </span>
        </div>
      </div>

      {/* 创建新自选股 */}
      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
        <h2 className="font-medium mb-3">新建自选股列表</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (newWatchlistName.trim()) {
              createMutation.mutate(newWatchlistName.trim());
              setNewWatchlistName("");
            }
          }}
          className="flex gap-2"
        >
          <input
            type="text"
            value={newWatchlistName}
            onChange={(e) => setNewWatchlistName(e.target.value)}
            placeholder="列表名称，如：核心持仓"
            className="flex-1 bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm focus:border-sky-500 outline-none"
          />
          <button
            type="submit"
            disabled={createMutation.isPending || !newWatchlistName.trim()}
            className="px-4 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 disabled:text-slate-500 rounded text-sm"
          >
            创建
          </button>
        </form>
      </div>

      {/* 添加股票 */}
      {watchlists && watchlists.length > 0 && (
        <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
          <h2 className="font-medium mb-3">添加股票到自选股</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (addSymbol.symbol.trim() && addSymbol.watchlistId > 0) {
                addSymbolMutation.mutate({
                  watchlistId: addSymbol.watchlistId,
                  symbol: addSymbol.symbol.trim(),
                  name: addSymbol.name.trim() || undefined,
                });
                setAddSymbol((s) => ({ ...s, symbol: "", name: "" }));
              }
            }}
            className="flex flex-wrap gap-2"
          >
            <select
              value={addSymbol.watchlistId}
              onChange={(e) =>
                setAddSymbol((s) => ({ ...s, watchlistId: Number(e.target.value) }))
              }
              className="bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
            >
              {watchlists.map((wl) => (
                <option key={wl.id} value={wl.id}>
                  {wl.name}
                </option>
              ))}
            </select>
            <input
              type="text"
              value={addSymbol.symbol}
              onChange={(e) =>
                setAddSymbol((s) => ({ ...s, symbol: e.target.value }))
              }
              placeholder="代码（如 600000）"
              maxLength={6}
              pattern="[0368][0-9]{5}"
              className="w-32 bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm focus:border-sky-500 outline-none"
            />
            <input
              type="text"
              value={addSymbol.name}
              onChange={(e) =>
                setAddSymbol((s) => ({ ...s, name: e.target.value }))
              }
              placeholder="名称（可选）"
              className="flex-1 min-w-32 bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm focus:border-sky-500 outline-none"
            />
            <button
              type="submit"
              disabled={addSymbolMutation.isPending || !addSymbol.symbol}
              className="px-4 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 disabled:text-slate-500 rounded text-sm"
            >
              添加
            </button>
          </form>
          <p className="text-xs text-slate-500 mt-2">
            代码格式：沪市 60/68 开头，深市 00/30 开头，北交所 43/8x/920 开头
            （920 为北交所新代码段，如 920819）
          </p>
        </div>
      )}

      {/* 列表 */}
      {isLoading ? (
        <div className="text-slate-500 text-center py-8">加载中...</div>
      ) : !watchlists || watchlists.length === 0 ? (
        <div className="text-slate-500 text-center py-8">
          还没有自选股，先创建一个吧
        </div>
      ) : (
        <div className="space-y-4">
          {watchlists.map((wl) => (
            <div
              key={wl.id}
              className="bg-slate-900 rounded-lg border border-slate-800 overflow-x-auto"
            >
              <div className="px-4 py-2 border-b border-slate-800 flex items-center justify-between">
                <h3 className="font-medium">{wl.name}</h3>
                <span className="text-xs text-slate-500">
                  {wl.symbols.length} 只
                </span>
              </div>
              {wl.symbols.length === 0 ? (
                <div className="px-4 py-6 text-slate-500 text-sm text-center">
                  空列表
                </div>
              ) : (
                <table className="w-full text-sm">
                  <thead className="text-xs text-slate-500 uppercase">
                    <tr className="border-b border-slate-800">
                      <th className="text-left px-4 py-2">代码</th>
                      <th className="text-left px-4 py-2">名称</th>
                      <th className="text-right px-4 py-2">最新价</th>
                      <th className="text-right px-4 py-2">涨跌幅</th>
                      <th className="text-right px-4 py-2">数据源</th>
                      <th className="text-right px-4 py-2">时间</th>
                      <th className="px-4 py-2"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {wl.symbols.map((s) => {
                      const quote = liveQuotes[s.symbol];
                      return (
                        <tr
                          key={s.symbol}
                          className="border-b border-slate-800/50 hover:bg-slate-800/30"
                        >
                          <td className="px-4 py-2 numeric">{s.symbol}</td>
                          <td className="px-4 py-2 text-slate-300">
                            {quote?.name ?? s.name ?? "—"}
                          </td>
                          <td className="px-4 py-2 text-right numeric">
                            {quote ? quote.price.toFixed(2) : "—"}
                          </td>
                          <td
                            className={`px-4 py-2 text-right numeric ${
                              quote
                                ? quote.price >= quote.previous_close
                                  ? "text-up"
                                  : "text-down"
                                : "text-slate-500"
                            }`}
                          >
                            {quote
                              ? `${(
                                  ((quote.price - quote.previous_close) /
                                    quote.previous_close) *
                                  100
                                ).toFixed(2)}%`
                              : "—"}
                          </td>
                          <td className="px-4 py-2 text-right text-slate-500 text-xs">
                            {quote?.source ?? "—"}
                            {quote?.is_stale && " · stale"}
                          </td>
                          <td className="px-4 py-2 text-right text-slate-500 text-xs">
                            {quote?.market_time
                              ? new Date(quote.market_time).toLocaleTimeString()
                              : "—"}
                          </td>
                          <td className="px-4 py-2 text-right space-x-3">
                            <Link
                              to={`/intraday?symbol=${s.symbol}`}
                              className="text-xs text-slate-500 hover:text-sky-400"
                            >
                              分时
                            </Link>
                            <button
                              onClick={() =>
                                removeSymbolMutation.mutate({
                                  watchlistId: wl.id,
                                  symbol: s.symbol,
                                })
                              }
                              className="text-xs text-slate-500 hover:text-rose-400"
                            >
                              移除
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
