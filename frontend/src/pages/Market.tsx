import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  fetchStockFundFlowHistory,
  listBoardConstituents,
  listBoardFundFlow,
  listBoards,
  listStockFundFlowRank,
} from "../lib/api";
import DatacenterPanel from "../components/DatacenterPanel";
import LimitUpPanel from "../components/LimitUpPanel";
import LimitUpSentimentPanel from "../components/LimitUpSentimentPanel";
import type { BoardKind, FundFlowPoint, FundFlowRow } from "../lib/types";

const BOARD_KINDS: Array<{ value: BoardKind; label: string }> = [
  { value: "industry", label: "行业板块" },
  { value: "concept", label: "概念板块" },
  { value: "region", label: "地域板块" },
];

// 板块与成分股的条数选项（后端单次上限 1000，东财单页 100 会由后端自动翻页）
const BOARD_LIMIT_OPTIONS = [50, 100, 200, 500, 1000];
const MEMBER_LIMIT_OPTIONS = [30, 50, 100, 200, 500, 1000];

const TABS = [
  { value: "boards", label: "板块行情" },
  { value: "board-flow", label: "板块资金流" },
  { value: "stock-flow", label: "个股资金流" },
  { value: "limit-up", label: "涨停板" },
  { value: "sentiment", label: "情绪曲线" },
  { value: "datacenter", label: "数据中心" },
] as const;

type Tab = (typeof TABS)[number]["value"];

const RED = "#fb7185";
const GREEN = "#34d399";

