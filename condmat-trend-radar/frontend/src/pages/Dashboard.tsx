import { lazy, Suspense, useEffect, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { formatDate, useApiData } from '../hooks';
import { DailyRun, DashboardData, ImmediateDownloadResponse, LiveScanStatus, PaperSummary } from '../types';
import PaperRow from '../components/PaperRow';
import { ErrorState, LoadingSkeleton, RunSummary, TrendIndicator } from '../components/primitives';
import { getAbstractBackfillProgress, getSourceBackfillProgress, isTerminalDailyRunStatus, newestDailyRun, sourceDisplayName } from '../runProgress';
import '../styles/dashboard.css';
const Analytics = lazy(() => import('./AnalyticsScientific'));

function count(value: number | undefined) {
  return value == null ? '—' : value.toLocaleString('zh-CN');
}

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

  const metrics = data?.metrics;
  const queueCounts = metrics && [
    ['今日预印本转见刊', metrics.preprint_to_publication],
    ['今日下载成功', metrics.downloads_completed],
    ['待处理下载', metrics.download_pending],
    ['下载失败', metrics.download_failed],
    ['待人工复核', metrics.manual_reviews],
  ] as const;
  const scanState = stageLabels[stage] || visibleRun?.status || '空闲';
  const progressPercent = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0;

  return <div className="page-stack dashboard-page desk-overview">
    <header className="desk-overview__toolbar">
      <div className="desk-overview__tabs" role="tablist" aria-label="总览视图">
        <button type="button" role="tab" aria-selected={view === 'today'} onClick={() => setView('today')}>今日雷达</button>
        <button type="button" role="tab" aria-selected={view === 'analytics'} onClick={() => setView('analytics')}>统计洞察</button>
      </div>
      {view === 'today' && <div className="desk-overview__scan-actions">
        <span className="desk-overview__scan-state" data-state={activeRun ? 'running' : visibleRun?.status || 'idle'}>{scanState}</span>
        <button className="button primary" type="button" disabled={running || Boolean(activeRun)} onClick={updateNow}>{running ? '启动中…' : activeRun ? '扫描进行中' : '立即实时扫描'}</button>
      </div>}
    </header>
    {view === 'today' ? <div className="desk-overview__today">
    {data && metrics && <dl className="desk-overview__figures" aria-label="收录与监控">
      <div>
        <dt>最新数据日论文</dt>
        <dd>{count(metrics.latest_day_papers ?? metrics.new_papers)}<small>篇</small></dd>
        <dd>{count(metrics.latest_day_preprints ?? metrics.new_preprints)} 篇预印本 <span>·</span> {count(metrics.latest_day_publications ?? metrics.new_publications)} 篇正式见刊</dd>
      </div>
      <div>
        <dt>近七日论文</dt>
        <dd>{count(metrics.papers_7d ?? metrics.latest_scan_versions)}<small>篇</small></dd>
        {data.metrics_range_from && data.latest_data_date && <dd><time>{data.metrics_range_from}</time> 至 <time>{data.latest_data_date}</time></dd>}
      </div>
      <div>
        <dt>今日监控新命中</dt>
        <dd>{count(metrics.monitor_new_hits)}<small>条</small></dd>
        <dd><time>{data.today}</time></dd>
      </div>
    </dl>}
    <div className="desk-overview__work">
    {data?.latest_data_date && data.latest_data_date !== data.today &&
      <p className="desk-overview__lag">今日尚无可入库的论文数据，收录统计截至 <time>{data.latest_data_date}</time>。</p>}

    {visibleRun && <details className="desk-overview__progress" key={activeRun?.id || 'last-run'} open={Boolean(activeRun)}>
      <summary><span>{activeRun ? '本次扫描' : '上次扫描'}</span><strong>{stageLabels[stage] || stage} · {progressPercent}%</strong></summary>
      <section className="scan-progress-card" aria-live="polite">
      <div className="scan-progress-track"><i style={{ width: `${progressPercent}%` }} /></div>
      <p>{scanModeLabel} · {progressFacts} · 下次自动扫描 {formatDate(liveData?.next_scan_at, true)}</p>
      {isSourceBackfill && sourceBackfill.available && <div className="source-backfill-progress" aria-label="缺失来源补齐进度">
        <div><strong>缺失来源补齐</strong><span>{sourceBackfill.requested == null ? `已处理 ${sourceBackfill.processed ?? 0}` : `${sourceBackfill.processed ?? 0} / ${sourceBackfill.requested}`}{sourceBackfill.currentSource ? ` · 当前 / 刚处理 ${sourceDisplayName(sourceBackfill.currentSource)}` : ''}</span></div>
        <div className="source-backfill-track"><i style={{ width: `${sourceBackfill.percent == null ? 0 : Math.max(2, sourceBackfill.percent)}%` }} /></div>
        <dl><div><dt>成功</dt><dd>{sourceBackfill.completed ?? 0}</dd></div><div><dt>未找到</dt><dd>{sourceBackfill.notFound ?? 0}</dd></div><div><dt>失败</dt><dd>{sourceBackfill.failed ?? 0}</dd></div><div><dt>身份冲突</dt><dd>{sourceBackfill.identityConflicts ?? 0}</dd></div>{sourceBackfill.queueRemaining != null && <div><dt>本批剩余</dt><dd>{sourceBackfill.queueRemaining}</dd></div>}</dl>
      </div>}
      {isAbstractBackfill && abstractBackfill.available && <div className="source-backfill-progress abstract-backfill-progress" aria-label="缺失摘要补全进度">
        <div><strong>缺失摘要补全</strong><span>{abstractBackfill.total == null ? `已处理 ${abstractBackfill.processed ?? 0}` : `${abstractBackfill.processed ?? 0} / ${abstractBackfill.total}`}{abstractBackfill.currentSource ? ` · 当前 / 刚处理 ${sourceDisplayName(abstractBackfill.currentSource)}` : ''}</span></div>
        <div className="source-backfill-track"><i style={{ width: `${abstractBackfill.percent == null ? 0 : Math.max(2, abstractBackfill.percent)}%` }} /></div>
        <dl><div><dt>补全</dt><dd>{abstractBackfill.enriched ?? 0}</dd></div><div><dt>未找到</dt><dd>{abstractBackfill.notFound ?? 0}</dd></div><div><dt>失败</dt><dd>{abstractBackfill.failed ?? 0}</dd></div>{abstractBackfill.queueRemaining != null && <div><dt>本批剩余</dt><dd>{abstractBackfill.queueRemaining}</dd></div>}{abstractBackfill.currentPaperId && <div className="pipeline-current-paper"><dt>当前论文</dt><dd title={abstractBackfill.currentPaperId}>{abstractBackfill.currentPaperId}</dd></div>}</dl>
      </div>}
      </section>
    </details>}

    {message && <p className="inline-message" role="status">{message}</p>}
    {loading && !data && <LoadingSkeleton rows={5} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    <div className="desk-overview__columns">
      {data && <section className="desk-overview__papers">
        <header className="desk-overview__heading"><h2>最近论文</h2><span>按论文版本日期排序</span></header>
        {data.spotlight.length ? <div className="desk-overview__paper-list">{data.spotlight.map((paper) =>
          <PaperRow key={paper.paper_version_id} paper={paper} onOpen={() => onSelectPaper(paper.canonical_paper_id)} onDownload={() => enqueue(paper)} onMonitor={() => monitor(paper)} />)}</div>
          : <p className="desk-overview__empty">资料库中暂无可展示的论文。</p>}
      </section>}
      <aside className="desk-overview__rail" aria-label="材料与扫描">
        {data && <section>
          <header className="desk-overview__heading"><h2>材料动向</h2>{data.material_data_anchor && <span>截至 {data.material_data_anchor}</span>}</header>
          {data.hot_materials.length > 0 ? <div className="desk-overview__materials">
            <div className="desk-overview__material-head"><span>材料</span><span>近 7 日</span><span>近 30 日</span></div>
            {data.hot_materials.map(material => <details className="desk-overview__material" key={material.id}>
              <summary><span>{material.canonical_name}</span><b>{count(material.count_7d)}</b><b>{count(material.count_30d)}</b></summary>
              <div className="desk-overview__material-detail">
                <p>{[material.material_family, material.phase].filter(Boolean).join(' · ')} · {count(material.evidence_mentions ?? material.total_papers)} 条证据</p>
                <dl>
                  <div><dt>月基线</dt><dd>{count(material.monthly_baseline)}</dd></div>
                  <div><dt>预印本变化</dt><dd><TrendIndicator value={material.preprint_change} /></dd></div>
                  <div><dt>见刊变化</dt><dd><TrendIndicator value={material.publication_change} /></dd></div>
                  <div><dt>综合趋势</dt><dd><TrendIndicator value={Math.round(material.trend_score * 100)} /></dd></div>
                  <div><dt>最近论文</dt><dd>{material.latest_paper_date || '—'}</dd></div>
                </dl>
                <button type="button" className="button secondary" onClick={() => onSelectMaterial(material.id)}>查看材料详情</button>
              </div>
            </details>)}
          </div> : <p className="desk-overview__empty">当前论文尚未形成材料趋势。</p>}
        </section>}
        <section className="desk-overview__scans">
          <header className="desk-overview__heading"><h2>自动扫描</h2></header>
          <div className="desk-overview__schedule">
            <button type="button" className="desk-overview__toggle" aria-pressed={Boolean(liveData?.enabled)} disabled={!liveData} onClick={setLiveEnabled}>
              <i aria-hidden="true" />{liveData ? liveData.enabled ? '自动扫描已开启' : '自动扫描已暂停' : '读取设置中'}
            </button>
            <select aria-label="自动扫描间隔" disabled={!liveData} value={Math.round((liveData?.interval_seconds ?? 900) / 60)} onChange={(event) => setIntervalMinutes(Number(event.target.value))}>
              {[5, 15, 30, 60].map(minutes => <option key={minutes} value={minutes}>{minutes} 分钟</option>)}
            </select>
          </div>
          <p className="desk-overview__updated">资料库更新 <time>{formatDate(data?.last_updated_at, true)}</time></p>
          <details className="desk-overview__details"><summary>最近扫描摘要</summary>
            <p className="desk-overview__updated">界面刷新 <time>{updatedAt ? formatDate(updatedAt.toISOString(), true) : '—'}</time></p>
            <RunSummary run={visibleRun} />
          </details>
          {metrics && queueCounts && <details className="desk-overview__details">
            <summary>下载与复核{(metrics.download_pending > 0 || metrics.download_failed > 0 || metrics.manual_reviews > 0) && <small>有待处理项</small>}</summary>
            <dl>{queueCounts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{count(value)}</dd></div>)}</dl>
          </details>}
        </section>
      </aside>
    </div>
    </div>
    </div> : <Suspense fallback={<LoadingSkeleton rows={8} />}><Analytics onSelectMaterial={onSelectMaterial} onSelectPaper={onSelectPaper} /></Suspense>}
  </div>;
}
