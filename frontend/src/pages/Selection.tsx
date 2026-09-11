import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { listUniverseSnapshots, rankStocks } from "../lib/api";

const percent = (value: number) => `${(value * 100).toFixed(2)}%`;

export default function SelectionPage() {
  const [tradingDay, setTradingDay] = useState("");
  const [topN, setTopN] = useState(5);
  const snapshots = useQuery({
    queryKey: ["universe-snapshots"],
    queryFn: listUniverseSnapshots,
  });
  const ranking = useMutation({
    mutationFn: rankStocks,
  });

  useEffect(() => {
    const latest = snapshots.data?.snapshots[0]?.trading_day;
    if (latest && !tradingDay) setTradingDay(latest);
  }, [snapshots.data, tradingDay]);

  const run = () => {
    if (!tradingDay) return;
    ranking.mutate({ trading_day: tradingDay, top_n: topN, exclude_st: true });
  };

  return (
    <div className="max-w-6xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">智能选股</h1>
        <p className="text-sm text-slate-400 mt-1">
          基于指定交易日的不可变股票池与当日以前的本地行情，按动量、波动、回撤和流动性综合排名。
        </p>
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 flex flex-wrap items-end gap-4">
        <label className="text-sm text-slate-300">
          <span className="block text-xs text-slate-500 mb-1">交易日</span>
          <input
            type="date"
            value={tradingDay}
            onChange={(event) => setTradingDay(event.target.value)}
            className="bg-slate-950 border border-slate-700 rounded px-3 py-2"
          />
        </label>
        <label className="text-sm text-slate-300">
          <span className="block text-xs text-slate-500 mb-1">候选数量</span>
          <input
            type="number"
            min={1}
            max={50}
            value={topN}
            onChange={(event) => setTopN(Number(event.target.value))}
            className="w-24 bg-slate-950 border border-slate-700 rounded px-3 py-2"
          />
        </label>
        <button
          type="button"
          onClick={run}
          disabled={!tradingDay || ranking.isPending}
          className="rounded bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 px-4 py-2 text-sm font-medium"
        >
          {ranking.isPending ? "计算中..." : "生成排名"}
        </button>
        <div className="text-xs text-amber-400 ml-auto">
          研究候选，不会提交真实订单
        </div>
      </div>

      {ranking.isError && (
        <div className="rounded border border-rose-900 bg-rose-950/30 p-4 text-sm text-rose-300">
          {ranking.error instanceof Error ? ranking.error.message : "选股失败"}
        </div>
      )}

      {ranking.data && (
        <div className="space-y-3">
          <div className="text-sm text-slate-400">
            运行 #{ranking.data.run_id} · 基础候选 {ranking.data.total_candidates} ·
            数据合格 {ranking.data.eligible_count}
          </div>
          <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 bg-slate-950">
                <tr>
                  <th className="px-3 py-2 text-left">排名</th>
                  <th className="px-3 py-2 text-left">股票</th>
                  <th className="px-3 py-2 text-right">综合分</th>
                  <th className="px-3 py-2 text-right">20日动量</th>
                  <th className="px-3 py-2 text-right">60日动量</th>
                  <th className="px-3 py-2 text-right">年化波动</th>
                  <th className="px-3 py-2 text-right">60日回撤</th>
                  <th className="px-3 py-2 text-right">均成交额</th>
                  <th className="px-3 py-2 text-right">收盘价</th>
                </tr>
              </thead>
              <tbody>
                {ranking.data.candidates.map((item) => (
                  <tr key={item.symbol} className="border-t border-slate-800">
                    <td className="px-3 py-2 text-sky-300">#{item.rank}</td>
                    <td className="px-3 py-2">
                      <div>{item.name || item.symbol}</div>
                      <div className="text-xs text-slate-500">
                        {item.symbol} · {item.exchange}/{item.board}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right font-medium">{item.score.toFixed(4)}</td>
                    <td className="px-3 py-2 text-right">{percent(item.momentum_20)}</td>
                    <td className="px-3 py-2 text-right">{percent(item.momentum_60)}</td>
                    <td className="px-3 py-2 text-right">{percent(item.volatility_20)}</td>
                    <td className="px-3 py-2 text-right">{percent(item.max_drawdown_60)}</td>
                    <td className="px-3 py-2 text-right">{(item.average_amount_20 / 1e6).toFixed(1)}M</td>
                    <td className="px-3 py-2 text-right">{item.last_price.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
