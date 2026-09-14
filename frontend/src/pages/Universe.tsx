/** 股票池（universe）：本地全市场可交易 A 股快照。
 *
 * 数据来自 GET /api/universe/*，由东方财富股票主数据同步而来（baostock 兜底）。
 * 这一页回答三个问题：本地池子里到底有多少只、什么时候同步的、哪些被剔除了。
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  fetchMarketSession,
  fetchUniverseStatus,
  listUniverseMembers,
  listUniverseSnapshots,
  syncUniverse,
} from "../lib/api";
import type {
  MarketSessionResponse,
  UniverseProviderHealth,
  UniverseSnapshotSummary,
} from "../lib/types";

const PAGE_SIZE = 50;

const EXCHANGES = [
  { value: "", label: "全部市场" },
  { value: "SH", label: "上交所" },
  { value: "SZ", label: "深交所" },
  { value: "BJ", label: "北交所" },
];

const STATUS_OPTIONS = [
  { value: "included", label: "仅可交易" },
  { value: "excluded", label: "仅已剔除" },
  { value: "all", label: "全部" },
] as const;

type UniverseStatusFilter = (typeof STATUS_OPTIONS)[number]["value"];

const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
  unknown: "未知",
};

const EXCLUDE_LABELS: Record<string, string> = {
  delisted: "已退市",
  suspended: "停牌",
  st: "ST",
  new_listing: "次新",
  illiquid: "流动性不足",
  incomplete_history: "历史不足",
  not_listed_yet: "待上市",
};

const panel = "bg-slate-900 rounded-lg border border-slate-800 overflow-hidden";
const th = "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap";
const input =
  "bg-slate-950 border border-slate-800 rounded px-3 py-2 text-sm text-slate-200";

function formatTime(raw: string | null): string {
  if (!raw) return "—";
  return raw.replace("T", " ").slice(0, 19);
}

/** 今日是否交易的提示条：把「可交易」与「今天能下单」彻底分开。 */
function MarketSessionBadge({
  session,
}: {
  session: MarketSessionResponse | undefined;
}) {
  if (!session) return null;
  if (session.is_trading_day) {
    return (
      <div className="mt-2 inline-flex items-center gap-2 rounded border border-emerald-900 bg-emerald-950/30 px-3 py-1 text-xs text-emerald-300">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        今日（{session.day}）是交易日
      </div>
    );
  }
  return (
    <div className="mt-2 inline-flex flex-wrap items-center gap-x-2 gap-y-1 rounded border border-amber-900 bg-amber-950/30 px-3 py-1 text-xs text-amber-300">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
      今日休市（{session.day}），最近交易日 {session.last_trading_day}
      {session.next_trading_day ? `，下一交易日 ${session.next_trading_day}` : ""}
      <span className="text-amber-400/70">
        · 休市不影响股票池的「可交易」标记
      </span>
    </div>
  );
}

