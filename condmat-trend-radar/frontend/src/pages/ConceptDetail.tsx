import { FormEvent, useEffect, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import TrendLine from '../components/TrendLine';
import PaperTable, { Paper } from '../components/PaperTable';
import { conceptClassLabel, statusDescription, statusLabel, Translation } from '../i18n';

type Smoothing = 'raw' | 'rolling3' | 'rolling6' | 'ewma';

type Lifecycle = {
  first_seen: string;
  historical_first_seen: string | null;
  trend_first_seen: string | null;
  peak_month: string;
  peak_value: number;
  half_life_months: number | null;
  active_duration_months: number;
  burst_duration_months: number;
  baseline_total_count: number;
  historical_count_before_trend: number;
  trend_total_count: number;
  novelty_score: number;
  concept_class: string;
  is_platform_term: number;
  status: string;
};

type ConceptData = {
  concept: string;
  lifecycle: null | Lifecycle;
  series: Array<{
    month: string;
    raw_freq: number;
    weighted_freq: number;
    normalized_share: number;
    weighted_normalized_share: number;
    momentum: number;
  }>;
  associated_materials: Array<{ term: string; count: number }>;
  associated_methods: Array<{ term: string; count: number }>;
  cooccurring_concepts: Array<{ term: string; count: number }>;
  journal_distribution: Array<{ journal: string; count: number }>;
};

type ExportResult = { ok: boolean; task_count: number; csv_path: string; json_path: string; manifest_path: string; message: string };

type Props = {
  concept: string;
  t: Translation;
  onConceptChange: (concept: string) => void;
};

const smoothingOptions: Smoothing[] = ['raw', 'rolling3', 'rolling6', 'ewma'];

export default function ConceptDetail({ concept, t, onConceptChange }: Props) {
  const [input, setInput] = useState(concept);
  const [smoothing, setSmoothing] = useState<Smoothing>('raw');
  const [data, setData] = useState<ConceptData | null>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [error, setError] = useState('');
  const [exportResult, setExportResult] = useState<ExportResult | null>(null);

  useEffect(() => {
    setInput(concept);
  }, [concept]);

  useEffect(() => {
    setError('');
    setData(null);
    const params = new URLSearchParams({ smoothing, scope: 'all' });
    apiGet<ConceptData>(`/api/concept/${encodeURIComponent(concept)}?${params.toString()}`)
      .then(setData)
      .catch((err) => setError(err.message));
    apiGet<{ items: Paper[] }>(`/api/concept/${encodeURIComponent(concept)}/papers`)
      .then((payload) => setPapers(payload.items))
      .catch(() => setPapers([]));
  }, [concept, smoothing]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (input.trim()) onConceptChange(input.trim());
  };

  const lifecycle = data?.lifecycle;

  const exportTasks = (highPriority: boolean) => {
    setExportResult(null);
    apiPostJson<ExportResult>('/api/integration/export_to_downloader', {
      concepts: [data?.concept ?? concept],
      mode: 'or',
      from: lifecycle?.historical_first_seen ?? '2015-01',
      to: '2026-07',
      scope: 'core',
      limit: highPriority ? 50 : 100,
      min_momentum: highPriority ? 5 : 0,
      dry_run: false,
    })
      .then(setExportResult)
      .catch((err) => setError(`${t.exportFailed}: ${err.message}`));
  };

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.conceptDetailTitle}</p>
          <h2>{data?.concept ?? concept}</h2>
        </div>
        <div className="control-stack">
          <form className="search-form" onSubmit={submit}>
            <input value={input} onChange={(event) => setInput(event.target.value)} aria-label={t.conceptFilter} />
            <button>{t.open}</button>
          </form>
          <div className="control-row" aria-label={t.smoothing}>
            {smoothingOptions.map((item) => (
              <button key={item} className={`segmented ${smoothing === item ? 'active' : ''}`} onClick={() => setSmoothing(item)}>
                {smoothingLabel(item, t)}
              </button>
            ))}
          </div>
          <div className="control-row">
            <button className="utility-button" type="button" onClick={() => exportTasks(false)}>{t.exportDownloadTasks}</button>
            <button className="utility-button" type="button" onClick={() => exportTasks(true)}>{t.highPriorityExport}</button>
          </div>
        </div>
      </header>
      {error && <div className="error-panel">{t.error}: {error}</div>}
      {exportResult && <div className="success-panel">{exportResult.message} {t.taskCount}: {exportResult.task_count}<br />{t.exportedPath}: <code>{exportResult.csv_path}</code></div>}
      {data && (
        <>
          <section className="explain-panel lifecycle-evidence">
            <h3>{conceptCategory(data.concept, lifecycle, t)}</h3>
            <p>{conceptNarrative(data.concept, lifecycle, t)}</p>
          </section>
          <section className="stats-row">
            <LifecycleStat label={t.status} value={statusLabel(lifecycle?.status, t)} detail={statusDescription(lifecycle?.status, t)} />
            <LifecycleStat label={t.conceptClass} value={conceptClassLabel(lifecycle?.concept_class, t)} />
            <LifecycleStat label={t.historicalFirstSeen} value={lifecycle?.historical_first_seen ?? t.none} />
            <LifecycleStat label={t.trendFirstSeen} value={lifecycle?.trend_first_seen ?? t.none} />
            <LifecycleStat label={t.baselineTotalCount} value={lifecycle?.baseline_total_count ?? 0} />
            <LifecycleStat label={t.historicalCountBeforeTrend} value={lifecycle?.historical_count_before_trend ?? 0} />
            <LifecycleStat label={t.trendTotalCount} value={lifecycle?.trend_total_count ?? 0} />
            <LifecycleStat label={t.noveltyScore} value={formatScore(lifecycle?.novelty_score)} detail={`${t.platformFlag}: ${lifecycle?.is_platform_term ? t.yes : t.no}`} />
          </section>
          <TrendLine series={data.series} title={`${data.concept} ${t.trend}`} t={t} />
          <section className="detail-grid">
            <MiniList title={t.associatedMaterials} items={data.associated_materials} onClick={onConceptChange} t={t} />
            <MiniList title={t.associatedMethods} items={data.associated_methods} t={t} />
            <MiniList title={t.cooccurringConcepts} items={data.cooccurring_concepts} onClick={onConceptChange} t={t} />
            <MiniList title={t.journalDistribution} items={data.journal_distribution.map((item) => ({ term: item.journal, count: item.count }))} t={t} />
          </section>
          <section className="panel">
            <h3>{t.representativePapers}</h3>
            <PaperTable papers={papers} t={t} onConceptSelect={onConceptChange} />
          </section>
        </>
      )}
    </div>
  );
}

