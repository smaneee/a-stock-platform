/** 分时图：当日价格 + 均价 + 成交量。
 *
 * 口径对齐常见交易软件：
 * - 价格轴以昨收为中轴上下对称，昨收画虚线，涨红跌绿；
 * - 均价线用「累计成交额 ÷ 累计成交量」现场算（等价于均价字段），不依赖
 *   某个数据源是否提供均价，换数据源也不会算错；
 * - 成交量按分钟增量画柱，该分钟相对昨收涨则红、跌则绿。
 */
import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { IntradayPoint } from "../lib/types";

const UP = "#ef4444";
const DOWN = "#10b981";
const FLAT = "#94a3b8";
const AVG_LINE = "#fbbf24";
const axisTick = { fill: "#94a3b8", fontSize: 11 };
const tooltipStyle = {
  background: "#0f172a",
  border: "1px solid #1e293b",
  fontSize: 12,
};

interface Row {
  label: string;
  price: number;
  avg: number | null;
  volume: number;
  pct: number | null;
}

function toRows(points: IntradayPoint[], previousClose: number): Row[] {
  let cumVolume = 0;
  let cumAmount = 0;
  return points.map((point) => {
    cumVolume += point.volume;
    cumAmount += point.amount;
    return {
      label: point.time.slice(11, 16),
      price: point.price,
      avg: cumVolume > 0 ? cumAmount / cumVolume : null,
      volume: point.volume,
      pct:
        previousClose > 0
          ? ((point.price - previousClose) / previousClose) * 100
          : null,
    };
  });
}

type AxisDomain = [number, number] | ["auto", "auto"];

/** 价格轴围绕昨收对称，让昨收落在视觉中轴。 */
function priceDomain(rows: Row[], previousClose: number): AxisDomain {
  if (previousClose <= 0 || rows.length === 0) return ["auto", "auto"];
  let deviation = previousClose * 0.005;
  for (const row of rows) {
    deviation = Math.max(deviation, Math.abs(row.price - previousClose));
  }
  deviation *= 1.08;
  return [previousClose - deviation, previousClose + deviation];
}

/** 右侧涨跌幅轴：与价格轴共用同一个对称区间，只是换算成百分比。 */
function percentDomain(domain: AxisDomain, previousClose: number): AxisDomain {
  const [low, high] = domain;
  if (previousClose <= 0 || typeof low !== "number" || typeof high !== "number") {
    return ["auto", "auto"];
  }
  return [
    ((low - previousClose) / previousClose) * 100,
    ((high - previousClose) / previousClose) * 100,
  ];
}

/** 成交量按「手」显示（后端口径是股）。 */
function formatHands(shares: number): string {
  const hands = shares / 100;
  if (hands >= 1e8) return `${(hands / 1e8).toFixed(2)}亿手`;
  if (hands >= 1e4) return `${(hands / 1e4).toFixed(1)}万手`;
  return hands.toFixed(0);
}

function ChartTooltip(props: {
  active?: boolean;
  payload?: Array<{ payload: Row }>;
  label?: string;
}) {
  if (!props.active || !props.payload || props.payload.length === 0) return null;
  const row = props.payload[0].payload;
  return (
    <div className="rounded px-3 py-2 leading-5" style={tooltipStyle}>
      <div className="text-slate-400">{props.label}</div>
      <div className="text-slate-100">
        价格 <span className="numeric">{row.price.toFixed(2)}</span>
      </div>
      <div style={{ color: AVG_LINE }}>
        均价{" "}
        <span className="numeric">
          {row.avg === null ? "—" : row.avg.toFixed(2)}
        </span>
      </div>
      {row.pct !== null && (
        <div style={{ color: row.pct >= 0 ? UP : DOWN }}>
          涨跌{" "}
          <span className="numeric">
            {row.pct >= 0 ? "+" : ""}
            {row.pct.toFixed(2)}%
          </span>
        </div>
      )}
      <div className="text-slate-400">
        成交 <span className="numeric">{formatHands(row.volume)}</span>
      </div>
    </div>
  );
}

interface Props {
  points: IntradayPoint[];
  previousClose: number;
}

export default function IntradayChart({ points, previousClose }: Props) {
  const rows = useMemo(
    () => toRows(points, previousClose),
    [points, previousClose],
  );
  const domain = useMemo(
    () => priceDomain(rows, previousClose),
    [rows, previousClose],
  );
  const percent = useMemo(
    () => percentDomain(domain, previousClose),
    [domain, previousClose],
  );
  // 只有真的跨过午休才画分隔线，否则 recharts 会为一个不存在的刻度报警告
  const hasNoonGap = rows.some((row) => row.label === "11:30");
  const lastPrice = rows.length > 0 ? rows[rows.length - 1].price : 0;
  const lineColor =
    previousClose > 0 ? (lastPrice >= previousClose ? UP : DOWN) : FLAT;

  return (
    <div>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={rows}
            margin={{ top: 8, right: 8, bottom: 0, left: -14 }}
          >
            <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
            <XAxis
              dataKey="label"
              tick={axisTick}
              minTickGap={48}
              interval="preserveStartEnd"
            />
            <YAxis
              yAxisId="price"
              domain={domain}
              tick={axisTick}
              tickFormatter={(value: number) => value.toFixed(2)}
            />
            <YAxis
              yAxisId="percent"
              orientation="right"
              domain={percent}
              tick={axisTick}
              tickFormatter={(value: number) =>
                `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`
              }
            />
            <Tooltip content={<ChartTooltip />} />
            {previousClose > 0 && (
              <ReferenceLine
                yAxisId="price"
                y={previousClose}
                stroke="#64748b"
                strokeDasharray="4 4"
              />
            )}
            {hasNoonGap && (
              <ReferenceLine
                yAxisId="price"
                x="11:30"
                stroke="#334155"
                strokeDasharray="3 3"
              />
            )}
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="avg"
              name="均价"
              stroke={AVG_LINE}
              strokeWidth={1.2}
              dot={false}
              connectNulls
              isAnimationActive={false}
            />
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="price"
              name="价格"
              stroke={lineColor}
              strokeWidth={1.8}
              dot={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="h-20">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={rows}
            margin={{ top: 0, right: 8, bottom: 0, left: -14 }}
          >
            <XAxis dataKey="label" hide />
            <YAxis
              tick={axisTick}
              width={56}
              tickFormatter={(value: number) => formatHands(value)}
            />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="volume" name="成交量" isAnimationActive={false}>
              {rows.map((row, index) => (
                <Cell
                  key={index}
                  fill={row.pct === null ? FLAT : row.pct >= 0 ? UP : DOWN}
                />
              ))}
            </Bar>
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
