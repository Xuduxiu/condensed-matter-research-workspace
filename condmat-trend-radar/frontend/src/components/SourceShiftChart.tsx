import { useCallback, useMemo } from 'react';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';
import { useChart, type ChartPalette } from './chartTheme';

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export type SourceWindowItem = {
  name: string;
  current_count: number;
  baseline_count: number;
  current_share_pct: number;
  baseline_share_pct: number;
};

export default function SourceShiftChart({ data }: { data: SourceWindowItem[] }) {
  const items = useMemo(() => data.filter((item) => item.current_count + item.baseline_count > 0).slice(0, 12).reverse(), [data]);
  const option = useCallback(({ token, axisText, gridLine, tooltip }: ChartPalette): EChartsCoreOption => ({
    color: [token.accent, token['text-tertiary']],
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' }, ...tooltip,
      formatter: (params: Array<{ dataIndex?: number }>) => {
        const item = items[Number(params[0]?.dataIndex ?? 0)];
        return item ? `<b>${item.name}</b><br/>本期 ${item.current_count} 篇 · ${item.current_share_pct.toFixed(1)}%<br/>基线 ${item.baseline_count} 篇 · ${item.baseline_share_pct.toFixed(1)}%` : '';
      },
    },
    legend: { top: 0, right: 0, textStyle: { color: axisText }, data: ['本期来源份额', '基线来源份额'] },
    grid: { left: 125, right: 25, top: 44, bottom: 38 },
    xAxis: { type: 'value', nameTextStyle: { color: axisText }, name: '论文份额', min: 0, max: 100, axisLabel: { color: axisText, formatter: '{value}%' }, splitLine: { lineStyle: { color: gridLine } } },
    yAxis: { type: 'category', data: items.map((item) => item.name), axisLabel: { color: axisText, width: 108, overflow: 'truncate' }, axisLine: { lineStyle: { color: gridLine } } },
    series: [
      { name: '本期来源份额', type: 'bar', data: items.map((item) => item.current_share_pct), barMaxWidth: 14 },
      { name: '基线来源份额', type: 'bar', data: items.map((item) => item.baseline_share_pct), barMaxWidth: 14 },
    ],
  }), [items]);

  const ref = useChart(option);

  return <div className="analytics-chart" ref={ref} role="img" aria-label="本期与基线窗口论文来源份额对比" />;
}
