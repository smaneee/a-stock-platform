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
