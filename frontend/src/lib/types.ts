/** API 数据类型，对齐后端 Pydantic 响应模型。 */

export interface QuoteData {
  symbol: string;
  name: string;
  price: number;
  open: number;
  high: number;
  low: number;
  previous_close: number;
  volume: number;
  amount: number;
  bid_price: number;
  ask_price: number;
  source: string;
  market_time: string | null;
  received_at: string;
  is_stale: boolean;
}

/* ---------- 投资研究工作台 ---------- */

export interface ValuationInput {
  revenue: number;
  revenue_growth: number;
  fcf_margin: number;
  discount_rate: number;
  terminal_growth: number;
  shares: number;
  net_debt: number;
  years: number;
  basis: string;
  revenue_basis: "report_period" | "annualized";
  bear_overrides: Record<string, number>;
  bull_overrides: Record<string, number>;
}

export interface FundamentalDetailResponse {
  snapshot: {
    symbol: string;
    name: string;
    price: number | null;
    industry: string | null;
    revenue: number | null;
    revenue_yoy: number | null;
    net_profit_yoy: number | null;
    pe_dynamic: number | null;
    pb: number | null;
    roe: number | null;
    report_date: string | null;
    snapshot_date: string;
    source: string;
    derived: {
      shares_outstanding: number | null;
      annualized_revenue: number | null;
      annualization_factor: number | null;
    };
    statement_detail: {
      report_date: string | null;
      operating_cash_flow: number | null;
      capital_expenditure: number | null;
      free_cash_flow: number | null;
      fcf_margin: number | null;
      ocf_to_profit: number | null;
      monetary_funds: number | null;
      identified_debt: number | null;
      identified_net_debt: number | null;
      goodwill: number | null;
      goodwill_to_equity: number | null;
      source: string;
      net_debt_note: string;
      fetched_at: string | null;
    } | null;
  };
  quality: {
    score: number | null;
    grade: string;
    profile_label: string;
    profile_reason: string;
    coverage: number;
    missing: string[];
    insufficient_evidence: boolean;
  };
  evidence_confidence: {
    score: number;
    label: string;
    expired: boolean;
    reasons: string[];
    note: string;
  };
  notes: string[];
}

export interface StatementDetailRefreshResponse {
  symbol: string;
  name: string;
  statement_detail: NonNullable<FundamentalDetailResponse["snapshot"]["statement_detail"]>;
}

export interface InvestmentEvidenceItem {
  evidence: string;
  origin: string;
  source: string;
}

export interface InvestmentAnalysisResponse {
  symbol: string;
  name: string;
  "1_conclusion": {
    conclusion: string;
    conclusion_key: string;
    horizon: string;
    attractiveness_quality_score: number | null;
    evidence_confidence_score: number;
    separation_note: string;
  };
  "2_data_asof": {
    report_date: string | null;
    fetched_at: string | null;
    price: number | null;
    source: string;
    expired: boolean;
    completeness: { metric_coverage: number; missing_metrics: string[]; missing_inputs: string[] };
  };
  "3_dimensions": {
    valuation: {
      available: boolean;
      basis: string;
      upside_vs_price: Record<string, number | null>;
      applicability: { applicable: boolean; model: string; caveat: string };
    };
    portfolio_risk: { position_ceiling: { ceiling_pct: number | null; reason: string } };
  };
  "4_evidence": { support: InvestmentEvidenceItem[]; oppose: InvestmentEvidenceItem[] };
  "5_scenarios": {
    scenarios: Array<{
      label: string;
      overrides: Record<string, number>;
      result: { per_share: number; terminal_value_share: number; formula: string };
    }>;
  };
  "6_open_items": {
    unverified: string[];
    invalidation_conditions: string[];
    review_triggers: string[];
  };
  disclaimer: string;
}

export interface ReverseValuationResponse {
  status: "solved" | "below_range" | "above_range";
  implied_growth: number | null;
  target_price: number;
  matched_price?: number;
  growth_bounds: [number, number];
  value_at_bounds: [number, number];
  note: string;
  applicability: { applicable: boolean; model: string; caveat: string };
}

export interface InvestmentExplainResponse {
  analysis: InvestmentAnalysisResponse;
  explanation: {
    status: string;
    text?: string;
    missing?: string[];
    validation?: { passed: boolean; unverified_numbers?: string[] };
    message?: string;
  };
  boundary: string;
}

export interface Watchlist {
  id: number;
  name: string;
  symbols: Array<{ symbol: string; name: string | null }>;
}

export interface Strategy {
  id: number;
  name: string;
  description: string | null;
  enabled: boolean;
  version: string;
}

