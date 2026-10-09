import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPostJson, apiUrl } from '../api';
import { useApiData, useDebouncedValue } from '../hooks';
import { BatchDownloadRun, ImmediateDownloadResponse, PaperSearchResponse, PaperSummary, RemotePaper, RemoteSearchResponse } from '../types';
import PaperRow from '../components/PaperRow';
import PaperTable from '../components/PaperTable';
import RemotePaperRow from '../components/RemotePaperRow';
import { EmptyState, ErrorState, FilterBar, LoadingSkeleton, SearchInput, StatusBadge } from '../components/primitives';

type Scope = 'metadata' | 'fulltext' | 'remote' | 'combined';
const emptyLocal = (query: string, pageSize: number): PaperSearchResponse => ({ items: [], total: 0, count: 0, limit: pageSize, offset: 0, query, search_scope: 'remote' });
const emptyRemote = (query: string): RemoteSearchResponse => ({ items: [], count: 0, query, sources: {}, deduplicated: 0 });
const isBatchActive = (status: string | undefined) => status === 'queued' || status === 'running';

export default function Discover({ onSelectPaper }: { onSelectPaper: (id: string) => void }) {
  const [query, setQuery] = useState('');
  const debouncedQuery = useDebouncedValue(query);
  const [scope, setScope] = useState<Scope>('metadata');
  const [view, setView] = useState<'list' | 'table'>('list');
  const [material, setMaterial] = useState(''); const [journal, setJournal] = useState(''); const [versionType, setVersionType] = useState(''); const [pdfStatus, setPdfStatus] = useState('');
  const [yearFrom, setYearFrom] = useState(''); const [yearTo, setYearTo] = useState(''); const [oaOnly, setOaOnly] = useState(false);
  const [page, setPage] = useState(0); const [pageSize, setPageSize] = useState(50); const [message, setMessage] = useState(''); const [remoteBusy, setRemoteBusy] = useState('');
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [batch, setBatch] = useState<BatchDownloadRun | null>(() => {
    try { const saved = window.localStorage.getItem('radar-last-download-batch'); return saved ? JSON.parse(saved) as BatchDownloadRun : null; }
    catch { return null; }
  });
  const usesLocal = scope !== 'remote'; const usesRemote = scope === 'remote' || scope === 'combined'; const remoteReady = usesRemote && debouncedQuery.trim().length >= 2;
  const localParams = useMemo(() => {
    const value = new URLSearchParams({ q: debouncedQuery, search_scope: scope === 'combined' ? 'all' : scope, material, journal, version_type: versionType, pdf_status: pdfStatus, open_access_only: String(oaOnly), sort: debouncedQuery ? 'relevance' : 'recent', limit: String(pageSize), offset: String(page * pageSize) });
    if (yearFrom) value.set('year_from', yearFrom); if (yearTo) value.set('year_to', yearTo); return value.toString();
  }, [debouncedQuery, scope, material, journal, versionType, pdfStatus, oaOnly, pageSize, page, yearFrom, yearTo]);
  const remoteParams = useMemo(() => new URLSearchParams({ q: debouncedQuery.trim(), source: 'all', limit: String(Math.min(pageSize, 18)) }).toString(), [debouncedQuery, pageSize]);
  const local = useApiData<PaperSearchResponse>((signal) => usesLocal ? apiGet(`/api/ui-v2/papers?${localParams}`, { signal, cacheMs: 20_000 }) : Promise.resolve(emptyLocal(debouncedQuery, pageSize)), [localParams, usesLocal]);
  const remote = useApiData<RemoteSearchResponse>((signal) => remoteReady ? apiGet(`/api/library/remote-search?${remoteParams}`, { signal, cacheMs: 60_000 }) : Promise.resolve(emptyRemote(debouncedQuery)), [remoteParams, remoteReady]);
  useEffect(() => setPage(0), [debouncedQuery, scope, material, journal, versionType, pdfStatus, oaOnly, yearFrom, yearTo, pageSize]);
  useEffect(() => {
    if (!batch) return;
    try { window.localStorage.setItem('radar-last-download-batch', JSON.stringify(batch)); } catch { /* local storage unavailable */ }
  }, [batch]);  const activeBatchId = batch?.id;
  const activeBatchStatus = batch?.status;
  const refreshLocal = local.refresh;
  useEffect(() => {
    if (!activeBatchId || !isBatchActive(activeBatchStatus)) return undefined;
    let cancelled = false;
    const poll = async () => { try {
      const current = await apiGet<BatchDownloadRun>(`/api/library/download-batches/${encodeURIComponent(activeBatchId)}`, { force: true });
      if (cancelled) return; setBatch(current);
      if (!isBatchActive(current.status)) { const zoteroReady = Boolean(current.result?.zotero); setMessage(current.status === 'completed' ? `批量下载完成：${current.completed_count}/${current.total_count} 篇 PDF；${zoteroReady ? '已生成一个 Zotero 联动包。' : 'Zotero 包生成失败。'}` : `批量下载结束：成功 ${current.completed_count}，失败 ${current.failed_count}；${zoteroReady ? 'Zotero 元数据包已生成。' : '请查看失败原因。'}`); refreshLocal(); }
    } catch (reason) { if (!cancelled) setMessage(reason instanceof Error ? reason.message : '批量下载状态读取失败。'); } };
    void poll(); const timer = window.setInterval(poll, 1200); return () => { cancelled = true; window.clearInterval(timer); };
  }, [activeBatchId, activeBatchStatus, refreshLocal]);
  const enqueue = async (paper: PaperSummary) => { setMessage(`正在立即下载《${paper.title}》并生成 Zotero 联动包…`); try { const result = await apiPostJson<ImmediateDownloadResponse>(`/api/library/download-now/${encodeURIComponent(paper.canonical_paper_id)}`, { paper_version_id: paper.paper_version_id, zotero: true }); setMessage(result.status === 'completed' ? `PDF 已完成，Zotero 已关联 ${result.zotero?.attachment_count ?? 0} 个附件。` : `PDF 未完成：${result.download.error || result.download.status}；Zotero 元数据包已生成。`); refreshLocal(); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '立即下载失败。'); } };
  const toggleSelected = (id: string, value: boolean) => setSelected((current) => { const next = new Set(current); if (value && next.size < 100) next.add(id); else if (!value) next.delete(id); return next; });
  const togglePage = (value: boolean) => { const ids = (local.data?.items ?? []).map((paper) => paper.paper_version_id); setSelected((current) => { const next = new Set(current); ids.forEach((id) => { if (value && next.size < 100) next.add(id); else if (!value) next.delete(id); }); return next; }); };
  const startBatch = async (paperVersionIds: string[], label = '即时下载与单一 Zotero 导出') => {
    const ids = [...new Set(paperVersionIds)].slice(0, 100);
    if (!ids.length) return;
    setMessage(`正在启动 ${ids.length} 篇论文的${label}…`);
    try {
      const accepted = await apiPostJson<{ run_id: string; total_count: number }>('/api/library/download-now/batch', { paper_version_ids: ids });
      setBatch({ id: accepted.run_id, status: 'queued', total_count: accepted.total_count, processed_count: 0, completed_count: 0, failed_count: 0, created_at: new Date().toISOString() });
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '批量下载启动失败。');
    }
  };
  const batchDownload = () => { void startBatch([...selected]); };
  const retryFailedBatch = () => {
    const failedIds = (batch?.result?.items ?? []).filter((item) => !['completed', 'already_available'].includes(item.status)).map((item) => item.paper_version_id);
    if (!failedIds.length) { setMessage('该批次没有可重试的失败项。'); return; }
    void startBatch(failedIds, '按新增合法来源重试');
  };  const monitor = async (paper: PaperSummary) => { try { await apiPostJson('/api/library/monitors', { name: paper.title.slice(0, 80), query_text: paper.title, filters: { canonical_paper_id: paper.canonical_paper_id }, auto_download_oa: false, enabled: true }); setMessage('论文监控已保存。'); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '监控保存失败。'); } };
  const importRemote = async (paper: RemotePaper, download: boolean) => { setRemoteBusy(paper.remote_id); setMessage(''); try { const result = await apiPostJson<{ paper: { canonical_paper_id: string; paper_version_id: string } }>('/api/library/remote/import', { record: paper.import_record, enqueue_download: false, reading_status: download ? 'later' : null }); if (download) { setMessage('远程论文已保存，正在立即下载 PDF 并生成 Zotero 包…'); const immediate = await apiPostJson<ImmediateDownloadResponse>(`/api/library/download-now/${encodeURIComponent(result.paper.canonical_paper_id)}`, { paper_version_id: result.paper.paper_version_id, zotero: true }); setMessage(immediate.status === 'completed' ? `远程论文已保存并下载，Zotero 已关联 ${immediate.zotero?.attachment_count ?? 0} 个附件。` : `论文已保存；PDF 未完成：${immediate.download.error || immediate.download.status}。Zotero 元数据包已生成。`); } else { setMessage('远程记录已去重保存到统一文献库。'); } remote.refresh(); local.refresh(); if (!download) onSelectPaper(result.paper.canonical_paper_id); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '远程论文保存失败。'); } finally { setRemoteBusy(''); } };
  const totalPages = Math.max(1, Math.ceil((local.data?.total ?? 0) / pageSize)); const localItems = local.data?.items ?? []; const pageSelected = localItems.length > 0 && localItems.every((paper) => selected.has(paper.paper_version_id)); const sourceSummary = Object.entries(remote.data?.sources ?? {}).map(([name, state]) => `${name} ${state.status === 'ok' ? state.count : '失败'}`).join(' · '); const zotero = batch?.result?.zotero;
  return <div className="page-stack discover-page">
    <section className="search-command"><SearchInput value={query} onChange={setQuery} onSubmit={() => { local.refresh(); remote.refresh(); }} placeholder="检索标题、摘要、作者、材料或 DOI…" /><div className="scope-tabs" role="tablist" aria-label="搜索范围">{([['metadata', '本地元数据'], ['fulltext', 'PDF 全文'], ['remote', '远程论文源'], ['combined', '本地 + 远程']] as Array<[Scope, string]>).map(([key, label]) => <button key={key} className={scope === key ? 'active' : ''} type="button" role="tab" aria-selected={scope === key} onClick={() => setScope(key)}>{label}{(key === 'remote' || key === 'combined') && <i className="live-dot" />}</button>)}</div></section>
    {usesRemote && <div className="integration-notice"><StatusBadge tone="success">实时检索</StatusBadge><div><strong>OpenAlex + Crossref + arXiv 已统一接入</strong><p>搜索结果先在内存中按 DOI、arXiv ID 和标题作者去重；只有点击保存或下载才写入本地数据库。{scope === 'combined' && ' 下方本地筛选仅作用于本地结果。'}</p></div></div>}
    {usesLocal && <FilterBar><label>版本<select value={versionType} onChange={(event) => setVersionType(event.target.value)}><option value="">全部</option><option value="preprint">预印本</option><option value="publication">正式见刊</option></select></label><label>材料<input value={material} onChange={(event) => setMaterial(event.target.value)} placeholder="例如 MoS2" /></label><label>期刊<input value={journal} onChange={(event) => setJournal(event.target.value)} placeholder="期刊名称" /></label><label>年份<input className="short" type="number" value={yearFrom} onChange={(event) => setYearFrom(event.target.value)} placeholder="起" /><span>—</span><input className="short" type="number" value={yearTo} onChange={(event) => setYearTo(event.target.value)} placeholder="止" /></label><label>PDF<select value={pdfStatus} onChange={(event) => setPdfStatus(event.target.value)}><option value="">全部</option><option value="downloaded">已下载</option><option value="missing">仅元数据</option><option value="pending">待下载</option><option value="retryable_failed">下载失败</option></select></label><label className="check-control"><input type="checkbox" checked={oaOnly} onChange={(event) => setOaOnly(event.target.checked)} />开放获取</label><button className="filter-reset" type="button" onClick={() => { setMaterial(''); setJournal(''); setVersionType(''); setPdfStatus(''); setYearFrom(''); setYearTo(''); setOaOnly(false); }}>重置</button></FilterBar>}
    {message && <p className="inline-message" role="status">{message}</p>}
    {batch && <section className="bulk-bar batch-progress" aria-live="polite"><div className="batch-copy"><strong>批量 PDF + Zotero</strong><span>{isBatchActive(batch.status) ? `进行中：${batch.processed_count}/${batch.total_count}` : `状态：${batch.status} · 成功 ${batch.completed_count} · 失败 ${batch.failed_count}`}</span>{batch.status === 'partial' && <small>未下载项没有可确认的合法 OA 地址；可按新增来源重新尝试。</small>}</div><div className="batch-actions">{batch.status === 'partial' && batch.failed_count > 0 && <button className="button secondary" type="button" onClick={retryFailedBatch}>重试失败项</button>}{zotero && <><a className="button secondary" href={apiUrl(zotero.ris_download_url)}>下载 RIS</a><a className="button primary" href={apiUrl(zotero.zip_download_url)}>下载 Zotero ZIP</a></>}</div></section>}
    {usesLocal && <section className="results-block"><header className="results-toolbar"><div><strong>{(local.data?.total ?? 0).toLocaleString()} 条本地真实结果</strong><span>第 {page + 1} / {totalPages} 页 · 服务端分页</span></div><div><button className={view === 'list' ? 'active' : ''} type="button" onClick={() => setView('list')}>列表</button><button className={view === 'table' ? 'active' : ''} type="button" onClick={() => setView('table')}>表格</button></div></header><div className="bulk-bar"><label className="check-control"><input type="checkbox" checked={pageSelected} disabled={!localItems.length} onChange={(event) => togglePage(event.target.checked)} />全选当前页</label><span>已选 {selected.size} 篇（可跨页，最多 100 篇）</span>{selected.size > 0 && <button className="button ghost" type="button" onClick={() => setSelected(new Set())}>清空选择</button>}<button className="button primary" type="button" disabled={!selected.size || isBatchActive(batch?.status)} onClick={batchDownload}>{isBatchActive(batch?.status) ? '批量下载中…' : '立即下载并导出 Zotero'}</button><small>每篇立即解析下载；完成后生成一个含 PDF 附件的 Zotero 包。</small></div>{local.loading && <LoadingSkeleton rows={8} />}{local.error && <ErrorState message={local.error.message} offline={local.error.code === 'offline'} onRetry={local.refresh} />}{!local.loading && !local.error && local.data?.items.length === 0 && <EmptyState title="没有匹配的本地论文" message="尝试减少筛选条件，或使用远程论文源继续检索。" />}{!local.loading && local.data && view === 'list' && <div className="paper-list">{local.data.items.map((paper) => <PaperRow paper={paper} key={paper.paper_version_id} selectable selected={selected.has(paper.paper_version_id)} onSelect={(value) => toggleSelected(paper.paper_version_id, value)} onOpen={() => onSelectPaper(paper.canonical_paper_id)} onDownload={() => enqueue(paper)} onMonitor={() => monitor(paper)} />)}</div>}{!local.loading && local.data && view === 'table' && <PaperTable papers={local.data.items} selected={selected} onToggle={toggleSelected} onOpen={onSelectPaper} onDownload={enqueue} />}{(local.data?.total ?? 0) > 0 && <footer className="pagination"><button type="button" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</button><span>{page + 1} / {totalPages}</span><button type="button" disabled={page + 1 >= totalPages} onClick={() => setPage((value) => value + 1)}>下一页</button><label>每页<select value={pageSize} onChange={(event) => setPageSize(Number(event.target.value))}><option>25</option><option>50</option><option>100</option></select></label></footer>}</section>}
    {usesRemote && <section className="results-block remote-results"><header className="results-toolbar"><div><strong>{remoteReady ? `${remote.data?.count ?? 0} 条远程去重结果` : '输入至少 2 个字符开始远程检索'}</strong><span>{sourceSummary || '等待查询'}{remote.data?.deduplicated ? ` · 合并重复 ${remote.data.deduplicated} 条` : ''}</span></div><StatusBadge tone={remote.error ? 'danger' : remote.loading ? 'warning' : 'success'}>{remote.error ? '远程错误' : remote.loading ? '检索中' : '真实来源'}</StatusBadge></header>{remote.loading && <LoadingSkeleton rows={5} />}{remote.error && <ErrorState message={remote.error.message} offline={remote.error.code === 'offline'} onRetry={remote.refresh} />}{!remoteReady && <EmptyState title="准备远程检索" message="远程检索不会自动入库；你可以先查看结果，再选择保存或下载。" />}{remoteReady && !remote.loading && !remote.error && remote.data?.items.length === 0 && <EmptyState title="远程来源没有返回结果" message="可更换关键词，或查看来源状态是否有单项失败。" />}{remote.data?.items.map((paper) => <RemotePaperRow key={paper.remote_id} paper={paper} busy={remoteBusy === paper.remote_id} onSave={() => importRemote(paper, false)} onDownload={() => importRemote(paper, true)} onOpenLocal={() => paper.local_canonical_paper_id && onSelectPaper(paper.local_canonical_paper_id)} />)}{Object.entries(remote.data?.sources ?? {}).some(([, state]) => state.status === 'error') && <div className="source-errors">{Object.entries(remote.data?.sources ?? {}).filter(([, state]) => state.status === 'error').map(([name, state]) => <p key={name}><strong>{name}</strong>：{state.error}</p>)}</div>}</section>}
  </div>;
}