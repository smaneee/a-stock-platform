import { useQuery } from "@tanstack/react-query";

import { fetchHealth } from "../lib/api";

export default function Dashboard() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["health-detail"],
    queryFn: fetchHealth,
    refetchInterval: 15_000,
  });

  return (
    <div className="max-w-5xl space-y-6">
      <h1 className="text-2xl font-semibold">看板</h1>

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
