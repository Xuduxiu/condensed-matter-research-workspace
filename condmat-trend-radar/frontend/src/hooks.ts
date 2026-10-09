import { DependencyList, useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from './api';

export function useDebouncedValue<T>(value: T, delay = 320) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function useApiData<T>(loader: (signal: AbortSignal) => Promise<T>, deps: DependencyList) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const refreshRef = useRef(0);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => {
    refreshRef.current += 1;
    setRevision(refreshRef.current);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loader(controller.signal)
      .then((value) => {
        setData(value);
        setUpdatedAt(new Date());
      })
      .catch((reason: unknown) => {
        if (reason instanceof ApiError && reason.code === 'cancelled') return;
        setError(reason instanceof ApiError ? reason : new ApiError('数据加载失败。'));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, revision]);

  return { data, error, loading, updatedAt, refresh };
}

export function formatNumber(value: number | null | undefined) {
  return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value);
}

export function formatBytes(value: number | null | undefined) {
  if (value == null) return '—';
  if (value < 1024) return `${value} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[index]}`;
}

export function formatDate(value: string | null | undefined, includeTime = false) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, includeTime ? 19 : 10);
  return new Intl.DateTimeFormat('zh-CN', includeTime
    ? { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}
