// Thin client for /api/v1. Every value shown in the UI comes from here - no placeholder data.

const TOKEN_KEY = "ydv.token";

export function getToken(): string | null {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(t: string | null): void {
  try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* storage unavailable */ }
}

export const clearToken = (): void => setToken(null);

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export async function api<T = unknown>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}`, {
    method: init.method ?? "GET", headers, credentials: "omit", cache: "no-store",
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (res.status === 204) return undefined as T;
  let data: unknown = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    if (res.status === 401) { clearToken(); window.dispatchEvent(new Event("ydv:unauthenticated")); }
    const err = (data as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(res.status, err?.code ?? "error", err?.message ?? res.statusText);
  }
  return data as T;
}
