import { lazy, Suspense, useEffect, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { formatDate, useApiData } from '../hooks';
import { DailyRun, DashboardData, ImmediateDownloadResponse, LiveScanStatus, PaperSummary } from '../types';
import MaterialRow from '../components/MaterialRow';
import PaperRow from '../components/PaperRow';
import { ErrorState, LoadingSkeleton, MetricStrip, RunSummary, StatusBadge } from '../components/primitives';
import { getAbstractBackfillProgress, getSourceBackfillProgress, isTerminalDailyRunStatus, newestDailyRun, sourceDisplayName } from '../runProgress';
const Analytics = lazy(() => import('./AnalyticsScientific'));

const stageLabels: Record<string, string> = {
  queued: '等待扫描', fetching_sources: '正在交叉扫描多源论文', auditing_source_coverage: '正在核对多源覆盖',
  quality_reclassification: '正在重分类来源质量', entity_extraction: '正在从论文抽取实体',
  term_statistics: '正在重建术语统计', lifecycle_analysis: '正在分析研究生命周期',
  cooccurrence_network: '正在构建概念共现网络',
  backfilling_missing_sources: '正在补齐缺失来源', updating_library: '正在合并论文身份',
  seeding_library_versions: '正在生成统一论文版本', linking_preprint_publications: '正在关联预印本与正式论文',
  rebuilding_search_index: '正在原子重建全文索引', syncing_topics_materials: '正在同步主题与论文材料',
  indexing_new_papers: '正在增量更新搜索', extracting_materials: '正在从论文抽取材料',
  backfilling_abstracts: '正在补全缺失摘要', evaluating_monitors: '正在计算监控命中',
  processing_downloads: '正在处理下载', completed: '扫描完成', failed: '扫描失败',
};

export default function Dashboard({ onSelectPaper, onSelectMaterial }: { onSelectPaper: (id: string) => void; onSelectMaterial: (id: string) => void }) {
  const { data, error, loading, refresh, updatedAt } = useApiData<DashboardData>((signal) => apiGet('/api/ui-v2/dashboard', { signal, force: true }), []);
  const { data: liveData, refresh: refreshLive } = useApiData<LiveScanStatus>((signal) => apiGet('/api/live-scan/status', { signal, force: true }), []);
  const [message, setMessage] = useState('');
  const [running, setRunning] = useState(false);
  const [scanRun, setScanRun] = useState<DailyRun | null>(null);
  const [view, setView] = useState<'today' | 'analytics'>('today');
  const localActiveRun = scanRun && !isTerminalDailyRunStatus(scanRun.status) ? scanRun : null;
  const liveActiveRun = liveData?.active_run && !isTerminalDailyRunStatus(liveData.active_run.status)
    && !(scanRun?.id === liveData.active_run.id && isTerminalDailyRunStatus(scanRun.status))
    ? liveData.active_run
    : null;
  const activeRun = localActiveRun && (!liveActiveRun || localActiveRun.id === liveActiveRun.id)
    ? localActiveRun
    : liveActiveRun ?? localActiveRun;
  const activeRunId = activeRun?.id;
  const visibleRun = activeRun ?? newestDailyRun(scanRun, data?.latest_run) ?? null;
  const report = visibleRun?.report ?? {};
  const stage = String(report.stage || (visibleRun?.status === 'running' ? 'fetching_sources' : visibleRun?.status || 'idle'));
  const percent = Number(report.percent ?? (visibleRun?.status === 'completed' ? 100 : 0));
  const ingestReport = report.ingest && typeof report.ingest === 'object' ? report.ingest as Record<string, unknown> : {};
  const progressCounts = report.counts && typeof report.counts === 'object' ? report.counts as Record<string, unknown> : {};
  const ingestCounts = ingestReport.counts && typeof ingestReport.counts === 'object' ? ingestReport.counts as Record<string, unknown> : {};
  const sourceLabel = typeof report.source_label === 'string' ? report.source_label : '';
  const progressPage = Number(report.page ?? 0);
  const progressFetched = Number(progressCounts.fetched ?? report.fetched ?? ingestCounts.fetched ?? ingestReport.fetched_count ?? 0);
  const progressEligible = Number(progressCounts.eligible ?? report.eligible ?? ingestCounts.eligible ?? ingestReport.eligible_count ?? 0);
  const progressReviewCandidates = Number(progressCounts.review_candidates ?? report.review_candidates ?? ingestCounts.review_candidates ?? ingestReport.review_candidates_count ?? 0);
  const progressInserted = Number(progressCounts.inserted ?? report.inserted ?? ingestCounts.inserted ?? ingestReport.inserted_count ?? report.kept ?? ingestReport.kept_count ?? 0);
  const progressUpdated = Number(progressCounts.updated ?? report.updated ?? ingestCounts.updated ?? ingestReport.updated_count ?? 0);
  const progressDeduped = Number(progressCounts.deduped ?? report.deduped ?? ingestCounts.deduped ?? ingestReport.deduped_count ?? 0);
  const sourceBackfill = getSourceBackfillProgress(report);
  const abstractBackfill = getAbstractBackfillProgress(report);
  const isSourceBackfill = stage === 'backfilling_missing_sources';
  const isAbstractBackfill = stage === 'backfilling_abstracts';
  const scanModeLabel = String(report.scan_mode || 'live') === 'full'
    ? 'OpenAlex / Crossref / arXiv 多源历史回补'
    : 'OpenAlex / Crossref / arXiv 安全水位增量';
  const scanWindow = report.date_from && report.date_to
    ? `${String(report.date_from)} 至 ${String(report.date_to)}`
    : '';
  const ingestProgressFacts = [
    scanWindow ? `窗口 ${scanWindow}` : '',
    sourceLabel ? `当前来源 ${sourceLabel}` : '',
    progressPage > 0 ? `第 ${progressPage} 页` : '',
    `抓取 ${Number.isFinite(progressFetched) ? progressFetched : 0}`,
    `符合范围（源记录） ${Number.isFinite(progressEligible) ? progressEligible : 0}`,
    `待复核 ${Number.isFinite(progressReviewCandidates) ? progressReviewCandidates : 0}`,
    `新增 ${Number.isFinite(progressInserted) ? progressInserted : 0}`,
    `更新 ${Number.isFinite(progressUpdated) ? progressUpdated : 0}`,
    `去重 / 无变化 ${Number.isFinite(progressDeduped) ? progressDeduped : 0}`,
  ];
  const sourceBackfillFacts = [
    scanWindow ? `窗口 ${scanWindow}` : '',
    sourceBackfill.currentSource ? `当前 / 刚处理来源 ${sourceDisplayName(sourceBackfill.currentSource)}` : '',
    sourceBackfill.requested == null
      ? `已处理 ${sourceBackfill.processed ?? 0}`
      : `补齐 ${sourceBackfill.processed ?? 0}/${sourceBackfill.requested}`,
    `成功 ${sourceBackfill.completed ?? 0}`,
    `未找到 ${sourceBackfill.notFound ?? 0}`,
    `失败 ${sourceBackfill.failed ?? 0}`,
    `身份冲突 ${sourceBackfill.identityConflicts ?? 0}`,
  ];
  const abstractBackfillFacts = [
    scanWindow ? `窗口 ${scanWindow}` : '',
    abstractBackfill.currentSource ? `当前 / 刚处理来源 ${sourceDisplayName(abstractBackfill.currentSource)}` : '',
    abstractBackfill.total == null
      ? `已处理 ${abstractBackfill.processed ?? 0}`
      : `摘要 ${abstractBackfill.processed ?? 0}/${abstractBackfill.total}`,
    `补全 ${abstractBackfill.enriched ?? 0}`,
    `未找到 ${abstractBackfill.notFound ?? 0}`,
    `失败 ${abstractBackfill.failed ?? 0}`,
    abstractBackfill.queueRemaining == null ? '' : `本批剩余 ${abstractBackfill.queueRemaining}`,
  ];
  const progressFacts = (
    isSourceBackfill ? sourceBackfillFacts : isAbstractBackfill ? abstractBackfillFacts : ingestProgressFacts
  ).filter(Boolean).join(' · ');
  useEffect(() => {
    const timer = window.setInterval(refreshLive, 10_000);
    return () => window.clearInterval(timer);
  }, [refreshLive]);
  useEffect(() => {
    if (liveData?.last_scan_at) refresh();
  }, [liveData?.last_scan_at, refresh]);

  useEffect(() => {
    if (!activeRunId) return;
    let cancelled = false;
    let timer: number | undefined;
    const controller = new AbortController();
    const poll = async () => {
      let continuePolling = true;
      let nextDelay = 1200;
      try {
        const current = await apiGet<DailyRun>(`/api/daily/runs/${encodeURIComponent(activeRunId)}`, { force: true, signal: controller.signal });
        if (cancelled) return;
        setScanRun(current);
        nextDelay = ['backfilling_missing_sources', 'backfilling_abstracts'].includes(String(current.report?.stage ?? '')) ? 800 : 1200;
        if (isTerminalDailyRunStatus(current.status)) {
          continuePolling = false;
          const completedMode = String(current.report?.scan_mode || 'live');
          setMessage(current.status === 'completed'
            ? completedMode === 'full'
              ? '多源历史回补完成，扫描窗口与来源覆盖已写入报告。'
              : '实时扫描完成，论文、摘要、材料和下载状态已刷新。'
            : `扫描结束：${current.status}`);
          refresh();
          refreshLive();
        }
      } catch (reason) {
        if (!cancelled) {
          setMessage(reason instanceof Error ? `${reason.message} 将继续重试扫描状态。` : '扫描状态读取失败，将继续重试。');
          nextDelay = 2000;
        }
      } finally {
        if (!cancelled && continuePolling) timer = window.setTimeout(poll, nextDelay);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer != null) window.clearTimeout(timer);
    };
  }, [activeRunId, refreshLive, refresh]);
  const enqueue = async (paper: PaperSummary) => {
    setMessage(`正在立即下载《${paper.title}》并生成 Zotero 包…`);
    try {
      const result = await apiPostJson<ImmediateDownloadResponse>(`/api/library/download-now/${encodeURIComponent(paper.canonical_paper_id)}`, { paper_version_id: paper.paper_version_id, zotero: true });
      setMessage(result.status === 'completed'
        ? `PDF 已完成，Zotero 已关联 ${result.zotero?.attachment_count ?? 0} 个附件。`
        : `PDF 未完成：${result.download.error || result.download.status}；Zotero 元数据包已生成。`);
      refresh();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '立即下载失败。'); }
  };

  const monitor = async (paper: PaperSummary) => {
    try {
      await apiPostJson('/api/library/monitors', { name: paper.title.slice(0, 80), query_text: paper.title, filters: { canonical_paper_id: paper.canonical_paper_id }, auto_download_oa: false, enabled: true });
      setMessage('论文监控已保存。');
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '监控保存失败。'); }
  };

  const updateNow = async () => {
    setRunning(true); setMessage('正在创建实时扫描任务…');
    try {
      const accepted = await apiPostJson<{ run_id: string; status: string }>('/api/daily/run', { apply: true, skip_network: false, download_limit: 3, scan_mode: 'live', abstract_backfill_limit: liveData?.abstract_backfill_limit ?? 10 });
      const current = await apiGet<DailyRun>(`/api/daily/runs/${encodeURIComponent(accepted.run_id)}`, { force: true });
      setScanRun(current);
      setMessage(accepted.status === 'already_running' ? '已有扫描正在运行，已接入其实时进度。' : '实时扫描已经开始。');
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '实时扫描启动失败。'); }
    finally { setRunning(false); }
  };

  const setLiveEnabled = async () => {
    try {
      await apiPostJson('/api/live-scan/settings', { enabled: !liveData?.enabled });
      setMessage(liveData?.enabled ? '自动实时扫描已暂停。' : '自动实时扫描已开启。');
      refreshLive();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '扫描设置保存失败。'); }
  };

  const setIntervalMinutes = async (minutes: number) => {
    try {
      await apiPostJson('/api/live-scan/settings', { interval_seconds: minutes * 60 });
      setMessage(`自动扫描间隔已设为 ${minutes} 分钟。`);
      refreshLive();
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '扫描间隔保存失败。'); }
  };

  return <div className="page-stack dashboard-page">
    <div className="tab-bar dashboard-view-tabs" role="tablist" aria-label="总览视图"><button type="button" role="tab" aria-selected={view === 'today'} className={view === 'today' ? 'active' : ''} onClick={() => setView('today')}>今日雷达</button><button type="button" role="tab" aria-selected={view === 'analytics'} className={view === 'analytics' ? 'active' : ''} onClick={() => setView('analytics')}>统计洞察</button></div>
    {view === 'today' ? <>
    <section className="command-strip live-command-strip">
      <div><StatusBadge tone={visibleRun?.status === 'failed' ? 'danger' : activeRun ? 'warning' : 'success'}>{stageLabels[stage] || visibleRun?.status || '空闲'}</StatusBadge><span>论文库变更 {formatDate(data?.last_updated_at, true)}</span><span className="muted">界面刷新 {updatedAt ? formatDate(updatedAt.toISOString(), true) : '—'}</span></div>
      <div><button className={`live-toggle ${liveData?.enabled ? 'active' : ''}`} type="button" onClick={setLiveEnabled}>{liveData?.enabled ? '● 自动扫描开启' : '○ 自动扫描暂停'}</button><select aria-label="自动扫描间隔" value={Math.round((liveData?.interval_seconds ?? 900) / 60)} onChange={(event) => setIntervalMinutes(Number(event.target.value))}><option value="5">5 分钟</option><option value="15">15 分钟</option><option value="30">30 分钟</option><option value="60">60 分钟</option></select><button className="button primary" type="button" disabled={running || Boolean(activeRun)} onClick={updateNow}>{running ? '启动中…' : activeRun ? '扫描进行中' : '立即实时扫描'}</button></div>
    </section>

    {visibleRun && <section className="scan-progress-card" aria-live="polite">
      <div><strong>{stageLabels[stage] || stage}</strong><span>{Math.max(0, Math.min(100, percent))}%</span></div>
      <div className="scan-progress-track"><i style={{ width: `${Math.max(2, Math.min(100, percent))}%` }} /></div>
      <p>模式：{scanModeLabel} · {progressFacts} · 下一次自动扫描 {formatDate(liveData?.next_scan_at, true)}</p>
      {isSourceBackfill && sourceBackfill.available && <div className="source-backfill-progress" aria-label="缺失来源补齐进度">
        <div><strong>缺失来源补齐</strong><span>{sourceBackfill.requested == null ? `已处理 ${sourceBackfill.processed ?? 0}` : `${sourceBackfill.processed ?? 0} / ${sourceBackfill.requested}`}{sourceBackfill.currentSource ? ` · 当前 / 刚处理 ${sourceDisplayName(sourceBackfill.currentSource)}` : ''}</span></div>
        <div className="source-backfill-track"><i style={{ width: `${sourceBackfill.percent == null ? 0 : Math.max(2, sourceBackfill.percent)}%` }} /></div>
        <dl><div><dt>成功</dt><dd>{sourceBackfill.completed ?? 0}</dd></div><div><dt>未找到</dt><dd>{sourceBackfill.notFound ?? 0}</dd></div><div><dt>失败</dt><dd>{sourceBackfill.failed ?? 0}</dd></div><div><dt>身份冲突</dt><dd>{sourceBackfill.identityConflicts ?? 0}</dd></div>{sourceBackfill.queueRemaining != null && <div><dt>本批剩余</dt><dd>{sourceBackfill.queueRemaining}</dd></div>}</dl>
      </div>}
      {isAbstractBackfill && abstractBackfill.available && <div className="source-backfill-progress abstract-backfill-progress" aria-label="缺失摘要补全进度">
        <div><strong>缺失摘要补全</strong><span>{abstractBackfill.total == null ? `已处理 ${abstractBackfill.processed ?? 0}` : `${abstractBackfill.processed ?? 0} / ${abstractBackfill.total}`}{abstractBackfill.currentSource ? ` · 当前 / 刚处理 ${sourceDisplayName(abstractBackfill.currentSource)}` : ''}</span></div>
        <div className="source-backfill-track"><i style={{ width: `${abstractBackfill.percent == null ? 0 : Math.max(2, abstractBackfill.percent)}%` }} /></div>
        <dl><div><dt>补全</dt><dd>{abstractBackfill.enriched ?? 0}</dd></div><div><dt>未找到</dt><dd>{abstractBackfill.notFound ?? 0}</dd></div><div><dt>失败</dt><dd>{abstractBackfill.failed ?? 0}</dd></div>{abstractBackfill.queueRemaining != null && <div><dt>本批剩余</dt><dd>{abstractBackfill.queueRemaining}</dd></div>}{abstractBackfill.currentPaperId && <div className="pipeline-current-paper"><dt>当前论文</dt><dd title={abstractBackfill.currentPaperId}>{abstractBackfill.currentPaperId}</dd></div>}</dl>
      </div>}    </section>}

    {data?.latest_data_date && data.latest_data_date !== data.today && <div className="data-lag-notice"><StatusBadge tone="neutral">数据日 {data.latest_data_date}</StatusBadge><span>当天论文源尚未发布可入库记录；总览已自动使用最近有数据日，并保留扫描进度作为实时状态。</span></div>}

    {message && <p className="inline-message" role="status">{message}</p>}
    {loading && <LoadingSkeleton rows={8} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <>
      <MetricStrip items={[
        { label: `最新数据日论文 ${data.latest_data_date?.slice(5) || ''}`, value: data.metrics.latest_day_papers ?? data.metrics.new_papers, tone: 'accent' },
        { label: '最新日预印本', value: data.metrics.latest_day_preprints ?? data.metrics.new_preprints, tone: 'preprint' },
        { label: '最新日正式见刊', value: data.metrics.latest_day_publications ?? data.metrics.new_publications, tone: 'publication' },
        { label: '近 7 日论文', value: data.metrics.papers_7d ?? data.metrics.latest_scan_versions, tone: 'accent' },
        { label: '今日监控新命中', value: data.metrics.monitor_new_hits, tone: 'warning' },
        { label: '预印本转见刊', value: data.metrics.preprint_to_publication },
        { label: '自动下载成功', value: data.metrics.downloads_completed, tone: 'success' },
        { label: '待处理下载', value: data.metrics.download_pending, tone: 'warning' },
        { label: '下载失败', value: data.metrics.download_failed, tone: 'danger' },
        { label: '待人工复核', value: data.metrics.manual_reviews, tone: 'warning' },
      ]} />
      <div className="dashboard-grid">
        <section className="section-block attention-block"><header className="section-header"><div><span className="section-kicker">PAPER SIGNALS</span><h2>实时发现论文</h2></div><span className="muted">按本地最新版本日期排序</span></header><div className="paper-list">{data.spotlight.map((paper) => <PaperRow key={paper.paper_version_id} paper={paper} onOpen={() => onSelectPaper(paper.canonical_paper_id)} onDownload={() => enqueue(paper)} onMonitor={() => monitor(paper)} />)}</div></section>
        <div className="dashboard-side">
          <section className="section-block materials-compact"><header className="section-header"><div><span className="section-kicker">PAPER-DERIVED MATERIALS</span><h2>论文材料变化</h2></div><small>数据锚点 {data.material_data_anchor || '—'}</small></header><div>{data.hot_materials.map((material, index) => <MaterialRow key={material.id} material={material} rank={index + 1} onOpen={() => onSelectMaterial(material.id)} />)}</div><p className="data-note">材料来自论文标题/摘要抽取及证据链接，不展示零论文的预设实体。</p></section>
          <section className="section-block run-block"><header className="section-header"><div><span className="section-kicker">SCAN PIPELINE</span><h2>最近扫描摘要</h2></div></header><RunSummary run={visibleRun} /></section>
        </div>
      </div>
    </>}
    </> : <Suspense fallback={<LoadingSkeleton rows={8} />}><Analytics onSelectMaterial={onSelectMaterial} onSelectPaper={onSelectPaper} /></Suspense>}
  </div>;
}
