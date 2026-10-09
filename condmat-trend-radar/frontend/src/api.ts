const API_BASE = import.meta.env.VITE_API_BASE ?? '';
const cache = new Map<string, { expiresAt: number; value: unknown }>();

export class ApiError extends Error {
  status: number;
  code: 'offline' | 'permission' | 'server' | 'cancelled';

  constructor(message: string, status = 0, code: ApiError['code'] = 'server') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function safeMessage(status: number, fallback: string) {
  if (status === 401 || status === 403) return '当前操作没有权限。';
  if (status === 404) return '请求的数据不存在或接口尚未接入。';
  if (status >= 500) return '服务暂时不可用，请稍后重试。';
  return fallback || '请求失败，请重试。';
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...init.headers },
    });
    if (!response.ok) {
      let detail = '';
      try {
        const payload = await response.json() as { detail?: string };
        detail = typeof payload.detail === 'string' && payload.detail.length < 180 ? payload.detail : '';
      } catch {
        detail = '';
      }
      throw new ApiError(
        safeMessage(response.status, detail),
        response.status,
        response.status === 401 || response.status === 403 ? 'permission' : 'server',
      );
    }
    return response.json() as Promise<T>;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('请求已取消。', 0, 'cancelled');
    }
    throw new ApiError('无法连接本地 API，请确认服务正在运行。', 0, 'offline');
  }
}

export async function apiGet<T>(path: string, options: { signal?: AbortSignal; cacheMs?: number; force?: boolean } = {}): Promise<T> {
  const key = `${API_BASE}${path}`;
  const cached = cache.get(key);
  if (!options.force && cached && cached.expiresAt > Date.now()) return cached.value as T;
  const value = await request<T>(path, { signal: options.signal });
  if ((options.cacheMs ?? 0) > 0) cache.set(key, { value, expiresAt: Date.now() + (options.cacheMs ?? 0) });
  return value;
}

export async function apiPostJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const value = await request<T>(path, {
    method: 'POST',
    signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  cache.clear();
  return value;
}

export async function apiDelete<T>(path: string, signal?: AbortSignal): Promise<T> {
  const value = await request<T>(path, { method: 'DELETE', signal });
  cache.clear();
  return value;
}

export const apiUrl = (path: string) => `${API_BASE}${path}`;
export const clearApiCache = () => cache.clear();
