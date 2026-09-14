import { useQuery } from "@tanstack/react-query";

import { fetchHealth, fetchMetrics } from "../lib/api";

const STATUS_META: Record<string, { color: string; label: string }> = {
  "real-time": { color: "bg-emerald-500", label: "实时" },
  delayed: { color: "bg-amber-500", label: "延迟" },
  disconnected: { color: "bg-rose-500", label: "数据断开" },
  simulated: { color: "bg-sky-500", label: "模拟数据" },
};

interface TopbarProps {
  /** 窄屏下点左上角按钮打开导航抽屉 */
  onOpenNav?: () => void;
}

export default function Topbar({ onOpenNav }: TopbarProps) {
  const { data: health, isLoading, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  });

  const { data: metrics } = useQuery({
    queryKey: ["metrics"],
    queryFn: fetchMetrics,
    refetchInterval: 10_000,
  });

  // 后端连通性指示
  let indicator: { color: string; label: string };
  if (isLoading) {
    indicator = { color: "bg-slate-500", label: "检查中" };
  } else if (isError || !health) {
    indicator = { color: "bg-rose-500", label: "后端离线" };
  } else {
    const ok = health.database.ok && health.scheduler.running;
    indicator = ok
      ? { color: "bg-emerald-500", label: "后端正常" }
      : { color: "bg-amber-500", label: "后端异常" };
  }

  // 数据源状态（实时/延迟/断开/模拟）
  const dataStatus = metrics?.data_status;
  const statusMeta = dataStatus ? STATUS_META[dataStatus] : null;

  return (
    <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center justify-between gap-2 px-3 sm:px-6">
      <div className="flex min-w-0 items-center gap-1">
        <button
          type="button"
          aria-label="打开导航"
          onClick={onOpenNav}
          className="-ml-1 rounded p-1.5 text-slate-300 hover:bg-slate-800 lg:hidden"
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          >
            <path d="M3 6h18M3 12h18M3 18h18" />
          </svg>
        </button>
        <div className="truncate text-sm text-slate-400">
          A 股实时分析平台 · 研究用途
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-3 text-xs sm:gap-4">
        {statusMeta && (
          <span className="flex items-center gap-1.5">
            <span className={`w-2 h-2 rounded-full ${statusMeta.color}`} />
            <span className="text-slate-300">数据：{statusMeta.label}</span>
          </span>
        )}
        <span className="flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${indicator.color}`} />
          <span className="text-slate-300">{indicator.label}</span>
        </span>
      </div>
    </header>
  );
}
