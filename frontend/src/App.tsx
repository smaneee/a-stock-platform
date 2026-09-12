import { NavLink, Route, Routes } from "react-router-dom";

import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import Dashboard from "./pages/Dashboard";
import WatchlistPage from "./pages/Watchlist";
import SignalsPage from "./pages/Signals";
import StrategiesPage from "./pages/Strategies";
import BacktestPage from "./pages/Backtest";
import PortfolioBacktestPage from "./pages/PortfolioBacktest";
import PaperTradingPage from "./pages/PaperTrading";
import SelectionPage from "./pages/Selection";

export default function App() {
  return (
    <div className="flex h-full bg-slate-950 text-slate-100">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        <Topbar />
        <main className="flex-1 overflow-auto p-6">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/watchlist" element={<WatchlistPage />} />
            <Route path="/market" element={<MarketPage />} />
            <Route path="/signals" element={<SignalsPage />} />
            <Route path="/selection" element={<SelectionPage />} />
            <Route path="/strategies" element={<StrategiesPage />} />
            <Route path="/backtest" element={<BacktestPage />} />
            <Route
              path="/portfolio-backtest"
              element={<PortfolioBacktestPage />}
            />
            <Route path="/paper" element={<PaperTradingPage />} />
            <Route
              path="*"
              element={
                <div className="text-slate-400 text-center py-12">
                  页面不存在 ·{" "}
                  <NavLink to="/" className="text-sky-400 underline">
                    回到首页
                  </NavLink>
                </div>
              }
            />
          </Routes>
        </main>
        <footer className="px-6 py-2 text-xs text-slate-500 border-t border-slate-800">
          ⚠️ 分析结果仅用于研究，不构成投资建议。
        </footer>
      </div>
    </div>
  );
}
import MarketPage from "./pages/Market";
