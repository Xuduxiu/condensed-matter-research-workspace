import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPostJson, apiUrl } from '../api';
import { formatBytes, formatDate, useApiData } from '../hooks';
import { ImmediateDownloadResponse, PaperAiAnalysis, PaperDetail, ZoteroPackage } from '../types';
import { DownloadStatus, ErrorState, LoadingSkeleton, StatusBadge, VersionBadge } from './primitives';

export default function PaperDetailPanel({ id, onClose, onSelectMaterial }: { id: string; onClose: () => void; onSelectMaterial: (id: string) => void }) {
  const { data, error, loading, refresh } = useApiData<PaperDetail>((signal) => apiGet(`/api/ui-v2/papers/${encodeURIComponent(id)}`, { signal, cacheMs: 10_000 }), [id]);
  const aiStatus = useApiData<{ configured: boolean; fast_model: string }>((signal) => apiGet('/api/library/ai/status', { signal, cacheMs: 20_000 }), []);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState('');
  const [collectionId, setCollectionId] = useState('');
  const [note, setNote] = useState('');
  const [zotero, setZotero] = useState<ZoteroPackage | null>(null);
  const [aiAnalysis, setAiAnalysis] = useState<PaperAiAnalysis | null>(null);

  useEffect(() => {
    setNote(data?.user_state.note ?? '');
    setCollectionId(data?.available_collections[0]?.id ?? '');
  }, [data?.user_state.note, data?.available_collections]);
  useEffect(() => { setZotero(null); setAiAnalysis(null); }, [id]);
  useEffect(() => { setAiAnalysis(data?.ai_analysis ?? null); }, [data?.ai_analysis]);

  const latest = data?.versions[0];
  const latestTask = data?.download_tasks[0];
  const abstract = String(data?.paper.abstract || latest?.abstract || '').trim();
  const uniqueMaterials = useMemo(() => Array.from(new Map((data?.materials ?? []).map((item) => [item.id, item])).values()), [data?.materials]);

  const runAction = async (key: string, action: () => Promise<string>) => {
    setBusy(key);
    setMessage('');
    try {
      setMessage(await action());
      refresh();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '操作失败。');
    } finally {
      setBusy('');
    }
  };

  const downloadAndZotero = () => runAction('download', async () => {
    if (!latest) return '没有可处理的论文版本。';
    const result = await apiPostJson<ImmediateDownloadResponse>(`/api/library/download-now/${encodeURIComponent(id)}`, {
      paper_version_id: latest.id,
      zotero: true,
    });
    setZotero(result.zotero ?? null);
    if (result.status === 'completed') {
      return `PDF 已${result.download.status === 'already_available' ? '存在' : '下载'}，Zotero 包已关联 ${result.zotero?.attachment_count ?? 0} 个 PDF。`;
    }
    return `PDF 未完成：${result.download.error || result.download.status}。已生成可导入 Zotero 的元数据包。`;
  });

  const analyzeWithDeepSeek = () => runAction('ai', async () => {
    if (!aiStatus.data?.configured) return 'DeepSeek 尚未载入本地密钥；请在系统页确认状态。';
    const result = await apiPostJson<PaperAiAnalysis & { cached: boolean }>(`/api/library/ai/papers/${encodeURIComponent(id)}/analyze`, { tier: 'fast', force: Boolean(aiAnalysis) });
    setAiAnalysis(result);
    return result.cached ? '已读取本地缓存的 DeepSeek 解读。' : 'DeepSeek 解读已生成并保存到本机数据库。';
  });
  const enrich = () => runAction('enrich', async () => {
    const result = await apiPostJson<{ abstract_available: boolean; abstract_source: string; materials: { materials_discovered: number; links_created: number } }>(`/api/library/enrich/${encodeURIComponent(id)}`, {});
    return result.abstract_available
      ? `摘要已从 ${result.abstract_source} 回填；新增 ${result.materials.links_created} 条材料证据。`
      : '可用来源仍未提供摘要；已重新执行材料抽取。';
  });

  const addMonitor = () => runAction('monitor', async () => {
    if (!data) return '论文尚未加载。';
    await apiPostJson('/api/library/monitors', {
      name: data.paper.title.slice(0, 80), query_text: data.paper.title,
      filters: { canonical_paper_id: id }, auto_download_oa: false, enabled: true,
    });
    return '论文监控已保存。';
  });

  const toggleFavorite = () => runAction('favorite', async () => {
    await apiPostJson(`/api/library/user-state/${encodeURIComponent(id)}`, { favorite: !data?.user_state.favorite });
    return data?.user_state.favorite ? '已取消收藏。' : '已加入收藏。';
  });

  const setReading = (readingStatus: string) => runAction('reading', async () => {
    await apiPostJson(`/api/library/user-state/${encodeURIComponent(id)}`, { reading_status: readingStatus });
    return readingStatus === 'later' ? '已加入稍后阅读。' : readingStatus === 'read' ? '已标记为已读。' : '阅读状态已更新。';
  });

  const saveNote = () => runAction('note', async () => {
    await apiPostJson(`/api/library/user-state/${encodeURIComponent(id)}`, { note });
    return '论文笔记已保存。';
  });

  const addToCollection = () => runAction('collection', async () => {
    if (!collectionId) return '请先在文献库创建专题。';
    await apiPostJson(`/api/library/collections/${encodeURIComponent(collectionId)}/papers`, { canonical_paper_ids: [id] });
    return '已加入专题。';
  });

  const openPdf = (fileId: string) => window.open(apiUrl(`/api/library/files/${encodeURIComponent(fileId)}/content`), '_blank', 'noopener,noreferrer');
  const retryable = latestTask && ['retryable_failed', 'permanent_failed', 'manual_review'].includes(latestTask.status);

  return <div className="detail-inner">
    <header className="detail-header"><div><span>论文详情</span><small>Paper record</small></div><button className="icon-button" type="button" aria-label="关闭论文详情" onClick={onClose}>×</button></header>
    {loading && <LoadingSkeleton rows={8} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <>
      <section className="detail-title">
        <div className="detail-badges"><VersionBadge type={String(latest?.version_type ?? '')} />{data.files.length > 0 && <StatusBadge tone="success">本地 PDF</StatusBadge>}{data.files.some((file) => file.access_basis === 'institutional_ip') && <StatusBadge tone="warning">校园授权</StatusBadge>}{data.user_state.favorite && <StatusBadge tone="accent">已收藏</StatusBadge>}{data.user_state.reading_status !== 'unread' && <StatusBadge tone="neutral">{data.user_state.reading_status}</StatusBadge>}</div>
        <h2>{data.paper.title}</h2>
        <p>{data.authors.map((author) => author.display_name).filter((name, index, all) => all.indexOf(name) === index).join(' · ') || '作者未提供'}</p>
      </section>

      <div className="detail-actions action-wrap">
        <button className="button primary" type="button" disabled={Boolean(busy)} onClick={downloadAndZotero}>{busy === 'download' ? '正在下载并联动…' : data.files.length ? '生成 PDF + Zotero 联动包' : '立即下载 PDF + Zotero'}</button>
        {!abstract && <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={enrich}>{busy === 'enrich' ? '正在查询来源…' : '补全摘要与材料'}</button>}        <button className="button secondary" type="button" disabled={Boolean(busy) || aiStatus.loading || !aiStatus.data?.configured} title={!aiStatus.data?.configured ? '请先在系统页确认 DeepSeek 配置' : undefined} onClick={analyzeWithDeepSeek}>{busy === 'ai' ? 'DeepSeek 解读中…' : !aiStatus.data?.configured ? 'DeepSeek 未就绪' : aiAnalysis ? '重新生成 DeepSeek 解读' : 'DeepSeek 解读'}</button>
        <button className={`button ${data.user_state.favorite ? 'favorite-active' : 'secondary'}`} type="button" disabled={Boolean(busy)} onClick={toggleFavorite}>{busy === 'favorite' ? '保存中…' : data.user_state.favorite ? '★ 已收藏' : '☆ 收藏'}</button>
        <button className="button secondary" type="button" disabled={Boolean(busy)} onClick={() => setReading(data.user_state.reading_status === 'later' ? 'unread' : 'later')}>{data.user_state.reading_status === 'later' ? '移出稍后阅读' : '稍后阅读'}</button>
        <button className="button ghost" type="button" disabled={Boolean(busy)} onClick={addMonitor}>加入监控</button>
        {Boolean(latest?.url) && <a className="button ghost" href={String(latest?.url)} target="_blank" rel="noreferrer">来源页面</a>}
      </div>

      {message && <p className="inline-message" role="status">{message}</p>}
      {zotero && <div className="zotero-result" role="status"><strong>Zotero 联动包</strong><span>{zotero.paper_count} 篇 · {zotero.attachment_count} 个 PDF 附件</span><a className="button secondary" href={apiUrl(zotero.ris_download_url)}>下载 RIS</a><a className="button secondary" href={apiUrl(zotero.zip_download_url)}>下载完整 ZIP</a></div>}

      <section className="detail-section"><h3>摘要</h3>{abstract ? <p className="detail-abstract">{abstract}</p> : <div className="missing-data-callout"><strong>当前记录没有摘要</strong><p>点击“补全摘要与材料”后会按 arXiv、Crossref、OpenAlex、Semantic Scholar 顺序查询，并把新摘要写回本地全文索引。</p></div>}</section>
      <section className="detail-section ai-analysis"><h3>DeepSeek 解读 {aiAnalysis && <span>{aiAnalysis.model}</span>}</h3>{aiAnalysis ? <><p className="detail-abstract">{aiAnalysis.analysis.summary_zh || '模型未返回摘要。'}</p>{aiAnalysis.analysis.research_question && <p><strong>研究问题：</strong>{aiAnalysis.analysis.research_question}</p>}{aiAnalysis.analysis.key_findings.length > 0 && <div><strong>关键发现</strong><ul>{aiAnalysis.analysis.key_findings.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></div>}{aiAnalysis.analysis.methods.length > 0 && <div className="detail-tags">{aiAnalysis.analysis.methods.map((item, index) => <span className="tag" key={`${item}-${index}`}>{item}</span>)}</div>}{aiAnalysis.analysis.caveats.length > 0 && <p className="muted">局限：{aiAnalysis.analysis.caveats.join('；')}</p>}<small className="muted">仅基于本地元数据、摘要与已抽取实体生成 · {formatDate(aiAnalysis.updated_at, true)}</small></> : <p className="muted">点击“DeepSeek 解读”后才会发送这篇论文的本地元数据和摘要；结果会缓存到本机数据库。</p>}</section>

      <section className="detail-section"><h3>元数据</h3><dl className="key-values"><div><dt>DOI</dt><dd>{String(data.paper.doi || latest?.doi || '—')}</dd></div><div><dt>arXiv</dt><dd>{String(data.paper.arxiv_id || latest?.arxiv_id || '—')}</dd></div><div><dt>期刊</dt><dd>{String(data.paper.journal || latest?.journal || '—')}</dd></div><div><dt>日期</dt><dd>{formatDate(String(latest?.publication_date || latest?.submitted_date || ''))}</dd></div><div><dt>来源</dt><dd>{String(latest?.source || '—')}</dd></div><div><dt>引用数</dt><dd>{String(data.paper.cited_by_count ?? '—')}</dd></div></dl></section>

      <section className="detail-section"><h3>材料与主题</h3><div className="detail-tags">{uniqueMaterials.map((item) => <button className="tag material" type="button" key={item.id} onClick={() => onSelectMaterial(item.id)}>{item.canonical_name}</button>)}{data.topics.map((item) => <span className="tag" key={item.id}>{item.canonical_name}</span>)}{!uniqueMaterials.length && !data.topics.length && <span className="muted">尚未从论文文本识别材料或主题</span>}</div></section>

      <section className="detail-section"><h3>版本关系 <span>{data.versions.length}</span></h3>{data.versions.map((version) => <div className="version-row" key={version.id}><VersionBadge type={version.version_type} /><div><strong>{version.title}</strong><small>{formatDate(version.publication_date)} · {version.source || '—'}</small></div></div>)}{!data.version_links.length && <p className="muted">未记录跨版本链接。</p>}</section>

      <section className="detail-section"><h3>文件与下载</h3>{data.files.map((file) => <div className="file-row file-open-row" key={file.id}><DownloadStatus downloaded extractionStatus={file.extraction_status} /><div><strong>{formatBytes(file.file_size)} · {file.page_count ?? '—'} 页</strong><small title={file.absolute_path}>{file.absolute_path}</small></div><button className="button secondary" type="button" onClick={() => openPdf(file.id)}>浏览 PDF</button></div>)}{!data.files.length && <DownloadStatus status={latestTask?.status} />}{latestTask?.last_error && <p className="error-copy">最近下载失败：{latestTask.last_error.slice(0, 200)}</p>}{retryable && <button className="button secondary full" type="button" disabled={Boolean(busy)} onClick={downloadAndZotero}>{busy === 'download' ? '立即重试中…' : '立即重试并生成 Zotero 包'}</button>}</section>

      <section className="detail-section"><h3>收藏与专题</h3><div className="collection-quick-add"><select value={collectionId} onChange={(event) => setCollectionId(event.target.value)}>{!data.available_collections.length && <option value="">尚未创建专题</option>}{data.available_collections.map((collection) => <option value={collection.id} key={collection.id}>{collection.name} · {collection.paper_count}</option>)}</select><button className="button secondary" type="button" disabled={!collectionId || Boolean(busy)} onClick={addToCollection}>加入专题</button></div><div className="detail-tags">{data.user_state.collections.map((collection) => <span className="tag material" key={collection.id}>{collection.name}</span>)}{!data.user_state.collections.length && <span className="muted">尚未归入专题。</span>}</div></section>

      <section className="detail-section"><h3>论文笔记</h3><textarea className="paper-note" value={note} onChange={(event) => setNote(event.target.value)} rows={5} placeholder="记录结论、待验证问题或组会备注…" /><div className="note-actions"><button className="button secondary" type="button" disabled={Boolean(busy) || note === data.user_state.note} onClick={saveNote}>保存笔记</button><button className="button ghost" type="button" disabled={Boolean(busy)} onClick={() => setReading('read')}>标记已读</button></div></section>
    </>}
  </div>;
}
