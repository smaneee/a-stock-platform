/** 后端 API 客户端。
 *
 * 全部走相对路径（/api、/ws），由 vite dev server 代理到后端 127.0.0.1:8000，
 * 生产环境可由 nginx 等反向代理转发。
 */
import type {
  BacktestRequest,
  BacktestResponse,
  BacktestSummary,
  HealthDetail,
  MetricsResponse,
  PaperAccount,
  PaperOrder,
  PaperOrderDetail,
  PaperPosition,
  PaperTrade,
  PortfolioBacktestRequest,
  PortfolioBacktestResponse,
  QuoteData,
  Signal,
  Strategy,
  Watchlist,
  AssetPoint,
  SelectionResult,
  HistoryIngestTask,
} from "./types";

const BASE = "/api";

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { params?: Record<string, string | number> },
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
    ...((init?.headers as Record<string, string>) ?? {}),
  };

  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // 非 JSON 响应保持默认
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export { ApiError };

// ---------- 健康检查 ----------

export const fetchHealth = () => request<HealthDetail>("/health");

export const fetchMetrics = () => request<MetricsResponse>("/metrics");

// ---------- 股票筛选 ----------

export const listUniverseSnapshots = () =>
  request<{ snapshots: Array<{ trading_day: string }> }>("/universe/snapshots", {
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

// ---------- 行情 ----------

export const fetchQuote = (symbol: string) =>
  request<QuoteData>(`/quotes/${encodeURIComponent(symbol)}`);

export const fetchBatchQuotes = (symbols: string[]) =>
  request<{ quotes: QuoteData[] }>("/quotes/batch", {
    method: "POST",
    body: JSON.stringify({ symbols }),
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

export const fetchAssetCurve = (accountId: number) =>
  request<{
    account_id: number;
    available_cash: number;
    frozen_cash: number;
    asset_curve: AssetPoint[];
  }>(`/paper/accounts/${accountId}/assets`);
