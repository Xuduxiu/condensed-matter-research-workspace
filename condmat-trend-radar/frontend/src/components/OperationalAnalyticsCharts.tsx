import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';

echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export type DownloadSourcePerformance = { name: string; attempted: number; completed: number; success_rate_pct: number };
export type CitationBucket = { name: string; count: number };

const axisText = '#9facbf';
const gridLine = '#253247';

function useChart(option: EChartsCoreOption) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [option]);
  return ref;
}

const tooltip = {
  backgroundColor: '#111925', borderColor: '#34445b', textStyle: { color: '#eef4ff', fontSize: 11 },
};

export function DownloadSourcePerformanceChart({ data }: { data: DownloadSourcePerformance[] }) {
  const items = useMemo(() => data.filter((item) => item.attempted > 0).slice(0, 12).reverse(), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: ['#61afef', '#98c379', '#e5c07b'],
    tooltip: {
      ...tooltip,
      trigger: 'axis',
      formatter: (params: Array<{ dataIndex?: number }>) => {
        const index = Number(params[0]?.dataIndex ?? 0);
        const item = items[index];
        return item ? `<b>${item.name}</b><br/>尝试 ${item.attempted}<br/>完成 ${item.completed}<br/>成功率 ${item.success_rate_pct.toFixed(1)}%` : '';
      },
    },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['尝试', '完成', '成功率'] },
    grid: { left: 125, right: 52, top: 44, bottom: 36 },
    xAxis: [
      { type: 'value', name: '任务数', minInterval: 1, axisLabel: { color: axisText }, splitLine: { lineStyle: { color: gridLine } } },
      { type: 'value', name: '成功率', min: 0, max: 100, position: 'top', axisLabel: { color: axisText, formatter: '{value}%' }, splitLine: { show: false } },
    ],
    yAxis: { type: 'category', data: items.map((item) => item.name), axisLabel: { color: axisText, width: 108, overflow: 'truncate' }, axisLine: { lineStyle: { color: gridLine } } },
    series: [
      { name: '尝试', type: 'bar', data: items.map((item) => item.attempted), barMaxWidth: 12 },
      { name: '完成', type: 'bar', data: items.map((item) => item.completed), barMaxWidth: 12 },
      { name: '成功率', type: 'line', xAxisIndex: 1, data: items.map((item) => item.success_rate_pct), symbolSize: 7 },
    ],
  }), [items]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="各下载来源尝试数完成数和成功率" />;
}

export function CitationDistributionChart({ data }: { data: CitationBucket[] }) {
  const option = useMemo<EChartsCoreOption>(() => ({
    color: ['#7c83ff'],
    tooltip: { ...tooltip, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 48, right: 22, top: 20, bottom: 42 },
    xAxis: { type: 'category', data: data.map((item) => item.name), axisLabel: { color: axisText }, axisLine: { lineStyle: { color: gridLine } } },
    yAxis: { type: 'value', name: '论文数', minInterval: 1, axisLabel: { color: axisText }, splitLine: { lineStyle: { color: gridLine } } },
    series: [{ name: '论文数', type: 'bar', data: data.map((item) => item.count), barMaxWidth: 30, itemStyle: { borderRadius: [3, 3, 0, 0] } }],
  }), [data]);
  const ref = useChart(option);
  return <div className="analytics-chart" ref={ref} role="img" aria-label="当前窗口论文引用次数分档" />;
}
