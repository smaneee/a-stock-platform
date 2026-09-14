/** 访问闸门：需要鉴权时先登录，未开启鉴权时直接渲染应用。
 *
 * 与后端 `/api/auth/*` 配套（见 backend/app/security/auth.py）。本机浏览器由
 * `/api/auth/local-token` 免密通过，手机等局域网设备需要输入访问口令。
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { bootstrapAuth, login, logout, subscribeToken, type AuthState } from "../lib/auth";

type Phase = "checking" | "ready" | "need-login";

export default function AuthGate({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<AuthState | null>(null);

  const check = useCallback(async () => {
    setPhase("checking");
    try {
      const next = await bootstrapAuth();
      setState(next);
      setError(next.error ?? null);
      setPhase(next.authenticated ? "ready" : "need-login");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setPhase("need-login");
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  // 令牌被清空（例如收到 401 或用户登出）时回到登录页
  useEffect(
    () =>
      subscribeToken(() => {
        void bootstrapAuth().then((next) => {
          setState(next);
          if (!next.authenticated) setPhase("need-login");
        });
      }),
    [],
  );

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    const next = await login(password);
    setBusy(false);
    setState(next);
    if (next.authenticated) {
      setPassword("");
      setError(null);
      setPhase("ready");
    } else {
      setError(next.error ?? "访问口令不正确。");
    }
  };

  if (phase === "checking") {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950 text-slate-400">
        正在检查访问权限…
      </div>
    );
  }

  if (phase === "need-login") {
    return (
      <div className="flex h-full items-center justify-center bg-slate-950 p-4">
        <form
          onSubmit={submit}
          className="w-full max-w-sm rounded-xl border border-slate-800 bg-slate-900/60 p-6 shadow-lg"
        >
          <h1 className="text-lg font-semibold text-slate-100">A 股量化平台</h1>
          <p className="mt-1 text-xs text-slate-400">
            本机访问免密；局域网设备需要访问口令（启动器窗口可查看）。
          </p>
          <label className="mt-5 block text-xs text-slate-400" htmlFor="access-password">
            访问口令
          </label>
          <input
            id="access-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none focus:border-sky-500"
            placeholder="ACCESS_PASSWORD"
          />
          {error ? <p className="mt-2 text-xs text-rose-400">{error}</p> : null}
          <button
            type="submit"
            disabled={busy || !password}
            className="mt-4 w-full rounded-md bg-sky-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "登录中…" : "进入"}
          </button>
          <button
            type="button"
            onClick={() => void check()}
            className="mt-2 w-full rounded-md border border-slate-700 px-3 py-2 text-xs text-slate-300"
          >
            重试（本机免密）
          </button>
          <p className="mt-4 text-[11px] leading-relaxed text-slate-500">
            ⚠️ 分析结果仅用于研究，不构成投资建议。实盘接口默认关闭。
          </p>
        </form>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-950">
      {state?.authRequired ? (
        <div className="flex items-center justify-end gap-2 border-b border-slate-800 bg-slate-950 px-3 py-1 text-[11px] text-slate-500 sm:px-6">
          <span>已通过访问校验</span>
          <button
            type="button"
            onClick={() => void logout().then(() => setPhase("need-login"))}
            className="rounded border border-slate-700 px-2 py-0.5 text-slate-400 hover:text-slate-200"
          >
            退出
          </button>
        </div>
      ) : null}
      <div className="min-h-0 flex-1">{children}</div>
    </div>
  );
}
