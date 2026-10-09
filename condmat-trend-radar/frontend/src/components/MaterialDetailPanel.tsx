import { useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { useApiData } from '../hooks';
import { MaterialDetail } from '../types';
import PaperRow from './PaperRow';
import { EmptyState, ErrorState, LoadingSkeleton, MetricStrip, StatusBadge } from './primitives';

function TrendChart({ series }: { series: MaterialDetail['series'] }) {
  if (!series.length) return <EmptyState title="暂无趋势序列" message="材料存在，但没有可用的月度版本日期。" />;
  const width = 720; const height = 210; const pad = 24;
  const max = Math.max(1, ...series.flatMap((item) => [item.preprints, item.publications]));
  const point = (value: number, index: number) => `${pad + index * ((width - pad * 2) / Math.max(1, series.length - 1))},${height - pad - value * ((height - pad * 2) / max)}`;
  return <div className="trend-chart-wrap"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="预印本与正式见刊月度趋势">
    {[0, .25, .5, .75, 1].map((ratio) => <line key={ratio} x1={pad} x2={width - pad} y1={pad + ratio * (height - pad * 2)} y2={pad + ratio * (height - pad * 2)} className="chart-grid" />)}
    <polyline points={series.map((item, index) => point(item.preprints, index)).join(' ')} className="chart-line preprint" />
    <polyline points={series.map((item, index) => point(item.publications, index)).join(' ')} className="chart-line publication" />
  </svg><div className="chart-legend"><span><i className="preprint" />预印本</span><span><i className="publication" />正式见刊</span><small>{series[0].month} — {series[series.length - 1]?.month}</small></div></div>;
}

export default function MaterialDetailPanel({ id, onClose, onSelectPaper }: { id: string; onClose: () => void; onSelectPaper: (id: string) => void }) {
  const { data, error, loading, refresh } = useApiData<MaterialDetail>((signal) => apiGet(`/api/ui-v2/materials/${encodeURIComponent(id)}`, { signal, cacheMs: 30_000 }), [id]);
  const [message, setMessage] = useState('');
  const addMonitor = async () => {
    if (!data) return;
    try {
      await apiPostJson('/api/library/monitors', { name: `${data.material.canonical_name} 材料监控`, query_text: data.material.canonical_name, filters: { material: data.material.canonical_name }, auto_download_oa: false, enabled: true });
      setMessage('材料监控已保存。');
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '监控保存失败。'); }
  };
  return <div className="detail-inner">
    <header className="detail-header"><div><span>材料详情</span><small>Material intelligence</small></div><button className="icon-button" type="button" aria-label="关闭材料详情" onClick={onClose}>×</button></header>
    {loading && <LoadingSkeleton rows={8} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <>
      <section className="detail-title material-title"><StatusBadge tone="accent">{data.material.material_family}</StatusBadge><h2>{data.material.canonical_name}</h2><p>{data.aliases.length ? `别名：${data.aliases.join(' · ')}` : '别名未提供'}</p></section>
      <div className="detail-actions"><button className="button primary" type="button" onClick={addMonitor}>加入监控</button></div>
      {message && <p className="inline-message" role="status">{message}</p>}
      <MetricStrip items={[{ label: '论文', value: data.counts.total }, { label: '预印本', value: data.counts.preprints, tone: 'preprint' }, { label: '正式见刊', value: data.counts.publications, tone: 'publication' }]} />
      <section className="detail-section"><h3>材料信息</h3><dl className="key-values"><div><dt>化学式</dt><dd>{data.material.composition || '—'}</dd></div><div><dt>相 / 结构</dt><dd>{data.material.phase || '—'}</dd></div><div><dt>厚度</dt><dd>{data.material.thickness || '—'}</dd></div><div><dt>材料家族</dt><dd>{data.material.material_family || '—'}</dd></div></dl></section>
      <section className="detail-section"><h3>论文抽取证据</h3><div className="material-evidence-list">{data.evidence.slice(0, 8).map((item, index) => <button type="button" key={`${item.canonical_paper_id}-${item.source}-${index}`} onClick={() => onSelectPaper(item.canonical_paper_id)}><span><StatusBadge tone={item.source === 'paper_text_formula' ? 'success' : 'accent'}>{item.source}</StatusBadge><b>{Math.round(item.confidence * 100)}%</b></span><strong>{item.title}</strong><p>{item.context.snippet || `${item.context.field || '论文文本'}中识别`}</p></button>)}</div>{!data.evidence.length && <p className="muted">尚未保存抽取证据。</p>}</section>
      <section className="detail-section"><h3>24 个月趋势</h3><TrendChart series={data.series} /></section>
      <section className="detail-section"><h3>相关主题</h3><div className="detail-tags">{data.topics.map((topic) => <span className="tag" key={topic.canonical_name}>{topic.canonical_name} · {topic.paper_count}</span>)}{!data.topics.length && <span className="muted">未提供</span>}</div></section>
      <section className="detail-section"><h3>研究团队</h3><p className="muted">作者隶属关系尚未形成团队聚合接口，因此不推断或伪造团队数量。</p></section>
      <section className="detail-section related-papers"><h3>最近论文</h3>{data.papers.map((paper) => <PaperRow paper={paper} dense key={paper.paper_version_id} onOpen={() => onSelectPaper(paper.canonical_paper_id)} />)}</section>
    </>}
  </div>;
}
