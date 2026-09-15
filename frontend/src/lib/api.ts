/** 后端 API 客户端。
 *
 * 全部走相对路径（/api、/ws），由 vite dev server 代理到后端 127.0.0.1:8000，
 * 生产环境可由 nginx 等反向代理转发。
 */
import type {
  BacktestRequest,
  BacktestResponse,
  BacktestSummary,
  BoardKind,
  BoardListResponse,
  BoardMemberResponse,
  BenchmarkIndexListResponse,
  DatacenterCatalogResponse,
  DatacenterQueryResponse,
  DragonTigerSeatsResponse,
  EvidenceResponse,
  FundFlowResponse,
  HealthDetail,
  MetricsResponse,
  PaperAccount,
  PaperOrder,
  PaperOrderDetail,
  PaperPosition,
  PaperRebalancePlan,
  PaperTrade,
  PortfolioBacktestRequest,
  PortfolioBacktestResponse,
  QuoteData,
  Signal,
  Strategy,
  Watchlist,
  AssetPoint,
  SelectionResult,
  SelectionEvaluationSummary,
  ScreenerResponse,
  ValidationEnvelope,
  HistoryIngestTask,
  QfqBackfillStatus,
  DailyPipelineRun,
  DailyPipelineSchedule,
  LiveRebalancePlan,
  LiveTradingStatus,
  StockFundFlowHistoryResponse,
  LimitUpCatalogResponse,
  LimitUpPoolResponse,
  LimitUpSentimentResponse,
  LimitUpCaptureRequest,
  LimitUpCaptureResponse,
  LimitUpSentimentBackfillRequest,
  LimitUpSentimentBackfillResponse,
  IndicatorCatalogResponse,
  IndicatorResponse,
  IntradaySeries,
  MarketSessionResponse,
  MarketProvidersResponse,
  UniverseMembersResponse,
  UniverseSnapshotSummary,
  UniverseStatusResponse,
  UniverseSyncResponse,
  FundamentalDetailResponse,
  InvestmentAnalysisResponse,
  InvestmentExplainResponse,
  ReverseValuationResponse,
  ValuationInput,
  StatementDetailRefreshResponse,
  InvestmentResearchRun,
  InvestmentResearchRunSummary,
} from "./types";
import { authHeaders, getToken, setToken } from "./auth";

