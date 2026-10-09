import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, HeatmapChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';

echarts.use([
  BarChart,
  HeatmapChart,
  LineChart,
  PieChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export type Distribution = { name: string; count: number; share_pct?: number };
export type TimelinePoint = { date: string; preprints: number; publications: number; total: number };
export type MonitorPoint = { date: string; hits: number };
export type MaterialSignal = {
  id: string;
  name: string;
  family?: string;
  paper_count: number;
  evidence_mentions?: number;
  preprints?: number;
  publications?: number;
};
export type MaterialBurstSignal = {
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
};
export type TrendSignal = {
  id?: string;
  name: string;
  class?: string;
  current_count: number;
  baseline_count: number;
  current_share_pct: number;
  baseline_share_pct: number;
  share_change_pp: number;
  growth_ratio?: number;
  momentum?: number;
  monthly?: Array<{ month: string; count: number; share_pct: number }>;
};
export type SourceOverlap = { sources: string[]; matrix: number[][]; paper_counts?: number[]; observed_papers?: number; multi_source_papers?: number; multi_source_coverage_pct?: number };
export type QualityPoint = {
  date?: string;
  month?: string;
  abstract_coverage_pct?: number;
  oa_coverage_pct?: number;
  pdf_coverage_pct?: number;
  doi_coverage_pct?: number;
};
export type PublicationPathway = {
  name?: string;
  from_source?: string;
  to_source?: string;
  count: number;
  median_lag_days?: number | null;
};

const axisText = '#9facbf';
const bodyText = '#dce6f4';
const gridLine = '#253247';
const colors = ['#61afef', '#c678dd', '#56b6c2', '#98c379', '#e5c07b', '#e06c75', '#7c83ff', '#73d4aa'];

function tooltipBase() {
  return {
    backgroundColor: '#111925',
    borderColor: '#34445b',
    textStyle: { color: '#eef4ff', fontSize: 11 },
    extraCssText: 'box-shadow:0 10px 28px rgba(0,0,0,.35);',
  };
}

function categoryAxis(data: string[], rotate = 0) {
  return {
    type: 'category' as const,
    data,
    axisLabel: { color: axisText, rotate, hideOverlap: true },
    axisLine: { lineStyle: { color: gridLine } },
    axisTick: { show: false },
  };
}

function valueAxis(name?: string) {
  return {
    type: 'value' as const,
    name,
    nameTextStyle: { color: axisText },
    minInterval: 1,
    axisLabel: { color: axisText },
    splitLine: { lineStyle: { color: gridLine } },
  };
}

function useChart(option: EChartsCoreOption | null, onClick?: (params: unknown) => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!ref.current || !option) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    if (onClick) chart.on('click', onClick);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [option, onClick]);
  return ref;
}

function movingAverage(values: number[], width: number) {
  return values.map((_, index) => {
    const start = Math.max(0, index - width + 1);
    const window = values.slice(start, index + 1);
    return Number((window.reduce((sum, value) => sum + value, 0) / window.length).toFixed(2));
  });
}

export function PaperPulseChart({ data }: { data: TimelinePoint[] }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const rolling = movingAverage(data.map((item) => item.total), 7);
    return {
      animationDuration: 450,
      color: [colors[0], colors[1], colors[3]],
      tooltip: { ...tooltipBase(), trigger: 'axis' },
      legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['预印本', '正式见刊', '7 日均线'] },
      grid: { left: 48, right: 22, top: 44, bottom: 42 },
      xAxis: categoryAxis(data.map((item) => item.date.slice(5))),
      yAxis: valueAxis('去重论文数'),
      series: [
        { name: '预印本', type: 'bar', stack: 'papers', data: data.map((item) => item.preprints), barMaxWidth: 18 },
        { name: '正式见刊', type: 'bar', stack: 'papers', data: data.map((item) => item.publications), barMaxWidth: 18 },
        { name: '7 日均线', type: 'line', smooth: true, showSymbol: false, data: rolling, lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.05 } },
      ],
    };
  }, [data]);
  const ref = useChart(option);
  return <div className="analytics-chart analytics-chart-wide" ref={ref} role="img" aria-label="逐日论文量、版本结构和七日移动平均" />;
}

export function HorizontalDistributionChart({ data, label, color = colors[2], limit = 12 }: { data: Distribution[]; label: string; color?: string; limit?: number }) {
  const items = useMemo(() => data.filter((item) => item.count > 0).slice(0, limit).reverse(), [data, limit]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [color],
    tooltip: { ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 142, right: 30, top: 16, bottom: 32 },
    xAxis: valueAxis('论文数'),
    yAxis: {
      ...categoryAxis(items.map((item) => item.name)),
      axisLabel: { color: axisText, width: 124, overflow: 'truncate' },
    },
    series: [{ name: label, type: 'bar', data: items.map((item) => item.count), barMaxWidth: 17, itemStyle: { borderRadius: [0, 3, 3, 0] } }],
  }), [color, items, label]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label={label} />;
}

