import { useMemo, useState, type ReactNode } from 'react';
import { apiGet, apiPostJson } from '../api';
import { formatDate, formatNumber, useApiData } from '../hooks';
import { ErrorState, LoadingSkeleton, MetricStrip, StatusBadge } from '../components/primitives';
import './analyticsScientific.css';
import './analyticsWarnings.css';
import {
  ChartEmpty,
  DownloadStatusChart,
  FieldDistributionChart,
  HorizontalDistributionChart,
  MaterialBurstChart,
  MaterialStageChart,
  MaterialVolumeChart,
  MonitorTimelineChart,
  PaperPulseChart,
  PublicationPathwaysChart,
  QualityTimelineChart,
  ShareShiftChart,
  SourceOverlapChart,
  TrendHeatmapChart,
  type Distribution,
  type MaterialBurstSignal,
  type MaterialSignal,
  type PublicationPathway,
  type QualityPoint,
  type SourceOverlap,
  type TrendSignal,
} from '../components/AnalyticsCharts';
import {
  CitationDistributionChart,
  DownloadSourcePerformanceChart,
  type CitationBucket,
  type DownloadSourcePerformance,
} from '../components/OperationalAnalyticsCharts';
import SourceShiftChart, { type SourceWindowItem } from '../components/SourceShiftChart';

type TrendSignalWithQuality = TrendSignal & {
  reliability?: number;
  status?: 'emerging' | 'rising' | 'stable' | 'cooling' | 'insufficient_evidence' | string;
};

type AnalyticsData = {
  generated_at: string;
  data_anchor: string | null;
  range_from: string | null;
  range_to: string | null;
  baseline_from?: string | null;
  baseline_to?: string | null;
  days: number;
  summary: {
    window_papers: number;
    baseline_papers?: number;
    preprints: number;
    publications: number;
    abstract_available: number;
    abstract_missing: number;
    abstract_coverage_pct: number;
    doi_count?: number;
    doi_coverage_pct?: number;
    oa_count: number;
    oa_coverage_pct: number;
    pdf_count: number;
    pdf_coverage_pct: number;
    monitor_hits: number;
    downloads_completed: number;
    downloads_failed: number;
    download_success_rate_pct: number;
  };
  paper_timeline: Array<{ date: string; preprints: number; publications: number; total: number }>;
  monitor_timeline: Array<{ date: string; hits: number }>;
  source_distribution: Distribution[];
  journal_distribution: Distribution[];
  download_status: Array<{ status: string; count: number }>;
  abstract_status: { available: number; missing: number; coverage_pct: number };
  top_materials: MaterialSignal[];
  topic_trends?: TrendSignalWithQuality[];
  material_trends?: TrendSignalWithQuality[];
  method_trends?: TrendSignalWithQuality[];
  field_distribution?: Distribution[];
  source_overlap?: SourceOverlap | null;
  source_window_distribution?: SourceWindowItem[];
  publication_pathways?: PublicationPathway[];
  primary_field_quality?: {
    classification_coverage_pct: number;
    direct_classification_pct: number;
    inferred_classification_pct: number;
    unclassified_count: number;
  };
  quality_timeline?: QualityPoint[];
  citation_distribution?: CitationBucket[];
  download_source_performance?: DownloadSourcePerformance[];
  data_quality?: {
    source_count?: number;
    canonical_papers?: number;
    duplicate_versions_merged?: number;
    cross_source_matches?: number;
    low_evidence_topics?: number;
    current_research_outputs?: number;
    baseline_research_outputs?: number;
    window_volume_ratio?: number;
    source_mix_shift?: number;
    source_mix_shift_pct?: number;
    current_topic_extraction_coverage_pct?: number;
    baseline_topic_extraction_coverage_pct?: number;
    current_material_extraction_coverage_pct?: number;
    baseline_material_extraction_coverage_pct?: number;
    current_method_extraction_coverage_pct?: number;
    baseline_method_extraction_coverage_pct?: number;
    pending_identity_conflicts?: number;
    hotspot_reliable?: boolean;
    common_trend_reliable?: boolean;
    topic_trend_reliable?: boolean;
    material_trend_reliable?: boolean;
    method_trend_reliable?: boolean;
    provenance_overlap_reliable?: boolean;
    data_freshness_days?: number;
    multi_source_coverage_pct?: number;
    topic_extraction_coverage_pct?: number;
    material_extraction_coverage_pct?: number;
    method_extraction_coverage_pct?: number;
    topic_coverage_comparable?: boolean;
    material_coverage_comparable?: boolean;
    method_coverage_comparable?: boolean;
    [key: string]: unknown;
  };
  data_warnings?: string[];
};

type MaterialResponse = {
  items: Array<{
    id: string;
    canonical_name: string;
    material_family?: string;
    count_7d: number;
    count_30d: number;
    count_12m: number;
    monthly_baseline: number;
    preprint_30d: number;
    publication_30d: number;
    trend_score: number;
    evidence_mentions?: number;
  }>;
  count: number;
  data_anchor: string;
};

