import { FormEvent, useEffect, useMemo, useState } from 'react';
import { apiGet, apiPostJson, apiUrl } from '../api';
import { formatDate, useApiData, useDebouncedValue } from '../hooks';
import { BatchDownloadRun, ImmediateDownloadResponse, PaperCollection, PaperSearchResponse, PaperSummary, SystemStatus, ZoteroPackage } from '../types';
import PaperTable from '../components/PaperTable';
import { ConfirmDialog, EmptyState, ErrorState, FilterBar, LoadingSkeleton, MetricStrip, SearchInput, StatusBadge } from '../components/primitives';

type Tab = 'all' | 'favorites' | 'later' | 'pdf' | 'metadata' | 'pending' | 'failed' | 'reviews' | 'topics' | 'zotero';
const tabs: Array<[Tab, string]> = [
  ['all', '全部论文'],
  ['favorites', '收藏'],
  ['later', '稍后阅读'],
  ['pdf', '已有 PDF'],
  ['metadata', '仅元数据'],
  ['pending', '待下载'],
  ['failed', '下载失败'],
  ['reviews', '人工复核'],
  ['topics', '专题'],
  ['zotero', 'Zotero 导出'],
];

type CollectionResponse = { items: PaperCollection[] };
type WorkbenchStatus = { installed: boolean; counts: { favorites: number; reading_later: number; collections: number; collection_papers: number; actions: number } };
type ManualReviewItem = {
  id: string;
  review_type: string;
  status: string;
  source_project?: string | null;
  source_record_id?: string | null;
  candidate_paper_id?: string | null;
  reason: string;
  confidence?: number | null;
  payload_json?: string | null;
  created_at: string;
};
type ReviewResponse = { items: ManualReviewItem[]; count: number };

const isBatchActive = (status?: BatchDownloadRun['status']) => status === 'queued' || status === 'running';

