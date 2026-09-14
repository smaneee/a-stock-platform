import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { listSignals } from "../lib/api";
import type { Signal } from "../lib/types";
import { useWebSocket, type WsMessage } from "../lib/ws";

const PAGE_SIZE = 100;

export default function SignalsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["signals", PAGE_SIZE],
    queryFn: () => listSignals(PAGE_SIZE),
  });
  // 实时流：新信号从 WebSocket 推入，开头插入
  const [live, setLive] = useState<Signal[]>([]);
  const liveRef = useRef<Signal[]>([]);
  // 维护一个"按 signal_id 去重"的集合
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (data) {
      // 初始化去重集合
      data.signals.forEach((s) => seenRef.current.add(s.signal_id));
    }
  }, [data]);

  const onWsMessage = (msg: WsMessage) => {
    if (msg.type === "signal") {
      const signal = msg.data;
      if (seenRef.current.has(signal.signal_id)) return;
      seenRef.current.add(signal.signal_id);
      liveRef.current = [signal, ...liveRef.current].slice(0, PAGE_SIZE);
      setLive(liveRef.current);
    }
  };

  const { connected } = useWebSocket({
    channel: "signals",
    onMessage: onWsMessage,
  });

  const merged = (() => {
    // live 在前，data 在后（去重 by signal_id）
    const seen = new Set<string>();
    const out: Signal[] = [];
    for (const s of live) {
      if (!seen.has(s.signal_id)) {
        seen.add(s.signal_id);
        out.push(s);
      }
    }
    for (const s of data?.signals ?? []) {
      if (!seen.has(s.signal_id)) {
        seen.add(s.signal_id);
        out.push(s);
      }
    }
    return out;
  })();

  return (
    <div className="max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">信号流</h1>
        <div className="flex items-center gap-2 text-xs">
          <span
            className={`w-2 h-2 rounded-full ${
              connected ? "bg-emerald-500" : "bg-slate-500"
            }`}
          />
          <span className="text-slate-400">
            实时 {connected ? "已连接" : "未连接"}
          </span>
        </div>
      </div>

      {isLoading ? (
        <div className="text-slate-500 text-center py-8">加载中...</div>
      ) : merged.length === 0 ? (
        <div className="bg-slate-900 rounded-lg p-8 border border-slate-800 text-center text-slate-500">
          <p>暂无信号。</p>
          <p className="text-xs mt-2">
            启用策略后，系统会在检测到信号时推送到这里。
          </p>
        </div>
      ) : (
        <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-xs text-slate-500 uppercase bg-slate-950">
              <tr>
                <th className="text-left px-4 py-2">时间</th>
                <th className="text-left px-4 py-2">代码</th>
                <th className="text-left px-4 py-2">策略</th>
                <th className="text-left px-4 py-2">方向</th>
                <th className="text-right px-4 py-2">触发价</th>
                <th className="text-left px-4 py-2">原因</th>
              </tr>
            </thead>
            <tbody>
              {merged.map((s) => (
                <tr
                  key={s.signal_id}
                  className="border-b border-slate-800/50 hover:bg-slate-800/30"
                >
                  <td className="px-4 py-2 text-slate-500 text-xs numeric">
                    {new Date(s.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-2 numeric">{s.symbol}</td>
                  <td className="px-4 py-2 text-slate-300">{s.strategy_name}</td>
                  <td className="px-4 py-2">
                    <span
                      className={
                        s.direction === "BUY"
                          ? "text-up font-medium"
                          : s.direction === "SELL"
                            ? "text-down font-medium"
                            : "text-slate-300"
                      }
                    >
                      {s.direction}
                    </span>
                  </td>
                  <td className="px-4 py-2 text-right numeric">
                    {s.price.toFixed(2)}
                  </td>
                  <td className="px-4 py-2 text-slate-300 text-xs">{s.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
