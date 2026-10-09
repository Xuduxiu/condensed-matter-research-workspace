import { useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { apiGet } from '../api';
import { Translation } from '../i18n';

type Network = {
  nodes: Array<{ id: string; name: string; value: number }>;
  links: Array<{ source: string; target: string; value: number }>;
};

type Props = {
  t: Translation;
  onConceptSelect: (concept: string) => void;
};

export default function Cooccurrence({ t, onConceptSelect }: Props) {
  const [minCount, setMinCount] = useState(2);
  const [data, setData] = useState<Network | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    apiGet<Network>(`/api/cooccurrence?min_count=${minCount}`).then(setData);
  }, [minCount]);

  useEffect(() => {
    if (!data || !ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      backgroundColor: 'transparent',
      tooltip: {
        formatter: (params: any) => {
          if (params.dataType === 'edge') {
            return `${t.concept}: ${params.data.source} / ${params.data.target}<br/>${t.cooccurrenceCount}: ${params.data.value}`;
          }
          return `${t.concept}: ${params.data.name}<br/>${t.frequency}: ${params.data.value}`;
        },
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          data: data.nodes.map((node) => ({
            ...node,
            symbolSize: Math.max(10, Math.min(52, node.value * 4)),
            itemStyle: { color: '#27d5a7' },
            label: { show: node.value >= 3, color: '#d7e2f0' },
          })),
          links: data.links.map((link) => ({
            ...link,
            lineStyle: { width: Math.max(1, Math.min(8, link.value)), color: '#5e728d', opacity: 0.55 },
          })),
          force: { repulsion: 180, edgeLength: 90 },
          emphasis: { focus: 'adjacency' },
        },
      ],
    });
    chart.on('click', (params: any) => {
      if (params.dataType === 'node') onConceptSelect(params.data.name);
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data, onConceptSelect, t]);

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.cooccurrenceTitle}</p>
          <h2>{t.cooccurrenceSubtitle}</h2>
        </div>
        <label className="slider-control">
          <span>{t.minimumCount}</span>
          <input
            type="range"
            min={1}
            max={8}
            value={minCount}
            onChange={(event) => setMinCount(Number(event.target.value))}
          />
          <strong>{minCount}</strong>
        </label>
      </header>
      <div className="chart network-chart" ref={ref} />
    </div>
  );
}
