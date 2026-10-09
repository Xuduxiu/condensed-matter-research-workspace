import { FormEvent, ReactNode, useEffect, useRef } from 'react';
import { formatNumber } from '../hooks';
import { getAbstractBackfillProgress, getSourceBackfillProgress, sourceDisplayName } from '../runProgress';
import { DailyRun, Tone } from '../types';

export function StatusBadge({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}

export function MetricStrip({ items }: { items: Array<{ label: string; value: number | string | null | undefined; note?: string; tone?: Tone }> }) {
  return <section className="metric-strip" aria-label="关键指标">{items.map((item) => (
    <div className={`metric-cell ${item.tone ?? 'neutral'}`} key={item.label}>
      <span>{item.label}</span><strong>{typeof item.value === 'number' ? formatNumber(item.value) : item.value ?? '—'}</strong>{item.note && <small>{item.note}</small>}
    </div>
  ))}</section>;
}

export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="filter-bar">{children}</div>;
}

export function SearchInput({ value, onChange, onSubmit, placeholder = '搜索论文标题、摘要、作者、材料…', disabled = false }: {
  value: string; onChange: (value: string) => void; onSubmit?: () => void; placeholder?: string; disabled?: boolean;
}) {
  const submit = (event: FormEvent) => { event.preventDefault(); onSubmit?.(); };
  return <form className="search-input" role="search" onSubmit={submit}>
    <span aria-hidden="true">⌕</span>
    <input value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} aria-label={placeholder} />
    {value && <button type="button" aria-label="清空搜索" onClick={() => onChange('')}>×</button>}
    <kbd>Enter</kbd>
  </form>;
}

export function EmptyState({ title = '暂无数据', message = '当前条件下没有可显示的记录。', action }: { title?: string; message?: string; action?: ReactNode }) {
  return <div className="state-panel empty-state"><span className="state-symbol">∅</span><strong>{title}</strong><p>{message}</p>{action}</div>;
}

export function ErrorState({ title = '数据加载失败', message, onRetry, offline = false }: { title?: string; message: string; onRetry?: () => void; offline?: boolean }) {
  return <div className="state-panel error-state" role="alert"><span className="state-symbol">{offline ? '⌁' : '!'}</span><strong>{title}</strong><p>{message}</p>{onRetry && <button className="button secondary" type="button" onClick={onRetry}>重试</button>}</div>;
}

export function LoadingSkeleton({ rows = 6 }: { rows?: number }) {
  return <div className="loading-skeleton" aria-label="正在加载" aria-busy="true">{Array.from({ length: rows }, (_, index) => <div key={index}><i /><span /><em /></div>)}</div>;
}

export function DownloadStatus({ downloaded, status, extractionStatus }: { downloaded?: boolean; status?: string | null; extractionStatus?: string | null }) {
  if (downloaded) return <StatusBadge tone={extractionStatus === 'completed' ? 'success' : 'accent'}>{extractionStatus === 'completed' ? '全文已解析' : 'PDF 已下载'}</StatusBadge>;
  const map: Record<string, [string, Tone]> = {
    pending: ['待下载', 'warning'], resolving: ['解析来源', 'warning'], downloading: ['下载中', 'accent'],
    retryable_failed: ['可重试失败', 'danger'], permanent_failed: ['下载失败', 'danger'], manual_review: ['待人工复核', 'warning'],
  };
  const [label, tone] = map[status ?? ''] ?? ['仅元数据', 'neutral'];
  return <StatusBadge tone={tone}>{label}</StatusBadge>;
}

export function VersionBadge({ type }: { type?: string | null }) {
  return type === 'preprint'
    ? <StatusBadge tone="preprint">预印本</StatusBadge>
    : type === 'publication' || type === 'published'
      ? <StatusBadge tone="publication">正式见刊</StatusBadge>
      : <StatusBadge>{type || '未提供'}</StatusBadge>;
}

