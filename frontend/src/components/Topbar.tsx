import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "../lib/api";

export default function Topbar() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 10_000,
  });

  let indicator: { color: string; label: string };
  if (isLoading) {
    indicator = { color: "bg-slate-500", label: "检查中" };
  } else if (isError || !data) {
    indicator = { color: "bg-rose-500", label: "后端离线" };
  } else {
    const ok = data.database.ok && data.scheduler.running;
    indicator = ok
      ? { color: "bg-emerald-500", label: "后端正常" }
      : { color: "bg-amber-500", label: "后端异常" };
  }

  return (
    <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center justify-between px-6">
      <div className="text-sm text-slate-400">
        A 股实时分析平台 · 研究用途
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span className={`w-2 h-2 rounded-full ${indicator.color}`} />
        <span className="text-slate-300">{indicator.label}</span>
      </div>
    </header>
  );
}