type RadarBriefResponse = {
  available: boolean;
  cached: boolean;
  provider?: string;
  model?: string;
  updated_at?: string;
  request?: { days?: number; tier?: string; limit?: number };
  evidence?: { window_from?: string; window_to?: string; paper_count?: number };
  analysis?: null | {
    headline: string;
    summary_zh: string;
    rising_topics: string[];
    notable_materials: string[];
    papers_to_read: Array<{ canonical_paper_id: string; title: string; reason: string }>;
    caveats: string[];
  };
};

const windowOptions = [
  { value: 7, label: '7 天' },
  { value: 30, label: '30 天' },
  { value: 90, label: '90 天' },
  { value: 180, label: '180 天' },
  { value: 365, label: '365 天' },
];

const questionLinks = [
  ['#research-output', '研究产出'],
  ['#recent-hotspots', '近期热点'],
  ['#material-progress', '材料进展'],
  ['#data-quality', '覆盖与质量'],
] as const;

function CoverageBar({ label, value, detail, tone }: { label: string; value: number; detail: string; tone: string }) {
  return <div className="coverage-row"><div><strong>{label}</strong><span>{detail}</span></div><b>{value.toFixed(1)}%</b><div className="coverage-track"><i className={tone} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div></div>;
}

function ChartPanel({ kicker, title, question, note, children, className = '' }: { kicker: string; title: string; question: string; note: string; children: ReactNode; className?: string }) {
  return <section className={`section-block analytics-panel scientific-chart-panel ${className}`}>
    <header className="section-header"><div><span className="section-kicker">{kicker}</span><h2>{title}</h2></div></header>
    <div className="chart-question"><strong>回答</strong><span>{question}</span></div>
    {children}
    <p className="chart-method-note">口径：{note}</p>
  </section>;
}

function sum(values: number[]) {
  return values.reduce((total, value) => total + value, 0);
}

function periodChange(data: AnalyticsData['paper_timeline']) {
  if (data.length < 2) return { recent: sum(data.map((item) => item.total)), previous: 0, pct: null as number | null };
  const width = Math.floor(data.length / 2);
  const previous = sum(data.slice(-width * 2, -width).map((item) => item.total));
  const recent = sum(data.slice(-width).map((item) => item.total));
  return { recent, previous, pct: previous > 0 ? (recent - previous) * 100 / previous : null };
}

function freshnessDays(anchor: string | null) {
  if (!anchor) return null;
  const parsed = new Date(`${anchor}T00:00:00Z`).getTime();
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.floor((Date.now() - parsed) / 86_400_000));
}

function topPositive(items: TrendSignalWithQuality[] | undefined) {
  return [...(items ?? [])]
    .filter((item) => item.current_count >= 2 && item.status !== 'insufficient_evidence' && (item.reliability ?? 1) >= 0.45)
    .sort((a, b) => b.share_change_pp - a.share_change_pp)[0];
}

function InsightSummary({ data, bursts }: { data: AnalyticsData; bursts: MaterialBurstSignal[] }) {
  const localChange = periodChange(data.paper_timeline);
  const current = data.summary.window_papers;
  const previous = data.summary.baseline_papers ?? localChange.previous;
  const change = previous > 0 ? (current - previous) * 100 / previous : null;
  const hotspotReliable = data.data_quality?.hotspot_reliable === true;
  const topicReliable = data.data_quality?.topic_trend_reliable
    ?? data.data_quality?.hotspot_reliable
    ?? false;
  const leadingTopic = topicReliable ? topPositive(data.topic_trends) : undefined;
  const materialComparable = data.data_quality?.material_coverage_comparable !== false;
  const leadingMaterial = materialComparable ? [...bursts].filter((item) => item.count_30d >= 2).sort((a, b) => b.trend_score - a.trend_score)[0] : undefined;
  const lag = freshnessDays(data.data_anchor);
  return <section className="insight-summary" aria-label="统计判断摘要">
    <article><span>观测语料变化</span><strong>{change === null ? '基线不足' : `${change >= 0 ? '+' : ''}${change.toFixed(1)}%`}</strong><p>本期 {current} 篇，对比紧邻等长基线 {previous} 篇；这是数据库观测量，不等同于真实科研投入。</p></article>
    <article><span>份额上升最快主题</span><strong>{leadingTopic?.name || (topicReliable ? '等待主题统计' : '暂不下结论')}</strong><p>{leadingTopic ? `论文份额变化 ${leadingTopic.share_change_pp >= 0 ? '+' : ''}${leadingTopic.share_change_pp.toFixed(3)} 个百分点，当前 ${leadingTopic.current_count} 篇。` : topicReliable ? '需要等长窗口、去重并按全体论文量归一化后才能判断。' : '当前/基线抽取覆盖、来源构成或数据新鲜度不满足可靠门槛。'}</p></article>
    <article><span>样本内材料突发</span><strong>{leadingMaterial?.canonical_name || (materialComparable ? '证据不足' : '暂不排名')}</strong><p>{leadingMaterial ? `近 30 日 ${leadingMaterial.count_30d} 篇，相对过去一年月均偏离 ${leadingMaterial.trend_score >= 0 ? '+' : ''}${(leadingMaterial.trend_score * 100).toFixed(0)}%。` : materialComparable ? '至少需要两篇材料证据才进入突发比较。' : '前后窗口材料抽取覆盖不可比，避免把漏抽取误判成热度变化。'}</p></article>
    <article><span>解释可信度</span><strong>{hotspotReliable ? '可解释' : lag !== null && lag > 7 ? '数据滞后' : '证据未达门槛'}</strong><p>数据锚点滞后 {lag ?? '—'} 天 · 摘要 {data.summary.abstract_coverage_pct.toFixed(1)}% · 来源 {data.data_quality?.source_count ?? data.source_distribution.length} 个 · 多源交叉 {Number(data.data_quality?.multi_source_coverage_pct ?? 0).toFixed(1)}%。</p></article>
  </section>;
}

