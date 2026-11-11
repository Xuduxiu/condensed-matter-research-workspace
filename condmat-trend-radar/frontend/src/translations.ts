import { ApiError } from './api';

export const translationApi = '/api/library/translations';
export const translationUpdated = 'paper-desk:translation-updated';

export type TranslationScope = 'recent' | 'selected' | 'all';
export type TranslationRequest = {
  scope: TranslationScope;
  limit?: number;
  version_ids?: string[];
  batch_size: number;
  max_input_chars: number;
};
export type TranslationEstimate = {
  total: number;
  cached: number;
  skipped: number;
  needs_translation: number;
  input_chars: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  estimated_requests: number;
  batch_size: number;
  max_input_chars: number;
  oversized?: number;
  unique_inputs?: number;
  projection_100k?: { based_on_versions: number; estimated_requests: number; estimated_input_tokens: number; estimated_output_tokens: number } | null;
};
export type LanguageJob = {
  id: string;
  status: 'queued' | 'running' | 'paused' | 'completed' | 'partial';
  total: number;
  completed: number;
  failed: number;
  pending: number;
  running: number;
  skipped: number;
  error: string | null;
};
export type VersionTranslation = {
  version_id: string;
  source_hash: string;
  status: 'missing' | 'queued' | 'running' | 'ready' | 'failed' | 'skipped';
  title_zh: string | null;
  abstract_zh: string | null;
  error: string | null;
};
export type AutomaticTranslationStatus = {
  enabled: boolean;
  blocked: boolean;
  error: string | null;
  job_id: string | null;
  batch_size: number;
  max_input_chars: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  skipped: number;
  total: number;
  dirty?: number;
};

export function translationError(error: unknown) {
  if (error instanceof ApiError && error.status === 503) return '翻译服务暂不可用。请在 DSH 中确认当前模型已连接，再重试。';
  return error instanceof Error ? error.message : '读取翻译服务失败。';
}

export function jobIsActive(job: LanguageJob) {
  return job.status === 'queued' || job.status === 'running';
}