export interface Signal {
  signal_id: string;
  symbol: string;
  strategy_name: string;
  direction: "BUY" | "SELL" | "ALERT";
  strength: number;
  reason: string;
  price: number;
  source_time: string;
  created_at: string;
  strategy_version: string;
}

export interface BacktestRequest {
  symbol: string;
  strategy_name: string;
  start_time: string;
  end_time: string;
  initial_cash: number;
  idempotency_key?: string;
}

export interface BacktestTrade {
  time: string;
  symbol: string;
  side: "BUY" | "SELL";
  price: number;
  quantity: number;
  commission: number;
  stamp_tax?: number;
  pnl: number;
}

export interface BacktestResult {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  win_rate: number;
  profit_loss_ratio: number;
  trade_count: number;
  equity_curve: number[];
  trades: BacktestTrade[];
  /* ---- D6/D8 收益口径（引用结果前必看） ---- */
  return_convention?: string;
  return_convention_note?: string;
  dividends_modeled?: boolean;
  bars_adjust?: string;
  bars_adjust_label?: string;
  limit_reference?: string;
  costs_included?: boolean;
  /* ---- D6 昨收对齐诊断 ---- */
  limit_reference_missing?: number;
  limit_reference_complete?: boolean;
  limit_reference_note?: string;
}

export type BacktestStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface BacktestResponse {
  id: number;
  symbol: string;
  strategy_name: string;
  start_time: string;
  end_time: string;
  status: BacktestStatus;
  progress: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  result: BacktestResult | null;
}

export interface BacktestSummary {
  id: number;
  symbol: string;
  strategy_name: string;
  start_time: string;
  end_time: string;
  status: BacktestStatus;
  progress: number;
  error_message: string | null;
}

/* ---------- 组合回测 ---------- */

export interface PortfolioBacktestRequest {
  symbols: string[];
  strategy_name: string;
  weights?: Record<string, number> | null;
  benchmark_symbol?: string | null;
  start_time: string;
  end_time: string;
  initial_cash: number;
  max_single_position?: number;
  max_total_position?: number;
  commission_rate?: number;
  slippage?: number;
  risk_free_rate?: number;
  /** D5 单日成交量参与率上限；0=不限制（默认，保持历史行为） */
  max_participation_rate?: number;
  /** 超出参与率上限时是否按可成交量部分成交（false 则整笔拒绝） */
  allow_partial_fill?: boolean;
  idempotency_key?: string | null;
  sentiment_gate?: SentimentGateConfig | null;
}

/** 涨停板市场情绪闸门参数。 */
export interface SentimentGateConfig {
  enabled: boolean;
  /** 使用信号日之前第 N 个交易日的情绪（>=1，避免未来函数） */
  lag_days: number;
  min_seal_rate: number | null;
  max_broken_rate: number | null;
  min_max_streak: number | null;
  min_limit_up_count: number | null;
  on_missing: "allow" | "block";
  scale_exposure: boolean;
  min_exposure: number;
}

/** 单个信号日的闸门判定。 */
export interface SentimentGateDay {
  date: string;
  sentiment_date: string | null;
  allowed: boolean;
  exposure: number;
  seal_rate: number | null;
  broken_rate: number | null;
  max_streak: number | null;
  limit_up_count: number | null;
  reasons: string[];
}

/** 闸门在本轮回测里的作用汇总。 */
export interface PortfolioSentimentSummary {
  config: SentimentGateConfig;
  series_days: number;
  total_days: number;
  allowed_days: number;
  blocked_days: number;
  missing_days: string[];
  buy_signals: number;
  blocked_buy_signals: number;
  average_buy_exposure: number;
  days: SentimentGateDay[];
}

