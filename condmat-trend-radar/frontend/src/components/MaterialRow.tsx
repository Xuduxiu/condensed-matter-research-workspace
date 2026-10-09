import { KeyboardEvent } from 'react';
import { MaterialSummary } from '../types';
import { TrendIndicator } from './primitives';

export default function MaterialRow({ material, rank, onOpen }: { material: MaterialSummary; rank?: number; onOpen: () => void }) {
  const keyOpen = (event: KeyboardEvent) => { if (event.key === 'Enter') onOpen(); };
  return <button className="material-row" type="button" onClick={onOpen} onKeyDown={keyOpen}>
    <span className="material-rank mono">{rank == null ? '—' : String(rank).padStart(2, '0')}</span>
    <span className="material-name"><strong>{material.canonical_name}</strong><small>{material.material_family}{material.phase ? ` · ${material.phase}` : ''} · 证据 {material.evidence_mentions ?? material.total_papers}</small></span>
    <span><small>7 日</small><strong>{material.count_7d}</strong></span>
    <span><small>30 日</small><strong>{material.count_30d}</strong></span>
    <span><small>月基线</small><strong>{material.monthly_baseline}</strong></span>
    <span className="preprint-cell"><small>预印本</small><TrendIndicator value={material.preprint_change} /></span>
    <span className="publication-cell"><small>见刊</small><TrendIndicator value={material.publication_change} /></span>
    <span><small>最近论文</small><strong className="date-value">{material.latest_paper_date || '—'}</strong></span>
    <span className="trend-score"><small>趋势</small><TrendIndicator value={Math.round(material.trend_score * 100)} /></span>
  </button>;
}
