import { DailyRun } from './types';

const terminalRunStatuses = new Set(['completed', 'partial', 'failed', 'cancelled']);

export function isTerminalDailyRunStatus(status: unknown) {
  return terminalRunStatuses.has(String(status ?? '').toLowerCase());
}

export function newestDailyRun(...runs: Array<DailyRun | null | undefined>) {
  return runs
    .filter((run): run is DailyRun => Boolean(run))
    .sort((left, right) => Date.parse(right.started_at || '') - Date.parse(left.started_at || ''))[0] ?? null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function firstNumber(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string' && value.trim() !== '') {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return undefined;
}

function firstString(...values: unknown[]) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return undefined;
}

export type SourceBackfillProgress = {
  available: boolean;
  skipped: boolean;
  requested?: number;
  processed?: number;
  completed?: number;
  notFound?: number;
  failed?: number;
  identityConflicts?: number;
  metadataChanged?: number;
  queueRemaining?: number;
  currentSource?: string;
  currentStatus?: string;
  percent?: number;
};

/** Normalize final and streaming source-backfill report shapes. */
export function getSourceBackfillProgress(reportValue: unknown): SourceBackfillProgress {
  const report = asRecord(reportValue);
  const summary = asRecord(report.source_backfill);
  const live = asRecord(report.source_backfill_progress);
  const summaryCounts = asRecord(summary.counts);
  const liveCounts = asRecord(live.counts);
  const merged = { ...summary, ...summaryCounts, ...live, ...liveCounts };
  const stageIsBackfill = report.stage === 'backfilling_missing_sources';
  const available = stageIsBackfill || Object.keys(summary).length > 0 || Object.keys(live).length > 0;

  const completed = firstNumber(merged.completed, merged.completed_count, merged.success, merged.succeeded);
  const notFound = firstNumber(merged.not_found, merged.notFound, merged.not_found_count);
  const failed = firstNumber(merged.failed, merged.failed_count, merged.retryable_failed);
  const identityConflicts = firstNumber(merged.identity_conflicts, merged.identity_conflict, merged.conflicts);
  const explicitProcessed = firstNumber(merged.processed, merged.processed_count, merged.attempted);
  const outcomeProcessed = [completed, notFound, failed, identityConflicts]
    .reduce<number>((total, value) => total + (value ?? 0), 0);
  const processed = explicitProcessed ?? (available ? outcomeProcessed : undefined);
  const requested = firstNumber(merged.requested, merged.total, merged.total_count, merged.selected);
  const explicitPercent = firstNumber(merged.percent, merged.progress_percent);
  const percent = explicitPercent != null
    ? Math.max(0, Math.min(100, explicitPercent))
    : requested != null && requested > 0 && processed != null
      ? Math.max(0, Math.min(100, (processed / requested) * 100))
      : undefined;
  const changedIds = Array.isArray(merged.canonical_paper_ids) ? merged.canonical_paper_ids.length : undefined;

  return {
    available,
    skipped: merged.skipped === true,
    requested,
    processed,
    completed,
    notFound,
    failed,
    identityConflicts,
    metadataChanged: firstNumber(merged.metadata_changed, merged.metadata_changed_count, merged.changed, changedIds),
    queueRemaining: firstNumber(merged.queue_remaining, merged.remaining, merged.remaining_count, merged.pending),
    currentSource: firstString(merged.current_source, merged.source, merged.target_source),
    currentStatus: firstString(merged.current_status, merged.item_status, merged.status),
    percent,
  };
}

export type AbstractBackfillProgress = {
  available: boolean;
  total?: number;
  processed?: number;
  enriched?: number;
  notFound?: number;
  failed?: number;
  queueRemaining?: number;
  currentPaperId?: string;
  currentSource?: string;
  percent?: number;
};

export function getAbstractBackfillProgress(reportValue: unknown): AbstractBackfillProgress {
  const report = asRecord(reportValue);
  const abstracts = asRecord(report.abstracts);
  const live = asRecord(report.abstract_backfill_progress);
  const merged = { ...abstracts, ...asRecord(abstracts.counts), ...live, ...asRecord(live.counts) };
  const available = report.stage === 'backfilling_abstracts' || Object.keys(abstracts).length > 0 || Object.keys(live).length > 0;
  const results = Array.isArray(merged.results) ? merged.results : [];
  const total = firstNumber(merged.total, merged.requested, merged.total_count);
  const processed = firstNumber(merged.processed, merged.processed_count, results.length);
  const explicitPercent = firstNumber(merged.percent, merged.progress_percent);
  const percent = explicitPercent != null
    ? Math.max(0, Math.min(100, explicitPercent))
    : total != null && total > 0 && processed != null
      ? Math.max(0, Math.min(100, (processed / total) * 100))
      : undefined;

  return {
    available,
    total,
    processed,
    enriched: firstNumber(merged.enriched, merged.enriched_count),
    notFound: firstNumber(merged.not_found, merged.notFound, merged.not_found_count),
    failed: firstNumber(merged.failed, merged.failed_count),
    queueRemaining: firstNumber(merged.queue_remaining, merged.remaining, merged.remaining_count),
    currentPaperId: firstString(merged.current_paper_id, merged.paper_id, merged.canonical_paper_id),
    currentSource: firstString(merged.current_source, merged.source),
    percent,
  };
}

export function sourceDisplayName(source: string | undefined) {
  const normalized = String(source ?? '').toLowerCase();
  if (normalized === 'openalex') return 'OpenAlex';
  if (normalized === 'crossref') return 'Crossref';
  if (normalized === 'arxiv') return 'arXiv';
  if (normalized === 'semantic_scholar') return 'Semantic Scholar';
  if (normalized === 'resolver') return '多源解析器';
  if (normalized === 'local') return '本地记录';
  if (normalized === 'all_sources') return '全部来源';
  return source || undefined;
}