export function TrendIndicator({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="trend neutral">—</span>;
  const direction = value > 0 ? 'up' : value < 0 ? 'down' : 'neutral';
  return <span className={`trend ${direction}`}>{value > 0 ? '↗' : value < 0 ? '↘' : '→'} {value > 0 ? '+' : ''}{value}</span>;
}

export function RunSummary({ run }: { run?: DailyRun | null }) {
  if (!run) return <EmptyState title="尚无运行记录" message="每日任务已有接口，但当前数据库没有可展示的运行历史。" />;
  const report = run.report ?? (() => { try { return JSON.parse(run.report_json ?? '{}') as Record<string, unknown>; } catch { return {}; } })();
  const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const ingest = asRecord(report.ingest);
  const progressCounts = asRecord(report.counts);
  const ingestCounts = asRecord(ingest.counts);
  const downloads = asRecord(report.downloads);
  const backfill = getSourceBackfillProgress(report);
  const abstractBackfill = getAbstractBackfillProgress(report);
  const downloadResults = Array.isArray(downloads.results) ? downloads.results.map(asRecord) : [];
  const ingestErrors = Array.isArray(ingest.errors) ? ingest.errors.length : 0;
  const backfillFailures = (backfill.failed ?? 0) + (backfill.identityConflicts ?? 0);
  const abstractFailures = abstractBackfill.failed ?? 0;
  const downloadFailures = downloadResults.filter((item) => ['retryable_failed', 'permanent_failed', 'manual_review'].includes(String(item.status ?? ''))).length;
  const downloadSuccesses = downloadResults.filter((item) => item.status === 'completed').length;
  const fetching = report.stage === 'fetching_sources';
  const backfilling = report.stage === 'backfilling_missing_sources';
  const abstracting = report.stage === 'backfilling_abstracts';
  const backfillProgress = backfill.requested == null
    ? backfill.processed
    : `${backfill.processed ?? 0} / ${backfill.requested}`;
  const abstractProgress = abstractBackfill.total == null
    ? abstractBackfill.processed
    : `${abstractBackfill.processed ?? 0} / ${abstractBackfill.total}`;
  const values: Array<[string, unknown]> = backfilling
    ? [
      ['当前 / 刚处理来源', sourceDisplayName(backfill.currentSource)],
      ['已处理 / 本批', backfillProgress],
      ['补齐成功', backfill.completed],
      ['精确记录未找到', backfill.notFound],
      ['补齐失败', backfill.failed],
      ['身份冲突', backfill.identityConflicts],
      ['元数据有变更', backfill.metadataChanged],
      ['本批剩余', backfill.queueRemaining],
      ['子进度', backfill.percent == null ? undefined : `${Math.round(backfill.percent)}%`],
    ]
    : abstracting
      ? [
        ['当前 / 刚处理来源', sourceDisplayName(abstractBackfill.currentSource)],
        ['当前论文 ID', abstractBackfill.currentPaperId],
        ['已处理 / 本批', abstractProgress],
        ['摘要补全', abstractBackfill.enriched],
        ['摘要未找到', abstractBackfill.notFound],
        ['摘要失败', abstractBackfill.failed],
        ['本批剩余', abstractBackfill.queueRemaining],
        ['子进度', abstractBackfill.percent == null ? undefined : `${Math.round(abstractBackfill.percent)}%`],
      ]
      : fetching
        ? [
          ['当前来源', report.source_label ?? report.source],
          ['当前页', report.page],
          ['已抓取', progressCounts.fetched ?? report.fetched],
          ['符合范围（源记录）', progressCounts.eligible ?? report.eligible],
          ['待资格复核', progressCounts.review_candidates ?? report.review_candidates],
          ['新增入库', progressCounts.inserted ?? report.inserted ?? report.kept],
          ['元数据更新', progressCounts.updated ?? report.updated],
          ['去重 / 无变化', progressCounts.deduped ?? report.deduped],
          ['扫描进度', report.percent == null ? undefined : String(report.percent) + '%'],
          ['扫描窗口', report.date_from && report.date_to ? String(report.date_from) + ' → ' + String(report.date_to) : undefined],
        ]
        : [
          ['已抓取', ingestCounts.fetched ?? ingest.fetched ?? ingest.fetched_count ?? report.fetched],
          ['符合范围（源记录）', ingestCounts.eligible ?? ingest.eligible ?? ingest.eligible_count ?? report.eligible],
          ['待资格复核', ingestCounts.review_candidates ?? ingest.review_candidates ?? ingest.review_candidates_count ?? report.review_candidates],
          ['新增入库', ingestCounts.inserted ?? ingest.inserted ?? ingest.inserted_count ?? report.inserted ?? report.kept],
          ['元数据更新', ingestCounts.updated ?? ingest.updated ?? ingest.updated_count ?? report.updated],
          ['去重 / 无变化', ingestCounts.deduped ?? ingest.deduped ?? ingest.deduped_count ?? report.deduped],
          ['来源补齐处理', backfill.available ? backfillProgress : undefined],
          ['来源补齐成功', backfill.completed],
          ['精确记录未找到', backfill.notFound],
          ['来源补齐失败', backfill.failed],
          ['补齐身份冲突', backfill.identityConflicts],
          ['摘要处理', abstractBackfill.available ? abstractProgress : undefined],
          ['摘要补全', abstractBackfill.enriched],
          ['摘要未找到', abstractBackfill.notFound],
          ['摘要失败', abstractBackfill.failed],
          ['下载成功', report.downloaded ?? report.download_success ?? (downloadResults.length ? downloadSuccesses : undefined)],
          ['异常数', report.errors ?? report.error_count ?? (ingestErrors + backfillFailures + abstractFailures + downloadFailures)],
        ];
  const note = backfilling
    ? backfill.skipped
      ? '本次运行跳过联网来源补齐。'
      : '这里只补齐同一论文在缺失数据源中的精确记录；“未找到”与网络/解析失败分开计数，身份冲突不会自动合并。'
    : abstracting
      ? '摘要补全按论文逐条请求；“未找到”表示已完成检索但无摘要，“失败”表示网络或解析异常，可与成功补全数直接核对本批处理量。'
      : fetching
        ? '“符合范围”按来源记录累计，可能含跨来源重复；“新增入库”才是新建唯一论文，旧 kept 字段仅为该项兼容别名。'
        : run.error_message
          ? '本次运行记录了错误；详情已在系统页脱敏显示。'
          : '采集、来源补齐和摘要补全分别计数；零值与未提供严格区分，身份冲突不会自动合并。';
  return <div className="run-summary">
    <div className="run-head"><StatusBadge tone={run.status === 'completed' ? 'success' : run.status === 'failed' ? 'danger' : 'warning'}>{run.status}</StatusBadge><span>{run.trigger_type}</span></div>
    <div className="run-grid">{values.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value == null ? '—' : String(value)}</strong></div>)}</div>
    <p>{note}</p>
  </div>;
}
export function ConfirmDialog({ open, title, message, confirmLabel = '确认', danger = false, onConfirm, onCancel }: {
  open: boolean; title: string; message: string; confirmLabel?: string; danger?: boolean; onConfirm: () => void; onCancel: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (open) cancelRef.current?.focus(); }, [open]);
  if (!open) return null;
  return <div className="modal-layer" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onCancel(); }}>
    <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <h2 id="confirm-title">{title}</h2><p>{message}</p>
      <div><button ref={cancelRef} className="button ghost" type="button" onClick={onCancel}>取消</button><button className={`button ${danger ? 'danger' : 'primary'}`} type="button" onClick={onConfirm}>{confirmLabel}</button></div>
    </div>
  </div>;
}
