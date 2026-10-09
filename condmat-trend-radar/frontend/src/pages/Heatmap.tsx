import { useEffect, useState } from 'react';
import * as echarts from 'echarts';
import { apiGet, exportUrl } from '../api';
import HeatmapChart, { HeatmapData } from '../components/HeatmapChart';
import { conceptClassLabel, metricLabel, scopeLabel, Translation } from '../i18n';

type Scope = 'core' | 'core_context' | 'all';
type ConceptClass = 'all' | 'physics_concept' | 'material_system' | 'platform_material' | 'method';
type WindowInfo = {
  baseline_from: string;
  baseline_to: string;
  trend_from: string;
  trend_to: string;
  scope: Scope;
};
type TimeWindow = '12' | '24' | '60' | 'all';

type Props = {
  t: Translation;
  onConceptSelect: (concept: string) => void;
};

const scopeOptions: Scope[] = ['core', 'core_context', 'all'];
const metricOptions = ['raw_freq', 'weighted_freq', 'momentum', 'normalized_share', 'weighted_normalized_share'];
const classOptions: ConceptClass[] = ['all', 'physics_concept', 'material_system', 'platform_material', 'method'];

export default function Heatmap({ t, onConceptSelect }: Props) {
  const [metric, setMetric] = useState('momentum');
  const [scope, setScope] = useState<Scope>('core');
  const [timeWindow, setTimeWindow] = useState<TimeWindow>('24');
  const [conceptClass, setConceptClass] = useState<ConceptClass>('physics_concept');
  const [excludeMethods, setExcludeMethods] = useState(true);
  const [excludePlatformMaterials, setExcludePlatformMaterials] = useState(true);
  const [minCount, setMinCount] = useState(2);
  const [windowInfo, setWindowInfo] = useState<WindowInfo | null>(null);
  const [data, setData] = useState<HeatmapData | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    setError('');
    apiGet<{ window: WindowInfo }>(`/api/overview?scope=${scope}`)
      .then((payload) => setWindowInfo(payload.window))
      .catch((err) => setError(err.message));
  }, [scope]);

  useEffect(() => {
    if (!windowInfo) return;
    setData(null);
    setError('');
    const params = new URLSearchParams({
      metric,
      scope,
      concept_class: conceptClass,
      exclude_methods: String(excludeMethods),
      exclude_platform_materials: String(excludePlatformMaterials),
      min_count: String(minCount),
    });
    const range = rangeForWindow(timeWindow, windowInfo);
    params.set('from', range.from);
    params.set('to', range.to);
    apiGet<HeatmapData>(`/api/heatmap?${params.toString()}`)
      .then(setData)
      .catch((err) => setError(err.message));
  }, [metric, scope, timeWindow, windowInfo, conceptClass, excludeMethods, excludePlatformMaterials, minCount]);

  const exportPng = () => {
    const dom = document.getElementById('heatmap-chart');
    if (!dom) return;
    const chart = echarts.getInstanceByDom(dom);
    if (!chart) return;
    const url = chart.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: '#08111f' });
    const link = document.createElement('a');
    link.href = url;
    link.download = `condmat-heatmap-${metric}-${scope}.png`;
    link.click();
  };

  const timeOptions: Array<{ value: TimeWindow; label: string }> = [
    { value: '12', label: t.lastOneYear },
    { value: '24', label: t.lastTwoYears },
    { value: '60', label: t.lastFiveYears },
    { value: 'all', label: t.allHistory },
  ];

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.heatmapTitle}</p>
          <h2>{t.heatmapSubtitle}</h2>
        </div>
        <div className="control-stack">
          <div className="control-row">
            {metricOptions.map((item) => (
              <button key={item} className={`segmented ${metric === item ? 'active' : ''}`} onClick={() => setMetric(item)}>
                {metricLabel(item, t)}
              </button>
            ))}
            <button className="utility-button" onClick={exportPng}>{t.exportPNG}</button>
            <a className="utility-button" href={exportUrl('/api/export/terms.csv')}>{t.exportCSV}</a>
          </div>
          <div className="control-row">
            {scopeOptions.map((item) => (
              <button key={item} className={`segmented ${scope === item ? 'active' : ''}`} onClick={() => setScope(item)}>
                {scopeLabel(item, t)}
              </button>
            ))}
          </div>
          <div className="control-row">
            {timeOptions.map((item) => (
              <button key={item.value} className={`segmented ${timeWindow === item.value ? 'active' : ''}`} onClick={() => setTimeWindow(item.value)}>
                {item.label}
              </button>
            ))}
          </div>
        </div>
      </header>
      <section className="filter-panel" aria-label={t.filter}>
        {classOptions.map((item) => (
          <button key={item} className={`segmented ${conceptClass === item ? 'active' : ''}`} onClick={() => setConceptClass(item)}>
            {item === 'all' ? t.allClasses : conceptClassLabel(item, t)}
          </button>
        ))}
        <label className="checkbox-control"><input type="checkbox" checked={excludeMethods} onChange={(event) => setExcludeMethods(event.target.checked)} />{t.excludeMethods}</label>
        <label className="checkbox-control"><input type="checkbox" checked={excludePlatformMaterials} onChange={(event) => setExcludePlatformMaterials(event.target.checked)} />{t.excludePlatformMaterials}</label>
        <label className="number-control">{t.minimumCount}<input type="number" min="1" max="20" value={minCount} onChange={(event) => setMinCount(Number(event.target.value) || 1)} /></label>
      </section>
      {windowInfo && (
        <section className="window-strip" aria-label={t.windowSummary}>
          <div><span>{t.trendWindow}</span><strong>{rangeForWindow(timeWindow, windowInfo).from} - {rangeForWindow(timeWindow, windowInfo).to}</strong></div>
          <div><span>{t.currentCorpus}</span><strong>{scopeLabel(scope, t)}</strong></div>
          <div><span>{t.metric}</span><strong>{metricLabel(metric, t)}</strong></div>
        </section>
      )}
      {error && <div className="error-panel">{t.error}: {error}</div>}
      {!data && !error && <div className="loading">{t.loadingHeatmap}</div>}
      {data && <HeatmapChart data={data} t={t} onConceptSelect={onConceptSelect} />}
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
