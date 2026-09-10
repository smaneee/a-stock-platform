import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { disableStrategy, enableStrategy, listStrategies } from "../lib/api";

export default function StrategiesPage() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["strategies"],
    queryFn: listStrategies,
  });

  const enableMut = useMutation({
    mutationFn: (id: number) => enableStrategy(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["strategies"] }),
  });
  const disableMut = useMutation({
    mutationFn: (id: number) => disableStrategy(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["strategies"] }),
  });

  return (
    <div className="max-w-4xl space-y-6">
      <h1 className="text-2xl font-semibold">策略管理</h1>

      <div className="bg-slate-900 rounded-lg border border-slate-800 overflow-hidden">
        {isLoading ? (
          <div className="text-slate-500 text-center py-8">加载中...</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-slate-500 uppercase bg-slate-950">
              <tr>
                <th className="text-left px-4 py-2">名称</th>
                <th className="text-left px-4 py-2">说明</th>
                <th className="text-left px-4 py-2">版本</th>
                <th className="text-left px-4 py-2">状态</th>
                <th className="text-right px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {data?.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-slate-800/50 hover:bg-slate-800/30"
                >
                  <td className="px-4 py-2 font-medium">{s.name}</td>
                  <td className="px-4 py-2 text-slate-400 text-xs">
                    {s.description ?? "—"}
                  </td>
                  <td className="px-4 py-2 text-slate-500 text-xs">
                    v{s.version}
                  </td>
                  <td className="px-4 py-2">
                    {s.enabled ? (
                      <span className="text-emerald-400">● 运行中</span>
                    ) : (
                      <span className="text-slate-500">○ 已停止</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {s.enabled ? (
                      <button
                        onClick={() => disableMut.mutate(s.id)}
                        disabled={disableMut.isPending}
                        className="text-xs px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded"
                      >
                        停用
                      </button>
                    ) : (
                      <button
                        onClick={() => enableMut.mutate(s.id)}
                        disabled={enableMut.isPending}
                        className="text-xs px-3 py-1 bg-sky-600 hover:bg-sky-500 rounded"
                      >
                        启用
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <p className="text-xs text-slate-500">
        启用策略后，行情轮询会同时调用所有已启用策略生成信号。冷却时间默认 60 秒。
      </p>
    </div>
  );
}