export default function Library({ onSelectPaper }: { onSelectPaper: (id: string) => void }) {
  const [tab, setTab] = useState<Tab>('all');
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebouncedValue(query);
  const [material, setMaterial] = useState('');
  const [topic, setTopic] = useState('');
  const [author, setAuthor] = useState('');
  const [yearFrom, setYearFrom] = useState('');
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(50);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState('');
  const [confirmDownload, setConfirmDownload] = useState(false);
  const [activeCollection, setActiveCollection] = useState('');
  const [bulkCollection, setBulkCollection] = useState('');
  const [newCollectionName, setNewCollectionName] = useState('');
  const [newCollectionDescription, setNewCollectionDescription] = useState('');
  const [collectionBusy, setCollectionBusy] = useState(false);
  const [zotero, setZotero] = useState<ZoteroPackage | null>(null);
  const [batch, setBatch] = useState<BatchDownloadRun | null>(null);
  const [reviewBusy, setReviewBusy] = useState('');

  const pdfStatus = tab === 'pdf' ? 'downloaded' : tab === 'metadata' ? 'missing' : tab === 'pending' ? 'pending' : tab === 'failed' ? 'failed' : '';
  const params = useMemo(() => {
    const value = new URLSearchParams({
      q: debouncedQuery,
      search_scope: 'all',
      material,
      topic,
      author,
      pdf_status: pdfStatus,
      review_only: String(tab === 'reviews'),
      condmat_only: 'false',
      favorite_only: String(tab === 'favorites'),
      reading_status: tab === 'later' ? 'later' : '',
      collection_id: tab === 'topics' ? activeCollection : '',
      sort: debouncedQuery ? 'relevance' : 'recent',
      limit: String(pageSize),
      offset: String(page * pageSize),
    });
    if (yearFrom) value.set('year_from', yearFrom);
    return value.toString();
  }, [debouncedQuery, material, topic, author, pdfStatus, tab, pageSize, page, yearFrom, activeCollection]);

  const papers = useApiData<PaperSearchResponse>((signal) => apiGet(`/api/ui-v2/papers?${params}`, { signal, cacheMs: 10_000 }), [params]);
  const system = useApiData<SystemStatus>((signal) => apiGet('/api/ui-v2/system', { signal, cacheMs: 30_000 }), []);
  const collections = useApiData<CollectionResponse>((signal) => apiGet('/api/library/collections', { signal, cacheMs: 10_000 }), []);
  const workbench = useApiData<WorkbenchStatus>((signal) => apiGet('/api/library/workbench/status', { signal, cacheMs: 10_000 }), []);
  const reviews = useApiData<ReviewResponse>((signal) => tab === 'reviews'
    ? apiGet('/api/library/reviews?status=pending&limit=100', { signal, cacheMs: 5_000 })
    : Promise.resolve({ items: [], count: 0 }), [tab]);

  useEffect(() => { setPage(0); setSelected(new Set()); }, [tab, debouncedQuery, material, topic, author, yearFrom, pageSize, activeCollection]);
  useEffect(() => {
    const first = collections.data?.items[0]?.id ?? '';
    if (!activeCollection && first) setActiveCollection(first);
    if (!bulkCollection && first) setBulkCollection(first);
  }, [collections.data?.items, activeCollection, bulkCollection]);

  const refreshPapers = papers.refresh;
  const refreshSystem = system.refresh;
  const refreshCollections = collections.refresh;
  const refreshWorkbench = workbench.refresh;
  const refreshReviews = reviews.refresh;
  const refreshAll = () => {
    refreshPapers();
    refreshSystem();
    refreshCollections();
    refreshWorkbench();
    if (tab === 'reviews') refreshReviews();
  };

  const activeBatchId = batch?.id;
  const activeBatchStatus = batch?.status;
  useEffect(() => {
    if (!activeBatchId || !isBatchActive(activeBatchStatus)) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const current = await apiGet<BatchDownloadRun>(`/api/library/download-batches/${encodeURIComponent(activeBatchId)}`, { force: true });
        if (cancelled) return;
        setBatch(current);
        if (!isBatchActive(current.status)) {
          const packageResult = current.result?.zotero ?? null;
          setZotero(packageResult);
          setMessage(current.status === 'completed'
            ? `批量下载完成：PDF ${current.completed_count}/${current.total_count}；${packageResult ? 'Zotero 联动包已生成。' : 'Zotero 包生成失败。'}`
            : `批量下载结束：成功 ${current.completed_count}，失败 ${current.failed_count}；${packageResult ? 'Zotero 元数据包已生成。' : '请查看失败明细。'}`);
          refreshPapers();
          refreshSystem();
          refreshCollections();
          refreshWorkbench();
        }
      } catch (reason) {
        if (!cancelled) setMessage(reason instanceof Error ? reason.message : '批量下载状态读取失败。');
      }
    };
    void poll();
    const timer = window.setInterval(poll, 1200);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeBatchId, activeBatchStatus, refreshCollections, refreshPapers, refreshSystem, refreshWorkbench]);

  const toggle = (id: string, value: boolean) => setSelected((current) => {
    const next = new Set(current);
    if (value) next.add(id); else next.delete(id);
    return next;
  });
  const currentSelectedPapers = (papers.data?.items ?? []).filter((paper) => selected.has(paper.paper_version_id));

  const enqueue = async (paper: PaperSummary) => {
    setMessage(`正在立即下载《${paper.title}》并生成 Zotero 联动包…`);
    try {
      const result = await apiPostJson<ImmediateDownloadResponse>(`/api/library/download-now/${encodeURIComponent(paper.canonical_paper_id)}`, {
        paper_version_id: paper.paper_version_id,
        zotero: true,
      });
      setZotero(result.zotero ?? null);
      setMessage(result.status === 'completed'
        ? `PDF 已完成，Zotero 已关联 ${result.zotero?.attachment_count ?? 0} 个附件。`
        : `PDF 未完成：${result.download.error || result.download.status}；Zotero 元数据包已生成。`);
      refreshAll();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '立即下载失败。');
    }
  };

  const bulkDownload = async () => {
    setConfirmDownload(false);
    const paperVersionIds = Array.from(selected).slice(0, 100);
    if (!paperVersionIds.length) return;
    setZotero(null);
    setMessage(`正在启动 ${paperVersionIds.length} 篇论文的批量下载与 Zotero 导出…`);
    try {
      const accepted = await apiPostJson<{ run_id: string; total_count: number }>('/api/library/download-now/batch', {
        paper_version_ids: paperVersionIds,
      });
      setBatch({
        id: accepted.run_id,
        status: 'queued',
        total_count: accepted.total_count,
        processed_count: 0,
        completed_count: 0,
        failed_count: 0,
        created_at: new Date().toISOString(),
      });
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '批量下载启动失败。');
    }
  };

  const runQueue = async () => {
    try {
      await apiPostJson('/api/library/downloads/run', { limit: 5 });
      setMessage('下载 worker 已在后台启动，最多处理 5 个任务。');
      window.setTimeout(refreshAll, 1500);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '下载 worker 启动失败。');
    }
  };

  const exportRis = async () => {
    try {
      const result = await apiPostJson<ZoteroPackage>('/api/library/export/zotero', { paper_version_ids: Array.from(selected) });
      setZotero(result);
      setMessage(`Zotero 联动包已生成：${result.paper_count} 篇 · ${result.attachment_count} 个 PDF 附件。`);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : 'Zotero 包导出失败。');
    }
  };

  const addMonitor = async () => {
    try {
      await apiPostJson('/api/library/monitors', {
        name: query || material || topic || author || '文献库筛选监控',
        query_text: query || material || topic || author,
        filters: { material, topic, author, year_from: yearFrom || null },
        auto_download_oa: false,
        enabled: true,
      });
      setMessage('当前检索条件已加入监控。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '监控保存失败。');
    }
  };

  const resolveReview = async (item: ManualReviewItem, status: 'resolved' | 'rejected') => {
    if (status === 'rejected' && !window.confirm(`拒绝这条“${item.review_type}”人工复核项？`)) return;
    setReviewBusy(`${status}:${item.id}`);
    try {
      await apiPostJson(`/api/library/reviews/${encodeURIComponent(item.id)}`, {
        status,
        resolution: {
          decision: status === 'resolved' ? 'approved' : 'rejected',
          reviewed_from: 'radar_web_v2',
        },
      });
      setMessage(status === 'resolved' ? '复核项已通过并移出待处理列表。' : '复核项已拒绝并移出待处理列表。');
      refreshReviews();
      refreshPapers();
      refreshSystem();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '人工复核保存失败。');
    } finally {
      setReviewBusy('');
    }
  };

  const createCollection = async (event: FormEvent) => {
    event.preventDefault();
    if (!newCollectionName.trim()) return;
    setCollectionBusy(true);
    try {
      const item = await apiPostJson<PaperCollection>('/api/library/collections', {
        name: newCollectionName,
        description: newCollectionDescription,
      });
      setNewCollectionName('');
      setNewCollectionDescription('');
      setActiveCollection(item.id);
      setBulkCollection(item.id);
      setTab('topics');
      setMessage(`专题“${item.name}”已创建。`);
      collections.refresh();
      workbench.refresh();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '专题创建失败。');
    } finally {
      setCollectionBusy(false);
    }
  };

  const deleteActiveCollection = async () => {
    if (!activeCollectionData || !window.confirm(`删除专题“${activeCollectionData.name}”？论文不会从文献库删除。`)) return;
    try {
      await apiPostJson(`/api/library/collections/${encodeURIComponent(activeCollectionData.id)}/delete`, {});
      setMessage(`专题“${activeCollectionData.name}”已删除，论文记录保持不变。`);
      setActiveCollection('');
      setBulkCollection('');
      refreshAll();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '专题删除失败。');
    }
  };
  const addSelectedToCollection = async () => {
    if (!bulkCollection || !currentSelectedPapers.length) return;
    try {
      const result = await apiPostJson<{ added: number }>('/api/library/collections/' + encodeURIComponent(bulkCollection) + '/papers', {
        canonical_paper_ids: currentSelectedPapers.map((paper) => paper.canonical_paper_id),
      });
      setMessage(`已向专题新增 ${result.added} 篇论文。`);
      setSelected(new Set());
      refreshAll();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '加入专题失败。');
    }
  };

  const stats = system.data;
  const totalPages = Math.max(1, Math.ceil((papers.data?.total ?? 0) / pageSize));
  const collectionItems = collections.data?.items ?? [];
  const activeCollectionData = collectionItems.find((item) => item.id === activeCollection);
  const failedBatchItems = (batch?.result?.items ?? []).filter((item) => !['completed', 'already_available'].includes(item.status));
  const pendingReviews = reviews.data?.items ?? [];

  return <div className="page-stack library-page">
    <MetricStrip items={[
      { label: 'Canonical papers', value: stats?.database.counts.real_papers, tone: 'accent' },
      { label: 'Versions', value: stats?.database.counts.paper_versions },
      { label: 'Unique PDFs', value: stats?.pdf_library.unique_pdfs, tone: 'success' },
      { label: 'Favorites', value: workbench.data?.counts.favorites, tone: 'accent' },
      { label: 'Collections', value: workbench.data?.counts.collections },
      { label: 'Manual reviews', value: stats?.database.counts.manual_review_items, tone: 'warning' },
    ]} />

    <div className="tab-bar" role="tablist" aria-label="文献库视图">{tabs.map(([key, label]) => <button type="button" role="tab" aria-selected={tab === key} className={tab === key ? 'active' : ''} key={key} onClick={() => setTab(key)}>{label}</button>)}</div>

    {tab === 'zotero' && <div className="integration-notice"><StatusBadge tone="accent">导出工具</StatusBadge><div><strong>下载与 Zotero 已联动</strong><p>立即下载成功后，RIS 的 L1 字段会关联本地 PDF；完整 ZIP 同时包含元数据和 PDF 附件。</p></div></div>}

    {tab === 'topics' && <section className="collection-workspace">
      <div className="collection-list">
        <header><div><strong>我的专题</strong><span>{collectionItems.length} 个</span></div></header>
        {!collectionItems.length && <p className="muted">创建第一个专题，用于整理项目、材料体系或组会阅读清单。</p>}
        {collectionItems.map((collection) => <button className={activeCollection === collection.id ? 'active' : ''} type="button" key={collection.id} onClick={() => setActiveCollection(collection.id)}>
          <i style={{ backgroundColor: collection.color }} /><span><strong>{collection.name}</strong><small>{collection.description || '未添加说明'}</small></span><b>{collection.paper_count}</b>
        </button>)}
      </div>
      <form className="collection-create" onSubmit={createCollection}>
        <div><strong>新建专题</strong><span>专题是真实数据库记录，可用于批量归档。</span></div>
        <input value={newCollectionName} onChange={(event) => setNewCollectionName(event.target.value)} placeholder="例如：扭转双层石墨烯" maxLength={120} />
        <input value={newCollectionDescription} onChange={(event) => setNewCollectionDescription(event.target.value)} placeholder="用途或研究问题（可选）" maxLength={1000} />
        <button className="button secondary" type="submit" disabled={collectionBusy || !newCollectionName.trim()}>{collectionBusy ? '创建中…' : '创建专题'}</button>{activeCollectionData && <button className="button ghost" type="button" onClick={deleteActiveCollection}>删除当前专题</button>}
      </form>
    </section>}

    <section className="library-controls">
      <SearchInput value={query} onChange={setQuery} onSubmit={papers.refresh} placeholder="检索本地标题、摘要与全文…" />
      <FilterBar><label>作者<input value={author} onChange={(event) => setAuthor(event.target.value)} placeholder="作者" /></label><label>材料<input value={material} onChange={(event) => setMaterial(event.target.value)} placeholder="材料" /></label><label>主题<input value={topic} onChange={(event) => setTopic(event.target.value)} placeholder="主题" /></label><label>起始年<input className="short" type="number" value={yearFrom} onChange={(event) => setYearFrom(event.target.value)} placeholder="2015" /></label><button className="filter-reset" type="button" onClick={() => { setAuthor(''); setMaterial(''); setTopic(''); setYearFrom(''); }}>重置</button></FilterBar>
    </section>

    {message && <p className="inline-message" role="status">{message}</p>}
    {batch && <section className="section-block" aria-live="polite">
      <header className="section-header"><div><span className="section-kicker">BATCH DOWNLOAD</span><h2>批量 PDF + Zotero</h2></div><StatusBadge tone={batch.status === 'completed' ? 'success' : batch.status === 'partial' || batch.status === 'failed' ? 'danger' : 'warning'}>{batch.status}</StatusBadge></header>
      <div className="run-summary">
        <div className="run-grid"><div><span>总数</span><strong>{batch.total_count}</strong></div><div><span>已处理</span><strong>{batch.processed_count}</strong></div><div><span>PDF 成功</span><strong>{batch.completed_count}</strong></div><div><span>失败</span><strong>{batch.failed_count}</strong></div></div>
        {isBatchActive(batch.status) && <p>任务正在后台逐篇解析合法来源并下载；离开本页不会中断任务。</p>}
        {failedBatchItems.length > 0 && <details><summary>查看 {failedBatchItems.length} 条失败明细</summary><div className="run-log">{failedBatchItems.map((item) => <div key={item.paper_version_id}><StatusBadge tone="danger">{item.status}</StatusBadge><code title={item.paper_version_id}>{item.paper_version_id}</code><span>{item.error || '未提供失败原因'}</span></div>)}</div></details>}
      </div>
    </section>}
    {zotero && <div className="zotero-result"><strong>Zotero 联动包</strong><span>{zotero.paper_count} 篇 · {zotero.attachment_count} 个 PDF</span><a className="button secondary" href={apiUrl(zotero.ris_download_url)}>下载 RIS</a><a className="button secondary" href={apiUrl(zotero.zip_download_url)}>下载完整 ZIP</a></div>}

    <section className="bulk-bar">
      <div><label className="check-control"><input type="checkbox" checked={Boolean(papers.data?.items.length) && selected.size === papers.data?.items.length} onChange={(event) => setSelected(event.target.checked ? new Set(papers.data?.items.map((paper) => paper.paper_version_id)) : new Set())} />选择本页</label><span>已选 {selected.size}</span></div>
      <div>
        <button className="button secondary" type="button" disabled={!selected.size || isBatchActive(batch?.status)} onClick={() => setConfirmDownload(true)}>{isBatchActive(batch?.status) ? `下载中 ${batch?.processed_count ?? 0}/${batch?.total_count ?? 0}` : '批量下载'}</button>
        <button className="button secondary" type="button" disabled={!selected.size} onClick={exportRis}>导出 RIS</button>
        <button className="button secondary" type="button" disabled={!query && !material && !topic && !author} onClick={addMonitor}>加入监控</button>
        <select className="bulk-collection-select" value={bulkCollection} onChange={(event) => setBulkCollection(event.target.value)} disabled={!collectionItems.length}>{!collectionItems.length && <option value="">无专题</option>}{collectionItems.map((collection) => <option value={collection.id} key={collection.id}>{collection.name}</option>)}</select>
        <button className="button secondary" type="button" disabled={!selected.size || !bulkCollection} onClick={addSelectedToCollection}>加入专题</button>
        <button className="button ghost" type="button" onClick={runQueue}>处理遗留队列</button>
      </div>
    </section>

    {tab === 'reviews' && <section className="section-block">
      <header className="section-header"><div><span className="section-kicker">MANUAL REVIEW</span><h2>待人工复核</h2></div><span className="muted">{reviews.data?.count ?? 0} 条待处理</span></header>
      {reviews.loading && <LoadingSkeleton rows={4} />}
      {reviews.error && <ErrorState message={reviews.error.message} offline={reviews.error.code === 'offline'} onRetry={reviews.refresh} />}
      {!reviews.loading && !reviews.error && !pendingReviews.length && <EmptyState title="没有待复核项目" message="身份冲突、版本链接或低置信度记录会出现在这里。" />}
      {!reviews.loading && !reviews.error && pendingReviews.map((item) => <article className="monitor-hit" key={item.id}>
        <div><StatusBadge tone="warning">{item.review_type}</StatusBadge><strong>{item.reason}</strong><p>{item.source_project || '本地'} · {item.source_record_id || item.candidate_paper_id || '无来源编号'} · 置信度 {item.confidence == null ? '—' : `${Math.round(item.confidence * 100)}%`} · {formatDate(item.created_at, true)}</p></div>
        <div className="monitor-actions">
          {item.candidate_paper_id && <button className="mini-button" type="button" onClick={() => onSelectPaper(item.candidate_paper_id!)}>打开论文</button>}
          <button className="mini-button primary" type="button" disabled={Boolean(reviewBusy)} onClick={() => resolveReview(item, 'resolved')}>{reviewBusy === `resolved:${item.id}` ? '保存中…' : '通过'}</button>
          <button className="mini-button danger" type="button" disabled={Boolean(reviewBusy)} onClick={() => resolveReview(item, 'rejected')}>{reviewBusy === `rejected:${item.id}` ? '保存中…' : '拒绝'}</button>
        </div>
      </article>)}
    </section>}

    <section className="results-block">
      <header className="results-toolbar"><div><strong>{(papers.data?.total ?? 0).toLocaleString()} 条真实记录</strong><span>{tab === 'topics' && activeCollectionData ? `专题：${activeCollectionData.name} · ` : ''}第 {page + 1} / {totalPages} 页</span></div><StatusBadge tone={system.error ? 'warning' : 'success'}>{system.error ? '统计部分不可用' : '本地统一库'}</StatusBadge></header>
      {(papers.loading || system.loading && !stats) && <LoadingSkeleton rows={8} />}
      {papers.error && <ErrorState message={papers.error.message} offline={papers.error.code === 'offline'} onRetry={papers.refresh} />}
      {!papers.loading && !papers.error && papers.data?.items.length === 0 && <EmptyState title="当前视图没有论文" message={tab === 'topics' && !activeCollection ? '请先创建或选择一个专题。' : '尝试切换标签或放宽筛选条件。'} />}
      {papers.data && <PaperTable papers={papers.data.items} selected={selected} onToggle={toggle} onOpen={onSelectPaper} onDownload={enqueue} />}
      {(papers.data?.total ?? 0) > 0 && <footer className="pagination"><button type="button" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button><span>{page + 1} / {totalPages}</span><button type="button" disabled={page + 1 >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button><label>每页<select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}><option>25</option><option>50</option><option>100</option></select></label></footer>}
    </section>

    <ConfirmDialog open={confirmDownload} title="批量立即下载并联动 Zotero" message={`将后台处理所选 ${Math.min(selected.size, 100)} 篇论文（最多 100 篇），完成后生成一个包含可用 PDF 附件的 Zotero 联动包。`} confirmLabel="立即处理" onConfirm={bulkDownload} onCancel={() => setConfirmDownload(false)} />
  </div>;
}
