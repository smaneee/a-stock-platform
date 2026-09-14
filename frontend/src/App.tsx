import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import Dashboard from "./pages/Dashboard";
import EvidencePage from "./pages/Evidence";
import WatchlistPage from "./pages/Watchlist";
import SignalsPage from "./pages/Signals";
import StrategiesPage from "./pages/Strategies";
import BacktestPage from "./pages/Backtest";
import PortfolioBacktestPage from "./pages/PortfolioBacktest";
import PaperTradingPage from "./pages/PaperTrading";
import SelectionPage from "./pages/Selection";
import MarketPage from "./pages/Market";
import IndicatorsPage from "./pages/Indicators";
import RealtimePicksPage from "./pages/RealtimePicks";
import IntradayPage from "./pages/Intraday";
import UniversePage from "./pages/Universe";
import InvestmentResearchPage from "./pages/InvestmentResearch";

export default function App() {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

  // 切换页面后自动收起移动端抽屉（点链接、浏览器前进后退都覆盖）
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  return (
    <div className="flex h-full bg-slate-950 text-slate-100">
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <Topbar onOpenNav={() => setNavOpen(true)} />
        <main className="flex-1 overflow-auto p-3 sm:p-6">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/watchlist" element={<WatchlistPage />} />
            <Route path="/market" element={<MarketPage />} />
            <Route path="/intraday" element={<IntradayPage />} />
            <Route path="/indicators" element={<IndicatorsPage />} />
            <Route path="/realtime-picks" element={<RealtimePicksPage />} />
            <Route path="/investment-research" element={<InvestmentResearchPage />} />
            <Route path="/universe" element={<UniversePage />} />
            <Route path="/signals" element={<SignalsPage />} />
            <Route path="/selection" element={<SelectionPage />} />
            <Route path="/strategies" element={<StrategiesPage />} />
            <Route path="/backtest" element={<BacktestPage />} />
            <Route
              path="/portfolio-backtest"
              element={<PortfolioBacktestPage />}
            />
            <Route path="/paper" element={<PaperTradingPage />} />
            <Route path="/evidence" element={<EvidencePage />} />
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
        <footer className="px-3 py-2 text-xs text-slate-500 border-t border-slate-800 sm:px-6">
          ⚠️ 分析结果仅用于研究，不构成投资建议。
        </footer>
      </div>
    </div>
  );
}
