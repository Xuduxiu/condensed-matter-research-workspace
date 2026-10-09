import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import StatCard from '../components/StatCard';
import { Translation } from '../i18n';

const DEFAULT_PRESET = 'strict_core_published';
const WATCH_CONCEPTS = ['moir\u00e9 superlattice', 'fractional Chern insulator', 'altermagnetism', 'ZrTe5'];

type Ranked = { term: string; raw_freq?: number; momentum?: number; score?: number; growth?: number; count?: number; published_raw_freq?: number; preprint_raw_freq?: number };
type OverviewData = {
  total_papers: number;
  latest_month: string | null;
  this_month_papers: number;
  corpus_size: {
    active_paper_count: number;
    real_paper_count: number;
    strict_condmat_paper_count: number;
    strict_core_published_paper_count?: number;
    excluded_non_condmat_paper_count: number;
    latest_quality_report_path?: string | null;
    current_corpus_label: string;
  };
  hot_concepts: Ranked[];
  rising_concepts: Ranked[];
  cooling_concepts: Ranked[];
  top_materials: Ranked[];
  top_methods: Ranked[];
};
type SplitData = { published_heat: Ranked[]; preprint_rising: Ranked[]; validation_gap: Ranked[]; cooling_published: Ranked[] };
type CompareData = { series: Array<{ concept: string; points: Array<{ month: string; value: number }> }> };
type Evidence = { concept: string; key_papers: Array<{ title: string; journal: string; publication_date: string; doi?: string; cited_by_count?: number }>; associated_methods: Ranked[]; associated_materials: Ranked[]; evidence_missing: string[] };

type Props = { onConceptSelect: (concept: string) => void; t: Translation };