export default function UniversePage() {
  const queryClient = useQueryClient();
  const [tradingDay, setTradingDay] = useState("");
  const [exchange, setExchange] = useState("");
  const [status, setStatus] = useState<UniverseStatusFilter>("included");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(0);

  const snapshots = useQuery({
    queryKey: ["universe-snapshots"],
    queryFn: listUniverseSnapshots,
  });
  const statusQuery = useQuery({
    queryKey: ["universe-status"],
    queryFn: fetchUniverseStatus,
    refetchInterval: 30_000,
  });
  const session = useQuery({
    queryKey: ["market-session"],
    queryFn: fetchMarketSession,
    refetchInterval: 60_000,
  });

  const latestDay = snapshots.data?.snapshots[0]?.trading_day ?? "";
  const activeDay = tradingDay || latestDay;
  const activeSnapshot = snapshots.data?.snapshots.find(
    (item) => item.trading_day === activeDay,
  );

  const members = useQuery({
    queryKey: ["universe-members", activeDay, exchange, status],
    queryFn: () => listUniverseMembers(activeDay, { exchange, status }),
    enabled: Boolean(activeDay),
  });

  const sync = useMutation({
    mutationFn: syncUniverse,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["universe-snapshots"] });
      queryClient.invalidateQueries({ queryKey: ["universe-status"] });
      queryClient.invalidateQueries({ queryKey: ["universe-members"] });
    },
  });

  useEffect(() => {
    setPage(0);
  }, [keyword, exchange, status, activeDay]);

  const rows = useMemo(() => members.data?.members ?? [], [members.data]);
  const filtered = useMemo(() => {
    const raw = keyword.trim();
    if (!raw) return rows;
    const upper = raw.toUpperCase();
    return rows.filter(
      (item) =>
        item.symbol.toUpperCase().includes(upper) || item.name.includes(raw),
    );
  }, [rows, keyword]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = filtered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  return (
    <div className="max-w-7xl space-y-6">
      <div className="flex flex-wrap items-start gap-4">
        <div className="flex-1 min-w-[280px]">
          <h1 className="text-2xl font-semibold">股票池</h1>
          <p className="text-sm text-slate-400 mt-1">
            东方财富股票主数据同步到本地的全市场快照（沪深京三市 A 股）。
            选股、回测、模拟交易都只在这个池子里取材。
          </p>
          <MarketSessionBadge session={session.data} />
        </div>
        <button
          type="button"
          disabled={sync.isPending}
          onClick={() => sync.mutate()}
          className="rounded bg-sky-600 hover:bg-sky-500 disabled:bg-slate-700 px-4 py-2 text-sm font-medium"
        >
          {sync.isPending ? "同步中…" : "同步股票池"}
        </button>
      </div>

      {sync.isError && (
        <div className="rounded border border-rose-900 bg-rose-950/30 p-4 text-sm text-rose-300">
          同步失败：
          {sync.error instanceof Error ? sync.error.message : "未知错误"}
        </div>
      )}
      {sync.isSuccess && sync.data && (
        <div className="rounded border border-emerald-900 bg-emerald-950/30 p-4 text-sm text-emerald-300">
          同步完成：来源 {sync.data.source_provider} · 拉取 {sync.data.total_fetched} 只 ·
          新增 {sync.data.new_securities} · 更新 {sync.data.updated_securities} ·
          快照 {sync.data.snapshot_trading_day ?? "—"}
        </div>
      )}

      <Stats
        snapshot={activeSnapshot}
        shown={filtered.length}
        total={rows.length}
        loading={snapshots.isLoading}
      />

      <ProviderHealth
        items={statusQuery.data?.provider_health ?? []}
        loading={statusQuery.isLoading}
      />

      <div className={panel}>
        <div className="flex flex-wrap items-end gap-3 p-4 border-b border-slate-800">
          <label className="text-sm text-slate-300">
            <span className="block text-xs text-slate-500 mb-1">快照交易日</span>
            <select
              value={activeDay}
              onChange={(event) => {
                setTradingDay(event.target.value);
              }}
              className={input}
            >
              {(snapshots.data?.snapshots ?? []).map((item) => (
                <option key={item.id} value={item.trading_day}>
                  {item.trading_day}（{item.included_count}/{item.total_count}）
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm text-slate-300">
            <span className="block text-xs text-slate-500 mb-1">市场</span>
            <select
              value={exchange}
              onChange={(event) => setExchange(event.target.value)}
              className={input}
            >
              {EXCHANGES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm text-slate-300">
            <span className="block text-xs text-slate-500 mb-1">搜索</span>
            <input
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="代码或名称"
              className={`${input} w-40`}
            />
          </label>
          <label className="text-sm text-slate-300">
            <span className="block text-xs text-slate-500 mb-1">状态</span>
            <select
              value={status}
              onChange={(event) =>
                setStatus(event.target.value as UniverseStatusFilter)
              }
              className={input}
            >
              {STATUS_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <div className="text-xs text-slate-500 ml-auto pb-2">
            显示 {filtered.length} / {rows.length} 只
          </div>
        </div>

        {members.isError && (
          <div className="p-4 text-sm text-rose-300">
            读取失败：
            {members.error instanceof Error ? members.error.message : "未知错误"}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800">
            <thead className="bg-slate-950/60">
              <tr>
                <th className={th}>代码</th>
                <th className={th}>名称</th>
                <th className={th}>市场 / 板块</th>
                <th className={th}>状态</th>
                <th className={th}>上市日期</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {members.isLoading && (
                <tr>
                  <td className={`${td} text-slate-500`} colSpan={5}>
                    加载中…
                  </td>
                </tr>
              )}
              {!members.isLoading && pageRows.length === 0 && (
                <tr>
                  <td className={`${td} text-slate-500`} colSpan={5}>
                    没有匹配的标的
                  </td>
                </tr>
              )}
              {pageRows.map((item) => (
                <tr key={item.symbol} className="hover:bg-slate-800/40">
                  <td className={`${td} numeric`}>
                    <Link
                      to={`/intraday?symbol=${item.symbol}`}
                      className="text-slate-400 hover:text-sky-400"
                      title="查看分时"
                    >
                      {item.symbol}
                    </Link>
                  </td>
                  <td className={`${td} text-slate-200`}>
                    {item.name}
                    {item.is_st && (
                      <span className="ml-2 text-xs text-amber-400">ST</span>
                    )}
                  </td>
                  <td className={`${td} text-slate-400`}>
                    {item.exchange} / {BOARD_LABELS[item.board] ?? item.board}
                  </td>
                  <td className={`${td}`}>
                    {item.is_included ? (
                      <span className="text-emerald-400">可交易</span>
                    ) : (
                      <span className="text-rose-400">
                        已剔除
                        {item.exclude_reason
                          ? `（${EXCLUDE_LABELS[item.exclude_reason] ?? item.exclude_reason}）`
                          : ""}
                      </span>
                    )}
                  </td>
                  <td className={`${td} numeric text-slate-500`}>
                    {item.listing_date ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex items-center gap-3 px-4 py-3 border-t border-slate-800 text-sm text-slate-400">
          <button
            type="button"
            onClick={() => setPage((prev) => Math.max(0, prev - 1))}
            disabled={safePage === 0}
            className="rounded border border-slate-700 px-3 py-1 text-xs hover:border-sky-500 disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-xs">
            第 {safePage + 1} / {pageCount} 页
          </span>
          <button
            type="button"
            onClick={() => setPage((prev) => Math.min(pageCount - 1, prev + 1))}
            disabled={safePage >= pageCount - 1}
            className="rounded border border-slate-700 px-3 py-1 text-xs hover:border-sky-500 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}

function Stats({
  snapshot,
  shown,
  total,
  loading,
}: {
  snapshot: UniverseSnapshotSummary | undefined;
  shown: number;
  total: number;
  loading: boolean;
}) {
  const items: Array<{ label: string; value: string }> = [
    { label: "快照交易日", value: snapshot?.trading_day ?? "—" },
    { label: "全量标的", value: snapshot ? String(snapshot.total_count) : "—" },
    { label: "可交易", value: snapshot ? String(snapshot.included_count) : "—" },
    { label: "已剔除", value: snapshot ? String(snapshot.excluded_count) : "—" },
    { label: "同步来源", value: snapshot?.source_provider ?? "—" },
    { label: "同步时间", value: formatTime(snapshot?.source_synced_at ?? null) },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      {items.map((item) => (
        <div
          key={item.label}
          className="rounded border border-slate-800 bg-slate-900 p-3"
        >
          <div className="text-xs text-slate-500">{item.label}</div>
          <div className="mt-1 text-lg font-semibold">{loading ? "…" : item.value}</div>
        </div>
      ))}
      <div className="rounded border border-slate-800 bg-slate-900 p-3 col-span-2 md:col-span-3 lg:col-span-6">
        <div className="text-xs text-slate-500">当前筛选</div>
        <div className="mt-1 text-sm text-slate-300">
          {shown} 只匹配（快照内 {total} 只）
        </div>
        <p className="mt-1 text-xs text-slate-500">
          「可交易」= 通过了池子的排除规则（退市 / 停牌 / 长期停牌 / ST）被纳入本地池子，
          不是「今天能下单」，休市日也不会变成不可交易；快照交易日只会落在交易日，
          周末或节假日同步会自动归到最近一个交易日。
        </p>
      </div>
    </div>
  );
}

function ProviderHealth({
  items,
  loading,
}: {
  items: UniverseProviderHealth[];
  loading: boolean;
}) {
  return (
    <div className={panel}>
      <div className="px-4 py-2 border-b border-slate-800 text-sm text-slate-300">
        数据源健康
        <span className="ml-2 text-xs text-slate-500">
          股票主数据同步来源的最近状态
        </span>
      </div>
      {loading ? (
        <div className="p-4 text-sm text-slate-500">加载中…</div>
      ) : items.length === 0 ? (
        <div className="p-4 text-sm text-slate-500">暂无同步记录</div>
      ) : (
        <div className="divide-y divide-slate-800">
          {items.map((item) => (
            <div
              key={item.source_id}
              className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm"
            >
              <span
                className={
                  item.last_status === "ok" ? "text-emerald-400" : "text-rose-400"
                }
              >
                ●
              </span>
              <span className="text-slate-200 w-28">{item.source_id}</span>
              <span className="text-xs text-slate-500">
                最近成功 {formatTime(item.last_success_at)}
              </span>
              <span className="text-xs text-slate-500">
                连续失败 {item.consecutive_failures}
              </span>
              {item.last_error && (
                <span className="text-xs text-amber-400">{item.last_error}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
