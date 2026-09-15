import { useQuery } from "@tanstack/react-query";

import { fetchHealth, fetchMarketProviders, fetchMetrics } from "../lib/api";
import type { MetricsResponse } from "../lib/types";
import NowBuyPanel from "../components/NowBuyPanel";
import WinRatePanel from "../components/WinRatePanel";

export default function Dashboard() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["health-detail"],
    queryFn: fetchHealth,
    refetchInterval: 15_000,
  });
  const metrics = useQuery({
    queryKey: ["metrics"],
    queryFn: fetchMetrics,
    refetchInterval: 15_000,
  });
  const providers = useQuery({
    queryKey: ["market-providers"],
    queryFn: fetchMarketProviders,
    refetchInterval: 60_000,
  });

  return (
    <div className="max-w-5xl space-y-6">
      <h1 className="text-2xl font-semibold">看板</h1>

      <NowBuyPanel />

      <WinRatePanel />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Card title="数据库" loading={isLoading} error={isError}>
          {data?.database.ok ? (
            <div className="text-emerald-400">● 可用</div>
          ) : (
            <div className="text-rose-400">● 异常</div>
          )}
          {data?.database.error && (
            <div className="text-xs text-slate-500 mt-1">{data.database.error}</div>
          )}
        </Card>

        <Card title="调度器" loading={isLoading} error={isError}>
          {data?.scheduler.running ? (
            <div className="text-emerald-400">● 运行中</div>
          ) : (
            <div className="text-rose-400">● 已停止</div>
          )}
        </Card>

        <Card title="数据源" loading={isLoading} error={isError}>
          <div className="space-y-1">
            {data?.providers ? (
              Object.entries(data.providers).map(([name, ok]) => (
                <div key={name} className="flex items-center gap-2 text-sm">
                  <span
                    className={ok ? "text-emerald-400" : "text-slate-500"}
                  >
                    {ok ? "●" : "○"}
                  </span>
                  <span>{name}</span>
                </div>
              ))
            ) : (
              <div className="text-slate-500 text-sm">—</div>
            )}
          </div>
        </Card>
      </div>

      <DataSourcePanel
        metrics={metrics.data}
        order={providers.data?.providers ?? []}
        availability={providers.data?.status ?? {}}
        loading={metrics.isLoading || providers.isLoading}
      />

      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-medium">服务状态</h2>
          <button
            onClick={() => refetch()}
            className="text-xs px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded"
          >
            刷新
          </button>
        </div>
        <pre className="text-xs text-slate-300 bg-slate-950 p-3 rounded overflow-x-auto">
          {data ? JSON.stringify(data, null, 2) : "加载中..."}
        </pre>
      </div>

      <div className="bg-slate-900 rounded-lg p-4 border border-slate-800 text-sm text-slate-300">
        <p>👉 从左侧菜单进入 <strong>自选股</strong>、<strong>信号</strong>、<strong>策略</strong>、<strong>回测</strong>、<strong>模拟交易</strong> 等页面。</p>
      </div>
    </div>
  );
}

function Card({
  title,
  loading,
  error,
  children,
}: {
  title: string;
  loading: boolean;
  error: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-slate-900 rounded-lg p-4 border border-slate-800">
      <div className="text-xs text-slate-500 uppercase tracking-wide mb-2">
        {title}
      </div>
      {loading ? (
        <div className="text-slate-500 text-sm">检查中...</div>
      ) : error ? (
        <div className="text-rose-400 text-sm">查询失败</div>
      ) : (
        children
      )}
    </div>
  );
}

const DATA_STATUS_LABEL: Record<string, string> = {
  "real-time": "实时",
  delayed: "延迟",
  disconnected: "断开",
  simulated: "模拟",
};

const panelTh = "px-3 py-2 text-xs font-medium text-slate-400 whitespace-nowrap";

/** 实时数据源面板：优先级顺序、可用性、成功率与延迟。
 *
 * /api/market/providers 给出注册顺序与可用性，/api/metrics 给出累计成功率
 * 与平均延迟 —— 这两项直接决定「能不能及时看到数据」。
 */
function DataSourcePanel({
  metrics,
  order,
  availability,
  loading,
}: {
  metrics: MetricsResponse | undefined;
  order: string[];
  availability: Record<string, boolean>;
  loading: boolean;
}) {
  const entries = order.length ? order : Object.keys(metrics?.providers ?? {});
  const status = metrics?.data_status;
  return (
    <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 px-4 py-2 border-b border-slate-800">
        <span className="text-sm text-slate-300">实时数据源</span>
        {status && (
          <span
            className={`text-xs ${
              status === "real-time" ? "text-emerald-400" : "text-amber-400"
            }`}
          >
            行情状态：{DATA_STATUS_LABEL[status] ?? status}
          </span>
        )}
        <span className="ml-auto text-xs text-slate-500">
          按优先级排列，左起优先
        </span>
      </div>
      {loading ? (
        <div className="p-4 text-sm text-slate-500">加载中…</div>
      ) : entries.length === 0 ? (
        <div className="p-4 text-sm text-slate-500">暂无数据源</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-800">
            <thead className="bg-slate-950/60">
              <tr>
                <th className={`${panelTh} text-left`}>数据源</th>
                <th className={`${panelTh} text-left`}>可用</th>
                <th className={`${panelTh} text-right`}>成功 / 失败</th>
                <th className={`${panelTh} text-right`}>成功率</th>
                <th className={`${panelTh} text-right`}>平均延迟</th>
                <th className={`${panelTh} text-right`}>最近成功</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {entries.map((name) => {
                const stat = metrics?.providers?.[name];
                const ok = availability[name];
                return (
                  <tr key={name} className="hover:bg-slate-800/40">
                    <td className="px-3 py-2 text-sm text-slate-200">{name}</td>
                    <td className="px-3 py-2 text-sm">
                      {ok === undefined ? (
                        <span className="text-slate-600">—</span>
                      ) : ok ? (
                        <span className="text-emerald-400">●</span>
                      ) : (
                        <span className="text-rose-400">●</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-sm numeric text-right text-slate-300">
                      {stat ? `${stat.success} / ${stat.failure}` : "—"}
                    </td>
                    <td className="px-3 py-2 text-sm numeric text-right text-slate-300">
                      {stat ? `${(stat.success_rate * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-3 py-2 text-sm numeric text-right text-slate-300">
                      {stat ? `${stat.avg_latency_ms.toFixed(0)} ms` : "—"}
                    </td>
                    <td className="px-3 py-2 text-xs numeric text-right text-slate-500">
                      {stat?.last_success_at
                        ? stat.last_success_at.replace("T", " ").slice(11, 19)
                        : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
