import { ReactNode } from "react";

/** 可悬停、可键盘聚焦的说明入口；移动端点按按钮也会获得焦点。 */
export default function InfoTip({ text, label = "查看详细说明" }: { text: ReactNode; label?: string }) {
  return (
    <span className="group relative ml-1 inline-flex align-middle">
      <button
        type="button"
        aria-label={label}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-amber-500/70 bg-amber-950/40 text-[10px] font-bold leading-none text-amber-300 outline-none transition hover:border-amber-300 hover:bg-amber-900/60 focus-visible:ring-2 focus-visible:ring-amber-400"
      >
        !
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 top-6 z-50 hidden w-72 -translate-x-1/2 rounded-lg border border-slate-700 bg-slate-950 p-3 text-left text-xs font-normal leading-5 text-slate-200 shadow-2xl group-hover:block group-focus-within:block"
      >
        {text}
      </span>
    </span>
  );
}
