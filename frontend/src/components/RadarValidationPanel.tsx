/** 买点雷达的样本外验证面板（walk-forward 重放）。
 *
 * 数据来自 ``GET/POST /api/realtime/picks/validation``：把雷达的选股规则原样搬到历史上
 * 逐日重放——打分只用 ``<= t`` 的日线，成交按 ``t+1`` 开盘买入、``t+1+k`` 收盘卖出，
 * 扣除双边佣金 / 印花税 / 滑点，基准是同日同池等权收益。
 *
 * 这个面板的作用是「先证明、再使用」：样本外没有超额收益时必须显式提示不要实盘。
 * ``control=random`` 是脚手架自检，其超额收益应接近 0；明显偏离说明验证本身可疑。
 *
 * 分析结果仅用于研究，不构成投资建议。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchPicksValidation, startPicksValidation } from "../lib/api";
import type { ValidationReport } from "../lib/types";

const panel = "bg-slate-900 rounded-lg border border-slate-800";
const th =
  "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap align-top numeric";
const button =
  "rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed";

const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined || !Number.isFinite(value)
    ? "—"
    : value.toFixed(digits);

const signed = (value: number | null | undefined, digits = 2) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
};

const toneClass = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "text-slate-400";
  }
  if (value > 0) return "text-rose-400";
  if (value < 0) return "text-emerald-400";
  return "text-slate-300";
};

interface Verdict {
  tone: string;
  title: string;
  detail: string;
}

/** 主口径超额收益 + t 值给出结论；样本不足或为负时明确劝阻实盘。 */
function verdictOf(report: ValidationReport): Verdict {
  const primary =
    report.horizons.find((item) => item.horizon === report.primary_horizon) ??
    report.horizons[0];
  if (!primary || primary.observations === 0) {
    return {
      tone: "border-slate-700 bg-slate-800/40 text-slate-300",
      title: "样本不足，无法判断",
      detail: "本地日线还不够覆盖 lookback + 评估窗口，先在「智能选股」页补齐一年日线。",
    };
  }
  const horizon = primary.horizon;
  if (primary.mean_excess_pct <= 0) {
    return {
      tone: "border-rose-900 bg-rose-950/30 text-rose-200",
      title: "样本外没有超额收益 —— 不要据此实盘",
      detail:
        `主口径（持有 ${horizon} 个交易日）平均超额 ${signed(primary.mean_excess_pct)}，` +
        `胜率 ${(primary.hit_rate * 100).toFixed(1)}%，t=${primary.excess_t_stat}。` +
        "这套规则在这段历史上跑输同池等权，实盘前必须重新设计并再次验证。",
    };
  }
  if (Math.abs(primary.excess_t_stat) < 2 || primary.observations < 100) {
    return {
      tone: "border-amber-900 bg-amber-950/30 text-amber-200",
      title: "超额为正但不显著",
      detail:
        `平均超额 ${signed(primary.mean_excess_pct)}，t=${primary.excess_t_stat}，` +
        `样本 ${primary.observations}。窗口重叠会高估显著性，还不能据此加仓。`,
    };
  }
  return {
    tone: "border-emerald-900 bg-emerald-950/30 text-emerald-200",
    title: "样本内有正超额（仍需更多窗口确认）",
    detail:
      `平均超额 ${signed(primary.mean_excess_pct)}，t=${primary.excess_t_stat}，` +
      `样本 ${primary.observations}。单一窗口 + 单一样本区间，不代表未来有效。`,
  };
}

function ControlTag({ control }: { control: string }) {
  if (control === "none") return null;
  const label = control === "random" ? "随机对照组" : "最差对照组";
  const hint =
    control === "random"
      ? "不按打分选股，用于衡量噪声底噪（应接近 0）"
      : "取同池打分最差的同样数量标的";
  return (
    <span className="rounded border border-sky-900 bg-sky-950/30 px-2 py-0.5 text-xs text-sky-300">
      {label} · {hint}
    </span>
  );
}