function HotspotTable({ data, reliable }: { data: TrendSignalWithQuality[]; reliable: boolean }) {
  const items = [...data].sort((a, b) => b.share_change_pp - a.share_change_pp).slice(0, 16);
  if (!items.length) return <ChartEmpty message="主题趋势正在按 canonical paper、等长窗口和论文份额重建；不能用抓取条数代替热点。" />;
  return <div className="hotspot-table-wrap"><table className="hotspot-table"><thead><tr><th>主题</th><th>本期</th><th>基线</th><th>本期份额</th><th>份额变化</th><th>信号</th></tr></thead><tbody>{items.map((item) => {
    const evidence = Math.min(item.current_count, item.baseline_count);
    const signal = !reliable || item.status === 'insufficient_evidence' ? '不可判定' : (item.reliability ?? 1) < 0.45 || evidence < 3 ? '低样本' : evidence >= 10 ? '较稳健' : '观察中';
    return <tr key={`${item.id || item.name}-${item.class || ''}`}><td><strong>{item.name}</strong><small>{item.class || '研究主题'} · 可靠度 {((item.reliability ?? 0) * 100).toFixed(0)}%</small></td><td>{item.current_count}</td><td>{item.baseline_count}</td><td>{item.current_share_pct.toFixed(3)}%</td><td className={item.share_change_pp >= 0 ? 'positive' : 'negative'}>{item.share_change_pp >= 0 ? '+' : ''}{item.share_change_pp.toFixed(3)} pp</td><td><StatusBadge tone={signal === '较稳健' ? 'success' : signal === '观察中' ? 'warning' : 'neutral'}>{signal}</StatusBadge></td></tr>;
  })}</tbody></table></div>;
}

function DataQualitySummary({ data }: { data: AnalyticsData }) {
  const quality = data.data_quality ?? {};
  const sourceCount = Number(quality.source_count ?? data.source_distribution.length);
  const currentOutputs = Number(quality.current_research_outputs ?? data.summary.window_papers);
  const baselineOutputs = Number(quality.baseline_research_outputs ?? data.summary.baseline_papers ?? 0);
  const volumeRatio = Number(quality.window_volume_ratio ?? (baselineOutputs > 0 ? currentOutputs / baselineOutputs : 0));
  const sourceMixShift = Number(quality.source_mix_shift_pct ?? quality.source_mix_shift ?? 0);
  const coveragePair = (current: unknown, baseline: unknown) => `${Number(current ?? 0).toFixed(1)}% / ${Number(baseline ?? 0).toFixed(1)}%`;
  const comparableText = (value: boolean | undefined) => value === false ? '不可比，相关排名已隐藏' : '覆盖可比';
  const primary = data.primary_field_quality;
  const items = [
    { label: '本期 canonical 论文', value: currentOutputs, detail: '跨版本与跨来源统计主键' },
    { label: '等长基线论文', value: baselineOutputs, detail: '紧邻前一窗口，用于份额比较' },
    { label: '前后窗口规模比', value: baselineOutputs > 0 ? `${volumeRatio.toFixed(2)}×` : '无基线', detail: `${formatNumber(currentOutputs)} / ${formatNumber(baselineOutputs)}；偏离 1× 时原始数量不可直接比较` },
    { label: '来源构成漂移', value: `${sourceMixShift.toFixed(1)}%`, detail: '当前与基线来源份额总变差；超过 15% 时热点结论降级' },
    { label: '主题抽取覆盖', value: coveragePair(quality.current_topic_extraction_coverage_pct ?? quality.topic_extraction_coverage_pct, quality.baseline_topic_extraction_coverage_pct), detail: `当前 / 基线 · ${comparableText(quality.topic_coverage_comparable)}` },
    { label: '材料抽取覆盖', value: coveragePair(quality.current_material_extraction_coverage_pct ?? quality.material_extraction_coverage_pct, quality.baseline_material_extraction_coverage_pct), detail: `当前 / 基线 · ${comparableText(quality.material_coverage_comparable)}` },
    { label: '方法抽取覆盖', value: coveragePair(quality.current_method_extraction_coverage_pct ?? quality.method_extraction_coverage_pct, quality.baseline_method_extraction_coverage_pct), detail: `当前 / 基线 · ${comparableText(quality.method_coverage_comparable)}` },
    { label: '主分类覆盖', value: `${Number(primary?.classification_coverage_pct ?? 0).toFixed(1)}%`, detail: `来源直分 ${Number(primary?.direct_classification_pct ?? 0).toFixed(1)}% · 文本推断 ${Number(primary?.inferred_classification_pct ?? 0).toFixed(1)}%` },
    { label: '多源交叉覆盖', value: `${Number(quality.multi_source_coverage_pct ?? 0).toFixed(1)}%`, detail: `${sourceCount} 个可识别来源；交集低时不得声称全网覆盖` },
    { label: '待处理身份冲突', value: Number(quality.pending_identity_conflicts ?? 0), detail: 'DOI / arXiv ID / 标题作者证据冲突' },
  ];
  return <div className="quality-stat-grid">{items.map((item) => <article key={item.label}><span>{item.label}</span><strong>{typeof item.value === 'number' ? formatNumber(item.value) : item.value}</strong><p>{item.detail}</p></article>)}</div>;
}

