import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { apiGet, apiPostJson, exportUrl } from '../api';
import PaperTable, { Paper } from '../components/PaperTable';
import { conceptClassLabel, metricLabel, scopeLabel, statusLabel, Translation } from '../i18n';

type Scope = 'core' | 'core_context' | 'all';
type Smoothing = 'raw' | 'rolling3' | 'rolling6' | 'ewma';
type TimeWindow = '12' | '24' | '60' | 'all';
type PaperMode = 'or' | 'and';

type WindowInfo = {
  baseline_from: string;
  baseline_to: string;
  trend_from: string;
  trend_to: string;
  scope: Scope;
};

type ComparePoint = { month: string; value: number };
type ComparePayload = {
  metric: string;
  scope: string;
  smoothing: string;
  series: Array<{ concept: string; points: ComparePoint[] }>;
  table: LifecycleRow[];
};

type LifecycleRow = {
  concept: string;
  historical_first_seen: string | null;
  trend_first_seen: string | null;
  peak_month: string;
  peak_value: number;
  half_life_months: number | null;
  active_duration_months: number;
  baseline_total_count: number;
  trend_total_count: number;
  novelty_score: number;
  concept_class: string;
  status: string;
};

type SearchResult = { concept: string; concept_class: string; status: string };
type ExportResult = { ok: boolean; task_count: number; csv_path: string; json_path: string; manifest_path: string; message: string };

type Props = {
  t: Translation;
  onConceptSelect: (concept: string) => void;
};

const scopeOptions: Scope[] = ['core', 'core_context', 'all'];
const metricOptions = ['raw_freq', 'weighted_freq', 'momentum', 'normalized_share', 'weighted_normalized_share', 'z_score'];
const smoothingOptions: Smoothing[] = ['raw', 'rolling3', 'rolling6', 'ewma'];
const timeOptions: Array<{ value: TimeWindow; key: 'lastOneYear' | 'lastTwoYears' | 'lastFiveYears' | 'allHistory' }> = [
  { value: '12', key: 'lastOneYear' },
  { value: '24', key: 'lastTwoYears' },
  { value: '60', key: 'lastFiveYears' },
  { value: 'all', key: 'allHistory' },
];

