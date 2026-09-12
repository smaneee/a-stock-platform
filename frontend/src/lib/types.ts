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
  idempotency_key?: string | null;
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
}

/** 东方财富数据中心：一个数据集的自描述信息。 */
export interface DatacenterDatasetInfo {
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
