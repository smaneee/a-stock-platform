/** 策略证据页（P1-01）。
 *
 * 展示后端 `/api/evidence` 的版本化证据条目：状态、数据截止日、样本量、
 * 样本外结果、限制、证据来源与展示约束。
 *
 * 设计原则：
 *  - 负结果与「不确定」必须与「通过」同样显眼，不提供任何折叠/隐藏开关；
 *  - 状态一律取自后端产物（`status_vocabulary`），前端不自行判定「通过」；
 *  - 实盘闸门状态直接展示，避免「看板上有排名就以为能实盘」。
 */
import { useQuery } from "@tanstack/react-query";

import { fetchEvidence } from "../lib/api";
import type { EvidenceItem, EvidenceResponse } from "../lib/types";

const STATUS_STYLE: Record<string, string> = {
  passed_oos: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  failed_oos: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  inconclusive: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  in_progress: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  unverified: "bg-slate-500/15 text-slate-300 border-slate-500/40",
};

function StatusBadge({
  status,
  vocabulary,
}: {
  status: string;
  vocabulary?: Record<string, string>;
}) {
  const style = STATUS_STYLE[status] ?? STATUS_STYLE.unverified;
  return (
    <span className={`rounded border px-2 py-0.5 text-xs ${style}`}>
      {vocabulary?.[status] ?? status}
    </span>
  );
}

function KeyValues({ title, data }: { title: string; data?: Record<string, unknown> }) {
  if (!data || Object.keys(data).length === 0) return null;
  return (
    <div className="mt-3">
      <div className="text-xs font-medium text-slate-400">{title}</div>
      <div className="mt-1 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-slate-300 sm:grid-cols-2">
        {Object.entries(data).map(([key, value]) => (
          <div key={key} className="flex items-baseline justify-between gap-3 border-b border-slate-800/60 py-0.5">
            <span className="text-slate-500">{key}</span>
            <span className="numeric text-right">
              {Array.isArray(value) ? value.join(" ~ ") : String(value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function EvidenceCard({
  item,
  vocabulary,
}: {
  item: EvidenceItem;
  vocabulary?: Record<string, string>;
}) {
  const negative = item.status === "failed_oos" || item.status === "inconclusive";
  return (
    <section
      className={`rounded-lg border p-4 ${
        negative ? "border-rose-500/30 bg-rose-500/5" : "border-slate-800 bg-slate-900"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-100">{item.name}</h3>
          <p className="mt-0.5 text-xs text-slate-500">
            数据截止 {item.data_cutoff ?? "未知"} · 复权口径 {item.bars_adjust ?? "未知"} ·{" "}
            {item.reproducible_on_this_machine ? "本机可复现" : "本机不可复现（仅历史文献）"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {item.production_ready ? (
            <span className="rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300">
              可用于实盘
            </span>
          ) : (
            <span className="rounded border border-slate-600 px-2 py-0.5 text-xs text-slate-400">
              仅研究 / 模拟
            </span>
          )}
          <StatusBadge status={item.status} vocabulary={vocabulary} />
        </div>
      </div>

      <KeyValues title="样本" data={item.sample as Record<string, unknown>} />
      <KeyValues title="结果" data={item.result as Record<string, unknown>} />

      {item.failure_reason ? (
        <p className="mt-3 rounded border border-slate-700 bg-slate-950/60 px-3 py-2 text-xs text-slate-300">
          <span className="text-slate-500">判定依据：</span>
          {item.failure_reason}
        </p>
      ) : null}

      <div className="mt-3">
        <div className="text-xs font-medium text-slate-400">限制（不可省略）</div>
        <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-slate-400">
          {item.limitations.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
        <span>证据来源：{item.evidence_source}</span>
        <span className="rounded bg-slate-800/60 px-2 py-0.5">{item.ui_rule}</span>
      </div>
    </section>
  );
}

export default function EvidencePage() {
  const { data, isLoading, error } = useQuery<EvidenceResponse>({
    queryKey: ["evidence"],
    queryFn: fetchEvidence,
  });

  if (isLoading) {
    return <div className="text-sm text-slate-400">正在加载策略证据…</div>;
  }
  if (error || !data) {
    return (
      <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
        证据不可用：{error instanceof Error ? error.message : "未知错误"}。
        证据文件缺失时必须报错显示，不允许静默隐藏。
      </div>
    );
  }

  const negativeIds = new Set(data.summary.negative_or_uncertain);

  return (
    <div className="space-y-4">
      <header className="rounded-lg border border-slate-800 bg-slate-900 p-4">
        <h1 className="text-lg font-semibold text-slate-100">策略证据</h1>
        <p className="mt-1 text-xs text-slate-400">
          本页由版本化证据文件驱动（{data.source_file}），证据版本{" "}
          {data.artifact_updated_at ?? "未知"} · schema {data.schema_version}。
          负结果与不确定结论同页完整展示，**不提供隐藏开关**。
        </p>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2">
            <div className="text-slate-500">条目总数</div>
            <div className="numeric text-slate-200">{data.summary.total}</div>
          </div>
          <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2">
            <div className="text-slate-500">可用于实盘</div>
            <div className="numeric text-rose-300">{data.summary.production_ready_count}</div>
          </div>
          <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2">
            <div className="text-slate-500">负结果 / 不确定</div>
            <div className="numeric text-amber-300">{negativeIds.size}</div>
          </div>
          <div className="rounded border border-slate-800 bg-slate-950 px-3 py-2">
            <div className="text-slate-500">实盘开关</div>
            <div className="numeric text-slate-200">
              {data.production_gate.live_trading_enabled ? "已开启" : "关闭"}
            </div>
          </div>
        </div>
        <p className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          实盘闸门：{data.production_gate.reason}
        </p>
        <details className="mt-2 text-xs text-slate-400">
          <summary className="cursor-pointer text-slate-500">小额实盘试点的前置条件（{data.production_gate.requirements_for_small_live_pilot.length} 条）</summary>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {data.production_gate.requirements_for_small_live_pilot.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </details>
      </header>

      {data.items.map((item) => (
        <EvidenceCard key={item.id} item={item} vocabulary={data.status_vocabulary} />
      ))}

      <footer className="pb-4 text-xs text-slate-500">{data.disclaimer}</footer>
    </div>
  );
}