const pct = (value: number) => `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

/** 元 → 亿 / 万，便于阅读 */
const money = (value: number) => {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return value.toFixed(0);
};

/** A 股习惯：红涨绿跌 */
const trendClass = (value: number) =>
  value > 0 ? "text-rose-400" : value < 0 ? "text-emerald-400" : "text-slate-400";

const panel = "bg-slate-900 rounded-lg border border-slate-800 overflow-hidden";
const th = "px-3 py-2 text-left text-xs font-medium text-slate-400 whitespace-nowrap";
const td = "px-3 py-2 text-sm whitespace-nowrap";

function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm rounded-lg px-4 py-3">
      数据源暂不可用：{message}
    </div>
  );
}

function FundFlowTable({ rows }: { rows: FundFlowRow[] }) {
  return (
    <div className={panel}>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-slate-800">
          <thead className="bg-slate-950/60">
            <tr>
              <th className={th}>代码</th>
              <th className={th}>名称</th>
              <th className={`${th} text-right`}>最新价</th>
              <th className={`${th} text-right`}>涨跌幅</th>
              <th className={`${th} text-right`}>主力净流入</th>
              <th className={`${th} text-right`}>主力占比</th>
              <th className={`${th} text-right`}>超大单</th>
              <th className={`${th} text-right`}>大单</th>
              <th className={`${th} text-right`}>中单</th>
              <th className={`${th} text-right`}>小单</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {rows.map((row) => (
              <tr key={row.code} className="hover:bg-slate-800/40">
                <td className={`${td} text-slate-400`}>{row.code}</td>
                <td className={`${td} text-slate-200`}>{row.name}</td>
                <td className={`${td} numeric text-right text-slate-200`}>
                  {row.price.toFixed(2)}
                </td>
                <td className={`${td} numeric text-right ${trendClass(row.change_pct)}`}>
                  {pct(row.change_pct)}
                </td>
                <td
                  className={`${td} numeric text-right ${trendClass(row.main_net_inflow)}`}
                >
                  {money(row.main_net_inflow)}
                </td>
                <td className={`${td} numeric text-right text-slate-300`}>
                  {row.main_net_inflow_pct.toFixed(2)}%
                </td>
                <td className={`${td} numeric text-right ${trendClass(row.super_large_net_inflow)}`}>
                  {money(row.super_large_net_inflow)}
                </td>
                <td className={`${td} numeric text-right ${trendClass(row.large_net_inflow)}`}>
                  {money(row.large_net_inflow)}
                </td>
                <td className={`${td} numeric text-right ${trendClass(row.medium_net_inflow)}`}>
                  {money(row.medium_net_inflow)}
                </td>
                <td className={`${td} numeric text-right ${trendClass(row.small_net_inflow)}`}>
                  {money(row.small_net_inflow)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FlowHistoryChart({ points }: { points: FundFlowPoint[] }) {
  const data = points.map((point) => ({
    date: point.trade_date.slice(5),
    inflow: Number((point.main_net_inflow / 1e8).toFixed(4)),
    ratio: point.main_net_inflow_pct,
  }));

  return (
    <div className={`${panel} p-4`}>
      <div className="text-sm text-slate-300 mb-3">
        主力净流入（亿元，柱）与主力净占比（%，线值见提示）
      </div>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="date" stroke="#64748b" fontSize={11} />
            <YAxis stroke="#64748b" fontSize={11} />
            <Tooltip
              contentStyle={{
                background: "#0f172a",
                border: "1px solid #1e293b",
                borderRadius: 8,
                fontSize: 12,
              }}
              formatter={(value: number, name: string) =>
                name === "inflow" ? [`${value} 亿`, "主力净流入"] : [`${value}%`, "主力占比"]
              }
            />
            <ReferenceLine y={0} stroke="#475569" />
            <Bar dataKey="inflow" name="inflow">
              {data.map((item) => (
                <Cell key={item.date} fill={item.inflow >= 0 ? RED : GREEN} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export default function MarketPage() {
  const [tab, setTab] = useState<Tab>("boards");
  const [kind, setKind] = useState<BoardKind>("industry");
  const [board, setBoard] = useState<{ code: string; name: string } | null>(null);
  const [symbolInput, setSymbolInput] = useState("600519");
  const [symbol, setSymbol] = useState("600519");
  // 东财板块与成分股都是分页返回，默认条数太小会让人误以为「只有这么多」，
  // 这里给用户一个显式的条数选择（后端单次上限 1000，与上方 BOARD_LIMIT_OPTIONS
  // 注释一致；东财单页 100 由后端自动翻页。此前这里误写 500，已按
  // backend/app/api/market.py 的 Query(le=1000) 更正）。
  const [boardLimit, setBoardLimit] = useState(100);
  const [memberLimit, setMemberLimit] = useState(50);

  const boards = useQuery({
    queryKey: ["market", "boards", kind, boardLimit],
    queryFn: () => listBoards(kind, boardLimit),
    enabled: tab === "boards",
  });
  const constituents = useQuery({
    queryKey: ["market", "constituents", board?.code, memberLimit],
    queryFn: () => listBoardConstituents(board!.code, memberLimit),
    enabled: tab === "boards" && board !== null,
  });
  const boardFlow = useQuery({
    queryKey: ["market", "board-flow", kind],
    queryFn: () => listBoardFundFlow(kind, 50),
    enabled: tab === "board-flow",
  });
  const stockFlow = useQuery({
    queryKey: ["market", "stock-flow"],
    queryFn: () => listStockFundFlowRank(50),
    enabled: tab === "stock-flow",
  });
  const flowHistory = useQuery({
    queryKey: ["market", "flow-history", symbol],
    queryFn: () => fetchStockFundFlowHistory(symbol, 60),
    enabled: tab === "stock-flow" && /^\d{6}$/.test(symbol),
  });

  const error =
    boards.error ??
    boardFlow.error ??
    stockFlow.error ??
    flowHistory.error ??
    constituents.error;

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">市场行情</h1>
          <p className="text-xs text-slate-500 mt-1">
            数据源：东方财富 · 板块 / 资金流为当日横截面快照，数据中心含龙虎榜、大宗交易、融资融券、沪深港通等
          </p>
        </div>
        <div className="flex gap-2">
          {TABS.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => setTab(item.value)}
              className={`px-3 py-1.5 rounded text-sm transition-colors ${
                tab === item.value
                  ? "bg-sky-500/20 text-sky-300 border border-sky-500/40"
                  : "bg-slate-900 text-slate-300 border border-slate-800 hover:text-slate-100"
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </header>

      {(tab === "boards" || tab === "board-flow") && (
        <div className="flex gap-2">
          {BOARD_KINDS.map((item) => (
            <button
              key={item.value}
              type="button"
              onClick={() => {
                setKind(item.value);
                setBoard(null);
              }}
              className={`px-3 py-1 rounded text-xs transition-colors ${
                kind === item.value
                  ? "bg-slate-800 text-slate-100"
                  : "bg-slate-900 text-slate-400 hover:text-slate-200"
              }`}
            >
              {item.label}
            </button>
          ))}
          {tab === "boards" && (
            <label className="ml-auto flex items-center gap-2 text-xs text-slate-400">
              板块条数
              <select
                value={boardLimit}
                onChange={(event) => setBoardLimit(Number(event.target.value))}
                className="bg-slate-950 border border-slate-800 rounded px-2 py-1 text-slate-300"
              >
                {BOARD_LIMIT_OPTIONS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}

      <ErrorNotice error={error} />

      {tab === "boards" && (
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div className={`${panel} xl:col-span-2`}>
            <div className="px-4 py-3 border-b border-slate-800 text-sm text-slate-300">
              板块行情
              <span className="text-xs text-slate-500 ml-2">
                已加载 {boards.data?.items.length ?? 0} 个（东财按涨跌幅降序返回，可切换上方条数）
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800">
                <thead className="bg-slate-950/60">
                  <tr>
                    <th className={th}>板块</th>
                    <th className={`${th} text-right`}>涨跌幅</th>
                    <th className={`${th} text-right`}>主力净流入</th>
                    <th className={`${th} text-right`}>上涨/下跌</th>
                    <th className={th}>领涨股</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {(boards.data?.items ?? []).map((item) => (
                    <tr
                      key={item.code}
                      onClick={() => setBoard({ code: item.code, name: item.name })}
                      className={`cursor-pointer hover:bg-slate-800/40 ${
                        board?.code === item.code ? "bg-slate-800/60" : ""
                      }`}
                    >
                      <td className={`${td} text-slate-200`}>
                        {item.name}
                        <span className="ml-2 text-xs text-slate-500">{item.code}</span>
                      </td>
                      <td className={`${td} numeric text-right ${trendClass(item.change_pct)}`}>
                        {pct(item.change_pct)}
                      </td>
                      <td
                        className={`${td} numeric text-right ${trendClass(item.main_net_inflow)}`}
                      >
                        {money(item.main_net_inflow)}
                      </td>
                      <td className={`${td} numeric text-right text-slate-400`}>
                        <span className="text-rose-400">{item.up_count}</span>
                        {" / "}
                        <span className="text-emerald-400">{item.down_count}</span>
                      </td>
                      <td className={`${td} text-slate-300`}>
                        {item.leader_name ?? "—"}
                        {item.leader_change_pct !== null && (
                          <span className={`ml-2 numeric ${trendClass(item.leader_change_pct)}`}>
                            {pct(item.leader_change_pct)}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {boards.isLoading && (
              <div className="p-6 text-sm text-slate-500">加载中…</div>
            )}
          </div>

          <div className={panel}>
            <div className="px-4 py-3 border-b border-slate-800 text-sm text-slate-300 flex items-center gap-2">
              <span>
                {board ? `${board.name} · 成分股` : "点击左侧板块查看成分股"}
              </span>
              {board && (
                <span className="text-xs text-slate-500">
                  已加载 {constituents.data?.items.length ?? 0} 只
                </span>
              )}
              {board && (
                <label className="ml-auto flex items-center gap-2 text-xs text-slate-400">
                  显示
                  <select
                    value={memberLimit}
                    onChange={(event) => setMemberLimit(Number(event.target.value))}
                    className="bg-slate-950 border border-slate-800 rounded px-2 py-1 text-slate-300"
                  >
                    {MEMBER_LIMIT_OPTIONS.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-slate-800">
                <thead className="bg-slate-950/60">
                  <tr>
                    <th className={th}>股票</th>
                    <th className={`${th} text-right`}>最新价</th>
                    <th className={`${th} text-right`}>涨跌幅</th>
                    <th className={`${th} text-right`}>主力净流入</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {(constituents.data?.items ?? []).map((item) => (
                    <tr key={item.symbol} className="hover:bg-slate-800/40">
                      <td className={`${td} text-slate-200`}>
                        {item.name}
                        <span className="ml-2 text-xs text-slate-500">{item.symbol}</span>
                      </td>
                      <td className={`${td} numeric text-right text-slate-200`}>
                        {item.price.toFixed(2)}
                      </td>
                      <td className={`${td} numeric text-right ${trendClass(item.change_pct)}`}>
                        {pct(item.change_pct)}
                      </td>
                      <td
                        className={`${td} numeric text-right ${trendClass(item.main_net_inflow)}`}
                      >
                        {money(item.main_net_inflow)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {constituents.isLoading && (
              <div className="p-6 text-sm text-slate-500">加载中…</div>
            )}
          </div>
        </div>
      )}

      {tab === "board-flow" && (
        <FundFlowTable rows={boardFlow.data?.items ?? []} />
      )}

      {tab === "datacenter" && <DatacenterPanel />}

      {tab === "limit-up" && <LimitUpPanel />}

      {tab === "sentiment" && <LimitUpSentimentPanel />}

      {tab === "stock-flow" && (
        <div className="space-y-4">
          <div className={`${panel} p-4 flex flex-wrap items-end gap-3`}>
            <div>
              <label className="block text-xs text-slate-500 mb-1">
                个股代码（查资金流历史）
              </label>
              <input
                value={symbolInput}
                onChange={(event) => setSymbolInput(event.target.value.trim())}
                placeholder="600519"
                className="bg-slate-950 border border-slate-800 rounded px-3 py-1.5 text-sm text-slate-200 w-40 numeric"
              />
            </div>
            <button
              type="button"
              onClick={() => /^\d{6}$/.test(symbolInput) && setSymbol(symbolInput)}
              className="px-3 py-1.5 rounded text-sm bg-sky-500/20 text-sky-300 border border-sky-500/40"
            >
              查询
            </button>
            {!/^\d{6}$/.test(symbolInput) && (
              <span className="text-xs text-rose-300">请输入 6 位股票代码</span>
            )}
          </div>

          {flowHistory.isLoading && (
            <div className="text-sm text-slate-500">加载资金流历史…</div>
          )}
          {flowHistory.data && flowHistory.data.items.length > 0 && (
            <FlowHistoryChart points={flowHistory.data.items} />
          )}
          {flowHistory.data && flowHistory.data.items.length === 0 && (
            <div className="text-sm text-slate-500">
              {symbol} 暂无资金流历史数据（或被数据源限流）
            </div>
          )}

          <div>
            <div className="text-sm text-slate-300 mb-2">个股主力净流入排行</div>
            <FundFlowTable rows={stockFlow.data?.items ?? []} />
          </div>
        </div>
      )}
    </div>
  );
}
