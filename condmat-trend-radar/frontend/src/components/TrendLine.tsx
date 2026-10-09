import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import { Translation } from '../i18n';

type Point = {
  month: string;
  raw_freq: number;
  weighted_freq: number;
  momentum: number;
  normalized_share?: number;
  weighted_normalized_share?: number;
};

type Props = {
  series: Point[];
  title: string;
  t: Translation;
};

export default function TrendLine({ series, title, t }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis' },
      legend: { top: 0, textStyle: { color: '#aebbd0' } },
      grid: { left: 50, right: 20, top: 48, bottom: 42 },
      title: { text: title, left: 0, textStyle: { color: '#e8eef7', fontSize: 14 } },
      xAxis: {
        type: 'category',
        data: series.map((point) => point.month),
        axisLabel: { color: '#9eb0c7' },
        axisLine: { lineStyle: { color: '#30445f' } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: '#9eb0c7' },
        splitLine: { lineStyle: { color: '#203149' } },
      },
      series: [
        {
          name: t.weightedFrequency,
          type: 'line',
          smooth: false,
          showSymbol: true,
          symbolSize: 5,
          connectNulls: false,
          data: series.map((point) => point.weighted_freq),
          lineStyle: { width: 3, color: '#27d5a7' },
          areaStyle: { color: 'rgba(39, 213, 167, 0.12)' },
        },
        {
          name: t.momentum,
          type: 'line',
          smooth: false,
          showSymbol: true,
          symbolSize: 5,
          connectNulls: false,
          data: series.map((point) => point.momentum),
          lineStyle: { width: 2, color: '#f0cf65' },
        },
      ],
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [series, title, t]);

  return <div className="chart trend-chart" ref={ref} />;
}