export default function RadarValidationPanel() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["picks-validation"],
    queryFn: fetchPicksValidation,
    refetchInterval: (result) =>
      result.state.data?.status.state === "running" ? 3000 : false,
  });

  const mutation = useMutation({
    mutationFn: (control: "none" | "random" | "worst") =>
      startPicksValidation({ eval_days: 60, control }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["picks-validation"] });
    },
  });

  const status = query.data?.status;
  const report = query.data?.report ?? null;
  const running = status?.state === "running" || mutation.isPending;
  const verdict = report ? verdictOf(report) : null;
  const progress = status?.progress;
  const ratio =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.done / progress.total) * 100))
      : 0;

  return (
    <div className={`${panel} p-4`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm text-slate-400">样本外验证（walk-forward）</div>
            {report ? <ControlTag control={report.control} /> : null}
          </div>
          <div className="mt-1 text-xs text-slate-500">
            把雷达规则放到历史上逐日重放：次日开盘买入、持有 k 日收盘卖出，扣除佣金 /
            印花税 / 滑点，对比同池等权基准。次日停牌或开盘即涨停计为买不到。
          </div>
        </div>
        <div className="flex gap-2">
          <button
            className={button}
            disabled={running}
            onClick={() => mutation.mutate("none")}
          >
            {running ? "验证中…" : "运行验证（60 日）"}
          </button>
          <button
            className={button}
            disabled={running}
            onClick={() => mutation.mutate("random")}
            title="随机对照组：其超额收益应接近 0，用来检验验证脚手架本身"
          >
            随机对照自检
          </button>
        </div>
      </div>

      {running ? (
        <div className="mt-3">
          <div className="h-1.5 w-full overflow-hidden rounded bg-slate-800">
            <div
              className="h-full bg-sky-600 transition-all"
              style={{ width: `${ratio}%` }}
            />
          </div>
          <div className="mt-1 text-xs text-slate-500">
            评估日 {progress?.done ?? 0} / {progress?.total ?? 0}（单次约 100 秒，后台执行）
          </div>
        </div>
      ) : null}

      {status?.state === "failed" ? (
        <div className="mt-3 rounded border border-amber-900 bg-amber-950/20 p-3 text-xs text-amber-300">
          验证失败：{status.error}
        </div>
      ) : null}

      {query.isError ? (
        <div className="mt-3 rounded border border-amber-900 bg-amber-950/20 p-3 text-xs text-amber-300">
          读取验证结果失败：{(query.error as Error).message}
        </div>
      ) : null}

      {verdict ? (
        <div className={`mt-3 rounded border p-3 text-sm ${verdict.tone}`}>
          <div className="font-medium">{verdict.title}</div>
          <div className="mt-1 text-xs opacity-90">{verdict.detail}</div>
        </div>
      ) : null}

      {report ? (
        <>
          <div className="mt-3 overflow-x-auto">
            <table className="min-w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className={th}>持有期</th>
                  <th className={th}>样本</th>
                  <th className={th}>净收益</th>
                  <th className={th}>基准</th>
                  <th className={th}>超额</th>
                  <th className={th}>胜率</th>
                  <th className={th}>t 值</th>
                  <th className={th}>超额回撤</th>
                </tr>
              </thead>
              <tbody>
                {report.horizons.map((item) => (
                  <tr
                    key={item.horizon}
                    className={`border-b border-slate-800/50 ${
                      item.horizon === report.primary_horizon
                        ? "bg-slate-800/20"
                        : ""
                    }`}
                  >
                    <td className={td}>{item.horizon} 日</td>
                    <td className={td}>{item.observations}</td>
                    <td className={`${td} ${toneClass(item.mean_net_pct)}`}>
                      {signed(item.mean_net_pct)}
                    </td>
                    <td className={`${td} text-slate-400`}>
                      {signed(item.mean_benchmark_pct)}
                    </td>
                    <td className={`${td} ${toneClass(item.mean_excess_pct)}`}>
                      {signed(item.mean_excess_pct)}
                    </td>
                    <td className={td}>{(item.hit_rate * 100).toFixed(1)}%</td>
                    <td className={`${td} ${toneClass(item.excess_t_stat)}`}>
                      {num(item.excess_t_stat)}
                    </td>
                    <td className={`${td} text-slate-400`}>
                      {item.max_excess_drawdown_pct.toFixed(1)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {report.score_buckets.length ? (
            <div className="mt-3 text-xs text-slate-400">
              分数分层（主口径{" "}
              {report.horizons.find((h) => h.horizon === report.primary_horizon)
                ?.horizon ?? report.primary_horizon}{" "}
              日超额）：
              {report.score_buckets.map((bucket) => (
                <span key={bucket.label} className="ml-2 whitespace-nowrap">
                  {bucket.label}：{signed(bucket.mean_excess_pct)}（
                  {bucket.observations}）
                </span>
              ))}
            </div>
          ) : null}

          <div className="mt-3 grid gap-1 text-xs text-slate-500 sm:grid-cols-2">
            <div>
              评估窗口 {report.first_signal_day} ~ {report.last_signal_day}（
              {report.evaluation_days} 天，股票池 {report.universe_size} 只）
            </div>
            <div>
              日线 {report.bars_adjust === "qfq" ? "前复权" : "不复权"}{" "}
              {report.bars_first_day} ~ {report.bars_last_day} · 往返成本{" "}
              {report.costs.round_trip_pct}% · 耗时{" "}
              {report.elapsed_seconds.toFixed(0)} 秒
            </div>
          </div>

          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-400">
              已知偏差与口径说明（{report.caveats.length} 条，务必先读）
            </summary>
            <ul className="mt-2 space-y-1 text-xs text-slate-500">
              {report.caveats.map((text) => (
                <li key={text}>· {text}</li>
              ))}
            </ul>
          </details>
        </>
      ) : null}

      {!report && !running && status?.state !== "failed" ? (
        <div className="mt-3 text-xs text-slate-500">
          还没有验证结果。点「运行验证」后，后端会用本地日线逐日重放（约 100 秒）。
          先点「随机对照自检」可以确认这套验证本身没有系统性偏差。
        </div>
      ) : null}

      <div className="mt-3 text-xs text-amber-400/80">
        ⚠️ 分析结果仅用于研究，不构成投资建议。样本外没有超额收益时，请勿投入真实资金。
      </div>
    </div>
  );
}
