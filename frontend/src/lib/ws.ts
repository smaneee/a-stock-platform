/** WebSocket 客户端。
 *
 * 行情频道 (/ws/quotes) 和信号频道 (/ws/signals) 走不同连接，
 * 由 ConnectionManager 按 channel 字段路由。
 *
 * 客户端订阅消息格式：
 *   {"action": "subscribe", "symbols": ["600000", "000001"]}
 */
import { useEffect, useRef, useState } from "react";

import { authQuery } from "./auth";
import type { QuoteData, Signal } from "./types";

export type WsChannel = "quotes" | "signals";

export type WsMessage =
  | { type: "quote"; data: QuoteData }
  | { type: "signal"; data: Signal };

interface UseWebSocketOptions {
  channel: WsChannel;
  symbols?: string[]; // 行情频道：初始订阅列表
  onMessage?: (msg: WsMessage) => void;
  enabled?: boolean;
}

/** 构造相对 ws URL（vite 代理 /ws 到后端）。
 *
 * 开启访问鉴权时（局域网部署）握手需要携带令牌；HTTP 中间件不覆盖 WS 协议，
 * 后端在 WS 端点里单独校验（见 app/main.py `_ws_authorized`）。
 */
function wsUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}${authQuery()}`;
}

/** 通用 WebSocket hook：自动重连、订阅、消息分发。 */
export function useWebSocket({
  channel,
  symbols = [],
  onMessage,
  enabled = true,
}: UseWebSocketOptions) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  // 用 ref 存最新回调，避免重连时丢失闭包
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      const ws = new WebSocket(wsUrl(`/ws/${channel}`));
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        if (channel === "quotes" && symbols.length > 0) {
          ws.send(
            JSON.stringify({ action: "subscribe", symbols }),
          );
        }
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data) as WsMessage;
          onMessageRef.current?.(msg);
        } catch {
          // 忽略无法解析的消息
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!cancelled) {
          // 3 秒后重连
          reconnectTimerRef.current = window.setTimeout(connect, 3000);
        }
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
    };
    // 只在 channel 或 enabled 变化时重建连接
    // symbols 变化通过 send 推送，不重建连接
  }, [channel, enabled]); // eslint-disable-line react-hooks/exhaustive-deps

  // 动态订阅：symbols 变化时向已开连接发送新订阅
  useEffect(() => {
    if (
      connected &&
      channel === "quotes" &&
      wsRef.current &&
      symbols.length > 0
    ) {
      wsRef.current.send(
        JSON.stringify({ action: "subscribe", symbols }),
      );
    }
  }, [connected, channel, symbols]);

  return { connected };
}