export function DownloadStatusChart({ data }: { data: Array<{ status: string; count: number }> }) {
  const statusLabel: Record<string, string> = {
    completed: '已完成', pending: '等待', resolving: '解析中', downloading: '下载中',
    retryable_failed: '可重试失败', permanent_failed: '无合法 OA', manual_review: '人工复核', not_requested: '未请求',
  };
  const items = data.filter((item) => item.count > 0).map((item) => ({ ...item, label: statusLabel[item.status] || item.status }));
  const option = useMemo<EChartsCoreOption>(() => ({
    color: items.map((item) => item.status === 'completed' ? colors[3] : item.status.includes('failed') ? colors[5] : item.status === 'not_requested' ? '#536176' : colors[4]),
    tooltip: { ...tooltipBase(), trigger: 'item', formatter: '{b}<br/>{c} 篇 · {d}%' },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: axisText, fontSize: 10 } },
    series: [{
      name: '最新下载状态',
      type: 'pie',
      radius: ['48%', '72%'],
      center: ['50%', '43%'],
      minAngle: 2,
      label: { color: bodyText, formatter: '{b}\n{c}', fontSize: 9 },
      labelLine: { length: 8, length2: 5, lineStyle: { color: gridLine } },
      data: items.map((item) => ({ name: item.label, value: item.count })),
    }],
  }), [items]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="窗口内每篇论文的最新下载状态" />;
}

export function MonitorTimelineChart({ data }: { data: MonitorPoint[] }) {
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[4]],
    tooltip: { ...tooltipBase(), trigger: 'axis' },
    grid: { left: 42, right: 18, top: 20, bottom: 36 },
    xAxis: categoryAxis(data.map((item) => item.date.slice(5))),
    yAxis: valueAxis('命中数'),
    series: [{ name: '新增命中', type: 'line', smooth: true, showSymbol: false, data: data.map((item) => item.hits), lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.12 } }],
  }), [data]);
  const ref = useChart(option);
  return <div className="analytics-chart analytics-chart-small" ref={ref} role="img" aria-label="监控规则逐日新增命中" />;
}

export function MaterialVolumeChart({ data }: { data: MaterialSignal[] }) {
  const items = useMemo(() => [...data].sort((a, b) => b.paper_count - a.paper_count).slice(0, 12).reverse(), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[0], colors[1], colors[3]],
    tooltip: { ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['去重论文', '含预印本版本', '含见刊版本'] },
    grid: { left: 132, right: 22, top: 44, bottom: 34 },
    xAxis: valueAxis('篇'),
    yAxis: { ...categoryAxis(items.map((item) => item.name)), axisLabel: { color: axisText, width: 114, overflow: 'truncate' } },
    series: [
      { name: '去重论文', type: 'bar', data: items.map((item) => item.paper_count), barMaxWidth: 9 },
      { name: '含预印本版本', type: 'bar', data: items.map((item) => item.preprints ?? 0), barMaxWidth: 9 },
      { name: '含见刊版本', type: 'bar', data: items.map((item) => item.publications ?? 0), barMaxWidth: 9 },
    ],
  }), [items]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="窗口内材料论文量及预印本见刊版本结构" />;
}

export function MaterialStageChart({ data, onSelect }: { data: MaterialSignal[]; onSelect?: (id: string) => void }) {
  const items = useMemo(() => data.filter((item) => (item.preprints ?? 0) + (item.publications ?? 0) > 0), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[6]],
    tooltip: {
      ...tooltipBase(),
      formatter: (params: { data?: { item?: MaterialSignal; value?: number[] } }) => {
        const item = params.data?.item;
        const value = params.data?.value;
        if (!item || !value) return '';
        return `<b>${item.name}</b><br/>预印本先行信号 ${Number(value[0]).toFixed(1)}%<br/>去重论文 ${item.paper_count}<br/>证据片段 ${item.evidence_mentions ?? 0}`;
      },
    },
    grid: { left: 52, right: 26, top: 28, bottom: 48 },
    xAxis: { ...valueAxis('预印本先行信号 (%)'), min: 0, max: 100 },
    yAxis: valueAxis('去重论文数'),
    series: [{
      type: 'scatter',
      data: items.map((item) => {
        const denominator = (item.preprints ?? 0) + (item.publications ?? 0);
        return {
          value: [denominator ? (item.preprints ?? 0) * 100 / denominator : 0, item.paper_count, item.evidence_mentions ?? item.paper_count],
          item,
          symbolSize: Math.max(10, Math.min(42, 8 + Math.sqrt(item.evidence_mentions ?? item.paper_count) * 4)),
        };
      }),
      label: { show: true, formatter: (params: { data?: { item?: MaterialSignal } }) => params.data?.item?.name ?? '', color: bodyText, position: 'right', fontSize: 9 },
      itemStyle: { opacity: 0.82 },
      emphasis: { focus: 'self', scale: 1.25 },
    }],
  }), [items]);
  const handleClick = useMemo(() => onSelect ? (params: unknown) => {
    const item = (params as { data?: { item?: MaterialSignal } }).data?.item;
    if (item) onSelect(item.id);
  } : undefined, [onSelect]);
  const ref = useChart(option, handleClick);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="材料预印本先行程度与论文规模矩阵" />;
}

