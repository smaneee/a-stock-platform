/** 访问令牌管理（局域网访问控制 P0-04）。
 *
 * 三种情况：
 *  1. 后端未开启鉴权（`REQUIRE_AUTH=false`，仅本机开发）→ 直接可用，无感。
 *  2. 本机浏览器 + 已开启鉴权 → 调 `/api/auth/local-token` 免密换令牌，仍是无感启动。
 *  3. 手机等局域网设备 → 必须输入访问口令（由启动器/后端 ACCESS_PASSWORD 提供）。
 *
 * 令牌只存在浏览器内存与 localStorage 里；后端不落库、不写日志。
 */
const STORAGE_KEY = "astock.session.token";

export interface AuthState {
  authRequired: boolean;
  authenticated: boolean;
  expiresAt: number | null;
  error?: string;
}

let token: string | null = null;
try {
  token = window.localStorage.getItem(STORAGE_KEY);
} catch {
  token = null;
}

const listeners = new Set<() => void>();

export function getToken(): string | null {
  return token;
}

export function setToken(next: string | null, expiresAt?: number | null): void {
  token = next;
  try {
    if (next) {
      window.localStorage.setItem(STORAGE_KEY, next);
      if (expiresAt) {
        window.localStorage.setItem(`${STORAGE_KEY}.exp`, String(expiresAt));
      }
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
      window.localStorage.removeItem(`${STORAGE_KEY}.exp`);
    }
  } catch {
    // 隐私模式下 localStorage 不可用：退化为仅内存保存
  }
  listeners.forEach((fn) => fn());
}

/** 订阅令牌变化（登录/登出后需要重新拉数据）。 */
export function subscribeToken(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 附加到每个请求上的鉴权头；无令牌时返回空对象。 */
export function authHeaders(): Record<string, string> {
  return token ? { "X-Auth-Token": token } : {};
}

/** 供 WebSocket 使用的 query 片段。 */
export function authQuery(): string {
  return token ? `?token=${encodeURIComponent(token)}` : "";
}

async function readJson(resp: Response): Promise<Record<string, unknown>> {
  try {
    return (await resp.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** 启动时确定鉴权状态：优先复用有效令牌，其次尝试本机免密令牌。 */
export async function bootstrapAuth(): Promise<AuthState> {
  const session = await fetch("/api/auth/session", { headers: authHeaders() });
  if (!session.ok) {
    return {
      authRequired: true,
      authenticated: false,
      expiresAt: null,
      error: `无法读取鉴权状态（HTTP ${session.status}）`,
    };
  }
  const body = await readJson(session);
  const authRequired = Boolean(body.auth_required);
  if (!authRequired) {
    return { authRequired: false, authenticated: true, expiresAt: null };
  }
  if (body.authenticated) {
    return {
      authRequired: true,
      authenticated: true,
      expiresAt: (body.expires_at as number | null) ?? null,
    };
  }

  // 本机浏览器免密路径
  const local = await fetch("/api/auth/local-token", { headers: authHeaders() });
  if (local.ok) {
    const data = await readJson(local);
    if (typeof data.token === "string") {
      setToken(data.token, (data.expires_at as number | null) ?? null);
      return {
        authRequired: true,
        authenticated: true,
        expiresAt: (data.expires_at as number | null) ?? null,
      };
    }
  }
  return {
    authRequired: true,
    authenticated: false,
    expiresAt: null,
    error:
      local.status === 403
        ? "此设备不在本机白名单内，请输入访问口令。"
        : undefined,
  };
}

export async function login(password: string): Promise<AuthState> {
  const resp = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const body = await readJson(resp);
  if (!resp.ok) {
    return {
      authRequired: true,
      authenticated: false,
      expiresAt: null,
      error: typeof body.detail === "string" ? body.detail : `登录失败（HTTP ${resp.status}）`,
    };
  }
  if (!body.auth_required) {
    return { authRequired: false, authenticated: true, expiresAt: null };
  }
  const next = typeof body.token === "string" ? body.token : null;
  setToken(next, (body.expires_at as number | null) ?? null);
  return {
    authRequired: true,
    authenticated: Boolean(next),
    expiresAt: (body.expires_at as number | null) ?? null,
  };
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", { method: "POST", headers: authHeaders() });
  } catch {
    // 网络失败也要本地登出
  }
  setToken(null);
}
