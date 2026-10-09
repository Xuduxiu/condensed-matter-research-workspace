import { useEffect, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import StatCard from '../components/StatCard';
import { Translation } from '../i18n';

const DEFAULT_PRESET = 'strict_core_published';

type OverviewData = {
  corpus_size: {
    database_path: string;
    active_paper_count: number;
    real_paper_count: number;
    strict_condmat_paper_count: number;
    strict_core_published_paper_count?: number;
    strict_context_published_paper_count?: number;
    arxiv_preprint_paper_count?: number;
    excluded_non_condmat_paper_count: number;
    mock_paper_count: number;
    latest_quality_report_path?: string | null;
  };
  data_source: { active_sources?: Array<{ source: string; n: number }> };
};

type IngestStatus = {
  real_paper_count: number;
  completed_chunks: number;
  failed_chunks: number;
  pending_chunks: number;
  latest_report_path?: string;
  last_error?: null | { journal?: string; date_from?: string; date_to?: string; error_summary?: string; log_path?: string };
};

type ConfigStatus = {
  data_dir: string;
  db_path: string;
  openalex: { has_key: boolean; key_masked: string; mailto_configured: boolean };
  deepseek: { has_key: boolean; key_masked: string; base_url: string; model_fast: string; model_pro: string };
};

type UnifiedStatus = {
  installed: boolean;
  counts?: Record<string, number>;
  download_queue?: Record<string, number>;
};

type DailyStatus = {
  runs: Array<{ id: string; status: string; started_at: string; finished_at?: string; error_message?: string }>;
  source_cursors: Array<{ source_name: string; status: string; last_successful_cursor?: string; error_message?: string }>;
};

type Props = { t: Translation };

export default function SystemData({ t }: Props) {
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [config, setConfig] = useState<ConfigStatus | null>(null);
  const [unified, setUnified] = useState<UnifiedStatus | null>(null);
  const [daily, setDaily] = useState<DailyStatus | null>(null);
  const [dailyMessage, setDailyMessage] = useState('');
  const [error, setError] = useState('');

  const load = () => {
    setError('');
    apiGet<OverviewData>(`/api/overview?scope=all&corpus_preset=${DEFAULT_PRESET}`).then(setOverview).catch((err) => setError(err.message));
    apiGet<IngestStatus>('/api/ingest/status').then(setIngest).catch(() => undefined);
    apiGet<ConfigStatus>('/api/config/status').then(setConfig).catch(() => undefined);
    apiGet<UnifiedStatus>('/api/library/status').then(setUnified).catch(() => undefined);
    apiGet<DailyStatus>('/api/daily/status').then(setDaily).catch(() => undefined);
  };

  const runDaily = (apply: boolean) => {
    setDailyMessage(apply ? 'Daily update accepted / 每日更新已提交' : 'Running dry-run...');
    apiPostJson<Record<string, unknown>>('/api/daily/run', {
      apply,
      skip_network: false,
      download_limit: 10,
    }).then((result) => {
      setDailyMessage(JSON.stringify(result));
      load();
    }).catch((err) => setDailyMessage(`Error: ${err.message}`));
  };

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 10000);
    return () => window.clearInterval(timer);
  }, []);

  if (error) return <div className="error-panel">{t.error}: {error}</div>;
  if (!overview) return <div className="loading">{t.loadingSystemData}</div>;

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.systemData}</p>
          <h2>{t.systemTitle}</h2>
        </div>
        <button className="utility-button" type="button" onClick={load}>{t.refresh}</button>
      </header>

      <section className="stats-row">
        <StatCard label={t.realPapers} value={overview.corpus_size.real_paper_count} />
        <StatCard label={t.strictCore} value={overview.corpus_size.strict_core_published_paper_count ?? overview.corpus_size.strict_condmat_paper_count} />
        <StatCard label={t.contextStrict} value={overview.corpus_size.strict_context_published_paper_count ?? 0} />
        <StatCard label={t.arxivPreprint} value={overview.corpus_size.arxiv_preprint_paper_count ?? 0} />
        <StatCard label={t.excluded} value={overview.corpus_size.excluded_non_condmat_paper_count} />
        <StatCard label={t.mockExampleData} value={overview.corpus_size.mock_paper_count} />
        <StatCard label={t.completedChunks} value={ingest?.completed_chunks ?? 0} />
        <StatCard label={t.failedChunks} value={ingest?.failed_chunks ?? 0} />
      </section>

      <section className="detail-grid two-col">
        <div className="panel path-panel">
          <h3>{t.storage}</h3>
          <small>{t.dataDir}</small>
          <code>{config?.data_dir ?? '-'}</code>
          <small>{t.databasePath}</small>
          <code>{overview.corpus_size.database_path}</code>
          <small>{t.latestQualityReport}</small>
          <code>{overview.corpus_size.latest_quality_report_path ?? 'none'}</code>
          <small>{t.reportPath}</small>
          <code>{ingest?.latest_report_path ?? 'none'}</code>
        </div>
        <div className="panel path-panel">
          <h3>{t.providers}</h3>
          <small>{t.openalexKey}</small>
          <code>{config?.openalex.has_key ? config.openalex.key_masked : t.notSet}</code>
          <small>{t.deepseekKey}</small>
          <code>{config?.deepseek.has_key ? config.deepseek.key_masked : t.notSet}</code>
          <small>{t.deepseekEndpoint}</small>
          <code>{config?.deepseek.base_url ?? '-'}</code>
          <small>{t.models}</small>
          <code>{config ? `${config.deepseek.model_fast} / ${config.deepseek.model_pro}` : '-'}</code>
        </div>
      </section>

      <section className="panel path-panel">
        <div className="queue-status-head">
          <div>
            <h3>统一资料库与每日更新 / Unified library & daily update</h3>
            <p className="muted">单实例锁、成功游标、失败重试和运行日志由 Radar 统一管理。</p>
          </div>
          <div className="control-row">
            <button className="utility-button" type="button" onClick={() => runDaily(false)}>Dry-run</button>
            <button className="utility-button" type="button" disabled={!unified?.installed} onClick={() => runDaily(true)}>启动 / Start</button>
          </div>
        </div>
        <code>schema={unified?.installed ? 'installed' : 'not installed'} · versions={unified?.counts?.paper_versions ?? 0} · pdfs={unified?.counts?.paper_files ?? 0}</code>
        {dailyMessage && <p className="muted">{dailyMessage}</p>}
        {(daily?.runs ?? []).slice(0, 5).map((run) => (
          <p key={run.id}><strong>{run.status}</strong> {run.started_at} {run.error_message ?? ''}</p>
        ))}
        {(daily?.source_cursors ?? []).map((cursor) => (
          <p key={cursor.source_name}>cursor {cursor.source_name}: {cursor.status} · {cursor.last_successful_cursor ?? 'none'}</p>
        ))}
      </section>

      <section className="panel path-panel">
        <h3>{t.latestIngestError}</h3>
        {ingest?.last_error ? (
          <>
            <p>{ingest.last_error.journal} {ingest.last_error.date_from} - {ingest.last_error.date_to}</p>
            <code>{ingest.last_error.error_summary ?? 'no summary'}</code>
            {ingest.last_error.log_path && <code>{ingest.last_error.log_path}</code>}
          </>
        ) : <p className="muted">{t.noFailedChunkReported}</p>}
      </section>
    </div>
  );
}