function DataWarnings({ data }: { data: AnalyticsData }) {
  if (!data.data_warnings?.length) return null;
  return <section className="data-warning-panel" aria-label="统计解释限制"><div><StatusBadge tone="warning">结论已降级</StatusBadge><strong>当前数据不能支持“全网热点”的强结论</strong></div><ul>{data.data_warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></section>;
}

export default function AnalyticsScientific({ onSelectMaterial, onSelectPaper }: { onSelectMaterial: (id: string) => void; onSelectPaper: (id: string) => void }) {
  const [days, setDays] = useState(90);
  const { data, error, loading, refresh, updatedAt } = useApiData<AnalyticsData>(
    (signal) => apiGet(`/api/ui-v2/analytics?days=${days}`, { signal, cacheMs: 30_000 }),
    [days],
  );
  const bursts = useApiData<MaterialResponse>(
    (signal) => apiGet('/api/ui-v2/materials?view=burst&min_evidence=2&limit=60', { signal, cacheMs: 60_000 }),
    [],
  );
  const brief = useApiData<RadarBriefResponse>(
    (signal) => apiGet(`/api/library/ai/radar-brief?days=${Math.min(days, 90)}`, { signal, cacheMs: 20_000 }),
    [days],
  );
  const aiStatus = useApiData<{ configured: boolean; fast_model: string }>(
    (signal) => apiGet('/api/library/ai/status', { signal, cacheMs: 20_000 }),
    [],
  );
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefMessage, setBriefMessage] = useState('');

  const burstItems = useMemo<MaterialBurstSignal[]>(() => (bursts.data?.items ?? []).map((item) => ({ ...item })), [bursts.data]);
  const fieldData = data?.field_distribution ?? [];
  const topicTrendReliable = data?.data_quality?.topic_trend_reliable
    ?? data?.data_quality?.hotspot_reliable
    ?? false;
  const materialCoverageComparable = data?.data_quality?.material_coverage_comparable !== false;
  const materialTrendReliable = data?.data_quality?.material_trend_reliable
    ?? (materialCoverageComparable && data?.data_quality?.hotspot_reliable === true);
  const methodTrendReliable = data?.data_quality?.method_trend_reliable
    ?? (data?.data_quality?.method_coverage_comparable !== false && data?.data_quality?.hotspot_reliable === true);
  const reliableTopicTrends = topicTrendReliable ? (data?.topic_trends ?? []) : [];
  const reliableMaterialTrends = materialTrendReliable ? (data?.material_trends ?? []) : [];
  const reliableMethodTrends = methodTrendReliable ? (data?.method_trends ?? []) : [];

  const generateBrief = async () => {
    setBriefBusy(true);
    setBriefMessage('正在基于本地论文证据生成雷达简报…');
    try {
      const result = await apiPostJson<RadarBriefResponse>('/api/library/ai/radar-brief', { days: Math.min(days, 90), tier: 'fast', force: Boolean(brief.data?.available), limit: 20 });
      setBriefMessage(result.cached ? '已读取同一证据窗口的本地缓存。' : 'DeepSeek 雷达简报已生成并保存到本机数据库。');
      brief.refresh();
    } catch (reason) {
      setBriefMessage(reason instanceof Error ? reason.message : '雷达简报生成失败。');
    } finally {
      setBriefBusy(false);
    }
  };

  return <div className="page-stack analytics-page scientific-analytics">
    <section className="command-strip analytics-command">
      <div><StatusBadge tone={error ? 'danger' : 'success'}>{error ? '统计异常' : '真实去重语料'}</StatusBadge><span>数据锚点 {data?.data_anchor || '—'} · 观测 {data?.range_from || '—'} 至 {data?.range_to || '—'}</span></div>
      <div><label>时间窗口<select value={days} onChange={(event) => setDays(Number(event.target.value))}>{windowOptions.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label><button className="icon-button" type="button" aria-label="刷新统计" onClick={() => { refresh(); bursts.refresh(); }}>↻</button></div>
    </section>

    <nav className="analytics-question-nav" aria-label="分析维度">{questionLinks.map(([href, label]) => <a href={href} key={href}>{label}</a>)}</nav>

    {loading && <LoadingSkeleton rows={12} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <>
      <MetricStrip items={[
        { label: `近 ${data.days} 天去重论文`, value: data.summary.window_papers, tone: 'accent' },
        { label: '含预印本版本', value: data.summary.preprints, tone: 'preprint' },
        { label: '含正式见刊版本', value: data.summary.publications, tone: 'publication' },
        { label: '摘要覆盖', value: `${data.summary.abstract_coverage_pct.toFixed(1)}%`, tone: data.summary.abstract_coverage_pct >= 80 ? 'success' : 'warning' },
        { label: 'DOI 覆盖', value: `${Number(data.summary.doi_coverage_pct ?? 0).toFixed(1)}%`, tone: Number(data.summary.doi_coverage_pct ?? 0) >= 70 ? 'success' : 'warning' },
        { label: '开放获取', value: `${data.summary.oa_coverage_pct.toFixed(1)}%`, tone: 'success' },
        { label: '本地 PDF', value: `${data.summary.pdf_coverage_pct.toFixed(1)}%`, tone: 'accent' },
        { label: '监控命中', value: data.summary.monitor_hits, tone: 'warning' },
        { label: '下载成功率', value: `${data.summary.download_success_rate_pct.toFixed(1)}%`, tone: data.summary.download_success_rate_pct >= 80 ? 'success' : 'danger' },
      ]} />

      <InsightSummary data={data} bursts={burstItems} />
      <DataWarnings data={data} />

      <section className="section-block analytics-panel methodology-panel">
        <header className="section-header"><div><span className="section-kicker">SCIENTIFIC READING GUIDE</span><h2>先看份额，再看数量，最后看证据</h2></div></header>
        <div className="methodology-grid"><article><b>1</b><span><strong>数量回答“产出有多少”</strong><p>受数据源扩容、抓取延迟和周末效应影响，不能单独证明热点。</p></span></article><article><b>2</b><span><strong>归一化份额回答“注意力是否转移”</strong><p>用等长前后窗口并除以同期凝聚态论文总量，抵消语料规模增长。</p></span></article><article><b>3</b><span><strong>预印本/见刊回答“研究处于哪一阶段”</strong><p>预印本先行代表探索活跃，见刊增加代表成果正在沉淀，不代表质量高低。</p></span></article><article><b>4</b><span><strong>低样本只作为预警</strong><p>单篇突发、摘要缺失或单一数据源只进入观察名单，不下“全网热点”结论。</p></span></article></div>
      </section>

      <section className="section-block analytics-panel ai-brief-panel">
        <header className="section-header"><div><span className="section-kicker">DEEPSEEK EVIDENCE BRIEF</span><h2>AI 证据简报</h2></div><div className="ai-brief-actions"><StatusBadge tone={aiStatus.data?.configured ? 'success' : 'warning'}>{aiStatus.data?.configured ? aiStatus.data.fast_model : 'AI 未就绪'}</StatusBadge><button className="button primary" type="button" disabled={briefBusy || aiStatus.loading || !aiStatus.data?.configured} onClick={generateBrief}>{briefBusy ? '生成中…' : brief.data?.available ? '重新生成' : '生成雷达简报'}</button></div></header>
        {briefMessage && <p className="inline-message" role="status">{briefMessage}</p>}
        {brief.loading && <LoadingSkeleton rows={3} />}
        {brief.error && <ErrorState message={brief.error.message} offline={brief.error.code === 'offline'} onRetry={brief.refresh} />}
        {!brief.loading && !brief.error && !brief.data?.available && <div className="ai-brief-empty"><strong>尚未生成这个窗口的简报</strong><p>DeepSeek 只读取最近 {Math.min(days, 90)} 天最多 20 篇本地论文证据。AI 负责归纳，不参与计数，也不会替代下面的统计口径。</p></div>}
        {brief.data?.available && brief.data.analysis && <div className="ai-brief-content"><div className="ai-brief-summary"><small>{brief.data.evidence?.window_from || '—'} — {brief.data.evidence?.window_to || '—'} · 证据 {brief.data.evidence?.paper_count ?? 0} 篇 · {brief.data.cached ? '缓存' : '新生成'} · {formatDate(brief.data.updated_at, true)}</small><h3>{brief.data.analysis.headline}</h3><p>{brief.data.analysis.summary_zh}</p><div className="detail-tags">{brief.data.analysis.rising_topics.map((item) => <span className="tag" key={`topic-${item}`}>{item}</span>)}{brief.data.analysis.notable_materials.map((item) => <span className="tag material" key={`material-${item}`}>{item}</span>)}</div>{brief.data.analysis.caveats.length > 0 && <p className="ai-caveats">证据边界：{brief.data.analysis.caveats.join('；')}</p>}</div><div className="ai-reading-list"><strong>优先阅读</strong>{brief.data.analysis.papers_to_read.map((item) => <button type="button" key={item.canonical_paper_id} onClick={() => onSelectPaper(item.canonical_paper_id)}><span>{item.title}</span><small>{item.reason}</small></button>)}{!brief.data.analysis.papers_to_read.length && <p className="muted">模型没有在当前证据中指定优先论文。</p>}</div></div>}
      </section>

      <section className="analytics-section-heading" id="research-output"><div><span className="section-kicker">01 · RESEARCH OUTPUT</span><h2>整体研究产出与发表结构</h2></div><p>先判断数据库观测到的研究量是否变化，再拆解来源、方向和发表路径。</p></section>
      <ChartPanel kicker="PAPER PULSE" title="论文产出脉冲" question="近期凝聚态论文量是在加速、减速，还是只是日常波动？" note="canonical paper 按日去重；柱状图显示版本结构，折线为 7 日移动平均。预印本与见刊版本可能同时属于同一论文。" className="timeline-panel"><PaperPulseChart data={data.paper_timeline} /></ChartPanel>
      <div className="analytics-grid analytics-grid-three">
        <ChartPanel kicker="SOURCE COVERAGE" title="数据源覆盖" question="当前窗口由哪些来源支撑，是否过度依赖单一来源？" note="每个来源内按 canonical paper 去重；同一论文可被多个来源收录，所以各柱之和可能超过论文总数。"><HorizontalDistributionChart data={data.source_distribution} label="来源覆盖论文数" /></ChartPanel>
        <ChartPanel kicker="FIELD MIX" title="研究方向构成" question="凝聚态内部的主要研究方向当前各占多少？" note={`每篇论文仅取一个主研究方向；来源主分类直接判定 ${Number(data.primary_field_quality?.direct_classification_pct ?? 0).toFixed(1)}%，文本推断 ${Number(data.primary_field_quality?.inferred_classification_pct ?? 0).toFixed(1)}%，其余保留为未分类。`}>{fieldData.length ? <FieldDistributionChart data={fieldData} /> : <ChartEmpty message="主研究方向分类正在从标题、摘要和来源类别构建。" />}</ChartPanel>
        <ChartPanel kicker="JOURNAL SIGNAL" title="活跃期刊与平台" question="哪些发表载体贡献了最多可观察论文？" note="按窗口内 canonical paper 去重计数；这是载体活跃度，不是期刊质量或影响力排名。"><HorizontalDistributionChart data={data.journal_distribution} label="载体论文数" color="#c678dd" /></ChartPanel>
      </div>
      <div className="analytics-grid">
        <ChartPanel kicker="SOURCE MIX SHIFT" title="来源构成漂移" question="本期与基线窗口的来源结构是否一致，热点比较是否受新数据源启用影响？" note="比较两个等长窗口的来源论文份额；总变差超过 15% 时，主题热点必须降级或按共同来源分层。">{data.source_window_distribution?.length ? <SourceShiftChart data={data.source_window_distribution} /> : <ChartEmpty message="缺少前后窗口来源份额，暂不能验证热点可比性。" />}</ChartPanel>
        <ChartPanel kicker="PUBLICATION PATHWAYS" title="预印本到见刊路径" question="研究成果主要如何从预印本流向正式发表，转化通常需要多久？" note="仅统计能够可靠归并预印本与见刊版本的论文；未匹配不等于未发表。">{data.publication_pathways?.length ? <PublicationPathwaysChart data={data.publication_pathways} /> : <ChartEmpty message="正在通过 DOI、arXiv ID、标题和作者证据归并发表路径。" />}</ChartPanel>
        <ChartPanel kicker="MONITOR ACTIVITY" title="监控规则命中" question="你的关注方向近期是否出现更多新论文？" note="仅反映用户监控规则的命中，不代表整个凝聚态领域热度。"><MonitorTimelineChart data={data.monitor_timeline} /></ChartPanel>
        <ChartPanel kicker="EARLY CITATION CONTEXT" title="近期论文引用分档" question="当前窗口论文的早期可见引用分布如何？" note="引用存在严重时间滞后和数据源差异；它只提供背景，不能用于比较刚发布主题的质量。">{data.citation_distribution?.length ? <CitationDistributionChart data={data.citation_distribution} /> : <ChartEmpty message="引用元数据尚未形成可用分档。" />}</ChartPanel>
      </div>

      <section className="analytics-section-heading" id="recent-hotspots"><div><span className="section-kicker">02 · RECENT HOTSPOTS</span><h2>近期热点：看论文份额迁移</h2></div><p>当前窗口与紧邻的等长基线窗口比较；数量和份额同时上升，才是更可信的注意力转移。</p></section>
      <div className="analytics-grid">
        <ChartPanel kicker="TOPIC SHARE SHIFT" title="主题份额升降" question="哪些物理主题正在从凝聚态总体中获得或失去注意力？" note="本期与基线窗口等长；份额分母是各自窗口内全部 eligible canonical papers；绿色为份额上升，红色为下降。">{reliableTopicTrends.length ? <ShareShiftChart data={reliableTopicTrends} label="主题份额变化" /> : <ChartEmpty message="主题份额统计尚未通过覆盖可比性、来源构成和新鲜度门槛，当前不绘制排名。" />}</ChartPanel>
        <ChartPanel kicker="TOPIC EVOLUTION" title="热点主题月度演化" question="份额上升是持续趋势，还是单月尖峰？" note="颜色表示每月论文份额，不使用原始论文数；短促尖峰需要等待更多月份确认。">{reliableTopicTrends.length ? <TrendHeatmapChart data={reliableTopicTrends} /> : <ChartEmpty message="月度主题份额尚未达到可解释门槛，避免把抽取覆盖变化画成热点。" />}</ChartPanel>
      </div>
      <section className="section-block analytics-panel hotspot-evidence-panel"><header className="section-header"><div><span className="section-kicker">CANDIDATE SIGNAL EVIDENCE</span><h2>候选信号证据表</h2></div><span className="muted">低样本自动降级为观察信号</span></header><HotspotTable data={data.topic_trends ?? []} reliable={data.data_quality?.topic_trend_reliable ?? data.data_quality?.hotspot_reliable ?? false} /><p className="chart-method-note">口径：按份额变化排序；“较稳健”只表示样本量相对充足，不代表结论已经通过同行评议。</p></section>
      <div className="analytics-grid">
        <ChartPanel kicker="METHOD SHIFT" title="研究方法份额变化" question="计算、谱学、输运等方法的使用重心是否发生变化？" note="方法可多标签；采用论文份额而非方法标签总量，减少多标签数量变化造成的偏差。">{reliableMethodTrends.length ? <ShareShiftChart data={reliableMethodTrends} label="方法份额变化" /> : <ChartEmpty message="方法份额尚未同时通过覆盖、来源共同集、窗口规模与新鲜度门槛，当前不绘制排名。" />}</ChartPanel>
        <ChartPanel kicker="MATERIAL SHARE SHIFT" title="材料体系份额变化" question="样本内哪些材料体系的论文份额相对凝聚态语料上升？" note="材料可多标签；份额按包含该材料的 canonical papers / 同期全部论文计算。">{reliableMaterialTrends.length ? <ShareShiftChart data={reliableMaterialTrends} label="材料份额变化" /> : <ChartEmpty message="材料份额尚未同时通过覆盖、来源共同集、窗口规模与新鲜度门槛；只保留下方样本内描述。" />}</ChartPanel>
      </div>

      <section className="analytics-section-heading" id="material-progress"><div><span className="section-kicker">03 · MATERIAL PROGRESS</span><h2>材料研究进展与成熟度信号</h2></div><p>区分“研究量大”“增长突然”和“预印本先行”，三者含义不同。</p></section>
      <div className="analytics-grid">
        <ChartPanel kicker="MATERIAL VOLUME" title="材料论文量与版本结构" question="当前窗口哪些材料拥有最多研究活动，预印本与见刊如何构成？" note="去重论文、含预印本版本、含见刊版本为三组独立指标，后两者不可相加当作论文总数。">{materialCoverageComparable ? <MaterialVolumeChart data={data.top_materials} /> : <ChartEmpty message="材料抽取覆盖与基线不可比，当前不展示材料量排行，避免把漏抽取误当成低热度。" />}</ChartPanel>
        <ChartPanel kicker="MATERIAL STAGE" title="材料探索—沉淀矩阵" question="哪些材料更偏预印本探索，哪些已经出现较多正式见刊？" note="横轴为预印本版本数 /（预印本版本数 + 见刊版本数），是版本信号而非质量评分；气泡大小为证据片段数。">{materialCoverageComparable ? <MaterialStageChart data={data.top_materials} onSelect={onSelectMaterial} /> : <ChartEmpty message="材料抽取覆盖不可比，成熟度矩阵暂不绘制；零值不解释为没有研究。" />}</ChartPanel>
      </div>
      <ChartPanel kicker="BURST VS BASELINE" title="材料突发增长：近 30 日对比过去一年月均" question="哪些材料近期论文量明显高于自身常态，而不只是长期热门？" note="至少 2 篇独立论文证据；基线为过去 12 个月月均。该比值尚未校正全库月度规模，需与上方份额变化共同判断。" className="material-burst-wide">{!materialCoverageComparable ? <ChartEmpty message="前后窗口材料抽取覆盖不可比，当前不显示突发排名；补齐同口径抽取后再启用。" /> : bursts.loading ? <LoadingSkeleton rows={6} /> : bursts.error ? <ErrorState message={bursts.error.message} offline={bursts.error.code === 'offline'} onRetry={bursts.refresh} /> : burstItems.length ? <MaterialBurstChart data={burstItems} onSelect={onSelectMaterial} /> : <ChartEmpty message="当前没有满足两篇证据门槛的材料突发信号。" />}</ChartPanel>

      <section className="analytics-section-heading" id="data-quality"><div><span className="section-kicker">04 · COVERAGE &amp; QUALITY</span><h2>数据覆盖、去重与获取质量</h2></div><p>热点结论的可信度取决于摘要完整性、跨源交叉和身份归并，而不只是论文数量。</p></section>
      <DataQualitySummary data={data} />
      <div className="analytics-grid analytics-grid-three">
        <ChartPanel kicker="DATA COMPLETENESS" title="资料完整度" question="有多少论文具备可分析摘要、开放获取状态和本地全文？" note="分母均为当前窗口 canonical papers；PDF 覆盖率是本地工作流指标，不是开放获取率。"><div className="coverage-list"><CoverageBar label="摘要" value={data.summary.abstract_coverage_pct} detail={`${formatNumber(data.summary.abstract_available)} 有摘要 · ${formatNumber(data.summary.abstract_missing)} 缺失`} tone="abstract" /><CoverageBar label="开放获取" value={data.summary.oa_coverage_pct} detail={`${formatNumber(data.summary.oa_count)} 篇 OA`} tone="oa" /><CoverageBar label="本地 PDF" value={data.summary.pdf_coverage_pct} detail={`${formatNumber(data.summary.pdf_count)} 份文件`} tone="pdf" /></div></ChartPanel>
        <ChartPanel kicker="DOWNLOAD OUTCOME" title="下载任务结果" question="可见论文中哪些已下载、待处理、需要复核或没有合法 OA？" note="每篇论文只采用最新下载任务状态；未请求与失败严格区分，成功率只对已完成和失败任务计算。"><DownloadStatusChart data={data.download_status} /></ChartPanel>
        <ChartPanel kicker="QUALITY OVER TIME" title="数据质量趋势" question="随着持续扫描，摘要、OA、PDF 和去重覆盖是否真正改善？" note="每个时间点使用当时窗口内论文作为分母；历史快照缺失时不进行插值。">{data.quality_timeline && data.quality_timeline.length > 1 ? <QualityTimelineChart data={data.quality_timeline} /> : <ChartEmpty message="需要至少两个质量快照才能判断补全是否持续改善。" />}</ChartPanel>
      </div>
      <ChartPanel kicker="DOWNLOAD SOURCE PERFORMANCE" title="下载来源成功率" question="哪些合法全文来源贡献最多成功下载，失败集中在哪里？" note="同时展示尝试数、完成数和成功率；小样本 100% 不能与大样本来源直接比较。">{data.download_source_performance?.length ? <DownloadSourcePerformanceChart data={data.download_source_performance} /> : <ChartEmpty message="还没有足够下载任务形成来源级成功率。" />}</ChartPanel>
      <ChartPanel kicker="CROSS-SOURCE VALIDATION" title="数据源交叉覆盖矩阵" question="不同来源能互相验证多少论文，各自还贡献了多少独有记录？" note="格内显示占行来源的比例；悬停同时显示共同论文计数、行来源总数和比例。采用 canonical paper 身份归并，对角线为 100%。" className="source-overlap-wide">{data.source_overlap?.sources?.length ? <SourceOverlapChart data={data.source_overlap} /> : <ChartEmpty message="至少两个数据源完成身份归并后才显示交叉覆盖；没有矩阵时不得声称全网不重不漏。" />}</ChartPanel>

      <p className="analytics-footnote">生成 {formatDate(data.generated_at, true)} · 界面刷新 {updatedAt ? formatDate(updatedAt.toISOString(), true) : '—'} · 默认只统计真实、凝聚态 eligible、按 canonical paper 去重的记录。图表用于发现信号，不构成科研结论。</p>
    </>}
  </div>;
}