export function MaterialBurstChart({ data, onSelect }: { data: MaterialBurstSignal[]; onSelect?: (id: string) => void }) {
  const items = useMemo(() => data.filter((item) => item.count_30d > 0).slice(0, 14).reverse(), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[4], colors[0]],
    tooltip: {
      ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (params: Array<{ data?: { item?: MaterialBurstSignal } }>) => {
        const item = params[0]?.data?.item;
        if (!item) return '';
        return `<b>${item.canonical_name}</b><br/>近 30 日 ${item.count_30d} 篇<br/>过去一年月均 ${item.monthly_baseline.toFixed(1)} 篇<br/>相对偏离 ${item.trend_score >= 0 ? '+' : ''}${(item.trend_score * 100).toFixed(0)}%<br/>近 30 日：预印本 ${item.preprint_30d} · 见刊 ${item.publication_30d}`;
      },
    },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['近 30 日', '过去一年月均'] },
    grid: { left: 130, right: 22, top: 42, bottom: 34 },
    xAxis: valueAxis('论文数'),
    yAxis: { ...categoryAxis(items.map((item) => item.canonical_name)), axisLabel: { color: axisText, width: 112, overflow: 'truncate' } },
    series: [
      { name: '近 30 日', type: 'bar', data: items.map((item) => ({ value: item.count_30d, item })), barMaxWidth: 12 },
      { name: '过去一年月均', type: 'bar', data: items.map((item) => ({ value: item.monthly_baseline, item })), barMaxWidth: 12 },
    ],
  }), [items]);
  const handleClick = useMemo(() => onSelect ? (params: unknown) => {
    const item = (params as { data?: { item?: MaterialBurstSignal } }).data?.item;
    if (item) onSelect(item.id);
  } : undefined, [onSelect]);
  const ref = useChart(option, handleClick);
  return <div className="analytics-chart analytics-chart-tall" ref={ref} role="img" aria-label="材料近三十日论文量与过去一年月均对比" />;
}

function selectTrendItems(data: TrendSignal[], limit = 16) {
  const sorted = [...data].filter((item) => Number.isFinite(item.share_change_pp)).sort((a, b) => b.share_change_pp - a.share_change_pp);
  if (sorted.length <= limit) return sorted.reverse();
  const positive = sorted.slice(0, Math.ceil(limit * 0.6));
  const negative = sorted.slice(-Math.floor(limit * 0.4));
  return [...positive, ...negative].sort((a, b) => a.share_change_pp - b.share_change_pp);
}

export function ShareShiftChart({ data, label }: { data: TrendSignal[]; label: string }) {
  const items = useMemo(() => selectTrendItems(data), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    tooltip: {
      ...tooltipBase(),
      formatter: (params: { data?: { item?: TrendSignal } }) => {
        const item = params.data?.item;
        if (!item) return '';
        return `<b>${item.name}</b><br/>本期 ${item.current_count} 篇 · ${item.current_share_pct.toFixed(3)}%<br/>基线 ${item.baseline_count} 篇 · ${item.baseline_share_pct.toFixed(3)}%<br/>份额变化 ${item.share_change_pp >= 0 ? '+' : ''}${item.share_change_pp.toFixed(3)} 个百分点`;
      },
    },
    grid: { left: 148, right: 34, top: 18, bottom: 36 },
    xAxis: {
      type: 'value', name: '论文份额变化（百分点）', nameTextStyle: { color: axisText },
      axisLabel: { color: axisText, formatter: (value: number) => `${value > 0 ? '+' : ''}${value.toFixed(2)}` },
      splitLine: { lineStyle: { color: gridLine } },
    },
    yAxis: { ...categoryAxis(items.map((item) => item.name)), axisLabel: { color: axisText, width: 130, overflow: 'truncate' } },
    series: [{
      name: label,
      type: 'bar',
      data: items.map((item) => ({
        value: item.share_change_pp,
        item,
        itemStyle: { color: item.share_change_pp >= 0 ? colors[3] : colors[5], borderRadius: item.share_change_pp >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3] },
      })),
      barMaxWidth: 16,
    }],
  }), [items, label]);
  const ref = useChart(option);
  return <div className="analytics-chart analytics-chart-tall" ref={ref} role="img" aria-label={`${label}等长窗口论文份额变化`} />;
}

