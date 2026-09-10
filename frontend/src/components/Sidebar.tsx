import { NavLink } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "看板", icon: "📊" },
  { to: "/watchlist", label: "自选股", icon: "⭐" },
  { to: "/signals", label: "信号", icon: "🔔" },
  { to: "/strategies", label: "策略", icon: "⚙️" },
  { to: "/backtest", label: "回测", icon: "📈" },
  { to: "/paper", label: "模拟交易", icon: "💰" },
];

export default function Sidebar() {
  return (
    <aside className="w-56 bg-slate-900 border-r border-slate-800 flex flex-col">
      <div className="px-4 py-5 border-b border-slate-800">
        <div className="text-lg font-semibold text-sky-400">A 股分析</div>
        <div className="text-xs text-slate-500 mt-1">实时行情 · 策略 · 回测</div>
      </div>
      <nav className="flex-1 py-2">
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
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
      <div className="px-4 py-3 border-t border-slate-800 text-xs text-slate-500">
        v0.1.0
      </div>
    </aside>
  );
}
