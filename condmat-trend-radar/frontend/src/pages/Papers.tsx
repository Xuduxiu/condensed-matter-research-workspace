import { FormEvent, useEffect, useState } from 'react';
import { apiGet, apiPostJson, exportUrl } from '../api';
import PaperTable, { Paper } from '../components/PaperTable';
import { scopeLabel, Translation } from '../i18n';

type Scope = 'core' | 'core_context' | 'all';

type ExportResult = { ok: boolean; task_count: number; csv_path: string; json_path: string; manifest_path: string; message: string };

type Props = {
  t: Translation;
  onConceptSelect: (concept: string) => void;
};

const scopeOptions: Scope[] = ['core', 'core_context', 'all'];

export default function Papers({ t, onConceptSelect }: Props) {
  const [query, setQuery] = useState('');
  const [concept, setConcept] = useState('');
  const [scope, setScope] = useState<Scope>('core');
  const [papers, setPapers] = useState<Paper[]>([]);
  const [exportResult, setExportResult] = useState<ExportResult | null>(null);
  const [error, setError] = useState('');

  const load = () => {
    const params = new URLSearchParams({ scope });
    if (query) params.set('query', query);
    if (concept) params.set('concept', concept);
    apiGet<{ items: Paper[] }>(`/api/papers?${params.toString()}`).then((payload) => setPapers(payload.items)).catch((err) => setError(err.message));
  };

  useEffect(() => {
    load();
  }, [scope]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    load();
  };

  const exportTasks = () => {
    setExportResult(null);
    apiPostJson<ExportResult>('/api/integration/export_to_downloader', {
      concepts: concept ? [concept] : [],
      mode: 'or',
      from: '2015-01',
      to: '2026-07',
      scope,
      limit: 250,
      min_momentum: 0,
      dry_run: false,
    })
      .then(setExportResult)
      .catch((err) => setError(`${t.exportFailed}: ${err.message}`));
  };

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.papersTitle}</p>
          <h2>{t.papersSubtitle}</h2>
        </div>
        <a className="utility-button" href={exportUrl('/api/export/papers.csv')}>{t.exportCSV}</a>
        <button className="utility-button" type="button" onClick={exportTasks}>{t.exportDownloadTasks}</button>
      </header>
      <form className="filter-bar" onSubmit={submit}>
        <input placeholder={t.searchTitleAbstract} value={query} onChange={(event) => setQuery(event.target.value)} />
        <input placeholder={t.conceptFilter} value={concept} onChange={(event) => setConcept(event.target.value)} />
        {scopeOptions.map((item) => (
          <button key={item} type="button" className={`segmented ${scope === item ? 'active' : ''}`} onClick={() => setScope(item)}>
            {scopeLabel(item, t)}
          </button>
        ))}
        <button>{t.search}</button>
      </form>
      {error && <div className="error-panel">{t.error}: {error}</div>}
      {exportResult && <div className="success-panel">{exportResult.message} {t.taskCount}: {exportResult.task_count}<br />{t.exportedPath}: <code>{exportResult.csv_path}</code></div>}
      <PaperTable papers={papers} t={t} onConceptSelect={onConceptSelect} />
    </div>
  );
}