export function TrendHeatmapChart({ data }: { data: TrendSignal[] }) {
  const prepared = useMemo(() => {
    const items = [...data]
      .filter((item) => item.monthly?.length)
      .sort((a, b) => b.share_change_pp - a.share_change_pp)
      .slice(0, 20);
    const months = Array.from(new Set(items.flatMap((item) => item.monthly?.map((point) => point.month) ?? []))).sort();
    const values: Array<[number, number, number]> = [];
    items.forEach((item, termIndex) => {
      const byMonth = new Map((item.monthly ?? []).map((point) => [point.month, point.share_pct]));
      months.forEach((month, monthIndex) => values.push([monthIndex, termIndex, byMonth.get(month) ?? 0]));
    });
    return { items, months, values };
  }, [data]);
  const max = Math.max(0.001, ...prepared.values.map((item) => item[2]));
  const option = useMemo<EChartsCoreOption>(() => ({
    tooltip: {
      ...tooltipBase(),
      formatter: (params: { value?: number[] }) => {
        const value = params.value;
        if (!value) return '';
        return `<b>${prepared.items[value[1]]?.name ?? ''}</b><br/>${prepared.months[value[0]] ?? ''}<br/>论文份额 ${Number(value[2]).toFixed(3)}%`;
      },
    },
    grid: { left: 148, right: 20, top: 20, bottom: 76 },
    xAxis: { ...categoryAxis(prepared.months, 42), splitArea: { show: true } },
    yAxis: { ...categoryAxis(prepared.items.map((item) => item.name)), axisLabel: { color: axisText, width: 130, overflow: 'truncate' }, splitArea: { show: true } },
    visualMap: {
      min: 0, max, calculable: true, orient: 'horizontal', left: 'center', bottom: 4,
      textStyle: { color: axisText }, inRange: { color: ['#121d2d', '#1d5066', '#2c8e84', '#e5c07b', '#e06c75'] },
    },
    series: [{ type: 'heatmap', data: prepared.values, emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1 } }, progressive: 600 }],
  }), [max, prepared]);
  const ref = useChart(prepared.items.length ? option : null);
  return prepared.items.length
    ? <div className="analytics-chart analytics-chart-tall" ref={ref} role="img" aria-label="近期主题逐月论文份额热力图" />
    : <ChartEmpty message="主题月度序列尚未生成。" />;
}

export function FieldDistributionChart({ data }: { data: Distribution[] }) {
  const labels: Record<string, string> = { mesoscopic_and_electronic: '介观与电子结构', materials_science: '材料科学', strongly_correlated: '强关联体系', statistical_mechanics: '统计物理', soft_matter: '软物质', superconductivity: '超导', quantum_gases: '量子气体', disorder_and_neural_networks: '无序与复杂系统', other_condensed_matter: '其他凝聚态', unclassified: '未分类' };
  const items = data.filter((item) => item.count > 0).map((item) => ({ ...item, name: labels[item.name] || item.name }));
  const option = useMemo<EChartsCoreOption>(() => ({
    color: colors,
    tooltip: { ...tooltipBase(), trigger: 'item', formatter: '{b}<br/>{c} 篇 · {d}%' },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: axisText, fontSize: 10 } },
    series: [{
      name: '主研究方向', type: 'pie', radius: ['42%', '70%'], center: ['50%', '43%'], minAngle: 2,
      label: { color: bodyText, formatter: '{b}\n{d}%', fontSize: 9 },
      labelLine: { length: 8, length2: 5, lineStyle: { color: gridLine } },
      data: items.map((item) => ({ name: item.name, value: item.count })),
    }],
  }), [items]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="凝聚态论文主研究方向分布" />;
}

