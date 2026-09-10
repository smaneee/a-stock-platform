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
  cancelPaperOrder,
  createPaperAccount,
  fetchAssetCurve,
  listPaperAccounts,
  listPaperOrders,
  listPaperPositions,
  listPaperTrades,
  placePaperOrder,
} from "../lib/api";
import type { PaperAccount } from "../lib/types";
import { useWebSocket, type WsMessage } from "../lib/ws";

export default function PaperTradingPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [newAccount, setNewAccount] = useState({
    name: "测试账户",
    initial_cash: 100_000,
  });
  const [orderForm, setOrderForm] = useState({
    symbol: "600000",
    side: "BUY" as "BUY" | "SELL",
    quantity: 100,
  });

  const { data: accounts } = useQuery({
    queryKey: ["paper-accounts"],
    queryFn: listPaperAccounts,
  });

  // 自动选中第一个账户
  if (selectedId === null && accounts && accounts.length > 0) {
    setSelectedId(accounts[0].id);
  }

  const { data: positions } = useQuery({
    queryKey: ["paper-positions", selectedId],
    queryFn: () => listPaperPositions(selectedId!),
    enabled: selectedId !== null,
  });

  const { data: trades } = useQuery({
    queryKey: ["paper-trades", selectedId],
    queryFn: () => listPaperTrades(selectedId!, 30),
    enabled: selectedId !== null,
  });

  const { data: orders } = useQuery({
    queryKey: ["paper-orders", selectedId],
    queryFn: () => listPaperOrders(selectedId!, 50),
    enabled: selectedId !== null,
  });

  const { data: assets } = useQuery({
    queryKey: ["paper-assets", selectedId],
    queryFn: () => fetchAssetCurve(selectedId!),
    enabled: selectedId !== null,
  });

  // 实时行情缓存（用于下单时显示最新价）
  const [latestPrice, setLatestPrice] = useState<Record<string, number>>({});
  const onWs = (msg: WsMessage) => {
    if (msg.type === "quote") {
      setLatestPrice((p) => ({ ...p, [msg.data.symbol]: msg.data.price }));
    }
  };
  useWebSocket({
    channel: "quotes",
    symbols: orderForm.symbol ? [orderForm.symbol] : [],
    onMessage: onWs,
  });

  const createMut = useMutation({
    mutationFn: () =>
      createPaperAccount(newAccount.name, newAccount.initial_cash),
    onSuccess: (account: PaperAccount) => {
      queryClient.invalidateQueries({ queryKey: ["paper-accounts"] });
      setSelectedId(account.id);
    },
  });

  const placeOrderMut = useMutation({
    mutationFn: () => {
      const price = latestPrice[orderForm.symbol];
      return placePaperOrder({
        account_id: selectedId!,
        symbol: orderForm.symbol,
        side: orderForm.side,
        quantity: orderForm.quantity,
        price,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-positions", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["paper-trades", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["paper-orders", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["paper-assets", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["paper-accounts"] });
    },
  });

  const cancelOrderMut = useMutation({
    mutationFn: (orderId: number) => cancelPaperOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["paper-orders", selectedId] });
      queryClient.invalidateQueries({ queryKey: ["paper-accounts"] });
    },
  });

  const selected = accounts?.find((a) => a.id === selectedId);

  return (
    <div className="max-w-6xl space-y-6">
      <h1 className="text-2xl font-semibold">模拟交易</h1>

      {/* 账户管理 */}
      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
        <h2 className="font-medium mb-3">账户</h2>
        <div className="flex flex-wrap items-center gap-2">
          {accounts?.map((a) => (
            <button
              key={a.id}
              onClick={() => setSelectedId(a.id)}
              className={`text-sm px-3 py-1.5 rounded border ${
                a.id === selectedId
                  ? "bg-sky-600 border-sky-500"
                  : "bg-slate-800 border-slate-700 hover:bg-slate-700"
              }`}
            >
              {a.name} · ¥{a.available_cash.toFixed(0)}
            </button>
          ))}
          <span className="text-slate-600">|</span>
          <input
            type="text"
            value={newAccount.name}
            onChange={(e) =>
              setNewAccount((n) => ({ ...n, name: e.target.value }))
            }
            placeholder="账户名"
            className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm w-32"
          />
          <input
            type="number"
            value={newAccount.initial_cash}
            onChange={(e) =>
              setNewAccount((n) => ({
                ...n,
                initial_cash: Number(e.target.value) || 0,
              }))
            }
            min={1000}
            step={1000}
            className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm w-32"
          />
          <button
            onClick={() => createMut.mutate()}
            disabled={createMut.isPending}
            className="text-sm px-3 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 rounded"
          >
            新建
          </button>
        </div>
      </div>

      {selected && (
        <>
          {/* 资产曲线 */}
          {assets && assets.asset_curve.length > 0 && (
            <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
              <div className="flex items-center justify-between mb-3">
                <h2 className="font-medium">资产曲线</h2>
                <div className="text-sm text-slate-400">
                  可用 ¥{assets.available_cash.toFixed(2)} · 冻结 ¥
                  {assets.frozen_cash.toFixed(2)}
                </div>
              </div>
              <div className="h-48 bg-slate-950 rounded p-2">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={assets.asset_curve.map((p, i) => ({
                      i,
                      v: p.total_asset,
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
                      stroke="#34d399"
                      dot={false}
                      strokeWidth={1.5}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* 下单面板 */}
          <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
            <h2 className="font-medium mb-3">下单</h2>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
              <label className="block">
                <div className="text-xs text-slate-500 mb-1">代码</div>
                <input
                  type="text"
                  value={orderForm.symbol}
                  onChange={(e) =>
                    setOrderForm((f) => ({ ...f, symbol: e.target.value }))
                  }
                  maxLength={6}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
                />
              </label>
              <label className="block">
                <div className="text-xs text-slate-500 mb-1">方向</div>
                <select
                  value={orderForm.side}
                  onChange={(e) =>
                    setOrderForm((f) => ({
                      ...f,
                      side: e.target.value as "BUY" | "SELL",
                    }))
                  }
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
                >
                  <option value="BUY">买入</option>
                  <option value="SELL">卖出</option>
                </select>
              </label>
              <label className="block">
                <div className="text-xs text-slate-500 mb-1">数量（100 股整数手）</div>
                <input
                  type="number"
                  value={orderForm.quantity}
                  onChange={(e) =>
                    setOrderForm((f) => ({
                      ...f,
                      quantity: Number(e.target.value) || 0,
                    }))
                  }
                  step={100}
                  min={100}
                  className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-1.5 text-sm"
                />
              </label>
              <div className="text-xs text-slate-500">
                最新价：
                {latestPrice[orderForm.symbol]
                  ? "¥" + latestPrice[orderForm.symbol].toFixed(2)
                  : "—"}
              </div>
            </div>
            <button
              onClick={() => placeOrderMut.mutate()}
              disabled={placeOrderMut.isPending || !selectedId}
              className="mt-3 px-4 py-1.5 bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 disabled:text-slate-500 rounded text-sm"
            >
              {placeOrderMut.isPending ? "提交中..." : "提交委托"}
            </button>
            {placeOrderMut.isSuccess && (
              <p className="text-emerald-400 text-xs mt-2">委托已成交</p>
            )}
            {placeOrderMut.isError && (
              <p className="text-rose-400 text-xs mt-2">
                委托失败：{(placeOrderMut.error as Error).message}
              </p>
            )}
          </div>

          {/* 持仓 */}
          <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-hidden">
            <div className="px-4 py-2 border-b border-slate-800">
              <h2 className="font-medium">持仓</h2>
            </div>
            {!positions || positions.positions.length === 0 ? (
              <div className="px-4 py-6 text-slate-500 text-sm text-center">
                无持仓
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-xs text-slate-500 uppercase bg-slate-950">
                  <tr>
                    <th className="text-left px-4 py-2">代码</th>
                    <th className="text-right px-4 py-2">总持仓</th>
                    <th className="text-right px-4 py-2">可卖</th>
                    <th className="text-right px-4 py-2">成本价</th>
                    <th className="text-right px-4 py-2">现价</th>
                    <th className="text-right px-4 py-2">盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.positions.map((p) => {
                    const cur = latestPrice[p.symbol] ?? p.avg_cost;
                    const pnl = (cur - p.avg_cost) * p.quantity;
                    return (
                      <tr
                        key={p.symbol}
                        className="border-b border-slate-800/50"
                      >
                        <td className="px-4 py-2 numeric">{p.symbol}</td>
                        <td className="px-4 py-2 text-right numeric">
                          {p.quantity}
                        </td>
                        <td className="px-4 py-2 text-right numeric text-slate-400">
                          {p.available_quantity}
                        </td>
                        <td className="px-4 py-2 text-right numeric">
                          {p.avg_cost.toFixed(2)}
                        </td>
                        <td className="px-4 py-2 text-right numeric">
                          {cur.toFixed(2)}
                        </td>
                        <td
                          className={`px-4 py-2 text-right numeric ${
                            pnl > 0
                              ? "text-up"
                              : pnl < 0
                                ? "text-down"
                                : ""
                          }`}
                        >
                          {pnl.toFixed(2)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>

          {/* 成交记录 */}
          <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-hidden">
            <div className="px-4 py-2 border-b border-slate-800">
              <h2 className="font-medium">成交记录</h2>
            </div>
            {!trades || trades.trades.length === 0 ? (
              <div className="px-4 py-6 text-slate-500 text-sm text-center">
                暂无成交
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-xs text-slate-500 uppercase bg-slate-950">
                  <tr>
                    <th className="text-left px-4 py-2">时间</th>
                    <th className="text-left px-4 py-2">代码</th>
                    <th className="text-left px-4 py-2">方向</th>
                    <th className="text-right px-4 py-2">价格</th>
                    <th className="text-right px-4 py-2">数量</th>
                    <th className="text-right px-4 py-2">佣金</th>
                    <th className="text-right px-4 py-2">印花税</th>
                    <th className="text-right px-4 py-2">已实现盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.trades.map((t) => (
                    <tr
                      key={t.id}
                      className="border-b border-slate-800/50"
                    >
                      <td className="px-4 py-2 text-slate-500 text-xs">
                        {new Date(t.executed_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-2 numeric">{t.symbol}</td>
                      <td
                        className={`px-4 py-2 ${
                          t.side === "BUY" ? "text-up" : "text-down"
                        }`}
                      >
                        {t.side}
                      </td>
                      <td className="px-4 py-2 text-right numeric">
                        {t.price.toFixed(2)}
                      </td>
                      <td className="px-4 py-2 text-right numeric">
                        {t.quantity}
                      </td>
                      <td className="px-4 py-2 text-right numeric text-slate-400">
                        {t.commission.toFixed(2)}
                      </td>
                      <td className="px-4 py-2 text-right numeric text-slate-400">
                        {t.stamp_tax.toFixed(2)}
                      </td>
                      <td
                        className={`px-4 py-2 text-right numeric ${
                          t.realized_pnl > 0
                            ? "text-up"
                            : t.realized_pnl < 0
                              ? "text-down"
                              : "text-slate-400"
                        }`}
                      >
                        {t.realized_pnl.toFixed(2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* 委托单 */}
          <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-hidden">
            <div className="px-4 py-2 border-b border-slate-800">
              <h2 className="font-medium">委托单</h2>
            </div>
            {!orders || orders.orders.length === 0 ? (
              <div className="px-4 py-6 text-slate-500 text-sm text-center">
                暂无委托
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-xs text-slate-500 uppercase bg-slate-950">
                  <tr>
                    <th className="text-left px-4 py-2">时间</th>
                    <th className="text-left px-4 py-2">代码</th>
                    <th className="text-left px-4 py-2">方向</th>
                    <th className="text-right px-4 py-2">数量</th>
                    <th className="text-left px-4 py-2">状态</th>
                    <th className="text-left px-4 py-2">拒绝原因</th>
                    <th className="text-right px-4 py-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.orders.map((o) => (
                    <tr key={o.id} className="border-b border-slate-800/50">
                      <td className="px-4 py-2 text-slate-500 text-xs">
                        {new Date(o.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-2 numeric">{o.symbol}</td>
                      <td
                        className={`px-4 py-2 ${
                          o.side === "BUY" ? "text-up" : "text-down"
                        }`}
                      >
                        {o.side}
                      </td>
                      <td className="px-4 py-2 text-right numeric">
                        {o.quantity}
                      </td>
                      <td className="px-4 py-2 text-xs">{o.status}</td>
                      <td className="px-4 py-2 text-xs text-rose-400">
                        {o.reject_reason ?? "—"}
                      </td>
                      <td className="px-4 py-2 text-right">
                        {o.status === "SUBMITTED" && (
                          <button
                            onClick={() => cancelOrderMut.mutate(o.id)}
                            disabled={cancelOrderMut.isPending}
                            className="text-xs px-2 py-1 bg-slate-700 hover:bg-slate-600 rounded"
                          >
                            取消
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {!selected && (
        <div className="text-slate-500 text-center py-8">
          还没有账户，先创建一个吧
        </div>
      )}
    </div>
  );
}
