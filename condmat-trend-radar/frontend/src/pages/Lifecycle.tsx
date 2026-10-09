import { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { apiGet } from '../api';
import { conceptClassLabel, statusDescription, statusLabel, Translation } from '../i18n';

type ConceptClass = 'all' | 'physics_concept' | 'material_system' | 'platform_material' | 'method' | 'general_field';

type LifecycleItem = {
  concept: string;
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

type LifecyclePayload = {
  items: LifecycleItem[];
  summary: {
    median_concept_half_life: number | null;
    median_burst_duration: number | null;
    longest_active_concepts: LifecycleItem[];
    fastest_rising_concepts: LifecycleItem[];
    fastest_cooling_concepts: LifecycleItem[];
  };
};

type Props = {
  t: Translation;
  onConceptSelect: (concept: string) => void;
};

const statusOrder = ['emerging', 'active', 'persistent', 'cooling', 'stale'];
const classOptions: ConceptClass[] = ['all', 'physics_concept', 'material_system', 'platform_material', 'method', 'general_field'];

export default function Lifecycle({ t, onConceptSelect }: Props) {
  const [data, setData] = useState<LifecyclePayload | null>(null);
  const [conceptClass, setConceptClass] = useState<ConceptClass>('all');
  const [excludeMethods, setExcludeMethods] = useState(true);
  const [excludePlatformMaterials, setExcludePlatformMaterials] = useState(false);
  const [minTrendCount, setMinTrendCount] = useState(0);
  const [minHistoricalCount, setMinHistoricalCount] = useState(0);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    apiGet<LifecyclePayload>('/api/lifecycle').then(setData);
  }, []);

  const filteredItems = useMemo(() => {
    if (!data) return [];
    return data.items.filter((item) => {
      if (conceptClass !== 'all' && item.concept_class !== conceptClass) return false;
      if (excludeMethods && item.concept_class === 'method') return false;
      if (excludePlatformMaterials && (item.concept_class === 'platform_material' || item.is_platform_term)) return false;
      if (item.trend_total_count < minTrendCount) return false;
      if (item.baseline_total_count < minHistoricalCount) return false;
      return true;
    });
  }, [data, conceptClass, excludeMethods, excludePlatformMaterials, minTrendCount, minHistoricalCount]);

  useEffect(() => {
    if (!data || !ref.current) return;
    const chart = echarts.init(ref.current);
    const months = Array.from(new Set(filteredItems.map((item) => item.historical_first_seen ?? item.first_seen))).sort();
    chart.setOption({
      backgroundColor: 'transparent',
      legend: {
        top: 0,
        textStyle: { color: '#aebbd0' },
        data: statusOrder.map((status) => statusLabel(status, t)),
      },
      tooltip: {
        formatter: (params: any) => {
          const item = params.data.item as LifecycleItem;
          return `${t.concept}: ${item.concept}<br/>${t.historicalFirstSeen}: ${item.historical_first_seen ?? t.none}<br/>${t.trendFirstSeen}: ${item.trend_first_seen ?? t.none}<br/>${t.conceptClass}: ${conceptClassLabel(item.concept_class, t)}<br/>${t.noveltyScore}: ${formatScore(item.novelty_score)}<br/>${t.status}: ${statusLabel(item.status, t)}<br/>${statusDescription(item.status, t)}`;
        },
      },
      grid: { left: 64, right: 28, top: 56, bottom: 56 },
      xAxis: {
        type: 'category',
        name: t.historicalFirstSeen,
        data: months,
        axisLabel: { color: '#9eb0c7', rotate: 45 },
        axisLine: { lineStyle: { color: '#30445f' } },
      },
      yAxis: {
        type: 'value',
        name: t.halfLifeMonths,
        axisLabel: { color: '#9eb0c7' },
        splitLine: { lineStyle: { color: '#203149' } },
      },
      series: statusOrder.map((status) => ({
        name: statusLabel(status, t),
        type: 'scatter',
        symbolSize: (value: any) => Math.max(8, Math.min(42, value[2] * 3)),
        data: filteredItems
          .filter((item) => item.status === status)
          .map((item) => ({
            value: [item.historical_first_seen ?? item.first_seen, item.half_life_months ?? item.active_duration_months, item.peak_value],
            item,
          })),
        itemStyle: { color: statusColor(status), opacity: 0.88 },
      })),
    });
    chart.on('click', (params: any) => {
      const item = params.data?.item as LifecycleItem | undefined;
      if (item) onConceptSelect(item.concept);
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data, filteredItems, onConceptSelect, t]);

  if (!data) return <div className="loading">{t.loadingLifecycle}</div>;

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.conceptLifecycleTitle}</p>
          <h2>{t.conceptLifecycleSubtitle}</h2>
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
        <label className="number-control">{t.minTrendCount}<input type="number" min="0" value={minTrendCount} onChange={(event) => setMinTrendCount(Number(event.target.value) || 0)} /></label>
        <label className="number-control">{t.minHistoricalCount}<input type="number" min="0" value={minHistoricalCount} onChange={(event) => setMinHistoricalCount(Number(event.target.value) || 0)} /></label>
      </section>
      <section className="stats-row">
        <Summary label={t.medianConceptHalfLife} value={data.summary.median_concept_half_life ?? t.ongoing} />
        <Summary label={t.medianBurstDuration} value={data.summary.median_burst_duration ?? 0} />
        <Summary label={t.trackedConcepts} value={filteredItems.length} />
        <Summary label={t.emergingConcepts} value={filteredItems.filter((item) => item.status === 'emerging').length} />
      </section>
      <section className="status-explain-panel">
        {statusOrder.map((status) => (
          <div key={status}>
            <strong>{statusLabel(status, t)}</strong>
            <span>{statusDescription(status, t)}</span>
          </div>
        ))}
      </section>
      <div className="chart lifecycle-chart" ref={ref} />
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
                <th>{t.trendFirstSeen}</th>
                <th>{t.baselineTotalCount}</th>
                <th>{t.historicalCountBeforeTrend}</th>
                <th>{t.trendTotalCount}</th>
                <th>{t.noveltyScore}</th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.slice(0, 160).map((item) => (
                <tr key={item.concept} onClick={() => onConceptSelect(item.concept)}>
                  <td className="paper-title">{item.concept}</td>
                  <td>{statusLabel(item.status, t)}</td>
                  <td>{conceptClassLabel(item.concept_class, t)}</td>
                  <td className="mono">{item.historical_first_seen ?? t.none}</td>
                  <td className="mono">{item.trend_first_seen ?? t.none}</td>
                  <td>{item.baseline_total_count}</td>
                  <td>{item.historical_count_before_trend}</td>
                  <td>{item.trend_total_count}</td>
                  <td className="mono">{formatScore(item.novelty_score)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Summary({ label, value }: { label: string; value: string | number }) {
  return (
    <section className="stat-card compact">
      <span>{label}</span>
      <strong>{value}</strong>
    </section>
  );
}

function formatScore(value: number | null | undefined) {
  return Number(value ?? 0).toFixed(3);
}

function statusColor(status: string) {
  return {
    emerging: '#f0cf65',
    active: '#27d5a7',
    persistent: '#73a7ff',
    cooling: '#f26d5b',
    stale: '#7f8da3',
  }[status] ?? '#9eb0c7';
}
