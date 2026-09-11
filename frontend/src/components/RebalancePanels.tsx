import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  approveLiveRebalancePlan,
  cancelPaperRebalancePlan,
  createLiveRebalancePlan,
  createPaperRebalancePlan,
  executeLiveRebalancePlan,
  executePaperRebalancePlan,
  fetchLiveTradingStatus,
  listLiveRebalancePlans,
  listPaperRebalancePlans,
  reconcileLiveRebalancePlan,
} from "../lib/api";
import type { LiveRebalancePlan, SelectionResult } from "../lib/types";

interface Props {
  accountId: number;
  selectionRuns: SelectionResult[];
}

export default function RebalancePanels({ accountId, selectionRuns }: Props) {
  const queryClient = useQueryClient();
  const [selectionRunId, setSelectionRunId] = useState(0);
  const [validationOverride, setValidationOverride] = useState(false);
  const [liveSelectionRunId, setLiveSelectionRunId] = useState(0);
  const [liveKey, setLiveKey] = useState("");
  const [liveRiskAccepted, setLiveRiskAccepted] = useState(false);
  const [liveApprovalTokens, setLiveApprovalTokens] = useState<Record<number, string>>({});
  const { data: rebalancePlans } = useQuery({
    queryKey: ["paper-rebalance-plans", accountId],
    queryFn: () => listPaperRebalancePlans(accountId),
  });
  const { data: liveStatus } = useQuery({
    queryKey: ["live-trading-status"],
    queryFn: fetchLiveTradingStatus,
  });
  const { data: livePlans } = useQuery({
    queryKey: ["live-rebalance-plans"],
    queryFn: () => listLiveRebalancePlans(liveKey),
    enabled: liveStatus?.ready === true && liveKey.length >= 32,
  });
  const refreshPaper = () => {
    queryClient.invalidateQueries({ queryKey: ["paper-rebalance-plans", accountId] });
    queryClient.invalidateQueries({ queryKey: ["paper-positions", accountId] });
    queryClient.invalidateQueries({ queryKey: ["paper-trades", accountId] });
    queryClient.invalidateQueries({ queryKey: ["paper-orders", accountId] });
    queryClient.invalidateQueries({ queryKey: ["paper-accounts"] });
  };
  const createPaper = useMutation({
    mutationFn: () => createPaperRebalancePlan({
      account_id: accountId,
      selection_run_id: selectionRunId,
      validation_override: validationOverride,
    }),
    onSuccess: refreshPaper,
  });
  const executePaper = useMutation({
    mutationFn: executePaperRebalancePlan,
    onSuccess: refreshPaper,
  });
  const cancelPaper = useMutation({
    mutationFn: cancelPaperRebalancePlan,
    onSuccess: refreshPaper,
  });
  const createLive = useMutation({
    mutationFn: () => createLiveRebalancePlan(liveSelectionRunId, liveKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["live-rebalance-plans"] }),
  });
  const approveLive = useMutation({
    mutationFn: (planId: number) => approveLiveRebalancePlan(planId, liveKey),
    onSuccess: (result) => {
      setLiveApprovalTokens((current) => ({ ...current, [result.plan.id]: result.approval_token }));
      queryClient.invalidateQueries({ queryKey: ["live-rebalance-plans"] });
    },
  });
  const executeLive = useMutation({
    mutationFn: (planId: number) => executeLiveRebalancePlan(planId, liveApprovalTokens[planId] ?? "", liveKey),
    onSuccess: (plan) => {
      setLiveApprovalTokens((current) => {
        const next = { ...current };
        delete next[plan.id];
        return next;
      });
      queryClient.invalidateQueries({ queryKey: ["live-rebalance-plans"] });
    },
  });
  const reconcileLive = useMutation({
    mutationFn: (planId: number) => reconcileLiveRebalancePlan(planId, liveKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["live-rebalance-plans"] }),
  });

  return (
    <>
      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800 space-y-3">
        <div>
          <h2 className="font-medium">智能调仓方案</h2>
          <p className="mt-1 text-xs text-slate-500">根据选股运行生成模拟订单草案；确认后才成交，方案 15 分钟过期。</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <RunSelect value={selectionRunId} runs={selectionRuns} onChange={setSelectionRunId} />
          <label className="flex items-center gap-2 text-xs text-amber-300">
            <input type="checkbox" checked={validationOverride} onChange={(event) => setValidationOverride(event.target.checked)} />
            允许模拟盘绕过历史验证门槛
          </label>
          <button type="button" disabled={!selectionRunId || createPaper.isPending} onClick={() => createPaper.mutate()} className="ml-auto rounded bg-sky-600 px-4 py-2 text-sm disabled:bg-slate-700">
            {createPaper.isPending ? "生成中..." : "生成方案"}
          </button>
        </div>
        {(createPaper.isError || executePaper.isError) && <div className="text-xs text-rose-300">调仓操作失败，请查看服务端返回信息。</div>}
        {rebalancePlans?.items.map((plan) => (
          <div key={plan.id} className="rounded border border-slate-800 bg-slate-950 p-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <span>方案 #{plan.id} · 选股 #{plan.selection_run_id}</span>
              <span className="text-slate-500">{plan.status}</span>
              <span className="text-slate-500">{plan.proposal.orders.length} 笔订单</span>
              {plan.status === "DRAFT" && (
                <div className="ml-auto flex gap-2">
                  <button onClick={() => cancelPaper.mutate(plan.id)} className="rounded border border-slate-700 px-3 py-1 text-xs">取消</button>
                  <button onClick={() => executePaper.mutate(plan.id)} className="rounded bg-emerald-700 px-3 py-1 text-xs">确认模拟成交</button>
                </div>
              )}
            </div>
            <OrderList orders={plan.proposal.orders} />
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-rose-900 bg-rose-950/20 p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <h2 className="font-medium text-rose-200">QMT 实盘（真实资金）</h2>
            <p className="mt-1 text-xs text-slate-400">{liveStatus?.message ?? "检查本机 QMT 配置中..."}</p>
          </div>
          <span className={`ml-auto rounded px-2 py-1 text-xs ${liveStatus?.ready ? "bg-rose-800 text-white" : "bg-slate-800 text-slate-400"}`}>
            {liveStatus?.ready ? "已就绪" : "已锁定"}
          </span>
        </div>
        {liveStatus?.ready && (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <input
                type="password"
                value={liveKey}
                onChange={(event) => setLiveKey(event.target.value)}
                autoComplete="off"
                placeholder="本机会话实盘密钥"
                className="min-w-56 bg-slate-950 border border-rose-900 rounded px-3 py-2 text-sm"
              />
              <RunSelect value={liveSelectionRunId} runs={selectionRuns} onChange={setLiveSelectionRunId} />
              <label className="flex items-center gap-2 text-xs text-rose-200">
                <input type="checkbox" checked={liveRiskAccepted} onChange={(event) => setLiveRiskAccepted(event.target.checked)} />
                我理解后续确认会使用真实资金
              </label>
              <button type="button" disabled={liveKey.length < 32 || !liveSelectionRunId || !liveRiskAccepted || createLive.isPending} onClick={() => createLive.mutate()} className="ml-auto rounded bg-rose-800 px-4 py-2 text-sm disabled:bg-slate-800">
                读取真实账户并生成方案
              </button>
            </div>
            {(createLive.isError || approveLive.isError || executeLive.isError || reconcileLive.isError) && <div className="text-xs text-rose-300">实盘操作未通过安全校验，请查看服务端返回信息。</div>}
            {livePlans?.items.map((plan) => (
              <div key={plan.id} className="rounded border border-rose-900/70 bg-slate-950 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-3">
                  <span>实盘方案 #{plan.id} · {plan.status}</span>
                  <span className="text-slate-500">资产 ¥{plan.account_snapshot.total_asset.toFixed(2)}</span>
                  <span className="text-slate-500">{plan.proposal.orders.length} 笔</span>
                  {plan.status === "DRAFT" && <button onClick={() => approveLive.mutate(plan.id)} className="ml-auto rounded border border-rose-700 px-3 py-1 text-xs">再次确认并生成一次性令牌</button>}
                  {plan.status === "APPROVED" && liveApprovalTokens[plan.id] && <button onClick={() => executeLive.mutate(plan.id)} className="ml-auto rounded bg-rose-700 px-3 py-1 text-xs font-semibold">最终提交真实委托</button>}
                  {(plan.status === "SUBMITTED" || plan.status === "PARTIAL") && <button onClick={() => reconcileLive.mutate(plan.id)} className="ml-auto rounded border border-rose-700 px-3 py-1 text-xs">刷新委托状态</button>}
                </div>
                <OrderList orders={plan.proposal.orders} />
                {plan.execution?.orders && <LiveExecutionList orders={plan.execution.orders} />}
              </div>
            ))}
          </>
        )}
      </div>
    </>
  );
}

function RunSelect({ value, runs, onChange }: { value: number; runs: SelectionResult[]; onChange: (value: number) => void }) {
  return (
    <select value={value || ""} onChange={(event) => onChange(Number(event.target.value))} className="min-w-56 bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm">
      <option value="">选择最近选股结果</option>
      {runs.map((run) => <option key={run.run_id} value={run.run_id}>#{run.run_id} · {run.trading_day} · {run.candidates.length} 只</option>)}
    </select>
  );
}

function OrderList({ orders }: { orders: Array<{ side: string; symbol: string; quantity: number; indicative_price: number }> }) {
  return (
    <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
      {orders.map((order) => <span key={`${order.side}-${order.symbol}`}>{order.side} {order.symbol} × {order.quantity} @ {order.indicative_price.toFixed(2)}</span>)}
    </div>
  );
}

function LiveExecutionList({ orders }: { orders: NonNullable<LiveRebalancePlan["execution"]>["orders"] }) {
  if (!orders || orders.length === 0) return null;
  return (
    <div className="mt-3 grid gap-1 text-xs text-slate-300">
      {orders.map((order) => (
        <div key={`${order.side}-${order.symbol}-${order.order_id ?? "unknown"}`} className="flex flex-wrap gap-2">
          <span>{order.side} {order.symbol} × {order.quantity}</span>
          <span className="text-slate-500">委托 {order.order_id ?? "未知"}</span>
          <span className="text-rose-200">{order.broker_status ?? order.status}</span>
          {typeof order.broker_traded_volume === "number" && <span className="text-slate-500">成交 {order.broker_traded_volume}</span>}
          {typeof order.broker_traded_price === "number" && <span className="text-slate-500">@ {order.broker_traded_price.toFixed(2)}</span>}
          {order.broker_status_message && <span className="text-amber-300">{order.broker_status_message}</span>}
        </div>
      ))}
    </div>
  );
}