export function SourceOverlapChart({ data }: { data: SourceOverlap }) {
  const values = useMemo(() => {
    const points: Array<[number, number, number, number]> = [];
    data.sources.forEach((_, row) => data.sources.forEach((__, column) => {
      const raw = Number(data.matrix[row]?.[column] ?? 0);
      const base = Number(data.paper_counts?.[row] ?? data.matrix[row]?.[row] ?? 0);
      points.push([column, row, base > 0 ? raw * 100 / base : 0, raw]);
    }));
    return points;
  }, [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    tooltip: {
      ...tooltipBase(),
      formatter: (params: { value?: number[] }) => {
        const value = params.value;
        if (!value) return '';
        const rowTotal = Number(data.paper_counts?.[value[1]] ?? data.matrix[value[1]]?.[value[1]] ?? 0);
        return `<b>${data.sources[value[1]]} → ${data.sources[value[0]]}</b><br/>共同论文 ${value[3]} 篇<br/>行来源总数 ${rowTotal} 篇<br/>占行来源 ${Number(value[2]).toFixed(1)}%`;
      },
    },
    grid: { left: 100, right: 22, top: 20, bottom: 78 },
    xAxis: { ...categoryAxis(data.sources, 35), splitArea: { show: true } },
    yAxis: { ...categoryAxis(data.sources), splitArea: { show: true } },
    visualMap: { min: 0, max: 100, calculable: true, orient: 'horizontal', left: 'center', bottom: 4, textStyle: { color: axisText }, inRange: { color: ['#142238', '#24627b', '#56b6c2', '#e5c07b'] } },
    series: [{ type: 'heatmap', data: values, label: { show: data.sources.length <= 7, color: '#f2f7ff', formatter: (params: { value?: number[] }) => `${Number(params.value?.[2] ?? 0).toFixed(0)}%` } }],
  }), [data, values]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="多个论文数据源之间的交叉覆盖率" />;
}

export function QualityTimelineChart({ data }: { data: QualityPoint[] }) {
  const labels = data.map((item) => (item.date ?? item.month ?? '').slice(0, 10));
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[0], colors[3], colors[6], colors[4]],
    tooltip: { ...tooltipBase(), trigger: 'axis' },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['摘要', 'DOI', '开放获取', '本地 PDF'] },
    grid: { left: 46, right: 22, top: 44, bottom: 42 },
    xAxis: categoryAxis(labels),
    yAxis: { ...valueAxis('覆盖率'), min: 0, max: 100, axisLabel: { color: axisText, formatter: '{value}%' } },
    series: [
      { name: '摘要', type: 'line', smooth: true, showSymbol: false, connectNulls: false, data: data.map((item) => item.abstract_coverage_pct ?? null) },
      { name: 'DOI', type: 'line', smooth: true, showSymbol: false, connectNulls: false, data: data.map((item) => item.doi_coverage_pct ?? null) },
      { name: '开放获取', type: 'line', smooth: true, showSymbol: false, connectNulls: false, data: data.map((item) => item.oa_coverage_pct ?? null) },
      { name: '本地 PDF', type: 'line', smooth: true, showSymbol: false, connectNulls: false, data: data.map((item) => item.pdf_coverage_pct ?? null) },
    ],
  }), [data, labels]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="摘要 DOI 开放获取和本地文件覆盖随时间变化" />;
}

export function PublicationPathwaysChart({ data }: { data: PublicationPathway[] }) {
  const items = data.filter((item) => item.count > 0).slice(0, 12).reverse();
  const pathwayLabels: Record<string, string> = { preprint_only: '仅预印本', publication_only: '仅正式见刊', linked_preprint_publication: '预印本 → 正式见刊' };
  const labels = items.map((item) => pathwayLabels[item.name ?? ''] || item.name || [item.from_source, item.to_source].filter(Boolean).join(' → ') || '未知路径');
  const option = useMemo<EChartsCoreOption>(() => ({
    color: [colors[2], colors[4]],
    tooltip: { ...tooltipBase(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['论文数', '中位转化天数'] },
    grid: { left: 142, right: 50, top: 44, bottom: 34 },
    xAxis: [valueAxis('论文数'), { ...valueAxis('天'), position: 'top', splitLine: { show: false } }],
    yAxis: { ...categoryAxis(labels), axisLabel: { color: axisText, width: 124, overflow: 'truncate' } },
    series: [
      { name: '论文数', type: 'bar', data: items.map((item) => item.count), barMaxWidth: 15 },
      { name: '中位转化天数', type: 'line', xAxisIndex: 1, data: items.map((item) => item.median_lag_days ?? null), symbolSize: 7, connectNulls: false },
    ],
  }), [items, labels]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="预印本到正式发表的主要路径与转化时间" />;
}

export function ChartEmpty({ message }: { message: string }) {
  return <div className="analytics-chart-empty"><span aria-hidden="true">⌁</span><strong>统计尚未形成</strong><p>{message}</p></div>;
}
