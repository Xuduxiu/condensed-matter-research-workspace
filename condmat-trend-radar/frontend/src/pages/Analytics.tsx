import { useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';
import { apiGet, apiPostJson } from '../api';
import { formatDate, formatNumber, useApiData } from '../hooks';
import { ErrorState, LoadingSkeleton, MetricStrip, StatusBadge } from '../components/primitives';

type Distribution = { name: string; count: number };
type AnalyticsData = {
  generated_at: string;
  data_anchor: string | null;
  range_from: string | null;
  range_to: string | null;
  days: number;
  summary: {
    window_papers: number;
    preprints: number;
    publications: number;
    abstract_available: number;
    abstract_missing: number;
    abstract_coverage_pct: number;
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
  top_materials: Array<{ id: string; name: string; family: string; paper_count: number; evidence_mentions?: number; latest_paper_date?: string | null; preprints?: number; publications?: number }>;
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
echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);

const text = '#aebbd0';
const grid = '#233044';
const colors = ['#61afef', '#c678dd', '#56b6c2', '#98c379', '#e5c07b', '#e06c75', '#7c83ff'];

function useChart(option: EChartsCoreOption | null) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!ref.current || !option) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [option]);
  return ref;
}

function PaperTimeline({ data }: { data: AnalyticsData['paper_timeline'] }) {
  const option = useMemo<EChartsCoreOption>(() => ({
    animationDuration: 450,
    color: [colors[0], colors[1], colors[3]],
    tooltip: { trigger: 'axis', backgroundColor: '#121923', borderColor: '#34445b', textStyle: { color: '#eef4ff' } },
    legend: { top: 0, right: 0, textStyle: { color: text }, data: ['预印本', '正式见刊', '合计'] },
    grid: { left: 42, right: 18, top: 42, bottom: 36 },
    xAxis: { type: 'category', data: data.map((item) => item.date.slice(5)), boundaryGap: true, axisLabel: { color: text, hideOverlap: true }, axisLine: { lineStyle: { color: grid } } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } } },
    series: [
      { name: '预印本', type: 'bar', stack: 'papers', data: data.map((item) => item.preprints), itemStyle: { borderRadius: [2, 2, 0, 0] }, barMaxWidth: 18 },
      { name: '正式见刊', type: 'bar', stack: 'papers', data: data.map((item) => item.publications), itemStyle: { borderRadius: [2, 2, 0, 0] }, barMaxWidth: 18 },
      { name: '合计', type: 'line', smooth: true, showSymbol: false, data: data.map((item) => item.total), lineStyle: { width: 2 }, areaStyle: { opacity: .05 } },
    ],
  }), [data]);
  const ref = useChart(option);
  return <div className="analytics-chart analytics-chart-wide" ref={ref} role="img" aria-label="论文发现时间线" />;
}

function Donut({ data, label }: { data: Distribution[]; label: string }) {
  const option = useMemo<EChartsCoreOption>(() => ({
    color: colors,
    tooltip: { trigger: 'item', formatter: '{b}<br/>{c} · {d}%', backgroundColor: '#121923', borderColor: '#34445b', textStyle: { color: '#eef4ff' } },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: text, fontSize: 10 } },
    series: [{
      name: label,
      type: 'pie',
      radius: ['46%', '70%'],
      center: ['50%', '43%'],
      avoidLabelOverlap: true,
      minAngle: 3,
      label: { color: text, formatter: '{b}\n{c}', fontSize: 9 },
      labelLine: { length: 8, length2: 5, lineStyle: { color: grid } },
      data: data.map((item) => ({ name: item.name, value: item.count })),
    }],
  }), [data, label]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label={label} />;
}

function HorizontalBars({ data, label }: { data: Distribution[]; label: string }) {
  const items = data.slice(0, 10).reverse();
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[2]],
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#121923', borderColor: '#34445b', textStyle: { color: '#eef4ff' } },
    grid: { left: 128, right: 20, top: 8, bottom: 24 },
    xAxis: { type: 'value', minInterval: 1, axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } } },
    yAxis: { type: 'category', data: items.map((item) => item.name), axisLabel: { color: text, width: 112, overflow: 'truncate' }, axisLine: { lineStyle: { color: grid } } },
    series: [{ name: label, type: 'bar', data: items.map((item) => item.count), barMaxWidth: 16, itemStyle: { borderRadius: [0, 3, 3, 0] } }],
  }), [items, label]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label={label} />;
}

function MonitorTimeline({ data }: { data: AnalyticsData['monitor_timeline'] }) {
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[4]],
    tooltip: { trigger: 'axis', backgroundColor: '#121923', borderColor: '#34445b', textStyle: { color: '#eef4ff' } },
    grid: { left: 38, right: 14, top: 12, bottom: 28 },
    xAxis: { type: 'category', data: data.map((item) => item.date.slice(5)), axisLabel: { color: text, hideOverlap: true }, axisLine: { lineStyle: { color: grid } } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { color: text }, splitLine: { lineStyle: { color: grid } } },
    series: [{ name: '命中', type: 'line', smooth: true, showSymbol: false, data: data.map((item) => item.hits), lineStyle: { width: 3 }, areaStyle: { opacity: .12 } }],
  }), [data]);
  const ref = useChart(option);
  return <div className="analytics-chart analytics-chart-small" ref={ref} role="img" aria-label="监控命中时间线" />;
}

