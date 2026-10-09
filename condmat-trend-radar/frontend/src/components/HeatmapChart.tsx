import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';
import { metricLabel, Translation } from '../i18n';

export type HeatmapData = {
  months: string[];
  terms: string[];
  values: Array<[number, number, number]>;
  metric: string;
};

type Props = {
  data: HeatmapData;
  t: Translation;
  onConceptSelect?: (concept: string) => void;
};

export default function HeatmapChart({ data, t, onConceptSelect }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const maxValue = Math.max(1, ...data.values.map((item) => item[2]));
    const metricName = metricLabel(data.metric, t);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        formatter: (params: any) => {
          const [monthIndex, termIndex, value] = params.value;
          return `${t.concept}: ${data.terms[termIndex]}<br/>${t.month}: ${data.months[monthIndex]}<br/>${t.metric}: ${metricName}<br/>${t.value}: ${value.toFixed(2)}`;
        },
      },
      grid: { top: 24, right: 20, bottom: 70, left: 210 },
      xAxis: {
        type: 'category',
        data: data.months,
        axisLabel: { color: '#9eb0c7', rotate: 45 },
        axisLine: { lineStyle: { color: '#30445f' } },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: data.terms,
        axisLabel: { color: '#d7e2f0', fontSize: 12 },
        axisLine: { lineStyle: { color: '#30445f' } },
      },
      visualMap: {
        min: 0,
        max: maxValue,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 4,
        textStyle: { color: '#9eb0c7' },
        inRange: { color: ['#142438', '#1b5b72', '#27a189', '#f0cf65', '#f26d5b'] },
      },
      series: [
        {
          name: metricName,
          type: 'heatmap',
          data: data.values,
          emphasis: { itemStyle: { borderColor: '#ffffff', borderWidth: 1 } },
          progressive: 1000,
        },
      ],
    });
    chart.on('click', (params: any) => {
      const term = data.terms[params.value[1]];
      if (term && onConceptSelect) onConceptSelect(term);
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data, onConceptSelect, t]);

  return <div id="heatmap-chart" ref={ref} className="chart heatmap-chart" />;
}
