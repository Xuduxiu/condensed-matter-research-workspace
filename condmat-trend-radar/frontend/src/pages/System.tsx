import { useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { formatBytes, formatDate, formatNumber, useApiData } from '../hooks';
import { SystemStatus } from '../types';
import { ConfirmDialog, ErrorState, LoadingSkeleton, StatusBadge } from '../components/primitives';

function StatusRow({ label, value, tone, mono = false }: { label: string; value: React.ReactNode; tone?: 'success' | 'warning' | 'danger' | 'accent' | 'neutral'; mono?: boolean }) {
  return <div className="system-row"><span>{label}</span><strong className={mono ? 'mono' : ''}>{tone ? <StatusBadge tone={tone}>{value}</StatusBadge> : value}</strong></div>;
}

type WorkbenchStatus = { installed: boolean; migration: { version: number; name: string; applied_at: string }; counts: { favorites: number; reading_later: number; collections: number; collection_papers: number; actions: number }; institutional_access?: { mode: string } };
type AiStatus = { configured: boolean; base_url: string; fast_model: string; pro_model: string };

const CAMPUS_NETWORK_FAILURES = [
  'connection_error',
  'tls_error',
  'http_server_error',
  'request_timeout',
] as const;

export default function System() {
  const { data, error, loading, refresh } = useApiData<SystemStatus>((signal) => apiGet('/api/ui-v2/system', { signal, cacheMs: 20_000 }), []);
  const workbench = useApiData<WorkbenchStatus>((signal) => apiGet('/api/library/workbench/status', { signal, cacheMs: 20_000 }), []);
  const ai = useApiData<AiStatus>((signal) => apiGet('/api/library/ai/status', { signal, cacheMs: 20_000 }), []);
  const [message, setMessage] = useState('');
  const [confirmRun, setConfirmRun] = useState(false);
  const [retryingFailures, setRetryingFailures] = useState(false);
  const [fullLookbackDays, setFullLookbackDays] = useState(180);
  const run = async (apply: boolean) => {
    setConfirmRun(false);
    setMessage(apply ? `正在提交 ${fullLookbackDays} 天多源历史回补…` : '正在预检历史回补窗口…');
    try {
      const result = await apiPostJson<Record<string, unknown>>('/api/daily/run', {
        apply,
        skip_network: !apply,
        download_limit: 10,
        scan_mode: 'full',
        full_lookback_days: fullLookbackDays,
        abstract_backfill_limit: 10,
      });
      const windowText = result.date_from && result.date_to
        ? `${String(result.date_from)} 至 ${String(result.date_to)}`
        : `${fullLookbackDays} 天`;
      setMessage(apply
        ? `已提交 ${fullLookbackDays} 天 OpenAlex / Crossref / arXiv 历史回补。`
        : `预检通过：固定历史窗口 ${windowText}；未写入数据库。`);
      window.setTimeout(refresh, 800);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '任务执行失败。');
    }
  };
  const retryHistoricalFailures = async () => {
    setRetryingFailures(true);
    setMessage('正在用校园网重新解析连接、TLS、服务端错误与超时任务…');
    try {
      const result = await apiPostJson<{ requeued: number }>('/api/library/downloads/retry-failed', {
        limit: 25,
        failure_classes: [...CAMPUS_NETWORK_FAILURES],
      });
      if (!result.requeued) {
        setMessage('当前没有符合校园网重试条件的网络类失败任务。');
        return;
      }
      await apiPostJson('/api/library/downloads/run', { limit: Math.min(result.requeued, 25) });
      setMessage(`已重排 ${result.requeued} 个网络类失败任务，并启动校园网定向下载。`);
      window.setTimeout(refresh, 1200);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '重试历史失败任务失败。');
    } finally {
      setRetryingFailures(false);
    }
  };
  const check = Array.isArray(data?.database.quick_check) ? data?.database.quick_check.join(', ') : String(data?.database.quick_check ?? '—');
  const activeRun = data?.daily.runs.find((item) => ['queued', 'running', 'cancel_requested'].includes(item.status));
  const latestRunReport = data?.daily.runs[0]?.report ?? {};
  const latestWindow = latestRunReport.date_from && latestRunReport.date_to
    ? `${String(latestRunReport.date_from)} 至 ${String(latestRunReport.date_to)}`
    : '—';
  const institutionalMode = workbench.data?.institutional_access?.mode?.toLowerCase() ?? 'disabled';
  const campusIpEnabled = ['ip', 'ip_only', 'campus_ip', 'enabled', 'true', '1'].includes(institutionalMode);
  const accessModeLabel = workbench.loading
    ? '访问模式检测中'
    : workbench.error ? '访问模式不可用' : campusIpEnabled ? '校园网 IP 授权' : '仅开放获取';
  const accessModeTone = campusIpEnabled ? 'success' : 'neutral';


  return <div className="page-stack system-page">
    <section className="command-strip system-command-strip"><div><StatusBadge tone={error ? 'danger' : 'success'}>API {error ? '异常' : '在线'}</StatusBadge><span>统一数据库与运行状态</span><StatusBadge tone={accessModeTone}>{accessModeLabel}</StatusBadge></div><div><button className="button ghost" type="button" onClick={() => run(false)}>预检回补窗口</button><select aria-label="历史回补范围" value={fullLookbackDays} onChange={(event) => setFullLookbackDays(Number(event.target.value))}><option value={180}>180 天（90 天当前 + 等长基线）</option><option value={730}>730 天（长期同源回补）</option></select><button className="button primary" type="button" onClick={() => setConfirmRun(true)}>启动历史回补</button><button className="button secondary" type="button" onClick={async () => { try { await apiPostJson('/api/library/downloads/run', { limit: 5 }); setMessage('下载 worker 已启动。'); window.setTimeout(refresh, 1200); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '启动失败。'); } }}>处理下载</button><button className="button secondary" type="button" disabled={retryingFailures} onClick={retryHistoricalFailures}>{retryingFailures ? '校园网重试中…' : '校园网重试网络失败'}</button>{activeRun && <button className="button danger" type="button" onClick={async () => { try { await apiPostJson(`/api/daily/stop/${encodeURIComponent(activeRun.id)}`, {}); setMessage('已请求停止当前扫描；网络请求返回后会安全结束。'); window.setTimeout(refresh, 800); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '停止失败。'); } }}>停止扫描</button>}<button className="icon-button" type="button" aria-label="刷新系统状态" onClick={() => { refresh(); workbench.refresh(); ai.refresh(); }}>↻</button></div></section>
    <p className="system-note">180 天用于覆盖 90 天当前观察窗及相邻等长基线；730 天用于更长期的同源回补。结果仍受各公开来源的索引范围与延迟约束，不代表全网完整。</p>
    {message && <p className="inline-message" role="status">{message}</p>}
    {loading && <LoadingSkeleton rows={10} />}
    {error && <ErrorState message={error.message} offline={error.code === 'offline'} onRetry={refresh} />}
    {data && <div className="system-grid">
      <section className="system-card database-card"><header><span className="system-icon">DB</span><div><h2>数据库</h2><p>SQLite production store</p></div><StatusBadge tone={check.includes('ok') ? 'success' : 'warning'}>{check.includes('ok') ? 'HEALTHY' : '未验证'}</StatusBadge></header><StatusRow label="路径" value={data.database.path} mono /><StatusRow label="文件大小" value={formatBytes(data.database.size_bytes)} /><StatusRow label="quick_check" value={check} tone={check.includes('ok') ? 'success' : 'warning'} /><StatusRow label="Canonical" value={formatNumber(data.database.counts.papers)} /><StatusRow label="Versions" value={formatNumber(data.database.counts.paper_versions)} /><StatusRow label="外键错误" value={data.database.foreign_key_violations ?? '—'} tone={data.database.foreign_key_violations === 0 ? 'success' : 'warning'} /></section>
      <section className="system-card"><header><span className="system-icon pdf">PDF</span><div><h2>PDF 库</h2><p>Files & extraction</p></div></header><StatusRow label="唯一 PDF" value={formatNumber(data.pdf_library.unique_pdfs)} /><StatusRow label="总文件大小" value={formatBytes(data.pdf_library.total_size)} /><StatusRow label="重复文件" value={data.pdf_library.duplicates} /><StatusRow label="损坏 / 非法" value={data.pdf_library.invalid_files} tone={data.pdf_library.invalid_files ? 'warning' : 'success'} /><StatusRow label="已解析全文" value={data.pdf_library.parsed_files} /><StatusRow label="待解析" value={data.pdf_library.pending_parse} /></section>
      <section className="system-card"><header><span className="system-icon search">FTS</span><div><h2>搜索</h2><p>Metadata & full text</p></div></header><StatusRow label="FTS 条目" value={formatNumber(data.search.fts_entries)} tone="success" /><StatusRow label="PDF 全文条目" value={formatNumber(data.search.pdf_fulltext_entries)} /><StatusRow label="最后全文抽取" value={formatDate(data.search.last_content_extracted_at, true)} /><StatusRow label="元数据索引覆盖" value={`${data.database.counts.paper_versions ? ((data.search.fts_entries / data.database.counts.paper_versions) * 100).toFixed(1) : '0.0'}%`} tone={data.search.fts_entries >= data.database.counts.paper_versions ? 'success' : 'warning'} /></section>
      <section className="system-card"><header><span className="system-icon download">↓</span><div><h2>下载</h2><p>OA + campus-IP queue</p></div></header><StatusRow label="Pending" value={data.download_queue.pending ?? 0} tone="warning" /><StatusRow label="Downloading" value={data.download_queue.downloading ?? 0} tone="accent" /><StatusRow label="Completed" value={data.download_queue.completed ?? 0} tone="success" /><StatusRow label="Retryable failed" value={data.download_queue.retryable_failed ?? 0} tone="danger" /><StatusRow label="Permanent failed" value={data.download_queue.permanent_failed ?? 0} tone="danger" /><StatusRow label="Manual review" value={data.download_queue.manual_review ?? 0} tone="warning" /></section>
      <section className="system-card"><header><span className="system-icon">WB</span><div><h2>个人工作台</h2><p>Favorites & collections</p></div><StatusBadge tone={workbench.data?.installed ? 'success' : 'warning'}>{workbench.data?.installed ? 'V2 READY' : '检查中'}</StatusBadge></header><StatusRow label="收藏论文" value={workbench.data?.counts.favorites ?? '—'} /><StatusRow label="稍后阅读" value={workbench.data?.counts.reading_later ?? '—'} /><StatusRow label="专题数量" value={workbench.data?.counts.collections ?? '—'} /><StatusRow label="专题论文" value={workbench.data?.counts.collection_papers ?? '—'} /><StatusRow label="操作日志" value={workbench.data?.counts.actions ?? '—'} /><StatusRow label="Schema" value={workbench.data?.migration.name ?? 'radar_workbench_v2'} mono /></section>
      <section className="system-card ai-card"><header><span className="system-icon ai">AI</span><div><h2>DeepSeek</h2><p>Evidence-bound analysis</p></div><StatusBadge tone={ai.data?.configured ? 'success' : 'warning'}>{ai.data?.configured ? 'READY' : '未配置'}</StatusBadge></header><StatusRow label="快速模型" value={ai.data?.fast_model ?? '—'} mono /><StatusRow label="专业模型" value={ai.data?.pro_model ?? '—'} mono /><StatusRow label="API 地址" value={ai.data?.base_url ?? '—'} mono /><p className="system-note">{ai.data?.configured ? '论文详情中的 DeepSeek 解读已启用；只发送当前论文的本地元数据与摘要，结果缓存到本机。' : '尚未载入本地密钥；重启 API 后会自动检测本地 secrets 配置。'}</p></section>      <section className="system-card daily-card"><header><span className="system-icon daily">↻</span><div><h2>每日任务</h2><p>Runs & cursors</p></div><StatusBadge tone={data.daily.runs[0]?.status === 'failed' ? 'danger' : 'success'}>{data.daily.runs[0]?.status || '未运行'}</StatusBadge></header><StatusRow label="上次运行" value={formatDate(data.daily.runs[0]?.started_at, true)} /><StatusRow label="上次成功" value={formatDate(data.daily.runs.find((run) => run.status === 'completed')?.finished_at, true)} /><StatusRow label="当前锁" value={data.daily.runs[0]?.status === 'running' ? '运行中' : '空闲'} /><StatusRow label="最近扫描窗口" value={latestWindow} mono /><div className="cursor-list">{data.daily.source_cursors.map((cursor, index) => <code key={String(cursor.source_name ?? index)}>{String(cursor.source_name ?? 'source')}: {String(cursor.status ?? '—')} · {String(cursor.last_successful_cursor ?? '—')}</code>)}{!data.daily.source_cursors.length && <span className="muted">尚无源游标</span>}</div></section>
      <section className="system-card migration-card"><header><span className="system-icon migration">M1</span><div><h2>迁移</h2><p>Unified library v1</p></div><StatusBadge tone={data.migration.installed ? 'success' : 'danger'}>{data.migration.installed ? 'SCHEMA READY' : 'MISSING'}</StatusBadge></header><StatusRow label="Schema" value={data.migration.installed ? '已安装' : '未安装'} tone={data.migration.installed ? 'success' : 'danger'} /><StatusRow label="迁移记录" value={data.migration.migrations.length} /><StatusRow label="最近迁移" value={formatDate(String(data.migration.migrations[data.migration.migrations.length - 1]?.applied_at ?? ''), true)} /><StatusRow label="人工复核" value={data.database.counts.manual_review_items} tone="warning" /></section>
      <section className="system-card logs-card"><header><span className="system-icon logs">LOG</span><div><h2>最近运行日志</h2><p>Sanitized daily_runs</p></div></header><div className="run-log">{data.daily.runs.map((run) => <div key={run.id}><StatusBadge tone={run.status === 'completed' ? 'success' : run.status === 'failed' ? 'danger' : 'warning'}>{run.status}</StatusBadge><code>{formatDate(run.started_at, true)} · {run.trigger_type}</code><span>{run.error_message ? '记录了错误（详情已隐藏）' : run.dry_run ? 'dry-run' : '正常运行'}</span></div>)}{!data.daily.runs.length && <span className="muted">暂无运行日志</span>}</div></section>
    </div>}
    <ConfirmDialog open={confirmRun} title={`启动 ${fullLookbackDays} 天多源历史回补`} message={fullLookbackDays === 180 ? '覆盖 90 天当前观察窗与相邻 90 天基线，并按 DOI / arXiv / OpenAlex 身份去重；公开来源索引仍可能存在延迟。' : '回补约两年历史数据，用于更长期同源比较；耗时和 API 请求量会明显增加，且不等于全网完整。'} confirmLabel="启动历史回补" onConfirm={() => run(true)} onCancel={() => setConfirmRun(false)} />
  </div>;
}