export interface PortfolioBacktestResult {
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  win_rate: number;
  profit_loss_ratio: number;
  trade_count: number;
  turnover: number;
  concentration: number;
  benchmark_return: number;
  excess_return: number;
  alpha: number;
  beta: number;
  information_ratio: number;
  tracking_error: number;
  equity_curve: number[];
  benchmark_curve: number[];
  dates: string[];
  trades: BacktestTrade[];
  sentiment?: PortfolioSentimentSummary | null;
  /* ---- D9 尾部风险 ---- */
  volatility?: number;
  var_95?: number;
  cvar_95?: number;
  max_drawdown_duration?: number;
  worst_day_return?: number;
  /* ---- D7 基准分列 ---- */
  equal_weight_curve?: number[];
  cash_curve?: number[];
  equal_weight_return?: number;
  cash_return?: number;
  excess_vs_equal_weight?: number;
  excess_vs_cash?: number;
  benchmark_labels?: Record<string, string>;
  /* ---- D5 容量 ---- */
  min_capacity_multiple?: number | null;
  partial_fill_count?: number;
  /** 容量→规模换算：不超容量的账户规模上界（未启用参与率上限时为 null） */
  capacity?: {
    max_aum: number;
    basis: string;
    considered_trades: number;
    assumptions: string[];
    binding_trade: {
      date: string;
      symbol: string | null;
      side: string | null;
      quantity: number | null;
      capacity_multiple: number;
      account_equity_on_that_day: number;
    };
  } | null;
  /* ---- D8/D6 收益口径 ---- */
  return_convention?: string;
  return_convention_note?: string;
  dividends_modeled?: boolean;
  bars_adjust?: string;
  bars_adjust_label?: string;
  limit_reference?: string;
  costs_included?: boolean;
  /* ---- D6 昨收还原诊断 ---- */
  limit_reference_missing?: number;
  limit_reference_complete?: boolean;
  limit_reference_note?: string;
  /* ---- 审计：请求 / 执行 / 排除 ---- */
  requested_symbols?: string[];
  executed_symbols?: string[];
  excluded_symbols?: string[];
  exclusion_reasons?: Record<string, unknown>;
}

export type PortfolioBacktestStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface PortfolioBacktestResponse {
  id: number;
  symbols: string[];
  weights: Record<string, number> | null;
  benchmark_symbol: string | null;
  strategy_name: string;
  start_time: string;
  end_time: string;
  initial_cash: number;
  status: PortfolioBacktestStatus;
  progress: number;
  result: PortfolioBacktestResult | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  sentiment_gate?: SentimentGateConfig | null;
  /** D5 成交配置回显（参与率上限 / 是否允许部分成交） */
  execution?: { max_participation_rate: number; allow_partial_fill: boolean };
}

export interface PaperAccount {
  id: number;
  name: string;
  initial_cash: number;
  available_cash: number;
  frozen_cash: number;
}

export interface PaperPosition {
  symbol: string;
  quantity: number;
  available_quantity: number;
  avg_cost: number;
  realized_pnl: number;
}

export interface PaperOrder {
  order_id: number;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  status: string;
}

export interface PaperOrderDetail {
  id: number;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  status: string;
  reject_reason: string | null;
  signal_id: string | null;
  created_at: string;
}

export interface PaperRebalancePlan {
  id: number;
  account_id: number;
  selection_run_id: number;
  status: "DRAFT" | "EXECUTED" | "PARTIAL" | "CANCELLED" | "EXPIRED";
  validation_override: boolean;
  proposal: {
    selection_trading_day: string;
    total_asset: number;
    validation: {
      passed: boolean;
      override: boolean;
      evaluated_runs: number;
      mean_forward_return: number;
      average_rank_ic: number | null;
    };
    orders: Array<{
      side: "BUY" | "SELL";
      symbol: string;
      quantity: number;
      indicative_price: number;
      indicative_value: number;
    }>;
  };
  execution: { filled: number; total: number } | null;
  created_at: string;
  executed_at: string | null;
}

export interface LiveTradingStatus {
  enabled: boolean;
  ready: boolean;
  provider: "qmt";
  path_configured: boolean;
  path_exists: boolean;
  account_configured: boolean;
  sdk_available: boolean;
  api_token_configured: boolean;
  order_api_enabled: boolean;
  message: string;
}

export interface LiveRebalancePlan {
  id: number;
  selection_run_id: number;
  status: "DRAFT" | "APPROVED" | "EXECUTING" | "SUBMITTED" | "PARTIAL" | "FILLED" | "EXPIRED";
  account_snapshot: {
    cash: number;
    total_asset: number;
    positions: Record<string, { quantity: number; available_quantity: number }>;
  };
  proposal: {
    selection_trading_day: string;
    target_investment_ratio: number;
    max_symbol_weight: number;
    validation: {
      evaluated_runs: number;
      mean_forward_return: number;
      average_rank_ic: number;
    };
    orders: Array<{
      side: "BUY" | "SELL";
      symbol: string;
      quantity: number;
      indicative_price: number;
      indicative_value: number;
    }>;
  };
  execution: {
    submitted: number;
    total: number;
    filled?: number;
    reconciled_at?: string;
    orders?: Array<{
      side: "BUY" | "SELL";
      symbol: string;
      quantity: number;
      order_id: number | null;
      status: string;
      broker_status?: string;
      broker_traded_volume?: number;
      broker_traded_price?: number;
      broker_status_message?: string;
    }>;
  } | null;
  created_at: string;
  approval_expires_at: string | null;
  executed_at: string | null;
}

