/** 研究决策卡（S1）：把结构化研究结论渲染成一张卡片。
 *
 * 卡片里的数字与结构全部来自后端确定性计算；模型只贡献"论点"和"预期差"两段文字，
 * 且这两段必须通过证据 id 与数字校验（不合格时 `citations_valid=false`，页面显式提示）。
 * 增仓门禁（position_gate）也在这里展示：缺关键数据/引用无效/方法不适用 → 明确"不允许增仓"。
 */
import type { ThesisCard, ThesisPositionGate } from "../lib/types";

const panel = "rounded-lg border border-slate-800 bg-slate-900 p-4";

const DECISION_TONE: Record<string, string> = {
  research_candidate: "border-sky-700 bg-sky-950/30 text-sky-200",
  watch: "border-slate-700 bg-slate-950/60 text-slate-200",
  await_validation: "border-amber-800 bg-amber-950/20 text-amber-200",
  insufficient_data: "border-rose-900 bg-rose-950/20 text-rose-200",
  not_applicable: "border-rose-900 bg-rose-950/20 text-rose-200",
};

function Chip({ text, tone }: { text: string; tone: string }) {
  return <span className={`rounded border px-2 py-0.5 text-xs ${tone}`}>{text}</span>;
}

export default function ThesisCardPanel({
  card,
  gate,
}: {
  card: ThesisCard;
  gate: ThesisPositionGate | null;
}) {
  const tone = DECISION_TONE[card.decision] ?? DECISION_TONE.watch;
  return (
    <section className={panel}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-medium">研究决策卡（结构化）</h2>
          <p className="mt-1 text-xs text-slate-500">
            策略类型、期限、证据 id、假设、情景、失效条件、缺失数据由代码生成；模型只写论点与预期差。
          </p>
        </div>
        <div className={`rounded border px-3 py-1 text-sm ${tone}`}>{card.decision_label}</div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Chip text={`策略：${card.strategy_label}`} tone="border-slate-700 bg-slate-950 text-slate-300" />
        <Chip text={`期限：${card.horizon}`} tone="border-slate-700 bg-slate-950 text-slate-300" />
        <Chip
          text={card.citations_valid ? "引用校验：通过" : "引用校验：不通过"}
          tone={card.citations_valid ? "border-emerald-800 bg-emerald-950/30 text-emerald-300"
                                     : "border-rose-800 bg-rose-950/30 text-rose-300"}
        />
        <Chip text={`来源：${card.text_origin === "deterministic_skeleton" ? "仅代码（无模型文字）" : "模型主审 + 反方"}`}
              tone="border-slate-700 bg-slate-950 text-slate-400" />
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div>
          <div className="text-xs text-slate-500">论点（模型）</div>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-300">
            {card.thesis || "（未生成：解释层不可用或未通过校验；结构性结论仍然有效）"}
          </p>
          <div className="mt-3 text-xs text-slate-500">预期差 / 反方视角（模型）</div>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-300">
            {card.variant_view || "（未生成独立反方意见）"}
          </p>
        </div>
        <div className="space-y-3 text-sm">
          <div>
            <div className="text-xs text-slate-500">收益来源</div>
            <div className="text-slate-300">{card.return_source}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">数据截止</div>
            <div className="text-slate-300">{card.as_of}</div>
          </div>
          <div>
            <div className="text-xs text-slate-500">置信依据（不是上涨概率）</div>
            <div className="text-slate-300">
              质量分 {card.confidence_basis.quality_score ?? "—"} ·
              覆盖率 {card.confidence_basis.quality_coverage ?? "—"} ·
              证据置信度 {card.confidence_basis.evidence_confidence ?? "—"}
            </div>
          </div>
          <div>
            <div className="text-xs text-slate-500">估值方法</div>
            <div className="text-slate-300">
              {card.valuation_method.model ?? "未提供"}
              {card.valuation_method.applicable ? "" : "（**不适用于该行业**）"}
            </div>
            {card.valuation_method.caveat ? (
              <div className="mt-1 text-xs text-amber-300/80">{card.valuation_method.caveat}</div>
            ) : null}
          </div>
        </div>
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div>
          <div className="text-xs text-slate-500">支持证据 id（{card.supporting_evidence_ids.length}）</div>
          <div className="mt-1 flex flex-wrap gap-1">
            {card.supporting_evidence_ids.length ? card.supporting_evidence_ids.map((id) => (
              <span key={id} className="rounded bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-emerald-300">{id}</span>
            )) : <span className="text-xs text-slate-500">无</span>}
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">
            反对证据 id（{card.opposing_evidence_ids.length}，结构化意见 {card.opposing_opinions.length} 条）
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {card.opposing_evidence_ids.length ? card.opposing_evidence_ids.map((id) => (
              <span key={id} className="rounded bg-slate-950 px-1.5 py-0.5 font-mono text-[11px] text-rose-300">{id}</span>
            )) : <span className="text-xs text-amber-300/80">无（反方意见未标注 id 时会计入下方缺口）</span>}
          </div>
        </div>
      </div>

      {card.opposing_opinions.length ? (
        <div className="mt-3">
          <div className="text-xs text-slate-500">独立反方意见（结构化契约）</div>
          <ul className="mt-1 space-y-1 text-xs">
            {card.opposing_opinions.map((item, index) => (
              <li key={`${item.evidence_id}-${index}`} className={item.evidence_exists ? "text-rose-200" : "text-amber-300"}>
                · {item.claim}
                <span className="ml-1 font-mono text-[11px] text-slate-400">
                  [{item.evidence_id}{item.evidence_exists ? "" : " ✗ 不存在"}]
                </span>
                {item.why_it_matters ? <span className="text-slate-400"> — {item.why_it_matters}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {card.shared_evidence_ids.length ? (
        <div className="mt-2 text-xs text-slate-500">
          两方同时引用的证据：{card.shared_evidence_ids.join("、")}（同一证据的两面看法，不是缺陷）
        </div>
      ) : null}

      {card.cannot_answer.length ? (
        <div className="mt-2 rounded border border-slate-800 bg-slate-950/60 p-2 text-xs text-slate-400">
          反方明确"证据不足无法判断"：{card.cannot_answer.join("；")}
        </div>
      ) : null}

      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        <div>
          <div className="text-xs text-slate-500">失效条件</div>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-slate-400">
            {card.invalidation_conditions.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
        <div>
          <div className="text-xs text-slate-500">复核触发</div>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-slate-400">
            {card.review_triggers.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
      </div>

      {card.missing_data.length || card.gaps.length ? (
        <div className="mt-3 rounded border border-amber-900 bg-amber-950/20 p-3 text-xs text-amber-200">
          {card.missing_data.length ? <div>缺失数据：{card.missing_data.join("、")}</div> : null}
          {card.gaps.map((gap) => <div key={gap}>结构缺口：{gap}</div>)}
        </div>
      ) : null}

      <div className={`mt-3 rounded border p-3 text-sm ${gate?.allowed ? "border-emerald-900 bg-emerald-950/20 text-emerald-200" : "border-rose-900 bg-rose-950/20 text-rose-200"}`}>
        <div className="font-medium">
          增仓门禁：{gate ? (gate.allowed ? "允许（仅表示数据与校验通过，不等于建议增仓）" : "不允许") : "未计算"}
        </div>
        {gate?.blockers?.length ? (
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">
            {gate.blockers.map((item) => <li key={item}>{item}</li>)}
          </ul>
        ) : null}
        {gate?.note ? <div className="mt-1 text-xs opacity-80">{gate.note}</div> : null}
      </div>

      {card.citation_report?.invalid_ids?.length || card.citation_report?.unverified_numbers?.length ? (
        <div className="mt-3 rounded border border-rose-900 bg-rose-950/20 p-3 text-xs text-rose-200">
          引用校验明细：无效 id {JSON.stringify(card.citation_report.invalid_ids)} ·
          无依据数字 {JSON.stringify(card.citation_report.unverified_numbers)}
        </div>
      ) : null}

      <div className="mt-3 text-xs text-slate-500">
        版本：模型 {card.model_version} · 提示词 {card.prompt_version}
        {card.model_usage?.total_tokens ? ` · 本次 ${card.model_usage.total_tokens} tokens / ${card.model_usage.latency_ms ?? "—"} ms` : ""}
        {card.model_usage?.note ? ` · ${card.model_usage.note}` : ""}
      </div>
    </section>
  );
}
