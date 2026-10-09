import { useEffect, useMemo, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export type SourceWindowItem = {
  name: string;
  current_count: number;
  baseline_count: number;
  current_share_pct: number;
  baseline_share_pct: number;
};

export default function SourceShiftChart({ data }: { data: SourceWindowItem[] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const items = useMemo(() => data.filter((item) => item.current_count + item.baseline_count > 0).slice(0, 12).reverse(), [data]);
  const option = useMemo<EChartsCoreOption>(() => ({
    color: ['#61afef', '#536176'],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: '#111925', borderColor: '#34445b', textStyle: { color: '#eef4ff', fontSize: 11 },
      formatter: (params: Array<{ dataIndex?: number }>) => {
        const item = items[Number(params[0]?.dataIndex ?? 0)];
        return item ? `<b>${item.name}</b><br/>本期 ${item.current_count} 篇 · ${item.current_share_pct.toFixed(1)}%<br/>基线 ${item.baseline_count} 篇 · ${item.baseline_share_pct.toFixed(1)}%` : '';
      },
    },
    legend: { top: 0, right: 0, textStyle: { color: '#9facbf' }, data: ['本期来源份额', '基线来源份额'] },
    grid: { left: 125, right: 25, top: 44, bottom: 38 },
    xAxis: { type: 'value', name: '论文份额', min: 0, max: 100, axisLabel: { color: '#9facbf', formatter: '{value}%' }, splitLine: { lineStyle: { color: '#253247' } } },
    yAxis: { type: 'category', data: items.map((item) => item.name), axisLabel: { color: '#9facbf', width: 108, overflow: 'truncate' }, axisLine: { lineStyle: { color: '#253247' } } },
    series: [
      { name: '本期来源份额', type: 'bar', data: items.map((item) => item.current_share_pct), barMaxWidth: 14 },
      { name: '基线来源份额', type: 'bar', data: items.map((item) => item.baseline_share_pct), barMaxWidth: 14 },
    ],
  }), [items]);

  useEffect(() => {
    if (!ref.current) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption(option);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [option]);

  return <div className="analytics-chart" ref={ref} role="img" aria-label="本期与基线窗口论文来源份额对比" />;
}