const BASE = "/api";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { params?: Record<string, string | number | boolean> },
): Promise<T> {
  let url = `${BASE}${path}`;
  if (init?.params) {
    const search = new URLSearchParams(
      Object.entries(init.params).map(([k, v]) => [k, String(v)]),
    );
    url += `?${search.toString()}`;
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...((init?.headers as Record<string, string>) ?? {}),
  };

  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    let code: string | undefined;
    try {
      const body = (await resp.json()) as { detail?: string; code?: string };
      if (body.detail) detail = body.detail;
      code = body.code;
    } catch {
      // 非 JSON 响应保持默认
    }
    // 令牌缺失/过期：清掉本地令牌，让 AuthGate 回到登录页
    if (resp.status === 401) {
      if (code === "auth_required" || !getToken()) {
        notifyUnauthorized();
      }
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export { ApiError };

/** 令牌失效通知：交由 AuthGate 决定是否回到登录页。 */
function notifyUnauthorized(): void {
  setToken(null);
  window.dispatchEvent(new Event("astock:unauthorized"));
}

// ---------- 健康检查 ----------

export const fetchHealth = () => request<HealthDetail>("/health");

export const fetchMetrics = () => request<MetricsResponse>("/metrics");

// ---------- 投资研究工作台 ----------

export const fetchFundamentalDetail = (symbol: string) =>
  request<FundamentalDetailResponse>(`/fundamentals/${encodeURIComponent(symbol)}`);

export const refreshStatementDetail = (symbol: string) =>
  request<StatementDetailRefreshResponse>(
    `/fundamentals/${encodeURIComponent(symbol)}/statement-detail/refresh`,
    { method: "POST" },
  );

export const analyzeInvestment = (
  symbol: string,
  valuation: ValuationInput,
  horizon: string,
) =>
  request<InvestmentAnalysisResponse>(
    `/fundamentals/${encodeURIComponent(symbol)}/analysis`,
    {
      method: "POST",
      body: JSON.stringify({ valuation, portfolio: { horizon } }),
    },
  );

export const reverseValuation = (symbol: string, valuation: ValuationInput) => {
  const fixed = {
    revenue: valuation.revenue,
    fcf_margin: valuation.fcf_margin,
    discount_rate: valuation.discount_rate,
    terminal_growth: valuation.terminal_growth,
    shares: valuation.shares,
    net_debt: valuation.net_debt,
    years: valuation.years,
    basis: valuation.basis,
    revenue_basis: valuation.revenue_basis,
  };
  return request<ReverseValuationResponse>(
    `/fundamentals/${encodeURIComponent(symbol)}/reverse-valuation`,
    { method: "POST", body: JSON.stringify(fixed) },
  );
};

export const explainInvestment = (
  symbol: string,
  valuation: ValuationInput,
  horizon: string,
  question: string,
) =>
  request<InvestmentExplainResponse>(
    `/fundamentals/${encodeURIComponent(symbol)}/explain`,
    {
      method: "POST",
      body: JSON.stringify({ valuation, portfolio: { horizon }, question }),
    },
  );

export const saveInvestmentResearch = (
  symbol: string,
  valuation: ValuationInput,
  horizon: string,
  question: string,
) => request<InvestmentResearchRun>("/research/runs", {
  method: "POST",
  params: { symbol },
  body: JSON.stringify({ valuation, horizon, question, include_explanation: true }),
});

export const listInvestmentResearch = (symbol: string, limit = 10) =>
  request<{ count: number; items: InvestmentResearchRunSummary[] }>("/research/runs", {
    params: { symbol, limit },
  });

// ---------- 股票筛选 ----------

export const listUniverseSnapshots = () =>
  request<{ snapshots: UniverseSnapshotSummary[] }>("/universe/snapshots", {
    params: { limit: 20 },
  });

export const rankStocks = (body: {
  trading_day: string;
  top_n: number;
  exclude_st: boolean;
}) =>
  request<SelectionResult>("/selections/rank", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const evaluateSelection = (runId: number, horizonDays = 20) =>
  request<SelectionResult>(`/selections/${runId}/evaluate`, {
    method: "POST",
    params: { horizon_days: horizonDays, min_coverage_ratio: 0.8 },
  });

export const fetchSelectionEvaluationSummary = () =>
  request<SelectionEvaluationSummary>("/selections/evaluations/summary");

export const listSelectionRuns = (limit = 50) =>
  request<{ items: SelectionResult[] }>("/selections", { params: { limit } });

export const createHistoryIngest = (tradingDay: string) =>
  request<HistoryIngestTask>("/history-ingest", {
    method: "POST",
    body: JSON.stringify({ trading_day: tradingDay, lookback_days: 365 }),
  });

export const listHistoryIngest = () =>
  request<{ items: HistoryIngestTask[] }>("/history-ingest", {
    params: { limit: 5 },
  });

export const cancelHistoryIngest = (taskId: number) =>
  request<HistoryIngestTask>(`/history-ingest/${taskId}/cancel`, {
    method: "POST",
  });

export const fetchQfqBackfillStatus = () =>
  request<QfqBackfillStatus>("/history-adjust");

export const startQfqBackfill = (symbols?: string[]) =>
  request<QfqBackfillStatus>("/history-adjust", {
    method: "POST",
    params: symbols && symbols.length ? { symbols: symbols.join(",") } : undefined,
  });

export const createDailyPipelineRun = (body: {
  trading_day: string;
  paper_account_id?: number | null;
  auto_execute_paper?: boolean;
  paper_validation_override?: boolean;
  top_n?: number;
  history_lookback_days?: number;
}) =>
  request<DailyPipelineRun>("/daily-pipeline/runs", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const listDailyPipelineRuns = () =>
  request<{ items: DailyPipelineRun[] }>("/daily-pipeline/runs", {
    params: { limit: 20 },
  });

export const fetchDailyPipelineSchedule = () =>
  request<DailyPipelineSchedule>("/daily-pipeline/schedule");

// ---------- 行情 ----------

export const fetchQuote = (symbol: string) =>
  request<QuoteData>(`/quotes/${encodeURIComponent(symbol)}`);

export const fetchBatchQuotes = (symbols: string[]) =>
  request<{ quotes: QuoteData[] }>("/quotes/batch", {
    method: "POST",
    body: JSON.stringify({ symbols }),
  });

/** 当日分时曲线：实时分钟线 + 数据源 1 分钟分时补齐。 */
export const fetchIntraday = (symbol: string, limit = 600) =>
  request<IntradaySeries>(`/quotes/${encodeURIComponent(symbol)}/intraday`, {
    params: { limit },
  });

// ---------- 自选股 ----------

export const listWatchlists = () => request<Watchlist[]>("/watchlists");

export const createWatchlist = (name: string) =>
  request<Watchlist>("/watchlists", {
    method: "POST",
    body: JSON.stringify({ name }),
  });

export const addWatchlistSymbol = (
  watchlistId: number,
  symbol: string,
  name?: string,
) =>
  request<{ symbol: string; name: string | null }>(
    `/watchlists/${watchlistId}/symbols`,
    {
      method: "POST",
      body: JSON.stringify({ symbol, name }),
    },
  );

export const removeWatchlistSymbol = (watchlistId: number, symbol: string) =>
  request<{ deleted: string }>(
    `/watchlists/${watchlistId}/symbols/${encodeURIComponent(symbol)}`,
    { method: "DELETE" },
  );

// ---------- 策略 ----------

export const listStrategies = () => request<Strategy[]>("/strategies");

export const enableStrategy = (id: number) =>
  request<{ id: number; name: string; enabled: true }>(
    `/strategies/${id}/enable`,
    { method: "POST" },
  );

export const disableStrategy = (id: number) =>
  request<{ id: number; name: string; enabled: false }>(
    `/strategies/${id}/disable`,
    { method: "POST" },
  );

// ---------- 信号 ----------

export const listSignals = (limit = 100) =>
  request<{ signals: Signal[] }>("/signals", { params: { limit } });

// ---------- 回测 ----------

export const createBacktest = (body: BacktestRequest) =>
  request<{ id: number; status: string; duplicate?: boolean }>("/backtests", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const fetchBacktest = (id: number) =>
  request<BacktestResponse>(`/backtests/${id}`);

export const listBacktests = (limit = 20) =>
  request<{ items: BacktestSummary[]; count: number }>("/backtests", {
    params: { limit },
  });

export const cancelBacktest = (id: number) =>
  request<{ id: number; status: string }>(`/backtests/${id}/cancel`, {
    method: "POST",
  });

// ---------- 组合回测 ----------

export const createPortfolioBacktest = (body: PortfolioBacktestRequest) =>
  request<PortfolioBacktestResponse>("/portfolio-backtests", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const fetchPortfolioBacktest = (id: number) =>
  request<PortfolioBacktestResponse>(`/portfolio-backtests/${id}`);

export const listPortfolioBacktests = (limit = 20) =>
  request<{ tasks: PortfolioBacktestResponse[] }>("/portfolio-backtests", {
    params: { limit },
  });

export const cancelPortfolioBacktest = (id: number) =>
  request<PortfolioBacktestResponse>(`/portfolio-backtests/${id}/cancel`, {
    method: "POST",
  });

// ---------- 模拟交易 ----------

export const listPaperAccounts = () => request<PaperAccount[]>("/paper/accounts");

export const createPaperAccount = (name: string, initialCash: number) =>
  request<PaperAccount>("/paper/accounts", {
    method: "POST",
    body: JSON.stringify({ name, initial_cash: initialCash }),
  });

export const listPaperPositions = (accountId: number) =>
  request<{ positions: PaperPosition[] }>("/paper/positions", {
    params: { account_id: accountId },
  });

export const listPaperTrades = (accountId: number, limit = 50) =>
  request<{ trades: PaperTrade[] }>("/paper/trades", {
    params: { account_id: accountId, limit },
  });

export const listPaperOrders = (accountId: number, limit = 50) =>
  request<{ orders: PaperOrderDetail[] }>("/paper/orders", {
    params: { account_id: accountId, limit },
  });

export const cancelPaperOrder = (orderId: number) =>
  request<{ id: number; status: string }>(`/paper/orders/${orderId}/cancel`, {
    method: "POST",
  });

export const settlePaperAccount = (accountId: number) =>
  request<{
    account_id: number;
    total_asset: number;
    positions_settled: number;
  }>(`/paper/accounts/${accountId}/settle`, { method: "POST" });

export const placePaperOrder = (order: {
  account_id: number;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price?: number;
  signal_id?: string;
}) =>
  request<PaperOrder>("/paper/orders", {
    method: "POST",
    body: JSON.stringify(order),
  });

export const listPaperRebalancePlans = (accountId: number) =>
  request<{ items: PaperRebalancePlan[] }>("/paper/rebalance-plans", {
    params: { account_id: accountId },
  });

export const createPaperRebalancePlan = (body: {
  account_id: number;
  selection_run_id: number;
  validation_override: boolean;
}) =>
  request<PaperRebalancePlan>("/paper/rebalance-plans", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const executePaperRebalancePlan = (planId: number) =>
  request<PaperRebalancePlan>(`/paper/rebalance-plans/${planId}/execute`, {
    method: "POST",
  });

export const cancelPaperRebalancePlan = (planId: number) =>
  request<PaperRebalancePlan>(`/paper/rebalance-plans/${planId}/cancel`, {
    method: "POST",
  });

export const fetchLiveTradingStatus = () =>
  request<LiveTradingStatus>("/live/status");

export const listLiveRebalancePlans = (liveKey: string) =>
  request<{ items: LiveRebalancePlan[] }>("/live/rebalance-plans", {
    headers: { "X-Live-Trading-Key": liveKey },
  });

export const createLiveRebalancePlan = (selectionRunId: number, liveKey: string) =>
  request<LiveRebalancePlan>("/live/rebalance-plans", {
    method: "POST",
    headers: { "X-Live-Trading-Key": liveKey },
    body: JSON.stringify({ selection_run_id: selectionRunId }),
  });

export const approveLiveRebalancePlan = (planId: number, liveKey: string) =>
  request<{ plan: LiveRebalancePlan; approval_token: string }>(
    `/live/rebalance-plans/${planId}/approve`,
    {
      method: "POST",
      headers: { "X-Live-Trading-Key": liveKey },
      body: JSON.stringify({
        acknowledgement: "I_UNDERSTAND_REAL_MONEY_WILL_BE_USED",
      }),
    },
  );

export const executeLiveRebalancePlan = (planId: number, token: string, liveKey: string) =>
  request<LiveRebalancePlan>(`/live/rebalance-plans/${planId}/execute`, {
    method: "POST",
    headers: {
      "X-Trade-Approval-Token": token,
      "X-Live-Trading-Key": liveKey,
    },
  });

export const reconcileLiveRebalancePlan = (planId: number, liveKey: string) =>
  request<LiveRebalancePlan>(`/live/rebalance-plans/${planId}/reconcile`, {
    method: "POST",
    headers: { "X-Live-Trading-Key": liveKey },
  });

export const fetchAssetCurve = (accountId: number) =>
  request<{
    account_id: number;
    available_cash: number;
    frozen_cash: number;
    asset_curve: AssetPoint[];
  }>(`/paper/accounts/${accountId}/assets`);

// ---------- 东方财富：板块 / 资金流 ----------

export const listBoards = (kind: BoardKind, limit = 50, order: "desc" | "asc" = "desc") =>
  request<BoardListResponse>("/market/boards", {
    params: { kind, limit, order },
  });

export const listBoardConstituents = (boardCode: string, limit = 50) =>
  request<BoardMemberResponse>(`/market/boards/${boardCode}/constituents`, {
    params: { limit },
  });

export const listBenchmarkIndices = () =>
  request<BenchmarkIndexListResponse>("/market/indices");

export const listBoardFundFlow = (
  kind: BoardKind,
  limit = 50,
  order: "desc" | "asc" = "desc",
) =>
  request<FundFlowResponse>("/market/fund-flow/boards", {
    params: { kind, limit, order },
  });

export const listStockFundFlowRank = (limit = 50, order: "desc" | "asc" = "desc") =>
  request<FundFlowResponse>("/market/fund-flow/stocks", {
    params: { limit, order },
  });

export const fetchStockFundFlowHistory = (symbol: string, days = 60) =>
  request<StockFundFlowHistoryResponse>(
    `/market/fund-flow/stocks/${symbol}`,
    { params: { days } },
  );

export const fetchDatacenterCatalog = () =>
  request<DatacenterCatalogResponse>("/market/datacenter");

export interface DatacenterQueryParams {
  date?: string;
  date_from?: string;
  date_to?: string;
  trade_date?: string;
  symbol?: string;
  limit?: number;
  page?: number;
  order?: "desc" | "asc";
}

/** 去掉空值，避免把 undefined 拼进查询串。 */
const compactParams = (
  params: DatacenterQueryParams,
): Record<string, string | number> => {
  const query: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      query[key] = value as string | number;
    }
  }
  return query;
};

export const queryDatacenter = (
  dataset: string,
  params: DatacenterQueryParams = {},
) =>
  request<DatacenterQueryResponse>(`/market/datacenter/${dataset}`, {
    params: compactParams(params),
  });

export const fetchDragonTigerSeats = (
  symbol: string,
  tradeDate?: string,
  limit = 20,
) =>
  request<DragonTigerSeatsResponse>(
    `/market/datacenter/dragon-tiger/${symbol}/seats`,
    { params: compactParams({ trade_date: tradeDate, limit }) },
  );

// ---------- 东方财富：涨停板情绪池 ----------

export const fetchLimitUpCatalog = () =>
  request<LimitUpCatalogResponse>("/market/limit-up");

export const fetchLimitUpPool = (
  pool: string,
  limit = 50,
  page = 1,
  order?: "desc" | "asc",
) =>
  request<LimitUpPoolResponse>(`/market/limit-up/${pool}`, {
    params: order ? { limit, page, order } : { limit, page },
  });

/** 涨停板情绪曲线（封板率 / 连板高度），只返回已落库的交易日。 */
export const fetchLimitUpSentiment = (limit = 120) =>
  request<LimitUpSentimentResponse>("/market/limit-up/sentiment", {
    params: { limit },
  });

/** 手动抓取情绪池落库；backfill_days > 0 时回补最近 N 个自然日。 */
export const captureLimitUpSentiment = (body: LimitUpCaptureRequest) =>
  request<LimitUpCaptureResponse>("/market/limit-up/capture", {
    method: "POST",
    body: JSON.stringify(body),
  });

/**
 * 用本地不复权日线 + 涨跌停规则离线回算历史情绪。
 *
 * 上游东财只保留最近若干个交易日，历史曲线靠每日累积太慢；这个接口可以
 * 一次性把整段历史补齐（`source=derived`），且不会覆盖东财实抓的行。
 */
export const backfillLimitUpSentiment = (
  body: LimitUpSentimentBackfillRequest,
) =>
  request<LimitUpSentimentBackfillResponse>(
    "/market/limit-up/sentiment/backfill",
    { method: "POST", body: JSON.stringify(body) },
  );

// ---------- 技术指标 ----------

/** 指标目录：可用序列（key + 中文名）、支持的周期与 limit 边界。 */
export const fetchIndicatorCatalog = () =>
  request<IndicatorCatalogResponse>("/indicators");

/** 单标的技术指标序列（MA/EMA/MACD/RSI/BOLL/KDJ/ATR/OBV/CCI/WR）。 */
export const fetchIndicators = (
  symbol: string,
  period = "daily",
  limit = 250,
) =>
  request<IndicatorResponse>(`/indicators/${encodeURIComponent(symbol)}`, {
    params: { period, limit },
  });

// ---------- 策略证据（P1-01） ----------

/** 策略证据全量：状态、数据截止日、样本、结果、限制、证据来源。 */
export const fetchEvidence = () => request<EvidenceResponse>("/evidence");

// ---------- 股票池（universe） ----------

/** 股票池同步状态：数据源健康 + 最近快照日期。 */
export const fetchUniverseStatus = () =>
  request<UniverseStatusResponse>("/universe/status");

/** 今天是不是交易日 + 最近/下一交易日（用于休市提示，不参与股票池筛选）。 */
export const fetchMarketSession = () =>
  request<MarketSessionResponse>("/market/session");

/** 触发一次股票池同步（成功后会落库当日快照）。 */
export const syncUniverse = () =>
  request<UniverseSyncResponse>("/universe/sync", { method: "POST" });

/** 某快照的成员。
 *
 * status=included 仅可交易（默认）/ excluded 仅已剔除 / all 全部。
 * 后端 include_only=False 是「只要被剔除的」，想同时看到两边必须用 all。
 */
export const listUniverseMembers = (
  tradingDay: string,
  options: { exchange?: string; status?: "included" | "excluded" | "all" } = {},
) =>
  request<UniverseMembersResponse>(
    `/universe/snapshots/${encodeURIComponent(tradingDay)}/members`,
    {
      params: {
        status: options.status ?? "included",
        limit: 10000,
        ...(options.exchange ? { exchange: options.exchange } : {}),
      },
    },
  );

/** 已注册的数据源（按优先级）及其实时可用性。 */
export const fetchMarketProviders = () =>
  request<MarketProvidersResponse>("/market/providers");

// ---------- 实时研究候选排序 ----------

export interface RealtimePicksParams {
  top_n?: number;
  refine_pool?: number;
  lookback_days?: number;
  exclude_st?: boolean;
  min_amount_20?: number;
  min_triggers?: number;
  risk_budget_pct?: number;
  max_weight_pct?: number;
}

/**
 * 全市场实时扫描：返回研究候选排序和后端证据门禁。
 *
 * ``evidence.recommendation_allowed`` 是不可由前端放宽的安全契约；当前规则未满足
 * 独立样本外发布门槛时，任何分数、价格区间和风险预算都不得表述为买入建议。
 */
export const fetchRealtimePicks = (params: RealtimePicksParams = {}) => {
  const query: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) {
      query[key] = value as string | number | boolean;
    }
  }
  return request<ScreenerResponse>("/realtime/picks", { params: query });
};

export interface PicksValidationParams {
  eval_days?: number;
  horizons?: string;
  primary_horizon?: number;
  top_n?: number;
  min_amount_20?: number;
  min_triggers?: number;
  commission_rate?: number;
  stamp_tax_rate?: number;
  slippage_bps?: number;
  control?: "none" | "random" | "worst";
}

/** 读取样本外验证的状态与最近一次报告（报告在内存里，后端重启后需重算）。 */
export const fetchPicksValidation = () =>
  request<ValidationEnvelope>("/realtime/picks/validation");

/**
 * 启动一次样本外验证：把雷达规则放到历史上逐日重放。
 *
 * 计算在后端线程里跑（60 个评估日约 100 秒），接口立即返回 202 与任务状态，
 * 调用方需要轮询 ``fetchPicksValidation``。``control="random"`` 是脚手架自检，
 * 其超额收益应接近 0。
 */
export const startPicksValidation = (params: PicksValidationParams = {}) => {
  const query: Record<string, string | number | boolean> = {};
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) {
      query[key] = value as string | number | boolean;
    }
  }
  return request<ValidationEnvelope>("/realtime/picks/validation", {
    method: "POST",
    params: query,
  });
};
