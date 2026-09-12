/** 东方财富涨停板情绪池面板：涨停 / 跌停 / 炸板 / 强势 / 次新。
 *
 * 与数据中心面板一样，表头完全由后端返回的字段声明（fields）驱动，
 * 后端新增情绪池时前端无需改动。上游只提供「最近交易日」快照，
 * 因此面板只展示交易日，不提供日期筛选。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchLimitUpCatalog, fetchLimitUpPool } from "../lib/api";
import { formatCell } from "../lib/format";
import type { LimitUpPoolInfo } from "../lib/types";

const PAGE_SIZE = 50;

const panel = "bg-slate-900 rounded-lg border border-slate-800 overflow-hidden";
const th = "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap";

export default function LimitUpPanel() {
  const [pool, setPool] = useState("limit-up");
  const [page, setPage] = useState(1);
  const [order, setOrder] = useState<"desc" | "asc">("desc");

  const catalog = useQuery({
    queryKey: ["market", "limit-up-catalog"],
    queryFn: fetchLimitUpCatalog,
    staleTime: 10 * 60 * 1000,
  });

  const info: LimitUpPoolInfo | undefined = catalog.data?.pools.find(
    (item) => item.key === pool,
  );
  const fields = info?.fields ?? [];

  // 排序方向交给后端在该池的推荐字段上应用（如涨停池按首次封板时间）
  const result = useQuery({
    queryKey: ["market", "limit-up", pool, page, order],
    queryFn: () => fetchLimitUpPool(pool, PAGE_SIZE, page, order),
    enabled: info !== undefined,
    staleTime: 60 * 1000,
  });

  const rows = result.data?.items ?? [];
  const total = result.data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const error = catalog.error ?? result.error;

  const selectPool = (next: string) => {
    setPool(next);
    setPage(1);
  };

  return (
    <div className="space-y-4">
      <div className={`${panel} p-4 space-y-3`}>
        <div className="flex flex-wrap gap-2">
          {(catalog.data?.pools ?? []).map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => selectPool(item.key)}
              className={`px-3 py-1.5 rounded text-sm transition-colors ${
                pool === item.key
                  ? "bg-sky-500/20 text-sky-300 border border-sky-500/40"
                  : "bg-slate-900 text-slate-300 border border-slate-800 hover:text-slate-100"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        {info && <div className="text-xs text-slate-500">{info.description}</div>}
        <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400">
          <span>交易日 {result.data?.trade_date || "—"}</span>
          <span>
            命中 <span className="numeric text-slate-200">{total}</span> 只
          </span>
          <button
            type="button"
            onClick={() => {
              setOrder(order === "desc" ? "asc" : "desc");
              setPage(1);
            }}
            className="px-3 py-1.5 rounded text-sm bg-slate-900 text-slate-300 border border-slate-800"
          >
            {order === "desc" ? "降序 ↓" : "升序 ↑"}
          </button>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={() => setPage((current) => Math.max(1, current - 1))}
              disabled={page <= 1}
              className="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 disabled:opacity-40"
            >
              上一页
            </button>
            <span>
              {page} / {lastPage}
            </span>
            <button
              type="button"
              onClick={() => setPage((current) => Math.min(lastPage, current + 1))}
              disabled={page >= lastPage}
              className="px-2 py-0.5 rounded bg-slate-900 border border-slate-800 disabled:opacity-40"
            >
              下一页
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
          数据源暂不可用：{error instanceof Error ? error.message : String(error)}
        </div>
      )}

      <div className={panel}>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800">
            <thead className="bg-slate-950/60">
              <tr>
                {fields.map((field) => (
                  <th key={field.key} className={th}>
                    {field.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {rows.map((row, index) => (
                <tr key={`${String(row.symbol ?? index)}-${index}`} className="hover:bg-slate-800/40">
                  {fields.map((field) => (
                    <td key={field.key} className={td}>
                      {formatCell(field, row[field.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {result.isLoading && <div className="p-6 text-sm text-slate-500">加载中…</div>}
        {result.data && rows.length === 0 && (
          <div className="p-6 text-sm text-slate-500">
            最近交易日该池暂无个股（或数据源被限流）
          </div>
        )}
      </div>
    </div>
  );
}
