import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  cancelHistoryIngest,
  createDailyPipelineRun,
  createHistoryIngest,
  evaluateSelection,
  fetchDailyPipelineSchedule,
  fetchQfqBackfillStatus,
  fetchSelectionEvaluationSummary,
  listDailyPipelineRuns,
  listHistoryIngest,
  listPaperAccounts,
  listUniverseSnapshots,
  rankStocks,
  startQfqBackfill,
} from "../lib/api";

const percent = (value: number) => `${(value * 100).toFixed(2)}%`;

export default function SelectionPage() {
  const queryClient = useQueryClient();
  const [tradingDay, setTradingDay] = useState("");
  const [topN, setTopN] = useState(5);
  const [pipelineAccountId, setPipelineAccountId] = useState(0);
  const [pipelineAutoExecute, setPipelineAutoExecute] = useState(false);
  const [pipelineValidationOverride, setPipelineValidationOverride] = useState(true);
  const snapshots = useQuery({
    queryKey: ["universe-snapshots"],
    queryFn: listUniverseSnapshots,
  });
  const evaluationSummary = useQuery({
    queryKey: ["selection-evaluation-summary"],
    queryFn: fetchSelectionEvaluationSummary,
  });
  const ranking = useMutation({
    mutationFn: rankStocks,
  });
  const evaluation = useMutation({
    mutationFn: (runId: number) => evaluateSelection(runId, 20),
    onSuccess: () => evaluationSummary.refetch(),
  });
  const displayedResult = evaluation.data ?? ranking.data;
  const ingestTasks = useQuery({
    queryKey: ["history-ingest"],
    queryFn: listHistoryIngest,
    refetchInterval: 3000,
  });
  const dailyPipeline = useQuery({
    queryKey: ["daily-pipeline-runs"],
    queryFn: listDailyPipelineRuns,
    refetchInterval: 3000,
  });
  const pipelineSchedule = useQuery({
    queryKey: ["daily-pipeline-schedule"],
    queryFn: fetchDailyPipelineSchedule,
    refetchInterval: 30000,
  });
  const paperAccounts = useQuery({
    queryKey: ["paper-accounts"],
    queryFn: listPaperAccounts,
  });
  const createIngest = useMutation({
    mutationFn: createHistoryIngest,
    onSuccess: () => ingestTasks.refetch(),
  });
  const createPipeline = useMutation({
    mutationFn: () => createDailyPipelineRun({
      trading_day: tradingDay,
      paper_account_id: pipelineAccountId || null,
      auto_execute_paper: pipelineAutoExecute,
      paper_validation_override: pipelineValidationOverride,
      top_n: topN,
      history_lookback_days: 365,
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["daily-pipeline-runs"] }),
  });
  const cancelIngest = useMutation({
    mutationFn: cancelHistoryIngest,
    onSuccess: () => ingestTasks.refetch(),
  });
  const qfqStatus = useQuery({
    queryKey: ["qfq-backfill"],
    queryFn: fetchQfqBackfillStatus,
    refetchInterval: 3000,
  });
  const startQfq = useMutation({
    mutationFn: () => startQfqBackfill(),
    onSuccess: () => qfqStatus.refetch(),
  });
  const latestIngest = ingestTasks.data?.items[0];
  const qfq = qfqStatus.data;

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

      {evaluationSummary.data && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Metric label="已验证批次" value={String(evaluationSummary.data.evaluated_runs)} />
          <Metric label="历史候选胜率" value={percent(evaluationSummary.data.forward_win_rate)} />
          <Metric label="平均 RankIC" value={evaluationSummary.data.average_rank_ic === null ? "—" : evaluationSummary.data.average_rank_ic.toFixed(3)} />
          <Metric label="平均换手率" value={evaluationSummary.data.average_turnover === null ? "—" : percent(evaluationSummary.data.average_turnover)} />
        </div>
      )}

      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <div className="font-medium">每日自动流水线</div>
            <div className="text-xs text-slate-500 mt-1">
              自动串起股票池、历史入库、选股和模拟调仓草案；历史未完成时会等待后台任务。
            </div>
            <div className="text-xs text-slate-500 mt-1">
              {pipelineSchedule.data
                ? pipelineSchedule.data.enabled
                  ? `自动调度：交易日 ${String(pipelineSchedule.data.hour).padStart(2, "0")}:${String(pipelineSchedule.data.minute).padStart(2, "0")} 创建${
                      pipelineSchedule.data.next_run_at
                        ? ` · 下次 ${new Date(pipelineSchedule.data.next_run_at).toLocaleString("zh-CN")}`
                        : ""
                    }${pipelineSchedule.data.auto_execute_paper ? " · 自动执行模拟调仓" : " · 仅生成草案"}`
                  : "自动调度：未开启（在 .env 设置 DAILY_PIPELINE_AUTO_ENABLED=true）"
                : "自动调度：读取中…"}
            </div>
          </div>
          <select
            value={pipelineAccountId || ""}
            onChange={(event) => setPipelineAccountId(Number(event.target.value))}
            className="min-w-44 bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm"
          >
            <option value="">不接模拟账户</option>
            {paperAccounts.data?.map((account) => (
              <option key={account.id} value={account.id}>{account.name}</option>
            ))}
          </select>
          <label className="flex items-center gap-2 text-xs text-slate-300">
            <input
              type="checkbox"
              checked={pipelineValidationOverride}
              onChange={(event) => setPipelineValidationOverride(event.target.checked)}
            />
            模拟盘允许验证不足
          </label>
          <label className="flex items-center gap-2 text-xs text-amber-300">
            <input
              type="checkbox"
              checked={pipelineAutoExecute}
              onChange={(event) => setPipelineAutoExecute(event.target.checked)}
              disabled={!pipelineAccountId}
            />
            自动执行模拟调仓
          </label>
          <button
            type="button"
            disabled={!tradingDay || createPipeline.isPending}
            onClick={() => createPipeline.mutate()}
            className="ml-auto rounded bg-emerald-700 hover:bg-emerald-600 disabled:bg-slate-700 px-4 py-2 text-sm"
          >
            {createPipeline.isPending ? "创建中..." : "运行每日流水线"}
          </button>
        </div>
        {dailyPipeline.data?.items.slice(0, 3).map((run) => (
          <div key={run.id} className="space-y-2 text-xs text-slate-400">
            <div className="flex flex-wrap justify-between gap-2">
              <span>#{run.id} · {run.trading_day} · {run.status} · {run.stage}</span>
              <span>
                历史 #{run.history_task_id ?? "—"} · 选股 #{run.selection_run_id ?? "—"} · 调仓 #{run.paper_plan_id ?? "—"}
              </span>
            </div>
            <div className="h-2 rounded bg-slate-800 overflow-hidden">
              <div className="h-full bg-emerald-500" style={{ width: `${run.progress}%` }} />
            </div>
            {run.error_message && <div className="text-amber-400">{run.error_message}</div>}
          </div>
        ))}
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <div className="font-medium">历史行情准备</div>
            <div className="text-xs text-slate-500 mt-1">
              为当前快照中的全部可交易股票后台补齐一年日线；任务支持重启恢复和取消。
            </div>
          </div>
          <button
            type="button"
            disabled={!tradingDay || createIngest.isPending || latestIngest?.status === "running" || latestIngest?.status === "queued"}
            onClick={() => createIngest.mutate(tradingDay)}
            className="ml-auto rounded border border-slate-700 hover:border-sky-500 disabled:opacity-40 px-4 py-2 text-sm"
          >
            {createIngest.isPending ? "创建中..." : "开始全市场入库"}
          </button>
          {latestIngest && ["queued", "running"].includes(latestIngest.status) && (
            <button
              type="button"
              onClick={() => cancelIngest.mutate(latestIngest.id)}
              className="rounded border border-rose-800 px-3 py-2 text-sm text-rose-300"
            >
              取消
            </button>
          )}
        </div>
        {latestIngest && (
          <div className="space-y-2 text-xs text-slate-400">
            <div className="flex justify-between">
              <span>任务 #{latestIngest.id} · {latestIngest.status}</span>
              <span>{latestIngest.completed_symbols}/{latestIngest.requested_symbols} · {latestIngest.total_bars} bars</span>
            </div>
            <div className="h-2 rounded bg-slate-800 overflow-hidden">
              <div className="h-full bg-sky-500" style={{ width: `${latestIngest.progress}%` }} />
            </div>
            {latestIngest.last_error && <div className="text-amber-400">{latestIngest.last_error}</div>}
          </div>
        )}
      </div>

      <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div>
            <div className="font-medium">前复权日线准备</div>
            <div className="text-xs text-slate-500 mt-1">
              用通达信除权除息数据把本地未复权日线换算成前复权（adjust=qfq），全市场约
              4~5 分钟。买点雷达与样本外验证优先使用前复权口径，缺失时自动回退不复权。
            </div>
          </div>
          <button
            type="button"
            disabled={startQfq.isPending || qfq?.state === "running"}
            onClick={() => startQfq.mutate()}
            className="ml-auto rounded border border-slate-700 hover:border-sky-500 disabled:opacity-40 px-4 py-2 text-sm"
          >
            {startQfq.isPending ? "启动中..." : "开始前复权回填"}
          </button>
        </div>
        {qfq && (
          <div className="space-y-2 text-xs text-slate-400">
            <div className="flex justify-between">
              <span>
                任务 {qfq.state}
                {qfq.progress.total
                  ? ` · ${qfq.progress.done}/${qfq.progress.total}`
                  : ""}
              </span>
              {qfq.report && (
                <span>
                  成功 {qfq.report.symbols_ok} · 失败 {qfq.report.symbols_failed} · 写入{" "}
                  {qfq.report.rows_written} 行 · {qfq.report.elapsed_seconds}s
                </span>
              )}
            </div>
            {qfq.progress.total ? (
              <div className="h-2 rounded bg-slate-800 overflow-hidden">
                <div
                  className="h-full bg-emerald-500"
                  style={{
                    width: `${Math.round(
                      (qfq.progress.done / qfq.progress.total) * 100,
                    )}%`,
                  }}
                />
              </div>
            ) : null}
            {qfq.report?.failure_count ? (
              <div className="text-amber-400">
                {qfq.report.failure_count} 只标的未回填（不会写入假前复权数据），可重跑补齐。
              </div>
            ) : null}
            {qfq.error && <div className="text-rose-300">{qfq.error}</div>}
          </div>
        )}
      </div>

      {(ranking.isError || evaluation.isError) && (
        <div className="rounded border border-rose-900 bg-rose-950/30 p-4 text-sm text-rose-300">
          {evaluation.error instanceof Error
            ? evaluation.error.message
            : ranking.error instanceof Error
              ? ranking.error.message
              : "选股或评估失败"}
        </div>
      )}

      {displayedResult && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-sm text-slate-400">
            <span>
              运行 #{displayedResult.run_id} · 基础候选 {displayedResult.total_candidates} ·
              数据合格 {displayedResult.eligible_count}
            </span>
            <button
              type="button"
              disabled={evaluation.isPending}
              onClick={() => evaluation.mutate(displayedResult.run_id)}
              className="ml-auto rounded border border-slate-700 px-3 py-1.5 text-xs hover:border-sky-500 disabled:opacity-40"
            >
              {evaluation.isPending ? "评估中..." : "验证未来20日表现"}
            </button>
          </div>
          {displayedResult.evaluation_coverage !== null && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Metric label="评估覆盖率" value={percent(displayedResult.evaluation_coverage)} />
              <Metric label="平均未来收益" value={percent(displayedResult.mean_forward_return ?? 0)} />
              <Metric label="候选胜率" value={percent(displayedResult.forward_win_rate ?? 0)} />
            </div>
          )}
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
                  <th className="px-3 py-2 text-right">未来20日</th>
                </tr>
              </thead>
              <tbody>
                {displayedResult.candidates.map((item) => (
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
                    <td className="px-3 py-2 text-right">
                      {item.forward_return === null ? "—" : percent(item.forward_return)}
                    </td>
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900 p-3">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold">{value}</div>
    </div>
  );
}