function smoothingLabel(value: Smoothing, t: Translation) {
  return { raw: t.raw, rolling3: t.rolling3, rolling6: t.rolling6, ewma: t.ewma }[value];
}

function conceptCategory(concept: string, lifecycle: Lifecycle | null | undefined, t: Translation) {
  if (!lifecycle) return concept;
  if (lifecycle.is_platform_term || lifecycle.concept_class === 'platform_material') return `${concept}: ${t.longPlatformMaterial}`;
  if (lifecycle.status === 'emerging') return `${concept}: ${t.emergingConcept}`;
  if (lifecycle.status === 'cooling' || lifecycle.status === 'stale') return `${concept}: ${t.revivalConcept}`;
  if (lifecycle.historical_count_before_trend > 5) return `${concept}: ${t.historicalConcept}`;
  return `${concept}: ${t.activeConcept}`;
}

function conceptNarrative(concept: string, lifecycle: Lifecycle | null | undefined, t: Translation) {
  if (!lifecycle) return `${concept}: ${t.noData}`;
  if (lifecycle.is_platform_term || lifecycle.concept_class === 'platform_material') return `${concept} ${t.platformTermExplanation}`;
  if (lifecycle.status === 'emerging') return `${concept} ${t.emergingTermExplanation}`;
  if (lifecycle.status === 'cooling' || lifecycle.status === 'stale') return `${concept} ${t.revivalTermExplanation}`;
  if (lifecycle.historical_count_before_trend > 5 || lifecycle.baseline_total_count >= 20) return `${concept} ${t.historicalTermExplanation}`;
  return `${concept} ${t.activeTermExplanation}`;
}

function LifecycleStat({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return (
    <section className="stat-card compact">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </section>
  );
}

function formatScore(value: number | null | undefined) {
  return Number(value ?? 0).toFixed(3);
}

function MiniList({
  title,
  items,
  onClick,
  t,
}: {
  title: string;
  items: Array<{ term: string; count: number }>;
  onClick?: (term: string) => void;
  t: Translation;
}) {
  return (
    <section className="panel">
      <h3>{title}</h3>
      <div className="rank-list dense">
        {items.length === 0 && <div className="empty-state">{t.noData}</div>}
        {items.slice(0, 12).map((item) => (
          <button key={`${title}-${item.term}`} className="rank-row" onClick={() => onClick?.(item.term)}>
            <span className="rank-term">{item.term}</span>
            <span className="rank-value">{item.count}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