export default function Compare({ t, onConceptSelect }: Props) {
  const [concepts, setConcepts] = useState<string[]>(['ZrTe5', 'HfTe5', 'fractional Chern insulator']);
  const [input, setInput] = useState('');
  const [metric, setMetric] = useState('momentum');
  const [scope, setScope] = useState<Scope>('core');
  const [timeWindow, setTimeWindow] = useState<TimeWindow>('24');
  const [smoothing, setSmoothing] = useState<Smoothing>('raw');
  const [paperMode, setPaperMode] = useState<PaperMode>('or');
  const [windowInfo, setWindowInfo] = useState<WindowInfo | null>(null);
  const [suggestions, setSuggestions] = useState<SearchResult[]>([]);
  const [data, setData] = useState<ComparePayload | null>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [error, setError] = useState('');
  const [exportResult, setExportResult] = useState<ExportResult | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    apiGet<{ window: WindowInfo }>(`/api/overview?scope=${scope}`)
      .then((payload) => setWindowInfo(payload.window))
      .catch((err) => setError(err.message));
  }, [scope]);

  useEffect(() => {
    const query = input.trim();
    if (query.length < 2) {
      setSuggestions([]);
      return;
    }
    const handle = window.setTimeout(() => {
      apiGet<{ items: SearchResult[] }>(`/api/concepts/search?q=${encodeURIComponent(query)}`)
        .then((payload) => setSuggestions(payload.items))
        .catch(() => setSuggestions([]));
    }, 180);
    return () => window.clearTimeout(handle);
  }, [input]);

  const range = useMemo(() => (windowInfo ? rangeForWindow(timeWindow, windowInfo) : null), [timeWindow, windowInfo]);

  useEffect(() => {
    if (!range || concepts.length === 0) return;
    setError('');
    const params = new URLSearchParams({
      concepts: concepts.join(','),
      metric,
      scope,
      smoothing,
      from: range.from,
      to: range.to,
    });
    apiGet<ComparePayload>(`/api/compare?${params.toString()}`)
      .then(setData)
      .catch((err) => setError(err.message));
  }, [concepts, metric, scope, smoothing, range]);

  useEffect(() => {
    if (!range || concepts.length === 0) return;
    const params = new URLSearchParams({
      concepts: concepts.join(','),
      mode: paperMode,
      scope,
      from: range.from,
      to: range.to,
    });
    apiGet<{ items: Paper[] }>(`/api/compare/papers?${params.toString()}`)
      .then((payload) => setPapers(payload.items))
      .catch(() => setPapers([]));
  }, [concepts, paperMode, scope, range]);

  useEffect(() => {
    if (!chartRef.current || !data) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { top: 0, textStyle: { color: '#aebbd0' } },
      grid: { left: 58, right: 24, top: 58, bottom: 48 },
      xAxis: {
        type: 'category',
        data: data.series[0]?.points.map((point) => point.month) ?? [],
        axisLabel: { color: '#9eb0c7', rotate: 45 },
        axisLine: { lineStyle: { color: '#30445f' } },
      },
      yAxis: {
        type: 'value',
        name: metricLabel(metric, t),
        axisLabel: { color: '#9eb0c7' },
        splitLine: { lineStyle: { color: '#203149' } },
      },
      series: data.series.map((item) => ({
        name: item.concept,
        type: 'line',
        smooth: false,
        showSymbol: true,
        symbolSize: 5,
        data: item.points.map((point) => point.value),
        lineStyle: { width: 2.4 },
      })),
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data, metric, t]);

  const addConcept = (value: string) => {
    const concept = value.trim();
    if (!concept || concepts.some((item) => item.toLowerCase() === concept.toLowerCase()) || concepts.length >= 8) return;
    setConcepts([...concepts, concept]);
    setInput('');
    setSuggestions([]);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    addConcept(input);
  };

  const downloadUrl = range
    ? exportUrl(`/api/export/download_tasks.csv?concept=${encodeURIComponent(concepts[0] ?? '')}&from=${range.from}&to=${range.to}`)
    : exportUrl('/api/export/download_tasks.csv');

  const exportTasks = () => {
    if (!range) return;
    setExportResult(null);
    apiPostJson<ExportResult>('/api/integration/export_to_downloader', {
      concepts,
      mode: paperMode,
      from: range.from,
      to: range.to,
      scope,
      limit: 100,
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
          <p className="eyebrow">{t.compareTitle}</p>
          <h2>{t.compareSubtitle}</h2>
        </div>
        <div className="control-stack">
          <div className="control-row">
            {scopeOptions.map((item) => (
              <button key={item} className={`segmented ${scope === item ? 'active' : ''}`} onClick={() => setScope(item)}>
                {scopeLabel(item, t)}
              </button>
            ))}
            <a className="utility-button" href={downloadUrl}>download_tasks.csv</a>
            <button className="utility-button" type="button" onClick={exportTasks}>{t.exportToDownloader}</button>
          </div>
        </div>
      </header>

      <section className="panel compare-controls">
        <form className="search-form compare-search" onSubmit={submit}>
          <input value={input} onChange={(event) => setInput(event.target.value)} placeholder={t.addConcept} />
          <button disabled={concepts.length >= 8}>{t.addConcept}</button>
        </form>
        {suggestions.length > 0 && (
          <div className="suggestion-row">
            {suggestions.slice(0, 8).map((item) => (
              <button key={item.concept} className="tag" onClick={() => addConcept(item.concept)}>
                {item.concept} · {conceptClassLabel(item.concept_class, t)} · {statusLabel(item.status, t)}
              </button>
            ))}
          </div>
        )}
        <div className="selected-row" aria-label={t.selectedConcepts}>
          {concepts.map((concept) => (
            <button key={concept} className="selected-chip" onClick={() => setConcepts(concepts.filter((item) => item !== concept))}>
              {concept} ×
            </button>
          ))}
        </div>
        <div className="control-row wrap-row">
          {metricOptions.map((item) => (
            <button key={item} className={`segmented ${metric === item ? 'active' : ''}`} onClick={() => setMetric(item)}>
              {metricLabel(item, t)}
            </button>
          ))}
        </div>
        <div className="control-row wrap-row">
          {timeOptions.map((item) => (
            <button key={item.value} className={`segmented ${timeWindow === item.value ? 'active' : ''}`} onClick={() => setTimeWindow(item.value)}>
              {t[item.key]}
            </button>
          ))}
          {smoothingOptions.map((item) => (
            <button key={item} className={`segmented ${smoothing === item ? 'active' : ''}`} onClick={() => setSmoothing(item)}>
              {{ raw: t.raw, rolling3: t.rolling3, rolling6: t.rolling6, ewma: t.ewma }[item]}
            </button>
          ))}
          <button className={`segmented ${paperMode === 'or' ? 'active' : ''}`} onClick={() => setPaperMode('or')}>{t.anyConcept}</button>
          <button className={`segmented ${paperMode === 'and' ? 'active' : ''}`} onClick={() => setPaperMode('and')}>{t.allConcepts}</button>
        </div>
      </section>

      {range && (
        <section className="window-strip" aria-label={t.windowSummary}>
          <div><span>{t.trendWindow}</span><strong>{range.from} - {range.to}</strong></div>
          <div><span>{t.currentCorpus}</span><strong>{scopeLabel(scope, t)}</strong></div>
          <div><span>{t.metric}</span><strong>{metricLabel(metric, t)}</strong></div>
        </section>
      )}

      {error && <div className="error-panel">{t.error}: {error}</div>}
      {exportResult && <div className="success-panel">{exportResult.message} {t.taskCount}: {exportResult.task_count}<br />{t.exportedPath}: <code>{exportResult.csv_path}</code></div>}
      <div className="chart trend-chart" ref={chartRef} />

      <section className="panel">
        <h3>{t.lifecycleTable}</h3>
        <div className="table-shell lifecycle-table-shell">
          <table>
            <thead>
              <tr>
                <th>{t.concept}</th>
                <th>{t.status}</th>
                <th>{t.conceptClass}</th>
                <th>{t.historicalFirstSeen}</th>
                <th>{t.trendTotalCount}</th>
                <th>{t.peakMonth}</th>
                <th>{t.value}</th>
                <th>{t.halfLife}</th>
                <th>{t.noveltyScore}</th>
              </tr>
            </thead>
            <tbody>
              {data?.table.length === 0 && <tr><td className="empty-cell" colSpan={9}>{t.noData}</td></tr>}
              {data?.table.map((item) => (
                <tr key={item.concept} onClick={() => onConceptSelect(item.concept)}>
                  <td className="paper-title">{item.concept}</td>
                  <td>{statusLabel(item.status, t)}</td>
                  <td>{conceptClassLabel(item.concept_class, t)}</td>
                  <td className="mono">{item.historical_first_seen ?? t.none}</td>
                  <td>{item.trend_total_count}</td>
                  <td className="mono">{item.peak_month}</td>
                  <td className="mono">{Number(item.peak_value ?? 0).toFixed(2)}</td>
                  <td className="mono">{item.half_life_months ?? t.ongoing}</td>
                  <td className="mono">{Number(item.novelty_score ?? 0).toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <h3>{t.relatedPapers}</h3>
        <PaperTable papers={papers} t={t} onConceptSelect={onConceptSelect} />
      </section>
    </div>
  );
}

function rangeForWindow(timeWindow: TimeWindow, windowInfo: WindowInfo) {
  const to = windowInfo.trend_to;
  if (timeWindow === 'all') return { from: windowInfo.baseline_from, to };
  const months = Number(timeWindow);
  return { from: shiftMonth(to, -(months - 1)), to };
}

function shiftMonth(month: string, delta: number) {
  const [year, mon] = month.slice(0, 7).split('-').map(Number);
  const index = year * 12 + (mon - 1) + delta;
  return `${Math.floor(index / 12).toString().padStart(4, '0')}-${String((index % 12) + 1).padStart(2, '0')}`;
}