export interface PaperTrade {
  id: number;
  symbol: string;
  side: "BUY" | "SELL";
  quantity: number;
  price: number;
  commission: number;
  stamp_tax: number;
  realized_pnl: number;
  signal_id: string | null;
  executed_at: string;
}

export interface AssetPoint {
  total_asset: number;
  recorded_at: string;
}

export interface HealthDetail {
  status: string;
  disclaimer: string;
  database: { ok: boolean; error?: string };
  scheduler: { running: boolean };
  providers: Record<string, boolean>;
}

export interface MetricsResponse {
  data_status: "real-time" | "delayed" | "disconnected" | "simulated";
  providers: Record<string, {
    success: number;
    failure: number;
    success_rate: number;
    avg_latency_ms: number;
    consecutive_failures: number;
    last_success_at: string | null;
  }>;
  websocket: {
    connections: number;
    peak_connections: number;
    dropped_messages: number;
  };
  tasks: {
    live: Record<string, number>;
    cumulative: Record<string, number>;
  };
}

export interface SelectionCandidate {
  symbol: string;
  name: string;
  exchange: string;
  board: string;
  rank: number;
  score: number;
  momentum_20: number;
  momentum_60: number;
  volatility_20: number;
  max_drawdown_60: number;
  average_amount_20: number;
  last_price: number;
  bar_count: number;
  entry_date: string | null;
  exit_date: string | null;
  forward_return: number | null;
}

export interface SelectionResult {
  run_id: number;
  trading_day: string;
  total_candidates: number;
  eligible_count: number;
  evaluation_horizon: number | null;
  evaluation_coverage: number | null;
  mean_forward_return: number | null;
  median_forward_return: number | null;
  forward_win_rate: number | null;
  evaluated_at: string | null;
  candidates: SelectionCandidate[];
  disclaimer: string;
}

export interface SelectionEvaluationSummary {
  evaluated_runs: number;
  candidate_observations: number;
  mean_forward_return: number;
  median_forward_return: number;
  forward_win_rate: number;
  average_coverage: number;
  average_rank_ic: number | null;
  average_turnover: number | null;
}

