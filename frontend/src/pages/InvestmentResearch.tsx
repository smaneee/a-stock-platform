import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import {
  analyzeInvestment,
  explainInvestment,
  fetchFundamentalDetail,
  fetchInvestmentReview,
  listInvestmentResearch,
  refreshStatementDetail,
  reverseValuation,
  saveInvestmentResearch,
} from "../lib/api";
import type { InvestmentEvidenceItem, ValuationInput } from "../lib/types";

const panel = "rounded-lg border border-slate-800 bg-slate-900 p-4";
const input = "w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none";

const pctText = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;
const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(digits);

function EvidenceList({ items, tone }: { items: InvestmentEvidenceItem[]; tone: "good" | "bad" }) {
  if (!items.length) return <p className="text-sm text-slate-500">当前证据不足。</p>;
  return (
    <ul className="space-y-2">
      {items.map((item, index) => (
        <li key={`${item.evidence}-${index}`} className="rounded border border-slate-800 bg-slate-950/60 p-3 text-sm">
          <div className={tone === "good" ? "text-emerald-300" : "text-rose-300"}>{item.evidence}</div>
          <div className="mt-1 text-xs text-slate-500">{item.origin} · {item.source}</div>
        </li>
      ))}
    </ul>
  );
}

export default function InvestmentResearchPage() {
  const [symbolInput, setSymbolInput] = useState("000333");
  const [symbol, setSymbol] = useState("000333");
  const [horizon, setHorizon] = useState("3 年以上");
  const [question, setQuestion] = useState("请用投资委员会视角说明最关键的正反证据和未验证事项。只引用证据包已有数字和 id，不要计算、换算或新增任何数字。");
  const [valuation, setValuation] = useState<ValuationInput>({
    revenue: 0,
    revenue_growth: 0.06,
    fcf_margin: 0.09,
    discount_rate: 0.10,
    terminal_growth: 0.02,
    shares: 0,
    net_debt: 0,
    years: 5,
    basis: "使用者情景假设；营收与股本取自最新快照，现金流率和净负债需人工复核",
    revenue_basis: "report_period",
    bear_overrides: { revenue_growth: 0, fcf_margin: 0.06 },
    bull_overrides: { revenue_growth: 0.12, fcf_margin: 0.12 },
  });

  const detail = useQuery({
    queryKey: ["fundamental-detail", symbol],
    queryFn: () => fetchFundamentalDetail(symbol),
    retry: false,
  });
  const researchHistory = useQuery({
    queryKey: ["investment-research-history", symbol],
    queryFn: () => listInvestmentResearch(symbol),
    enabled: Boolean(detail.data),
  });

  useEffect(() => {
    const snapshot = detail.data?.snapshot;
    if (!snapshot) return;
    const observedGrowth = snapshot.revenue_yoy === null
      ? 0.06
      : Math.max(-0.10, Math.min(0.20, snapshot.revenue_yoy / 100));
    setValuation((current) => ({
      ...current,
      revenue: snapshot.revenue ?? 0,
      shares: snapshot.derived.shares_outstanding ?? 0,
      fcf_margin: snapshot.statement_detail?.fcf_margin ?? current.fcf_margin,
      net_debt: snapshot.statement_detail?.identified_net_debt ?? current.net_debt,
      basis: snapshot.statement_detail
        ? "营收、股本、自由现金流率和已识别净负债取自同报告期快照；增长率、折现率与永续增长率为使用者假设"
        : current.basis,
      revenue_growth: observedGrowth,
      bear_overrides: { revenue_growth: Math.min(0, observedGrowth - 0.06), fcf_margin: 0.06 },
      bull_overrides: { revenue_growth: Math.min(0.40, observedGrowth + 0.06), fcf_margin: 0.12 },
    }));
  }, [detail.data]);

  const analysis = useMutation({
    mutationFn: async () => {
      const [report, reverse] = await Promise.all([
        analyzeInvestment(symbol, valuation, horizon),
        reverseValuation(symbol, valuation),
      ]);
      return { report, reverse };
    },
  });
  const statements = useMutation({
    mutationFn: () => refreshStatementDetail(symbol),
    onSuccess: () => detail.refetch(),
  });
  const explanation = useMutation({
    mutationFn: () => explainInvestment(symbol, valuation, horizon, question),
  });
  const saveRun = useMutation({
    mutationFn: () => saveInvestmentResearch(symbol, valuation, horizon, question),
    onSuccess: () => researchHistory.refetch(),
  });
  // 复核：只读接口，触发条件与差异都由后端确定性计算
  const [reviewedId, setReviewedId] = useState<number | null>(null);
  const review = useMutation({
    mutationFn: (runId: number) => fetchInvestmentReview(runId),
    onSuccess: (_data, runId) => setReviewedId(runId),
  });

  const updateNumber = (key: keyof ValuationInput, value: string) => {
    setValuation((current) => ({ ...current, [key]: Number(value) }));
  };
  const submitSymbol = (event: FormEvent) => {
    event.preventDefault();
    const normalized = symbolInput.trim().replace(/\D/g, "").padStart(6, "0").slice(-6);
    setSymbol(normalized);
    analysis.reset();
    explanation.reset();
    saveRun.reset();
  };

  const snapshot = detail.data?.snapshot;
  const report = analysis.data?.report;
  const reverse = analysis.data?.reverse;
  const error = detail.error ?? statements.error ?? analysis.error ?? explanation.error ?? saveRun.error;
  const errorText = error instanceof Error ? error.message : null;
  const fields: Array<[keyof ValuationInput, string, string]> = [
    ["revenue_growth", "基准增长率", "小数，例如 0.06 = 6%"],
    ["fcf_margin", "自由现金流率", "当前通常为估计，需用三表复核"],
    ["discount_rate", "折现率", "风险越高，该值应越高"],
    ["terminal_growth", "永续增长率", "必须低于折现率"],
    ["net_debt", "净负债（元）", "有息负债减现金；净现金填负数"],
    ["years", "显式预测年数", "通常 5–10 年"],
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">投资决策工作台</h1>
        <p className="mt-1 text-sm text-slate-400">从企业质量、证据可靠性、三情景价值和当前价格隐含预期四个角度研究。所有估值假设均可见，结果不构成投资建议。</p>
      </div>

      <form onSubmit={submitSymbol} className={`${panel} flex flex-wrap items-end gap-3`}>
        <label className="min-w-52 flex-1 text-sm text-slate-400">股票代码
          <input className={`${input} mt-1`} value={symbolInput} onChange={(e) => setSymbolInput(e.target.value)} inputMode="numeric" />
        </label>
        <label className="min-w-44 text-sm text-slate-400">投资期限
          <select className={`${input} mt-1`} value={horizon} onChange={(e) => setHorizon(e.target.value)}>
            <option>1 年以内</option><option>1–3 年</option><option>3 年以上</option>
          </select>
        </label>
        <button className="rounded bg-sky-600 px-5 py-2 text-sm font-medium hover:bg-sky-500" type="submit">载入股票</button>
      </form>

      {errorText ? <div className="rounded border border-rose-900 bg-rose-950/30 p-3 text-sm text-rose-300">{errorText}</div> : null}
      {detail.isPending ? <div className={panel}>正在读取基本面快照…</div> : null}

      {detail.data && snapshot ? (
        <>
          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <div className={panel}><div className="text-xs text-slate-500">标的</div><div className="mt-1 text-lg">{snapshot.name} <span className="text-sm text-slate-400">{snapshot.symbol}</span></div><div className="text-xs text-slate-500">{snapshot.industry ?? "行业未知"}</div></div>
            <div className={panel}><div className="text-xs text-slate-500">现价</div><div className="mt-1 text-2xl text-sky-300">¥{num(snapshot.price)}</div><div className="text-xs text-slate-500">PE {num(snapshot.pe_dynamic)} · PB {num(snapshot.pb)}</div></div>
            <div className={panel}><div className="text-xs text-slate-500">企业质量</div><div className="mt-1 text-2xl">{num(detail.data.quality.score, 1)}</div><div className="text-xs text-slate-500">{detail.data.quality.grade} · {detail.data.quality.profile_label}</div></div>
            <div className={panel}><div className="text-xs text-slate-500">证据置信度</div><div className="mt-1 text-2xl">{num(detail.data.evidence_confidence.score, 1)}</div><div className="text-xs text-slate-500">{detail.data.evidence_confidence.label} · 不是上涨概率</div></div>
            <div className={panel}><div className="text-xs text-slate-500">数据时点</div><div className="mt-1 text-lg">{snapshot.report_date ?? "—"}</div><div className="text-xs text-slate-500">快照 {snapshot.snapshot_date} · 覆盖 {pctText(detail.data.quality.coverage)}</div></div>
          </section>

          {snapshot.statement_detail ? <section className={`${panel} border-emerald-900/70`}><div className="flex flex-wrap items-start justify-between gap-2"><div><h2 className="font-medium text-emerald-300">财务三表已接入</h2><p className="mt-1 text-xs text-slate-500">报告期 {snapshot.statement_detail.report_date} · {snapshot.statement_detail.source}</p></div><button type="button" disabled={statements.isPending} onClick={() => statements.mutate()} className="rounded border border-slate-700 px-3 py-1.5 text-xs hover:bg-slate-800">重新刷新</button></div><div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-5"><div><span className="text-xs text-slate-500">经营现金流</span><div>{num((snapshot.statement_detail.operating_cash_flow ?? 0) / 1e8, 1)} 亿</div></div><div><span className="text-xs text-slate-500">资本开支</span><div>{num((snapshot.statement_detail.capital_expenditure ?? 0) / 1e8, 1)} 亿</div></div><div><span className="text-xs text-slate-500">自由现金流率</span><div>{pctText(snapshot.statement_detail.fcf_margin, 2)}</div></div><div><span className="text-xs text-slate-500">已识别净负债</span><div>{num((snapshot.statement_detail.identified_net_debt ?? 0) / 1e8, 1)} 亿</div></div><div><span className="text-xs text-slate-500">商誉/净资产</span><div>{snapshot.statement_detail.goodwill_to_equity === null ? "—" : `${num(snapshot.statement_detail.goodwill_to_equity, 2)}%`}</div></div></div><p className="mt-3 text-[11px] text-slate-600">{snapshot.statement_detail.net_debt_note}</p></section> : <section className={`${panel} border-amber-900/70`}><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="font-medium text-amber-300">财务三表尚未加载</h2><p className="mt-1 text-xs text-slate-500">加载后会自动填入自由现金流率与已识别净负债，并提高证据覆盖率。</p></div><button type="button" disabled={statements.isPending} onClick={() => statements.mutate()} className="rounded bg-amber-600 px-4 py-2 text-sm hover:bg-amber-500 disabled:opacity-40">{statements.isPending ? "正在读取…" : "读取最新三表"}</button></div></section>}

          <section className={panel}>
            <div className="flex flex-wrap items-start justify-between gap-2"><div><h2 className="font-medium">估值假设</h2><p className="mt-1 text-xs text-amber-300">三表数值会自动带入，但增长率、折现率和永续增长率仍是研究假设，必须由你复核。</p></div><div className="text-xs text-slate-500">营收 {num(valuation.revenue / 1e8, 1)} 亿元 · 股本 {num(valuation.shares / 1e8, 2)} 亿股</div></div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {fields.map(([key, label, hint]) => (
                <label key={key} className="text-sm text-slate-400">{label}
                  <input className={`${input} mt-1`} type="number" step="any" value={String(valuation[key])} onChange={(e) => updateNumber(key, e.target.value)} />
                  <span className="mt-1 block text-[11px] text-slate-600">{hint}</span>
                </label>
              ))}
            </div>
            <label className="mt-3 block text-sm text-slate-400">假设依据
              <textarea className={`${input} mt-1 min-h-20`} value={valuation.basis} onChange={(e) => setValuation((v) => ({ ...v, basis: e.target.value }))} />
            </label>
            <button disabled={analysis.isPending || valuation.revenue <= 0 || valuation.shares <= 0} onClick={() => analysis.mutate()} className="mt-4 rounded bg-emerald-600 px-5 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-40" type="button">{analysis.isPending ? "正在计算…" : "生成投资分析"}</button>
          </section>
        </>
      ) : null}

      {report && reverse ? (
        <>
          <section className={`${panel} border-sky-900`}><div className="text-xs text-slate-500">规则结论</div><div className="mt-1 text-lg text-sky-200">{report["1_conclusion"].conclusion}</div><div className="mt-2 text-xs text-slate-500">期限：{report["1_conclusion"].horizon}</div></section>
          <section className="grid gap-3 lg:grid-cols-2">
            <div className={panel}><h2 className="font-medium">三情景价值</h2><div className="mt-3 grid grid-cols-3 gap-2">{report["5_scenarios"].scenarios.map((scenario) => <div key={scenario.label} className="rounded bg-slate-950 p-3 text-center"><div className="text-xs text-slate-500">{scenario.label}</div><div className="mt-1 text-lg">¥{num(scenario.result.per_share)}</div><div className="text-xs text-slate-500">较现价 {pctText(report["3_dimensions"].valuation.upside_vs_price[scenario.label])}</div></div>)}</div><p className="mt-3 text-xs text-slate-500">{report["3_dimensions"].valuation.applicability.caveat}</p></div>
            <div className={panel}><h2 className="font-medium">当前价格隐含预期</h2>{reverse.status === "solved" ? <><div className="mt-3 text-3xl text-violet-300">{pctText(reverse.implied_growth, 2)}</div><p className="mt-2 text-sm text-slate-400">在其余假设固定时，市场价格隐含的年收入增长率。</p></> : <p className="mt-3 text-amber-300">当前价格超出 {pctText(reverse.growth_bounds[0])} 至 {pctText(reverse.growth_bounds[1])} 的可解区间。</p>}<p className="mt-3 text-xs text-slate-500">{reverse.note}</p></div>
          </section>
          <section className="grid gap-3 lg:grid-cols-2"><div className={panel}><h2 className="mb-3 font-medium text-emerald-300">支持证据</h2><EvidenceList items={report["4_evidence"].support} tone="good" /></div><div className={panel}><h2 className="mb-3 font-medium text-rose-300">反对证据</h2><EvidenceList items={report["4_evidence"].oppose} tone="bad" /></div></section>
          <section className="grid gap-3 lg:grid-cols-3"><div className={panel}><h2 className="mb-2 font-medium">尚未验证</h2><ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">{report["6_open_items"].unverified.map((x) => <li key={x}>{x}</li>)}</ul></div><div className={panel}><h2 className="mb-2 font-medium">论点失效条件</h2><ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">{report["6_open_items"].invalidation_conditions.map((x) => <li key={x}>{x}</li>)}</ul></div><div className={panel}><h2 className="mb-2 font-medium">复核触发器</h2><ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">{report["6_open_items"].review_triggers.map((x) => <li key={x}>{x}</li>)}</ul></div></section>
          <section className={panel}><h2 className="font-medium">DeepSeek 解释与反方审查</h2><textarea className={`${input} mt-3 min-h-20`} value={question} onChange={(e) => setQuestion(e.target.value)} /><div className="mt-3 flex flex-wrap gap-2"><button type="button" disabled={explanation.isPending} onClick={() => explanation.mutate()} className="rounded bg-violet-600 px-5 py-2 text-sm hover:bg-violet-500 disabled:opacity-40">{explanation.isPending ? "正在审查…" : "让 DeepSeek 解读证据"}</button><button type="button" disabled={saveRun.isPending} onClick={() => saveRun.mutate()} className="rounded border border-emerald-700 px-5 py-2 text-sm text-emerald-300 hover:bg-emerald-950 disabled:opacity-40">{saveRun.isPending ? "正在重算并冻结…" : "保存研究记录 + 反方审查"}</button></div>{explanation.data ? <div className="mt-4 rounded bg-slate-950 p-4 text-sm leading-7 text-slate-300 whitespace-pre-wrap">{explanation.data.explanation.status === "ok" ? explanation.data.explanation.text : explanation.data.explanation.status === "rejected" ? `解释被证据门禁拦截：出现证据包外数字 ${explanation.data.explanation.validation?.unverified_numbers?.join("、") ?? "（详见接口结果）"}。确定性分析仍然有效。` : `解释层状态：${explanation.data.explanation.status}；缺少 ${explanation.data.explanation.missing?.join("、") ?? "可用连接"}`}</div> : null}{saveRun.data ? <div className="mt-4 rounded border border-emerald-900 bg-emerald-950/30 p-3 text-sm text-emerald-200">已冻结研究记录 #{saveRun.data.id} · 指纹 {saveRun.data.fingerprint.slice(0, 12)}… · DeepSeek 状态 {saveRun.data.explanation_status}</div> : null}<p className="mt-2 text-xs text-slate-500">保存时服务端会重新计算，并冻结数据时点、全部假设、分析、反向估值和解释指纹。模型不参与计算，也不能下单。</p></section>
        </>
      ) : null}

      {researchHistory.data?.items.length ? <section className={panel}><h2 className="font-medium">历史研究记录</h2><p className="mt-1 text-xs text-slate-500">记录一旦冻结不可修改；点「复核」让后端用当前数据重算，看是否需要重新研究、以及和上一条记录差在哪。</p><div className="mt-3 space-y-2">{researchHistory.data.items.map((run) => <div key={run.id} className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-800 bg-slate-950/60 p-3 text-sm"><div><span className="text-sky-300">#{run.id}</span> {run.conclusion ?? run.conclusion_key}<div className="mt-1 text-xs text-slate-500">财报 {run.report_date ?? "—"} · 价格 ¥{num(run.price)} · {new Date(run.created_at).toLocaleString("zh-CN")}</div></div><div className="flex items-center gap-3 text-right text-xs text-slate-500"><div>DeepSeek {run.explanation_status}<div className="font-mono">{run.fingerprint.slice(0, 12)}…</div></div><button type="button" disabled={review.isPending} onClick={() => review.mutate(run.id)} className="rounded border border-sky-800 px-3 py-1 text-xs text-sky-300 hover:bg-sky-950 disabled:opacity-40">{review.isPending ? "复核中…" : "复核"}</button></div></div>)}</div>{review.data && reviewedId !== null ? <div className="mt-3 rounded border border-sky-900 bg-sky-950/20 p-3 text-sm text-slate-300"><div className="font-medium text-sky-300">复核记录 #{review.data.run.id}：{review.data.needs_review === null ? "无法复核（缺少当前快照）" : review.data.needs_review ? `需要重新研究（触发 ${review.data.fired_count ?? review.data.fired_triggers.length} 项）` : "暂不需要重新研究"}</div>{review.data.fired_triggers.length ? <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-amber-200">{review.data.fired_triggers.map((t) => <li key={t.name}>[{t.severity}] {t.label}：{t.detail}</li>)}</ul> : <p className="mt-2 text-xs text-slate-500">所有触发条件均未满足。</p>}<div className="mt-2 text-xs text-slate-400">{review.data.diff.summary}{review.data.has_previous_run ? "" : "（无上一条记录，已与当前重算结果对照）"}</div>{review.data.diff.changes.length ? <ul className="mt-2 space-y-1 text-xs text-slate-400">{review.data.diff.changes.slice(0, 8).map((c) => <li key={c.field}><span className="text-slate-600">[{c.importance}]</span> {c.label}：{String(c.before)} → {String(c.after)}</li>)}</ul> : null}<p className="mt-2 text-xs text-slate-500">{review.data.note}{review.data.disclaimer ? ` ${review.data.disclaimer}` : ""}</p></div> : null}</section> : null}
    </div>
  );
}