function CoverageBar({ label, value, detail, tone }: { label: string; value: number; detail: string; tone: string }) {
  return <div className="coverage-row"><div><strong>{label}</strong><span>{detail}</span></div><b>{value.toFixed(1)}%</b><div className="coverage-track"><i className={tone} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div></div>;
}

export default function Analytics({ onSelectMaterial, onSelectPaper }: { onSelectMaterial: (id: string) => void; onSelectPaper: (id: string) => void }) {
  const [days, setDays] = useState(90);
  const { data, error, loading, refresh, updatedAt } = useApiData<AnalyticsData>(
    (signal) => apiGet(`/api/ui-v2/analytics?days=${days}`, { signal, cacheMs: 30_000 }),
    [days],
  );  const brief = useApiData<RadarBriefResponse>((signal) => apiGet(`/api/library/ai/radar-brief?days=${Math.min(days, 90)}`, { signal, cacheMs: 20_000 }), [days]);
  const aiStatus = useApiData<{ configured: boolean; fast_model: string }>((signal) => apiGet('/api/library/ai/status', { signal, cacheMs: 20_000 }), []);
  const [briefBusy, setBriefBusy] = useState(false);
  const [briefMessage, setBriefMessage] = useState('');
  const generateBrief = async () => {
    setBriefBusy(true); setBriefMessage('正在基于本地论文证据生成雷达简报…');
    try {
      const result = await apiPostJson<RadarBriefResponse>('/api/library/ai/radar-brief', { days: Math.min(days, 90), tier: 'fast', force: Boolean(brief.data?.available), limit: 20 });
      setBriefMessage(result.cached ? '已读取同一证据窗口的本地缓存。' : 'DeepSeek 雷达简报已生成并保存到本机数据库。');
      brief.refresh();
    } catch (reason) { setBriefMessage(reason instanceof Error ? reason.message : '雷达简报生成失败。'); }
    finally { setBriefBusy(false); }
  };

  return <div className="page-stack analytics-page">
    <section className="command-strip analytics-command">
      <div><StatusBadge tone={error ? 'danger' : 'success'}>{error ? '统计异常' : '真实数据库'}</StatusBadge><span>统计锚点 {data?.data_anchor || '—'} · {data?.range_from || '—'} 至 {data?.range_to || '—'}</span></div>
      <div><label>时间窗口<select value={days} onChange={(event) => setDays(Number(event.target.value))}><option value={7}>7 天</option><option value={30}>30 天</option><option value={90}>90 天</option><option value={180}>180 天</option><option value={365}>365 天</option></select></label><button className="icon-button" type="button" aria-label="刷新统计" onClick={refresh}>↻</button></div>
    </section>
    {loading && <LoadingSkeleton rows={10} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <>
      <MetricStrip items={[
        { label: `近 ${data.days} 天论文`, value: data.summary.window_papers, tone: 'accent' },
        { label: '预印本', value: data.summary.preprints, tone: 'preprint' },
        { label: '正式见刊', value: data.summary.publications, tone: 'publication' },
        { label: '摘要覆盖', value: `${data.summary.abstract_coverage_pct.toFixed(1)}%`, tone: data.summary.abstract_coverage_pct >= 80 ? 'success' : 'warning' },
        { label: '开放获取', value: `${data.summary.oa_coverage_pct.toFixed(1)}%`, tone: 'success' },
        { label: '本地 PDF', value: `${data.summary.pdf_coverage_pct.toFixed(1)}%`, tone: 'accent' },
        { label: '监控命中', value: data.summary.monitor_hits, tone: 'warning' },
        { label: '下载成功率', value: `${data.summary.download_success_rate_pct.toFixed(1)}%`, tone: data.summary.download_success_rate_pct >= 70 ? 'success' : 'danger' },
      ]} />

      <section className="section-block analytics-panel ai-brief-panel">
        <header className="section-header"><div><span className="section-kicker">DEEPSEEK EVIDENCE BRIEF</span><h2>AI 雷达简报</h2></div><div className="ai-brief-actions"><StatusBadge tone={aiStatus.data?.configured ? 'success' : 'warning'}>{aiStatus.data?.configured ? aiStatus.data.fast_model : 'AI 未就绪'}</StatusBadge><button className="button primary" type="button" disabled={briefBusy || aiStatus.loading || !aiStatus.data?.configured} onClick={generateBrief}>{briefBusy ? '生成中…' : brief.data?.available ? '重新生成' : '生成雷达简报'}</button></div></header>
        {briefMessage && <p className="inline-message" role="status">{briefMessage}</p>}
        {brief.loading && <LoadingSkeleton rows={3} />}
        {brief.error && <ErrorState message={brief.error.message} offline={brief.error.code === 'offline'} onRetry={brief.refresh} />}
        {!brief.loading && !brief.error && !brief.data?.available && <div className="ai-brief-empty"><strong>尚未生成这个时间窗口的简报</strong><p>点击后只发送最近 {Math.min(days, 90)} 天最多 20 篇论文的本地标题、摘要、材料与主题；不会在后台自动消耗额度。</p></div>}
        {brief.data?.available && brief.data.analysis && <div className="ai-brief-content"><div className="ai-brief-summary"><small>{brief.data.evidence?.window_from || '—'} — {brief.data.evidence?.window_to || '—'} · 证据 {brief.data.evidence?.paper_count ?? 0} 篇 · {brief.data.cached ? '缓存' : '新生成'} · {formatDate(brief.data.updated_at, true)}</small><h3>{brief.data.analysis.headline}</h3><p>{brief.data.analysis.summary_zh}</p><div className="detail-tags">{brief.data.analysis.rising_topics.map((item) => <span className="tag" key={`topic-${item}`}>{item}</span>)}{brief.data.analysis.notable_materials.map((item) => <span className="tag material" key={`material-${item}`}>{item}</span>)}</div>{brief.data.analysis.caveats.length > 0 && <p className="ai-caveats">证据边界：{brief.data.analysis.caveats.join('；')}</p>}</div><div className="ai-reading-list"><strong>优先阅读</strong>{brief.data.analysis.papers_to_read.map((item) => <button type="button" key={item.canonical_paper_id} onClick={() => onSelectPaper(item.canonical_paper_id)}><span>{item.title}</span><small>{item.reason}</small></button>)}{!brief.data.analysis.papers_to_read.length && <p className="muted">模型没有在当前证据中指定优先论文。</p>}</div></div>}
      </section>
      <section className="section-block analytics-panel timeline-panel">
        <header className="section-header"><div><span className="section-kicker">PAPER PULSE</span><h2>论文发现时间线</h2></div><span className="muted">逐日真实论文版本 · 数据锚点 {data.data_anchor || '—'}</span></header>
        <PaperTimeline data={data.paper_timeline} />
      </section>

      <div className="analytics-grid">
        <section className="section-block analytics-panel"><header className="section-header"><div><span className="section-kicker">SOURCE MIX</span><h2>论文来源</h2></div></header><Donut data={data.source_distribution} label="来源分布" /></section>
        <section className="section-block analytics-panel"><header className="section-header"><div><span className="section-kicker">JOURNAL SIGNAL</span><h2>活跃期刊</h2></div></header><HorizontalBars data={data.journal_distribution} label="论文数" /></section>
        <section className="section-block analytics-panel"><header className="section-header"><div><span className="section-kicker">DOWNLOAD FUNNEL</span><h2>下载任务结构</h2></div></header><Donut data={data.download_status.map((item) => ({ name: ({ completed: '已完成', pending: '等待', resolving: '解析中', downloading: '下载中', retryable_failed: '可重试失败', permanent_failed: '无合法 OA', manual_review: '人工复核', not_requested: '未请求' } as Record<string, string>)[item.status] || item.status, count: item.count }))} label="下载状态" /></section>
        <section className="section-block analytics-panel coverage-panel"><header className="section-header"><div><span className="section-kicker">DATA COMPLETENESS</span><h2>资料完整度</h2></div></header><div className="coverage-list"><CoverageBar label="摘要" value={data.summary.abstract_coverage_pct} detail={`${formatNumber(data.summary.abstract_available)} 有摘要 · ${formatNumber(data.summary.abstract_missing)} 缺失`} tone="abstract" /><CoverageBar label="开放获取" value={data.summary.oa_coverage_pct} detail={`${formatNumber(data.summary.oa_count)} 篇 OA`} tone="oa" /><CoverageBar label="本地 PDF" value={data.summary.pdf_coverage_pct} detail={`${formatNumber(data.summary.pdf_count)} 份文件`} tone="pdf" /></div></section>
      </div>

      <div className="analytics-lower">
        <section className="section-block analytics-panel"><header className="section-header"><div><span className="section-kicker">MONITOR ACTIVITY</span><h2>监控命中趋势</h2></div></header><MonitorTimeline data={data.monitor_timeline} /></section>
        <section className="section-block analytics-panel material-leaders"><header className="section-header"><div><span className="section-kicker">MATERIAL LEADERS</span><h2>窗口内材料领跑</h2></div><span className="muted">来自论文证据</span></header><div>{data.top_materials.map((item, index) => <button type="button" key={item.id} onClick={() => onSelectMaterial(item.id)}><b>{String(index + 1).padStart(2, '0')}</b><span><strong>{item.name}</strong><small>{item.family || '未分类'}</small></span><i>{item.paper_count}</i><em>预 {item.preprints ?? 0} · 刊 {item.publications ?? 0}</em></button>)}{!data.top_materials.length && <p className="muted analytics-empty">当前窗口没有可聚合的材料证据。</p>}</div></section>
      </div>
      <p className="analytics-footnote">生成 {formatDate(data.generated_at, true)} · 界面刷新 {updatedAt ? formatDate(updatedAt.toISOString(), true) : '—'} · 所有指标只统计真实且通过凝聚态筛选的论文。</p>
    </>}
  </div>;
}
