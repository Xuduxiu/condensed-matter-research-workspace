import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { useApiData, useDebouncedValue } from '../hooks';
import { MaterialSummary } from '../types';
import MaterialRow from '../components/MaterialRow';
import { EmptyState, ErrorState, LoadingSkeleton, StatusBadge } from '../components/primitives';

const views = [
  ['hot', '综合热点'], ['preprint', '预印本先行'], ['publication', '见刊加速'], ['burst', '突发增长'], ['persistent', '持续研究'],
] as const;

type MaterialResponse = {
  items: MaterialSummary[];
  count: number;
  returned_count: number;
  min_evidence: number;
  data_anchor: string;
  generation_method: string;
  dynamic_entities: number;
  extraction_sources: Record<string, number>;
  limit?: number;
  offset?: number;
};

const PAGE_SIZE = 100;

export default function Materials({ onSelectMaterial }: { onSelectMaterial: (id: string) => void }) {
  const [view, setView] = useState<(typeof views)[number][0]>('hot');
  const [query, setQuery] = useState('');
  const search = useDebouncedValue(query, 280);
  const [page, setPage] = useState(0);
  const [syncing, setSyncing] = useState(false);
  const [message, setMessage] = useState('');
  const params = useMemo(() => new URLSearchParams({
    view,
    min_evidence: String(view === 'burst' ? 1 : 2),
    q: search.trim(),
    limit: String(PAGE_SIZE),
    offset: String(page * PAGE_SIZE),
  }).toString(), [view, search, page]);
  const { data, error, loading, refresh } = useApiData<MaterialResponse>(
    (signal) => apiGet(`/api/ui-v2/materials?${params}`, { signal, cacheMs: 15_000 }),
    [params],
  );
  useEffect(() => setPage(0), [view, search]);
  const totalPages = Math.max(1, Math.ceil((data?.count ?? 0) / PAGE_SIZE));

  const sync = async () => {
    setSyncing(true);
    setMessage('正在扫描凝聚态论文标题和摘要；大语料库可能需要几分钟…');
    try {
      const result = await apiPostJson<{ papers_scanned: number; materials_discovered: number; links_created: number }>('/api/library/materials/sync', { limit: null });
      setMessage(`已扫描 ${result.papers_scanned.toLocaleString()} 篇论文，新发现 ${result.materials_discovered} 个材料实体，新增 ${result.links_created.toLocaleString()} 条证据。`);
      setPage(0);
      refresh();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '材料抽取失败。');
    } finally {
      setSyncing(false);
    }
  };

  return <div className="page-stack materials-page">
    <section className="materials-hero">
      <div><span className="section-kicker">PAPER-DERIVED MATERIAL SIGNALS</span><h2>从论文标题与摘要实时生成材料雷达</h2><p>材料实体由论文文本抽取并保存证据片段；固定别名表只用于规范化名称。综合视图至少需要 2 篇论文证据，“突发增长”保留单篇新材料。</p></div>
      <div><StatusBadge tone="accent">{data?.count ?? '—'} 个匹配材料</StatusBadge><StatusBadge tone="success">动态发现 {data?.dynamic_entities ?? '—'}</StatusBadge><StatusBadge tone="neutral">锚点 {data?.data_anchor ?? '—'}</StatusBadge><button className="button primary" type="button" disabled={syncing} onClick={sync}>{syncing ? '正在抽取…' : '重新扫描论文材料'}</button></div>
    </section>
    {message && <p className="inline-message" role="status">{message}</p>}
    <div className="tab-bar material-tabs" role="tablist" aria-label="材料视图">{views.map(([key, label]) => <button type="button" role="tab" aria-selected={view === key} className={view === key ? 'active' : ''} key={key} onClick={() => setView(key)}>{label}</button>)}</div>
    <div className="material-toolbar"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="在全部论文材料中搜索名称或家族…" aria-label="筛选材料" /><span>服务端检索全部材料 · 当前页 {data?.returned_count ?? 0} 条 · 共 {data?.count ?? 0} 条</span></div>
    <section className="material-table section-block">
      <div className="material-head"><span>#</span><span>材料</span><span>7 日</span><span>30 日</span><span>月基线</span><span>预印本</span><span>见刊</span><span>最近论文</span><span>综合趋势</span></div>
      {loading && <LoadingSkeleton rows={10} />}
      {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
      {!loading && !error && !data?.items.length && <EmptyState title="没有匹配材料" message="全部已抽取材料中没有匹配结果；可清空搜索或切换统计视图。" />}
      {(data?.items ?? []).map((material, index) => <MaterialRow material={material} rank={page * PAGE_SIZE + index + 1} key={material.id} onOpen={() => onSelectMaterial(material.id)} />)}
      {(data?.count ?? 0) > PAGE_SIZE && <footer className="pagination material-pagination"><button type="button" disabled={page === 0 || loading} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button><span>{page + 1} / {totalPages}</span><button type="button" disabled={page + 1 >= totalPages || loading} onClick={() => setPage((value) => value + 1)}>下一页</button></footer>}
    </section>
  </div>;
}
