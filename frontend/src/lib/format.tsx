import type { ReactNode } from "react";

/** 后端字段声明驱动的表格单元格格式化（数据中心 / 涨停板情绪池共用）。
 *
 * kind 由后端返回：text / date 原样显示，数值按量级换算成万/亿，
 * 表头含 "(%)" 的字段按 A 股习惯红涨绿跌。
 */
export interface TableFieldInfo {
  key: string;
  title: string;
  kind: string;
}

export type TableRow = Record<string, string | number | null>;

/** A 股习惯：红涨绿跌 */
export const trendClass = (value: number): string =>
  value > 0 ? "text-rose-400" : value < 0 ? "text-emerald-400" : "text-slate-400";

export const pct = (value: number): string =>
  `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

/** 按量级自动换算，价格保留两位小数，金额显示为万/亿。 */
export const formatNumber = (value: number): string => {
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e6) return `${(value / 1e4).toFixed(1)}万`;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
};

export function formatCell(field: TableFieldInfo, raw: TableRow[string]): ReactNode {
  if (raw === null || raw === undefined || raw === "") {
    return <span className="text-slate-600">—</span>;
  }
  if (field.kind === "date" || field.kind === "text") {
    return <span className="text-slate-300">{String(raw)}</span>;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    return <span className="text-slate-300">{String(raw)}</span>;
  }
  // 百分比字段带涨跌色，其余保持中性，避免把成交额也染成红绿
  if (field.title.includes("(%)")) {
    return <span className={`numeric ${trendClass(value)}`}>{pct(value)}</span>;
  }
  return <span className="numeric text-slate-300">{formatNumber(value)}</span>;
}
