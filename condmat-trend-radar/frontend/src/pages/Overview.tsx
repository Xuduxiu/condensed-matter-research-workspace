import { useEffect, useState } from 'react';
import { apiGet, apiPostJson, exportUrl } from '../api';
import StatCard from '../components/StatCard';
import { scopeLabel, Translation } from '../i18n';

type Scope = 'core' | 'core_context' | 'all';
type CorpusPreset = 'all_real' | 'strict_condmat' | 'core_all' | 'mock';
type IngestTask = { task_id: string; status: 'running' | 'finished' | 'failed'; fetched_count: number; kept_count: number; source_breakdown: Record<string, number>; error_summary?: string; log_path?: string };
type ChunkCounts = { completed: number; failed: number; pending: number; running: number; skipped: number; total: number };
type FullCorpusError = { journal?: string; date_from?: string; date_to?: string; error_summary?: string; error_summary_brief?: string; log_path?: string };
type FullCorpusStatus = {
  real_paper_count: number;
  completed_chunks: number;
  failed_chunks: number;
  pending_chunks: number;
  running_chunks: number;
  latest_report_path?: string;
  current_run_id?: string;
  current_run?: null | { status?: string };
  current_run_chunks?: ChunkCounts;
  all_chunks?: ChunkCounts;
  last_error?: null | FullCorpusError;
};
type Ranked = { term: string; momentum?: number; raw_freq?: number; growth?: number; count?: number; normalized_share?: number; weighted_normalized_share?: number };
type WindowInfo = { baseline_from: string; baseline_to: string; trend_from: string; trend_to: string; scope: Scope };
type OverviewData = {
  total_papers: number;
  latest_month: string;
  this_month_papers: number;
  window: WindowInfo;
  data_source: { is_mock: boolean; label: string; data_mode: string; corpus_preset?: CorpusPreset; sources: Array<{ source: string; data_mode: string; n: number }> };
  corpus_size: { database_path: string; total_papers: number; active_paper_count: number; real_paper_count: number; strict_condmat_paper_count: number; excluded_non_condmat_paper_count: number; mock_paper_count: number; current_corpus_preset: CorpusPreset; current_corpus_label: string; latest_quality_report_path?: string | null; concept_count: number; material_count: number; method_count: number; estimated_full_corpus: number | null };
  hot_concepts: Ranked[];
  rising_concepts: Ranked[];
  cooling_concepts: Ranked[];
  top_materials: Ranked[];
  top_methods: Ranked[];
  journal_distribution: Array<{ journal: string; count: number }>;
  source_distribution: Array<{ source: string; count: number }>;
};

type Props = { t: Translation; onConceptSelect: (concept: string) => void };

const scopeOptions: Scope[] = ['core', 'core_context', 'all'];
const corpusPresetOptions: CorpusPreset[] = ['strict_condmat', 'all_real', 'core_all', 'mock'];

