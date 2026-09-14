import { NavLink } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "看板", icon: "📊" },
  { to: "/watchlist", label: "自选股", icon: "⭐" },
  { to: "/intraday", label: "实时分时", icon: "💹" },
  { to: "/market", label: "市场行情", icon: "🧭" },
  { to: "/indicators", label: "技术指标", icon: "📐" },
  { to: "/realtime-picks", label: "买点雷达", icon: "📡" },
  { to: "/universe", label: "股票池", icon: "🗂️" },
  { to: "/signals", label: "信号", icon: "🔔" },
  { to: "/selection", label: "智能选股", icon: "🎯" },
  { to: "/strategies", label: "策略", icon: "⚙️" },
  { to: "/backtest", label: "回测", icon: "📈" },
  { to: "/portfolio-backtest", label: "组合回测", icon: "🧮" },
  { to: "/paper", label: "模拟交易", icon: "💰" },
  { to: "/evidence", label: "策略证据", icon: "🧾" },
];

interface SidebarProps {
  /** 移动端抽屉是否展开；≥ lg 时侧栏常驻，与它无关 */
  open?: boolean;
  onClose?: () => void;
}

/** 侧栏导航：桌面端常驻，窄屏（< 1024px）收成可开关的抽屉。 */
export default function Sidebar({ open = false, onClose }: SidebarProps) {
  return (
    <>
      {open && (
        <button
          type="button"
          aria-label="关闭导航"
          onClick={onClose}
          className="fixed inset-0 z-30 bg-slate-950/70 lg:hidden"
        />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col overflow-y-auto border-r border-slate-800 bg-slate-900 transition-transform duration-200 lg:static lg:z-auto lg:w-56 lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-start justify-between border-b border-slate-800 px-4 py-5">
          <div>
            <div className="text-lg font-semibold text-sky-400">A 股分析</div>
            <div className="mt-1 text-xs text-slate-500">实时行情 · 策略 · 回测</div>
          </div>
          <button
            type="button"
            aria-label="关闭导航"
            onClick={onClose}
            className="-mr-1 -mt-1 rounded p-1.5 text-slate-400 hover:bg-slate-800 lg:hidden"
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
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>
        <nav className="flex-1 py-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-3 px-4 py-2.5 text-sm transition-colors ${
                  isActive
                    ? "bg-slate-800 text-sky-300 border-l-2 border-sky-400"
                    : "text-slate-300 hover:bg-slate-800/50 hover:text-slate-100"
                }`
              }
            >
              <span className="text-base">{item.icon}</span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-slate-800 px-4 py-3 text-xs text-slate-500">
          v0.1.0
        </div>
      </aside>
    </>
  );
}