export interface HistoryIngestTask {
  id: number;
  snapshot_id: number | null;
  status: "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled";
  progress: number;
  start_date: string;
  end_date: string;
  adjust: string;
  requested_symbols: number;
  completed_symbols: number;
  coverage_ratio: number;
  total_bars: number;
  failed_count: number;
  failed_symbols: Record<string, string>;
  last_error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/** 前复权回填的一次汇总结果。 */
export interface QfqBackfillReport {
  started_at: string;
  finished_at: string;
  elapsed_seconds: number;
  period: string;
  source_adjust: string;
  target_adjust: string;
  symbols_total: number;
  symbols_ok: number;
  symbols_empty: number;
  symbols_failed: number;
  rows_written: number;
  events_applied: number;
  unresolved_count: number;
  unresolved_symbols: Record<string, number>;
  failure_count: number;
  failures: Record<string, string>;
}

/** 前复权回填任务状态（前端轮询用）。 */
export interface QfqBackfillStatus {
  /** idle / running / done / failed */
  state: string;
  progress: { done: number; total: number };
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  has_report: boolean;
  report: QfqBackfillReport | null;
}

export interface DailyPipelineRun {
  id: number;
  trading_day: string;
  status: "queued" | "running" | "waiting_history" | "succeeded" | "failed" | "cancelled";
  stage: string;
  paper_account_id: number | null;
  history_task_id: number | null;
  selection_run_id: number | null;
  paper_plan_id: number | null;
  config: Record<string, unknown>;
  auto_execute_paper: boolean;
  progress: number;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

/** 每日流水线自动调度配置（来自后端 .env，只读）。 */
export interface DailyPipelineSchedule {
  enabled: boolean;
  hour: number;
  minute: number;
  paper_account_id: number | null;
  auto_execute_paper: boolean;
  lookback_days: number;
  running: boolean;
  next_run_at: string | null;
}

/** 统一 API 错误格式。 */
export interface ApiError {
  detail: string;
}

// ---------- 东方财富市场数据 ----------

export type BoardKind = "industry" | "concept" | "region";

/** 板块行情（东方财富行业/概念/地域板块）。 */
export interface BoardQuote {
  code: string;
  name: string;
  kind: string;
  index_value: number;
  change_pct: number;
  change_amount: number;
  volume: number;
  amount: number;
  amplitude: number;
  turnover_rate: number;
  main_net_inflow: number;
  main_net_inflow_pct: number;
  up_count: number;
  down_count: number;
  flat_count: number;
  leader_symbol: string | null;
  leader_name: string | null;
  leader_change_pct: number | null;
}

/** 板块成分股。 */
export interface BoardMember {
  symbol: string;
  name: string;
  price: number;
  change_pct: number;
  volume: number;
  amount: number;
  turnover_rate: number;
  main_net_inflow: number;
  main_net_inflow_pct: number;
}

/** 资金流排行行（板块或个股）。 */
export interface FundFlowRow {
  code: string;
  name: string;
  kind: string;
  price: number;
  change_pct: number;
  main_net_inflow: number;
  main_net_inflow_pct: number;
  super_large_net_inflow: number;
  super_large_net_inflow_pct: number;
  large_net_inflow: number;
  large_net_inflow_pct: number;
  medium_net_inflow: number;
  medium_net_inflow_pct: number;
  small_net_inflow: number;
  small_net_inflow_pct: number;
}

/** 个股资金流历史中的一个交易日。 */
export interface FundFlowPoint {
  trade_date: string;
  main_net_inflow: number;
  small_net_inflow: number;
  medium_net_inflow: number;
  large_net_inflow: number;
  super_large_net_inflow: number;
  main_net_inflow_pct: number;
  close_price: number;
  change_pct: number;
}

export interface BoardListResponse {
  kind: string;
  count: number;
  items: BoardQuote[];
}

export interface BenchmarkIndex {
  symbol: string; // 规范代码，如 sh000300
  code: string; // 6 位代码
  name: string; // 中文简称
}

export interface BenchmarkIndexListResponse {
  count: number;
  default: string;
  items: BenchmarkIndex[];
}

export interface BoardMemberResponse {
  board_code: string;
  count: number;
  items: BoardMember[];
}

export interface FundFlowResponse {
  kind: string;
  count: number;
  items: FundFlowRow[];
}

export interface StockFundFlowHistoryResponse {
  symbol: string;
  count: number;
  items: FundFlowPoint[];
}

/** 东方财富数据中心：字段说明（kind 决定前端格式化方式）。 */
export interface DatacenterFieldInfo {
  key: string;
  title: string;
  kind: string;
  /** 字段口径说明：例如「上游当前恒为空」——用于解释「—」不是抓取失败 */
  note?: string;
}

/** 东方财富数据中心：一个数据集的自描述信息。 */
export interface DatacenterDatasetInfo {
  /** 数据集 key；`dragon-tiger-seats` 是席位字段清单（挂在 dragon-tiger 行上） */
  key: string;
  label: string;
  description: string;
  supports_date: boolean;
  supports_symbol: boolean;
  fields: DatacenterFieldInfo[];
}

export interface DatacenterCatalogResponse {
  count: number;
  datasets: DatacenterDatasetInfo[];
}

/** 数据中心一行（列名由数据集声明决定）。 */
export type DatacenterRow = Record<string, string | number | null>;

/** 策略证据条目（P1-01）。状态一律以后端 `status_vocabulary` 为准。 */
export interface EvidenceItem {
  id: string;
  name: string;
  status: "unverified" | "in_progress" | "failed_oos" | "inconclusive" | "passed_oos";
  production_ready: boolean;
  data_cutoff: string | null;
  bars_adjust?: string | null;
  sample: Record<string, unknown>;
  result: Record<string, unknown>;
  reproducible_on_this_machine: boolean;
  evidence_source: string;
  limitations: string[];
  failure_reason?: string | null;
  ui_rule: string;
}

export interface EvidenceSummary {
  total: number;
  by_status: Record<string, number>;
  production_ready_count: number;
  negative_or_uncertain: string[];
  artifact_updated_at?: string | null;
  schema_version?: string | null;
}

export interface EvidenceProductionGate {
  live_trading_enabled: boolean;
  reason: string;
  requirements_for_small_live_pilot: string[];
}

export interface EvidenceResponse {
  schema_version: string;
  artifact_updated_at: string | null;
  disclaimer: string;
  status_vocabulary: Record<string, string>;
  production_gate: EvidenceProductionGate;
  summary: EvidenceSummary;
  items: EvidenceItem[];
  live: Record<string, unknown>;
  source_file: string;
}

export interface DatacenterQueryResponse {
  dataset: string;
  label: string;
  total: number;
  page: number;
  count: number;
  rows: DatacenterRow[];
}

export interface DragonTigerSeatsResponse {
  symbol: string;
  trade_date: string | null;
  buy: DatacenterRow[];
  sell: DatacenterRow[];
}

/** 涨停板情绪池：字段说明（kind 决定前端格式化方式）。 */
export interface LimitUpFieldInfo {
  key: string;
  title: string;
  kind: string;
}

/** 情绪池自描述信息（涨停 / 跌停 / 炸板 / 强势 / 次新）。 */
export interface LimitUpPoolInfo {
  key: string;
  label: string;
  description: string;
  fields: LimitUpFieldInfo[];
}

export interface LimitUpCatalogResponse {
  count: number;
  pools: LimitUpPoolInfo[];
}

/** 情绪池一行；「无涨跌幅限制」的个股涨停价会是 null。 */
export type LimitUpRow = Record<string, string | number | null>;

export interface LimitUpPoolResponse {
  pool: string;
  label: string;
  trade_date: string;
  total: number;
  page: number;
  count: number;
  items: LimitUpRow[];
}

/** 涨停板情绪因子：一个交易日的市场情绪读数。 */
export interface LimitUpSentimentRow {
  trade_date: string;
  limit_up_count: number;
  limit_down_count: number;
  broken_board_count: number;
  strong_count: number;
  sub_new_count: number;
  /** 封板率 = 涨停 / (涨停 + 炸板)，取值 0~1；无样本时为 null */
  seal_rate: number | null;
  broken_rate: number | null;
  max_streak: number;
  first_board_count: number;
  streak_2_count: number;
  streak_3_count: number;
  streak_4_count: number;
  streak_5plus_count: number;
  total_seal_amount: number;
  total_limit_up_amount: number;
  source: string;
  captured_at: string;
}

export interface LimitUpSentimentResponse {
  count: number;
  start: string | null;
  end: string | null;
  latest: LimitUpSentimentRow | null;
  items: LimitUpSentimentRow[];
}

export interface LimitUpCaptureRequest {
  trade_date?: string;
  backfill_days?: number;
}

export interface LimitUpCaptureResponse {
  count: number;
  items: LimitUpSentimentRow[];
}

/** 用本地日线离线回算历史情绪（POST /api/market/limit-up/sentiment/backfill）。 */
export interface LimitUpSentimentBackfillRequest {
  start?: string | null;
  end?: string | null;
  coverage_floor?: number;
  include_st?: boolean;
  overwrite_derived?: boolean;
  dry_run?: boolean;
}

export interface LimitUpSentimentBackfillItem {
  trade_date: string;
  limit_up_count: number;
  limit_down_count: number;
  broken_board_count: number;
  coverage_symbols: number;
  seal_rate: number | null;
  broken_rate: number | null;
  max_streak: number;
  first_board_count: number;
  streak_2_count: number;
  streak_3_count: number;
  streak_4_count: number;
  streak_5plus_count: number;
}

export interface LimitUpSentimentBackfillResponse {
  start: string;
  end: string;
  symbols: number;
  bars_scanned: number;
  inserted: number;
  updated: number;
  skipped_existing: number;
  skipped_low_coverage: number;
  deleted_stale: number;
  coverage_floor: number;
  include_st: boolean;
  overwrite_derived: boolean;
  dry_run: boolean;
  days: number;
  low_coverage_days: string[];
  stale_days: string[];
  items: LimitUpSentimentBackfillItem[];
}

/** 技术指标目录：可用序列与周期（GET /api/indicators）。 */
export interface IndicatorSeriesInfo {
  key: string;
  title: string;
}

export interface IndicatorCatalogResponse {
  periods: string[];
  default_period: string;
  default_limit: number;
  min_limit: number;
  max_limit: number;
  count: number;
  series: IndicatorSeriesInfo[];
}

/** 单标的技术指标序列；NaN 已由后端转成 null，与 dates 等长。 */
export interface IndicatorResponse {
  symbol: string;
  period: string;
  /** 本次实际数据来源：cache / tdx / akshare_sina … */
  source: string;
  count: number;
  dates: string[];
  close: number[];
  titles: Record<string, string>;
  series: Record<string, Array<number | null>>;
  /** 每条序列最后一个有效值 */
  latest: Record<string, number | null>;
  /** 本次参与计算的日线复权口径：qfq（前复权，本地缓存优先）/ none */
  bars_adjust: string;
  /** 复权口径中文标签 */
  bars_adjust_label: string;
}

// ---------- 股票池（universe） ----------

/** GET /api/universe/snapshots 的单条快照摘要。 */
export interface UniverseSnapshotSummary {
  id: number;
  trading_day: string;
  total_count: number;
  included_count: number;
  excluded_count: number;
  source_provider: string;
  source_synced_at: string | null;
  created_at: string | null;
}

/** GET /api/universe/status 里的数据源健康项。 */
export interface UniverseProviderHealth {
  source_id: string;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_status: string;
  last_error: string | null;
  consecutive_failures: number;
}

export interface UniverseStatusResponse {
  provider_health: UniverseProviderHealth[];
  latest_snapshot_date: string | null;
}

/** GET /api/market/session：今天是不是交易日（与股票池「可交易」无关）。 */
export interface MarketSessionResponse {
  day: string;
  is_trading_day: boolean;
  last_trading_day: string;
  next_trading_day: string | null;
  calendar_total: number;
}

/** POST /api/universe/sync 的结果。 */
export interface UniverseSyncResponse {
  source_provider: string;
  synced_at: string;
  total_fetched: number;
  new_securities: number;
  updated_securities: number;
  attempted_providers: string[];
  provider_health: Record<string, unknown>;
  snapshot_id: number | null;
  snapshot_trading_day: string | null;
}

/** 快照成员（股票池里的单只标的）。 */
export interface UniverseMember {
  symbol: string;
  name: string;
  exchange: string;
  board: string;
  is_st: boolean;
  is_included: boolean;
  exclude_reason: string | null;
  audit_reason: string | null;
  sort_rank: number;
  listing_date: string | null;
  trading_status: string;
}

export interface UniverseMembersResponse {
  trading_day: string;
  include_only: boolean;
  exchange: string | null;
  exclude_reasons: string[] | null;
  count: number;
  members: UniverseMember[];
}

/** GET /api/market/providers：已注册的数据源与当前可用性。 */
export interface MarketProvidersResponse {
  providers: string[];
  status: Record<string, boolean>;
}

// ---------- 当日分时曲线 ----------

/** 分时曲线上的一个点（同一分钟一个）。 */
export interface IntradayPoint {
  /** ISO 本地时间，例如 2026-09-11T09:31:00 */
  time: string;
  price: number;
  /** 该分钟成交量（股） */
  volume: number;
  /** 该分钟成交额（元） */
  amount: number;
}

export interface IntradayStats {
  open: number;
  high: number;
  low: number;
  last: number;
  previous_close: number;
  change: number;
  change_pct: number;
  /** 当日累计成交量（股） */
  volume: number;
  /** 当日累计成交额（元） */
  amount: number;
}

/** GET /api/quotes/{symbol}/intraday */
export interface IntradaySeries {
  symbol: string;
  name: string;
  /** 该曲线对应的交易日（非交易日返回最近一个有数据的交易日） */
  trade_date: string;
  /** live=实时缓存 / baseline=数据源分时 / merged=两者合并 / empty=无数据 */
  source: "live" | "baseline" | "merged" | "empty";
  /** 该标的当前是否有实时分钟线在刷新 */
  is_live: boolean;
  stats: IntradayStats;
  points: IntradayPoint[];
}

// ---------- 实时研究候选排序 ----------

/** 单条研究候选（GET /api/realtime/picks）。 */
export interface ScreenerPick {
  rank: number;
  symbol: string;
  name: string;
  exchange: string;
  board: string;
  /** 板块中文名（main/gem/star/bse） */
  board_label: string;
  price: number;
  previous_close: number;
  change_pct: number;
  /** 触发器权重和（0~100），命中即给满权重，历史口径不变 */
  score: number;
  /** 连续强度分（0~100）= Σ 命中权重 × 连续强度；**排名主键**，用它区分同样命中数 */
  strength_score: number;
  triggers: string[];
  /** 触发条件的中文说明 */
  reasons: string[];
  risk_flags: string[];
  risk_labels: string[];
  /** 模型观察价格区间（不是挂单建议） */
  entry_low: number;
  entry_high: number;
  stop_loss: number;
  target_price: number;
  risk_reward: number;
  /** 模型风险预算上限（占总资金 %，不是仓位建议） */
  suggested_weight_pct: number;
  atr14: number;
  atr_pct: number;
  ma20: number;
  ma60: number;
  momentum_20: number;
  momentum_60: number;
  volatility_20: number;
  amount_20: number;
  volume_ratio: number;
  rsi14: number;
  kdj_k: number;
  kdj_d: number;
  macd_hist: number;
  boll_upper: number;
  boll_lower: number;
  bar_count: number;
  last_bar_date: string;
  /** 该候选的行情是否带来了当日 K 线 */
  live: boolean;
}

/** 扫描时点的市场情绪读数（涨停板情绪因子）。 */
export interface ScreenerSentiment {
  available: boolean;
  sentiment_date: string | null;
  seal_rate: number | null;
  broken_rate: number | null;
  max_streak: number | null;
  limit_up_count: number | null;
  exposure: number;
  /** strong / healthy / neutral / weak / unknown */
  stance: string;
  label: string;
  note: string;
}

/** 本次扫描实际生效的参数。 */
export interface ScreenerConfigView {
  top_n: number;
  lookback_days: number;
  refine_pool: number;
  exclude_st: boolean;
  min_amount_20: number;
  min_price: number;
  max_price: number;
  min_change_pct: number;
  max_change_pct: number;
  min_triggers: number;
  stop_atr_multiple: number;
  target_atr_multiple: number;
  risk_budget_pct: number;
  max_weight_pct: number;
  sentiment_lag_days: number;
  scale_exposure_by_sentiment: boolean;
}

export type ResearchEvidenceStatus =
  | "not_validated"
  | "validation_running"
  | "validation_failed"
  | "not_passed"
  | "insufficient_evidence";

/** 当前候选排序的证据门禁；前端不得自行放宽。 */
export interface ResearchEvidence {
  status: ResearchEvidenceStatus;
  label: string;
  recommendation_allowed: boolean;
  summary: string;
  validation_task_state: string;
  report_generated_at: string | null;
  primary_horizon: number | null;
  mean_excess_pct: number | null;
  evidence_source: string;
}

/** GET /api/realtime/picks */
export interface ScreenerResponse {
  /** 行情所属交易日（最近交易日） */
  session_day: string;
  /** 买点评估所针对的交易日 */
  signal_day: string;
  bars_last_day: string | null;
  /** 本地日线复权口径：qfq（前复权）/ none（不复权） */
  bars_adjust: string;
  live: boolean;
  /** 扫描完成时间（UTC，历史字段，UI 请改用 generated_at_cst） */
  generated_at: string;
  /** 扫描完成时间（北京时间 ISO8601，带 +08:00） */
  generated_at_cst?: string;
  /** 时间基准说明 */
  timezone?: string;
  /** 行情覆盖率 = quoted / universe_size */
  coverage_ratio?: number;
  /** 覆盖率是否足以支撑候选排序结论；false 时不得作为研究依据 */
  coverage_ok?: boolean;
  scan_seconds: number;
  universe_size: number;
  quoted: number;
  screened: number;
  refined: number;
  picks: ScreenerPick[];
  sentiment: ScreenerSentiment;
  evidence: ResearchEvidence;
  notes: string[];
  config: ScreenerConfigView;
}

// ---------- 研究候选排序样本外验证（walk-forward） ----------

/** 单个持有期的样本外统计。 */
export interface ValidationHorizonStat {
  /** 持有交易日数 */
  horizon: number;
  observations: number;
  /** 扣成本后平均收益（%） */
  mean_net_pct: number;
  median_net_pct: number;
  /** 扣成本后收益为正的比例 */
  hit_rate: number;
  mean_benchmark_pct: number;
  /** 平均超额收益（%，候选 − 同池等权基准） */
  mean_excess_pct: number;
  excess_daily_mean_pct: number;
  /** 按日度超额序列算的单样本 t（窗口重叠会偏高） */
  excess_t_stat: number;
  excess_days: number;
  max_excess_drawdown_pct: number;
}

/** 按命中分数分层的超额收益（主口径持有期）。 */
export interface ValidationBucketStat {
  label: string;
  observations: number;
  mean_excess_pct: number;
  hit_rate: number;
}

/** 单个评估日的记录。 */
export interface ValidationDailyRecord {
  signal_day: string;
  eligible: number;
  picks: number;
  tradable: number;
  symbols: string[];
  net_pct: number | null;
  benchmark_pct: number | null;
  excess_pct: number | null;
}

export interface ValidationCosts {
  commission_rate: number;
  stamp_tax_rate: number;
  slippage_bps: number;
  round_trip_pct: number;
}

/** GET/POST /api/realtime/picks/validation 的 report 字段。 */
export interface ValidationReport {
  generated_at: string;
  first_signal_day: string | null;
  last_signal_day: string | null;
  evaluation_days: number;
  universe_size: number;
  /** 本次使用的日线复权口径：qfq（前复权）/ none（不复权） */
  bars_adjust: string;
  bars_first_day: string | null;
  bars_last_day: string | null;
  elapsed_seconds: number;
  /** none = 按打分选股；random / worst = 对照组 */
  control: string;
  /** 主口径持有期 */
  primary_horizon: number;
  horizons: ValidationHorizonStat[];
  score_buckets: ValidationBucketStat[];
  daily: ValidationDailyRecord[];
  skipped_not_tradable: number;
  skipped_no_outcome: number;
  costs: ValidationCosts;
  caveats: string[];
  notes: string[];
}

export interface ValidationStatus {
  /** idle / running / done / failed */
  state: string;
  progress: { done: number; total: number };
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  has_report: boolean;
  report_generated_at: string | null;
}

export interface ValidationEnvelope {
  status: ValidationStatus;
  report: ValidationReport | null;
}