export default function Overview({ t, onConceptSelect }: Props) {
  const [scope, setScope] = useState<Scope>('core');
  const [corpusPreset, setCorpusPreset] = useState<CorpusPreset>('strict_condmat');
  const [data, setData] = useState<OverviewData | null>(null);
  const [error, setError] = useState('');
  const [ingestTask, setIngestTask] = useState<IngestTask | null>(null);
  const [fullStatus, setFullStatus] = useState<FullCorpusStatus | null>(null);

  useEffect(() => {
    setError('');
    apiGet<OverviewData>(`/api/overview?scope=${scope}&corpus_preset=${corpusPreset}`)
      .then((payload) => {
        setData(payload);
        setCorpusPreset(payload.data_source.corpus_preset || payload.corpus_size.current_corpus_preset || 'strict_condmat');
      })
      .catch((err) => setError(err.message));
  }, [scope, corpusPreset]);

  useEffect(() => {
    if (!ingestTask || ingestTask.status !== 'running') return;
    const timer = window.setInterval(() => {
      apiGet<IngestTask>(`/api/ingest/tasks/${ingestTask.task_id}`)
        .then((task) => {
          setIngestTask(task);
          if (task.status === 'finished') {
            apiGet<OverviewData>(`/api/overview?scope=${scope}&corpus_preset=strict_condmat`).then((payload) => {
              setData(payload);
              setCorpusPreset('strict_condmat');
            });
          }
        })
        .catch((err) => setError(err.message));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [ingestTask?.task_id, ingestTask?.status, scope]);


  useEffect(() => {
    let cancelled = false;
    const loadStatus = () => {
      apiGet<FullCorpusStatus>('/api/ingest/status')
        .then((status) => {
          if (!cancelled) setFullStatus(status);
        })
        .catch(() => undefined);
    };
    loadStatus();
    const timer = window.setInterval(loadStatus, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
  const startRealQuickstart = () => {
    setError('');
    apiPostJson<IngestTask>('/api/ingest/real_quickstart', {
      target: 3000,
      from: '2023-01-01',
      to: '2026-07-10',
      scope: 'core',
      prefer: 'openalex',
      fallback: 'crossref_arxiv',
    })
      .then(setIngestTask)
      .catch((err) => setError(`${t.startIngestFailed}: ${err.message}`));
  };

  if (error) return <div className="error-panel">{t.error}: {error}</div>;
  if (!data) return <div className="loading">{t.loadingRadar}</div>;

  const sourceLabel = corpusPresetLabel(corpusPreset);

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.overviewEyebrow}</p>
          <h2>{t.overviewTitle}</h2>
        </div>
        <div className="control-stack">
          <div className="control-row">
            {scopeOptions.map((item) => (
              <button key={item} className={`segmented ${scope === item ? 'active' : ''}`} onClick={() => setScope(item)}>
                {scopeLabel(item, t)}
              </button>
            ))}
          </div>
          <div className="control-row">
            <a className="export-link" href={exportUrl('/api/export/papers.csv')}>{t.exportCSV}</a>
            <a className="export-link" href={exportUrl('/api/export/download_tasks.csv')}>download_tasks.csv</a>
          </div>
        </div>
      </header>

      <section className={corpusPreset === 'mock' ? 'explain-panel warning-panel' : 'explain-panel'}>
        <p>{corpusPreset === 'mock' ? t.mockDataWarning : t.overviewDescription}</p>
      </section>

      <section className="filter-panel" aria-label={t.dataMode}>
        <span className="filter-note">{'\u5f53\u524d\u663e\u793a\u8bed\u6599'}</span>
        {corpusPresetOptions.map((item) => (
          <button key={item} className={corpusPreset === item ? 'segmented active' : 'segmented'} onClick={() => setCorpusPreset(item)}>
            {corpusPresetLabel(item)}
          </button>
        ))}
        <label className="checkbox-control"><input type="checkbox" checked readOnly />{t.excludeGenericTerms}</label>
        <label className="checkbox-control"><input type="checkbox" checked readOnly />{t.onlyPhysicsConcepts}</label>
        <label className="checkbox-control"><input type="checkbox" checked readOnly />{t.excludePlatformMaterials}</label>
      </section>

      <section className="panel setup-panel">
        <h3>{t.realDataSetup}</h3>
        <p>{data.corpus_size.real_paper_count === 0 ? t.noRealDataYet : t.realQuickstartHint}</p>
        <div className="setup-actions">
          <button className="utility-button" type="button" onClick={startRealQuickstart} disabled={ingestTask?.status === 'running'}>
            {t.import3000RealMetadata}
          </button>
          <code>.venv\Scripts\python.exe -m backend.ingest_real_quickstart --target 3000 --from 2023-01-01 --to 2026-07-10 --scope core --prefer openalex --fallback crossref_arxiv</code>
        </div>
        {ingestTask && (
          <div className="ingest-status">
            <span>{ingestTask.status === 'running' ? t.ingestRunning : ingestTask.status}</span>
            <span>{t.fetchedCount}: <strong>{ingestTask.fetched_count ?? 0}</strong></span>
            <span>{t.keptCount}: <strong>{ingestTask.kept_count ?? 0}</strong></span>
            <span>{t.currentSource}: {formatBreakdown(ingestTask.source_breakdown)}</span>
            {ingestTask.error_summary && <span>{ingestTask.error_summary}</span>}
            {ingestTask.log_path && <code>{t.errorLogPath}: {ingestTask.log_path}</code>}
          </div>
        )}
        <small>{t.openAlexLogHint}</small>
      </section>

      <section className="panel setup-panel">
        <h3>{t.fullCorpusStatus}</h3>
        <div className="ingest-status">
          <span>本轮: {formatChunkCounts(fullStatus?.current_run_chunks, t)} {fullStatus?.current_run?.status ? `(${fullStatus.current_run.status})` : ''}</span>
          <span>全部: {formatChunkCounts(fullStatus?.all_chunks, t)}</span>
          <span>{t.realPaperCount}: <strong>{fullStatus?.real_paper_count ?? data.corpus_size.real_paper_count}</strong></span>
          <span>{t.recentError}: {formatIngestError(fullStatus, t)}</span>
          {fullStatus?.last_error?.log_path && <code>{t.errorLogPath}: {fullStatus.last_error.log_path}</code>}
          {fullStatus?.latest_report_path && <code>{t.reportPath}: {fullStatus.latest_report_path}</code>}
        </div>
      </section>

      <section className="window-strip" aria-label={t.windowSummary}>
        <WindowBadge label={t.baselineWindow} value={`${data.window.baseline_from} - ${data.window.baseline_to}`} />
        <WindowBadge label={t.trendWindow} value={`${data.window.trend_from} - ${data.window.trend_to}`} />
        <WindowBadge label={t.currentCorpus} value={`${scopeLabel(data.window.scope, t)} / ${data.corpus_size.current_corpus_label ?? corpusPresetLabel(corpusPreset)}`} />
      </section>

      <section className="stats-row">
        <StatCard label={t.currentDataSource} value={sourceLabel} detail={sourceBreakdown(data)} />
        <StatCard label="当前显示语料" value={data.corpus_size.current_corpus_label ?? sourceLabel} detail={data.corpus_size.current_corpus_preset} />
        <StatCard label={t.totalPapers} value={data.total_papers} detail={t.paperLevelCounting} />
        <StatCard label={t.latestMonth} value={data.latest_month ?? t.none} detail={t.monthlyTrendBucket} />
        <StatCard label={t.newPapersThisMonth} value={data.this_month_papers} detail={t.publishedMetadata} />
        <StatCard label={t.realData} value={data.corpus_size.real_paper_count} detail={t.realMetadata} />
        <StatCard label="严格凝聚态论文数" value={data.corpus_size.strict_condmat_paper_count} detail="strict_condmat" />
        <StatCard label="排除非凝聚态论文数" value={data.corpus_size.excluded_non_condmat_paper_count} detail="保留原始 metadata" />
        <StatCard label={t.mockExampleData} value={data.corpus_size.mock_paper_count} detail={t.mockMetadata} />
        <StatCard label={t.dbConcepts} value={data.corpus_size.concept_count} detail={t.excludeGenericTerms} />
        <StatCard label={t.dbMaterials} value={data.corpus_size.material_count} detail={t.materials} />
      </section>

      <section className="panel path-panel">
        <h3>{t.databasePath}</h3>
        <code>{data.corpus_size.database_path}</code>
        <small>{t.estimatedFullCorpus}: {data.corpus_size.estimated_full_corpus ?? t.none}</small>
        <small>最近质量报告: {data.corpus_size.latest_quality_report_path ?? t.none}</small>
      </section>

      <section className="overview-grid">
        <RankPanel title={t.hotConcepts} items={data.hot_concepts} metric="momentum" onClick={onConceptSelect} t={t} />
        <RankPanel title={t.risingConcepts} items={data.rising_concepts} metric="growth" onClick={onConceptSelect} t={t} />
        <RankPanel title={t.coolingConcepts} items={data.cooling_concepts} metric="growth" onClick={onConceptSelect} t={t} />
        <RankPanel title={t.topMaterials} items={data.top_materials} metric="raw_freq" onClick={onConceptSelect} t={t} />
        <RankPanel title={t.topMethods} items={data.top_methods} metric="raw_freq" t={t} />
        <DistributionPanel title={t.journalSignal} items={data.journal_distribution} t={t} />
      </section>
    </div>
  );
}

function corpusPresetLabel(mode: CorpusPreset) {
  return { all_real: '\u5168\u90e8\u771f\u5b9e\u6570\u636e', strict_condmat: '\u4e25\u683c\u51dd\u805a\u6001', core_all: '\u6838\u5fc3\u671f\u520a\u5168\u90e8', mock: 'Mock \u793a\u4f8b' }[mode];
}

function WindowBadge({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function sourceBreakdown(data: OverviewData) {
  if (data.data_source.sources.length === 0) return data.source_distribution.map((item) => `${item.source}: ${item.count}`).join(', ');
  return data.data_source.sources.map((item) => `${item.data_mode}/${item.source}: ${item.n}`).join(', ');
}

function RankPanel({ title, items, metric, onClick, t }: { title: string; items: Ranked[]; metric: keyof Ranked; onClick?: (term: string) => void; t: Translation }) {
  return (
    <section className="panel">
      <h3>{title}</h3>
      <div className="rank-list">
        {items.length === 0 && <div className="empty-state">{t.noData}</div>}
        {items.slice(0, 20).map((item, index) => (
          <button key={`${title}-${item.term}`} className="rank-row" onClick={() => onClick?.(item.term)}>
            <span className="rank-index">{index + 1}</span>
            <span className="rank-term">{item.term}</span>
            <span className="rank-value">{Number(item[metric] ?? item.count ?? 0).toFixed(2)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function DistributionPanel({ title, items, t }: { title: string; items: Array<{ journal: string; count: number }>; t: Translation }) {
  const max = Math.max(1, ...items.map((item) => item.count));
  return (
    <section className="panel">
      <h3>{title}</h3>
      <div className="bar-list">
        {items.length === 0 && <div className="empty-state">{t.noData}</div>}
        {items.slice(0, 12).map((item) => (
          <div key={item.journal} className="bar-row">
            <span>{item.journal}</span>
            <div className="bar-track"><div style={{ width: `${(item.count / max) * 100}%` }} /></div>
            <strong>{item.count}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function formatChunkCounts(counts: ChunkCounts | undefined, t: Translation) {
  const item = counts ?? { completed: 0, failed: 0, pending: 0, running: 0, skipped: 0, total: 0 };
  return `${t.completedChunks} ${item.completed}/${item.total}, ${t.pendingChunks} ${item.pending}, ${t.failedChunks} ${item.failed}, running ${item.running}`;
}

function formatIngestError(status: FullCorpusStatus | null, t: Translation) {
  const error = status?.last_error;
  if (!error) return t.none;
  return error.error_summary_brief || cleanRawIngestError(error) || t.none;
}

function cleanRawIngestError(error: FullCorpusError) {
  const summary = error.error_summary || '';
  if (!summary) return '';
  if (summary.includes('"source": "crossref"') || summary.includes('"source":"crossref"')) {
    const status = summary.match(/"status"\s*:\s*(\d+)/)?.[1];
    const journal = error.journal || 'unknown journal';
    const dates = `${error.date_from || '?'}..${error.date_to || '?'}`;
    return `Crossref HTTP ${status || 'error'} while ingesting ${journal} ${dates}; see chunk log.`;
  }
  const compact = summary.replace(/\s+/g, ' ').trim();
  return compact.length > 220 ? `${compact.slice(0, 219)}...` : compact;
}
function formatBreakdown(breakdown: Record<string, number> | undefined) {
  if (!breakdown) return '';
  return Object.entries(breakdown).map(([key, value]) => `${key}: ${value}`).join(', ');
}
