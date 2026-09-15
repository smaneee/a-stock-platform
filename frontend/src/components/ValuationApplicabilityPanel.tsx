/** S2 面板：估值方法适用性（拒绝/警示/未知）+ 现价隐含增长率的敏感性 + 结构化失效条件。
 *
 * 三件事都直接对应方案 S2 的要求：
 *  · 「不适用的行业模型明确拒绝」→ 拒绝时页面必须显著标出，且不再展示价值判断；
 *  · 「同一价格只提高价格不能提高安全边际」→ 隐含增长率与敏感性都由后端算好，前端只展示；
 *  · 「把通用风险描述转换成有证据和时间的可验证条件」→ 每条条件显示阈值、来源与证据 id。
 */
import type {
  ReverseValuationResponse,
  StructuredCondition,
  ValuationApplicabilityRules,
} from "../lib/types";

const panel = "rounded-lg border border-slate-800 bg-slate-900 p-4";

const VERDICT_TONE: Record<string, { box: string; label: string }> = {
  ok: { box: "border-emerald-900 bg-emerald-950/20 text-emerald-200", label: "方法适用" },
  warning: { box: "border-amber-800 bg-amber-950/20 text-amber-200", label: "适用但有前提" },
  rejected: { box: "border-rose-900 bg-rose-950/20 text-rose-200", label: "方法不适用：已拒绝输出价值判断" },
  unknown: { box: "border-slate-700 bg-slate-950/60 text-slate-300", label: "证据不足：无法判定适用性" },
};

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export default function ValuationApplicabilityPanel({
  rules,
  reverse,
  conditions,
}: {
  rules: ValuationApplicabilityRules | null | undefined;
  reverse: ReverseValuationResponse | null | undefined;
  conditions: StructuredCondition[] | undefined;
}) {
  const tone = rules ? VERDICT_TONE[rules.verdict] ?? VERDICT_TONE.unknown : null;
  const sensitivity = reverse?.implied_growth_sensitivity;

  return (
    <section className={panel}>
      <h2 className="font-medium">估值方法适用性与现价隐含预期（S2）</h2>
      <p className="mt-1 text-xs text-slate-500">
        适用性由代码按已入库报表数据判定；隐含增长率由反向估值求解，敏感性展示它对折现率的依赖。
      </p>

      {rules ? (
        <div className={`mt-3 rounded border p-3 text-sm ${tone?.box}`}>
          <div className="font-medium">
            适用性判定：{tone?.label}
            {rules.requires_normalization ? "（要求正常化盈利）" : ""}
          </div>
          {rules.rejected_reason ? <div className="mt-1 text-xs">{rules.rejected_reason}</div> : null}
          {rules.caveats.length ? (
            <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">
              {rules.caveats.map((item) => <li key={item}>{item}</li>)}
            </ul>
          ) : null}
          {rules.missing_data.length ? (
            <div className="mt-1 text-xs">缺判定数据：{rules.missing_data.join("、")}</div>
          ) : null}
          <div className="mt-2 text-[11px] opacity-80">{rules.rule_note}</div>
          <div className="mt-1 font-mono text-[11px] opacity-80">
            {Object.entries(rules.data_used)
              .filter(([, value]) => value !== null && value !== undefined)
              .map(([key, value]) => `${key}=${String(value)}`)
              .join("  ")}
          </div>
        </div>
      ) : (
        <p className="mt-3 text-xs text-slate-500">本次分析未返回适用性判定（先跑一次「生成研究」）。</p>
      )}

      {reverse ? (
        <div className="mt-3 rounded border border-slate-800 bg-slate-950/60 p-3 text-sm">
          <div className="text-slate-300">
            现价 {reverse.target_price} 隐含收入增长率：
            <span className="ml-1 font-medium text-sky-300">
              {reverse.status === "solved"
                ? pct(reverse.implied_growth)
                : reverse.status === "below_range"
                  ? "低于可解区间下界（价格过低，反解不出）"
                  : "高于可解区间上界（价格过高，反解不出）"}
            </span>
            {reverse.status === "solved" && reverse.pricing_error !== undefined ? (
              <span className="ml-2 text-xs text-slate-500">定价误差 {reverse.pricing_error}</span>
            ) : null}
          </div>
          <div className="mt-1 text-xs text-slate-500">{reverse.note}</div>

          {sensitivity ? (
            <div className="mt-3">
              <div className="text-xs text-slate-500">
                敏感性：不同折现率下隐含的增长率（可解 {sensitivity.solved_count}/{sensitivity.rows.length}）
                {sensitivity.implied_growth_range
                  ? `｜区间 ${pct(sensitivity.implied_growth_range[0])} ~ ${pct(sensitivity.implied_growth_range[1])}`
                  : ""}
              </div>
              <table className="mt-1 min-w-full text-xs">
                <thead className="text-slate-500">
                  <tr>
                    <th className="px-2 py-1 text-left">折现率</th>
                    <th className="px-2 py-1 text-right">隐含增长率</th>
                    <th className="px-2 py-1 text-left">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {sensitivity.rows.map((row) => (
                    <tr key={row.discount_rate} className="border-t border-slate-800">
                      <td className="px-2 py-1">{pct(row.discount_rate, 2)}</td>
                      <td className="px-2 py-1 text-right text-slate-200">
                        {row.status === "solved" ? pct(row.implied_growth) : "—"}
                      </td>
                      <td className="px-2 py-1 text-slate-500">
                        {row.status === "solved" ? "可解" : row.status === "invalid" ? "假设不成立" : "区间外（未外推）"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="mt-1 text-[11px] text-slate-500">{sensitivity.note}</div>
            </div>
          ) : null}
        </div>
      ) : null}

      {conditions?.length ? (
        <div className="mt-3">
          <div className="text-xs text-slate-500">
            可验证的失效条件（阈值 + 来源 + 证据 id，共 {conditions.length} 条）
          </div>
          <ul className="mt-1 space-y-1 text-xs">
            {conditions.map((item) => (
              <li key={`${item.metric}-${item.evidence_id}`} className="text-slate-300">
                · {item.check}
                <span className="ml-2 text-slate-500">当前 {item.current}{item.unit ?? ""}</span>
                <span className="ml-2 text-slate-500">｜来源 {item.source}</span>
                <span className="ml-2 font-mono text-[11px] text-sky-300">{item.evidence_id}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
