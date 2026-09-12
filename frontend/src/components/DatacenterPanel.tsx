/** 东方财富数据中心面板：龙虎榜 / 大宗交易 / 融资融券 / 沪深港通 / 机构调研…
 *
 * 表头与格式化方式完全由后端返回的数据集声明（fields）驱动，新增数据集时
 * 前端无需改动。
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  fetchDatacenterCatalog,
  fetchDragonTigerSeats,
  queryDatacenter,
} from "../lib/api";
import type {
  DatacenterDatasetInfo,
  DatacenterRow,
  DragonTigerSeatsResponse,
} from "../lib/types";
import { formatCell, formatNumber, trendClass } from "../lib/format";

const PAGE_SIZE = 50;

const panel = "bg-slate-900 rounded-lg border border-slate-800 overflow-hidden";
const th = "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap";
const input =
  "bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-slate-200 numeric";

function SeatsTable({ title, rows }: { title: string; rows: DatacenterRow[] }) {
  return (
    <div className={panel}>
      <div className="px-4 py-2 border-b border-slate-800 text-sm text-slate-300">
        {title}
        <span className="ml-2 text-xs text-slate-500">共 {rows.length} 个席位</span>
      </div>
      {rows.length === 0 ? (
        <div className="p-4 text-sm text-slate-500">暂无席位数据</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800">
            <thead className="bg-slate-950/60">
              <tr>
                <th className={th}>席位名称</th>
                <th className={`${th} text-right`}>买入额</th>
                <th className={`${th} text-right`}>卖出额</th>
                <th className={`${th} text-right`}>净额</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {rows.map((row, index) => (
                <tr key={`${row.seat_name}-${index}`} className="hover:bg-slate-800/40">
                  <td className={`${td} text-slate-200`}>{String(row.seat_name ?? "—")}</td>
                  <td className={`${td} numeric text-right text-slate-300`}>
                    {formatNumber(Number(row.buy_amount ?? 0))}
                  </td>
                  <td className={`${td} numeric text-right text-slate-300`}>
                    {formatNumber(Number(row.sell_amount ?? 0))}
                  </td>
                  <td
                    className={`${td} numeric text-right ${trendClass(Number(row.net_amount ?? 0))}`}
                  >
                    {formatNumber(Number(row.net_amount ?? 0))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function DatacenterPanel() {
  const [dataset, setDataset] = useState("dragon-tiger");
  const [dateDraft, setDateDraft] = useState("");
  const [symbolDraft, setSymbolDraft] = useState("");
  const [applied, setApplied] = useState({ date: "", symbol: "" });
  const [order, setOrder] = useState<"desc" | "asc">("desc");
  const [page, setPage] = useState(1);
  const [seats, setSeats] = useState<DragonTigerSeatsResponse | null>(null);
  const [seatsError, setSeatsError] = useState<string | null>(null);

  const catalog = useQuery({
    queryKey: ["market", "datacenter-catalog"],
    queryFn: fetchDatacenterCatalog,
    staleTime: 10 * 60 * 1000,
  });
  const info: DatacenterDatasetInfo | undefined = catalog.data?.datasets.find(
    (item) => item.key === dataset,
  );

  const result = useQuery({
    queryKey: [
      "market",
      "datacenter",
      dataset,
      applied.date,
      applied.symbol,
      order,
      page,
    ],
    queryFn: () =>
      queryDatacenter(dataset, {
        date: applied.date || undefined,
        symbol: applied.symbol || undefined,
        order,
        page,
        limit: PAGE_SIZE,
      }),
    enabled: Boolean(info),
  });

  const dateValid = dateDraft === "" || /^\d{4}-\d{2}-\d{2}$/.test(dateDraft);
  const symbolValid = symbolDraft === "" || /^\d{6}$/.test(symbolDraft);

  const selectDataset = (key: string) => {
    setDataset(key);
    setPage(1);
    setSeats(null);
    setSeatsError(null);
  };

  const applyFilters = () => {
    if (!dateValid || !symbolValid) return;
    setApplied({ date: dateDraft, symbol: symbolDraft });
    setPage(1);
    setSeats(null);
  };

  const clearFilters = () => {
    setDateDraft("");
    setSymbolDraft("");
    setApplied({ date: "", symbol: "" });
    setPage(1);
    setSeats(null);
  };

  const loadSeats = async (row: DatacenterRow) => {
    setSeatsError(null);
    try {
      const data = await fetchDragonTigerSeats(
        String(row.symbol),
        String(row.trade_date) || undefined,
      );
      setSeats(data);
    } catch (error) {
      setSeats(null);
      setSeatsError(error instanceof Error ? error.message : String(error));
    }
  };

  const error = catalog.error ?? result.error;
  const total = result.data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const fields = info?.fields ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {(catalog.data?.datasets ?? []).map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => selectDataset(item.key)}
            className={`px-3 py-1 rounded text-xs transition-colors ${
              dataset === item.key
                ? "bg-slate-800 text-slate-100"
                : "bg-slate-900 text-slate-400 hover:text-slate-200"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {info && <p className="text-xs text-slate-500">{info.description}</p>}

      <div className={`${panel} p-4 flex flex-wrap items-end gap-3`}>
        {info?.supports_date && (
          <div>
            <label className="block text-xs text-slate-500 mb-1">日期（YYYY-MM-DD）</label>
            <input
              value={dateDraft}
              onChange={(event) => setDateDraft(event.target.value.trim())}
              placeholder="2026-09-11"
              className={`${input} w-40`}
            />
          </div>
        )}
        {info?.supports_symbol && (
          <div>
            <label className="block text-xs text-slate-500 mb-1">股票代码</label>
            <input
              value={symbolDraft}
              onChange={(event) => setSymbolDraft(event.target.value.trim())}
              placeholder="600519"
              className={`${input} w-32`}
            />
          </div>
        )}
        <button
          type="button"
          onClick={applyFilters}
          disabled={!dateValid || !symbolValid}
          className="px-3 py-1.5 rounded text-sm bg-sky-500/20 text-sky-300 border border-sky-500/40 disabled:opacity-50"
        >
          查询
        </button>
        <button
          type="button"
          onClick={clearFilters}
          className="px-3 py-1.5 rounded text-sm bg-slate-900 text-slate-300 border border-slate-800"
        >
          清空
        </button>
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
        {!dateValid && <span className="text-xs text-rose-300">日期格式应为 YYYY-MM-DD</span>}
        {!symbolValid && <span className="text-xs text-rose-300">请输入 6 位股票代码</span>}
      </div>

      {error && (
        <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
          数据源暂不可用：{error instanceof Error ? error.message : String(error)}
        </div>
      )}

      <div className={panel}>
        <div className="px-4 py-2 border-b border-slate-800 flex items-center justify-between text-xs text-slate-400">
          <span>{result.data ? `${result.data.label} · 命中 ${total} 条` : "加载中…"}</span>
          <div className="flex items-center gap-2">
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
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800">
            <thead className="bg-slate-950/60">
              <tr>
                {dataset === "dragon-tiger" && <th className={th}>席位</th>}
                {fields.map((field) => (
                  <th key={field.key} className={th}>
                    {field.title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {(result.data?.rows ?? []).map((row, index) => (
                <tr key={index} className="hover:bg-slate-800/40">
                  {dataset === "dragon-tiger" && (
                    <td className={td}>
                      <button
                        type="button"
                        onClick={() => void loadSeats(row)}
                        className="px-2 py-0.5 rounded text-xs bg-sky-500/20 text-sky-300 border border-sky-500/40"
                      >
                        查看
                      </button>
                    </td>
                  )}
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
        {result.data && result.data.rows.length === 0 && (
          <div className="p-6 text-sm text-slate-500">
            没有匹配的数据（该日期可能不是交易日，或数据源被限流）
          </div>
        )}
      </div>

      {seatsError && (
        <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
          席位数据不可用：{seatsError}
        </div>
      )}

      {seats && (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <SeatsTable
            title={`${seats.symbol} 买入席位${seats.trade_date ? ` · ${seats.trade_date}` : ""}`}
            rows={seats.buy}
          />
          <SeatsTable
            title={`${seats.symbol} 卖出席位${seats.trade_date ? ` · ${seats.trade_date}` : ""}`}
            rows={seats.sell}
          />
        </div>
      )}
    </div>
  );
}