export default function ResearchRadar({ onConceptSelect, t }: Props) {
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [split, setSplit] = useState<SplitData | null>(null);
  const [compare, setCompare] = useState<CompareData | null>(null);
  const [concept, setConcept] = useState('fractional Chern insulator');
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [llmResult, setLlmResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    apiGet<OverviewData>(`/api/overview?scope=core&corpus_preset=${DEFAULT_PRESET}`)
      .then(setOverview)
      .catch((err) => setError(err.message));
    apiGet<SplitData>('/api/published-preprint/top?limit=12')
      .then(setSplit)
      .catch(() => setSplit(null));
  }, []);

  useEffect(() => {
    const params = new URLSearchParams({
      concepts: WATCH_CONCEPTS.join(','),
      metric: 'momentum',
      scope: 'all',
      smoothing: 'raw',
      corpus_preset: DEFAULT_PRESET,
      from: '2015-01',
      to: '2026-07',
    });
    apiGet<CompareData>(`/api/compare?${params.toString()}`).then(setCompare).catch(() => setCompare(null));
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (!concept.trim()) return;
      apiGet<Evidence>(`/api/explainer/concept/${encodeURIComponent(concept)}?corpus_preset=${DEFAULT_PRESET}`)
        .then(setEvidence)
        .catch(() => setEvidence(null));
    }, 200);
    return () => window.clearTimeout(handle);
  }, [concept]);

  const compareRows = useMemo(() => {
    return (compare?.series ?? []).map((item) => {
      const latest = [...item.points].reverse().find((point) => point.value > 0) ?? item.points[item.points.length - 1];
      return { concept: item.concept, latestMonth: latest?.month ?? '-', value: latest?.value ?? 0 };
    });
  }, [compare]);

  const runDryReview = () => {
    setLlmResult(null);
    apiPostJson<Record<string, unknown>>('/api/review/build', { concept, corpus_preset: DEFAULT_PRESET, tier: 'fast' })
      .then(setLlmResult)
      .catch((err) => setError(err.message));
  };

  if (error) return <div className="error-panel">{t.error}: {error}</div>;
  if (!overview) return <div className="loading">{t.loadingRadar}</div>;

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">{t.researchRadar}</p>
          <h2>{t.researchTitle}</h2>
        </div>
        <div className="status-pill">{t.defaultCorpus}: strict_core_published</div>
      </header>

      <section className="stats-row">
        <StatCard label={t.realPapers} value={overview.corpus_size.real_paper_count} detail={t.rawMetadataRetained} />
        <StatCard label={t.strictCondmat} value={overview.corpus_size.strict_core_published_paper_count ?? overview.corpus_size.strict_condmat_paper_count} detail={overview.corpus_size.current_corpus_label} />
        <StatCard label={t.excluded} value={overview.corpus_size.excluded_non_condmat_paper_count} detail={t.notShownByDefault} />
        <StatCard label={t.latestMonth} value={overview.latest_month ?? '-'} detail={`${overview.this_month_papers} ${t.papers}`} />
      </section>

      <section className="overview-grid">
        <RankPanel title={t.hotConcepts} items={overview.hot_concepts} emptyLabel={t.noCachedData} onClick={onConceptSelect} />
        <RankPanel title={t.risingConcepts} items={overview.rising_concepts} valueKey="growth" emptyLabel={t.noCachedData} onClick={onConceptSelect} />
        <RankPanel title={t.coolingConcepts} items={overview.cooling_concepts} valueKey="growth" emptyLabel={t.noCachedData} onClick={onConceptSelect} />
      </section>

      <section className="overview-grid">
        <RankPanel title={t.publishedHeat} items={split?.published_heat ?? []} valueKey="score" emptyLabel={t.noCachedData} onClick={onConceptSelect} />
        <RankPanel title={t.preprintRising} items={split?.preprint_rising ?? []} valueKey="score" emptyLabel={t.noCachedData} onClick={onConceptSelect} />
        <RankPanel title={t.validationGap} items={split?.validation_gap ?? []} valueKey="score" emptyLabel={t.noCachedData} onClick={onConceptSelect} />
      </section>

      <section className="detail-grid two-col">
        <div className="panel">
          <h3>{t.compareCoreConcepts}</h3>
          <div className="mini-table">
            {compareRows.map((row) => (
              <button key={row.concept} className="mini-row" onClick={() => onConceptSelect(row.concept)}>
                <span>{row.concept}</span>
                <span className="mono">{row.latestMonth}</span>
                <strong>{Number(row.value).toFixed(2)}</strong>
              </button>
            ))}
          </div>
        </div>
        <div className="panel">
          <h3>{t.conceptExplainer}</h3>
          <div className="filter-bar compact-filter">
            <input value={concept} onChange={(event) => setConcept(event.target.value)} />
            <button className="utility-button" type="button" onClick={runDryReview}>{t.deepseekDryRun}</button>
          </div>
          {evidence ? (
            <div className="evidence-card">
              <p className="muted">{t.evidenceOnlyCard}</p>
              <div className="tag-list">
                {evidence.associated_methods.slice(0, 6).map((item) => <span key={item.term} className="tag readonly">{item.term}</span>)}
              </div>
              <ul className="paper-list">
                {evidence.key_papers.slice(0, 5).map((paper) => (
                  <li key={`${paper.title}-${paper.publication_date}`}>
                    <strong>{paper.title}</strong>
                    <small>{paper.journal} · {paper.publication_date} · {t.citations} {paper.cited_by_count ?? 0}</small>
                  </li>
                ))}
              </ul>
              {evidence.evidence_missing.length > 0 && <small>{t.missing}: {evidence.evidence_missing.join(', ')}</small>}
            </div>
          ) : <p className="muted">{t.noLocalEvidenceLoaded}</p>}
          {llmResult && <pre className="json-snippet">{JSON.stringify(llmResult, null, 2)}</pre>}
        </div>
      </section>

      <section className="panel path-panel">
        <h3>{t.latestQualityReport}</h3>
        <code>{overview.corpus_size.latest_quality_report_path ?? 'none'}</code>
      </section>
    </div>
  );
}

function RankPanel({ title, items, valueKey = 'momentum', emptyLabel = 'No cached data', onClick }: { title: string; items: Ranked[]; valueKey?: keyof Ranked; emptyLabel?: string; onClick: (concept: string) => void }) {
  return (
    <div className="panel">
      <h3>{title}</h3>
      <div className="rank-list dense">
        {items.length === 0 && <span className="muted">{emptyLabel}</span>}
        {items.slice(0, 12).map((item, idx) => (
          <button key={item.term} className="rank-row" onClick={() => onClick(item.term)}>
            <span className="rank-index">{idx + 1}</span>
            <span className="rank-term">{item.term}</span>
            <span className="rank-value">{Number(item[valueKey] ?? item.raw_freq ?? 0).toFixed(2)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}