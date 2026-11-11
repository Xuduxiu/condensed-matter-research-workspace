import { FormEvent, useEffect, useRef, useState } from 'react';
import { ApiError, apiGet, apiPostJson, apiPutJson } from '../api';
import { formatNumber, useApiData } from '../hooks';
import { AutomaticTranslationStatus, jobIsActive, LanguageJob, translationApi, translationError, TranslationEstimate, TranslationRequest, TranslationScope, translationUpdated } from '../translations';
import '../styles/translations.css';

const savedJobKey = 'paper-desk.translation-job';
const jobLabels: Record<LanguageJob['status'], string> = {
  queued: '已排队', running: '进行中', paused: '已暂停', completed: '已完成', partial: '部分失败',
};

function lastViewedJob() {
  try { return window.localStorage.getItem(savedJobKey) ?? ''; } catch { return ''; }
}
function rememberJob(id: string) {
  try {
    if (id) window.localStorage.setItem(savedJobKey, id);
    else window.localStorage.removeItem(savedJobKey);
  } catch { /* The server retains jobs when browser storage is unavailable. */ }
}

function AutomaticTranslation() {
  const path = translationApi + '/automatic';
  const [state, setState] = useState<AutomaticTranslationStatus | null>(null);
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [readError, setReadError] = useState('');
  const [saveError, setSaveError] = useState('');
  const lastCompleted = useRef<number | undefined>(undefined);
  const model = useApiData<{ available: boolean; model: string | null; provider: string | null }>(
    signal => apiGet(translationApi + '/provider', { signal }), [revision, state?.blocked],
  );
  useEffect(() => {
    if (saving) return;
    const controller = new AbortController();
    let timer: number;
    async function read() {
      try {
        const next = await apiGet<AutomaticTranslationStatus>(path, { force: true, signal: controller.signal });
        if (controller.signal.aborted) return;
        setState(next);
        if (lastCompleted.current !== undefined && next.completed > lastCompleted.current) window.dispatchEvent(new Event(translationUpdated));
        lastCompleted.current = next.completed;
        setReadError('');
        timer = window.setTimeout(read, next.running || (next.enabled && !next.blocked && next.pending) ? 2500 : 20000);
      } catch (error) {
        if (!controller.signal.aborted) setReadError(translationError(error));
      }
    }
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [path, saving, revision]);

  async function update(enabled: boolean, params?: { batch_size: number; max_input_chars: number }) {
    setSaving(true);
    setSaveError('');
    try { setState(await apiPutJson<AutomaticTranslationStatus>(path, { enabled, ...params })); }
    catch (error) { setSaveError(translationError(error)); }
    finally { setSaving(false); }
  }
  const processed = state ? state.completed + state.failed + state.skipped : 0;
  return <div className="translation-automatic">
    <div className="translation-auto-line">
      <label><input type="checkbox" role="switch" checked={Boolean(state?.enabled)} disabled={!state || saving || Boolean(readError)} onChange={event => update(event.target.checked)} /> 自动翻译全库与新增版本</label>
      <strong role="status">{saving ? '正在保存…' : readError ? '状态未更新' : !state ? '正在读取…' : state.blocked ? '已暂停' : state.enabled ? '已启用' : '已关闭'}</strong>
      {state?.enabled && state.blocked && <button type="button" className="button secondary" disabled={saving} onClick={() => update(true)}>恢复自动翻译</button>}
    </div>
    <p className="translation-hint">
      {model.data?.available && model.data.model ? <>当前 DSH 模型 <strong>{model.data.model}</strong>。</> : model.data?.available === false ? '当前 DSH 模型尚未连接。' : '使用当前 DSH 模型。'}
      新版本入库或摘要补全后，标题和摘要会自动加入已启用的翻译队列。
    </p>
    {state && (state.enabled || state.total > 0) && <>
      <progress value={processed} max={Math.max(state.total, 1)} aria-label="全库自动翻译进度" />
      <p className="translation-auto-counts">已完成 {formatNumber(state.completed)} · 待译 {formatNumber(state.pending)} · 处理中 {formatNumber(state.running)} · 跳过 {formatNumber(state.skipped)} · 失败 {formatNumber(state.failed)} · 共 {formatNumber(state.total)} 个版本</p>
      {Boolean(state.dirty) && <p className="translation-hint">另有 {formatNumber(state.dirty)} 个变更版本待纳入队列。</p>}
    </>}
    {state?.error && <p className="translation-error" role="alert">{state.error}</p>}
    {saveError && <p className="translation-error" role="alert">{saveError}</p>}
    {readError && <div className="translation-actions"><span className="translation-error" role="alert">{readError}</span><button className="button ghost" type="button" onClick={() => setRevision(value => value + 1)}>重新读取自动设置</button></div>}
    {state && <details className="translation-auto-settings"><summary>自动批次设置</summary>
      <form className="translation-options" key={state.batch_size + '/' + state.max_input_chars} onSubmit={event => {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        void update(state.enabled, { batch_size: Number(fields.get('batch_size')), max_input_chars: Number(fields.get('max_input_chars')) });
      }}>
        <label>每次请求版本数<input type="number" name="batch_size" min={1} max={32} defaultValue={state.batch_size} required /></label>
        <label>每次请求字符预算<input type="number" name="max_input_chars" min={1000} max={64000} defaultValue={state.max_input_chars} required /></label>
        <button className="button secondary" type="submit" disabled={saving || state.blocked || Boolean(readError)}>保存批次设置</button>
      </form>
    </details>}
  </div>;
}

function BatchTranslation({ selected }: { selected: Set<string> }) {
  const [scope, setScope] = useState<TranslationScope>(selected.size ? 'selected' : 'recent');
  const [limit, setLimit] = useState('100');
  const [batchSize, setBatchSize] = useState('8');
  const [charLimit, setCharLimit] = useState('16000');
  const [estimate, setEstimate] = useState<{ key: string; value: TranslationEstimate } | null>(null);
  const [action, setAction] = useState('');
  const [actionError, setActionError] = useState('');
  const [jobsError, setJobsError] = useState('');
  const [jobs, setJobs] = useState<LanguageJob[] | null>(null);
  const [chosen, setChosen] = useState(lastViewedJob);
  const [revision, setRevision] = useState(0);
  const counts = useRef(new Map<string, number>());

  const request: TranslationRequest = { scope, batch_size: Number(batchSize), max_input_chars: Number(charLimit) };
  if (scope === 'recent') request.limit = Number(limit);
  if (scope === 'selected') request.version_ids = [...selected];
  const key = JSON.stringify(request);
  const preview = estimate?.key === key ? estimate.value : undefined;
  const job = jobs?.find(item => item.id === chosen) ?? jobs?.[0];
  const processed = job ? job.completed + job.failed + job.skipped : 0;
  const canEstimate = !action && (scope !== 'selected' || selected.size > 0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const readJobs = async () => {
      try {
        const opts = { signal: controller.signal, force: true };
        const { items } = await apiGet<{ items: LanguageJob[] }>(translationApi + '/jobs?limit=20', opts);
        // An older bulk job can still be running after many single-paper requests.
        if (chosen && !items.some(item => item.id === chosen)) {
          try { items.push(await apiGet<LanguageJob>(translationApi + '/jobs/' + encodeURIComponent(chosen), opts)); }
          catch (error) {
            if (!(error instanceof ApiError && error.status === 404)) throw error;
            if (!controller.signal.aborted) { rememberJob(''); setChosen(''); }
          }
        }
        if (controller.signal.aborted) return;
        if (items.some(item => counts.current.has(item.id) && item.completed > counts.current.get(item.id)!)) window.dispatchEvent(new Event(translationUpdated));
        counts.current = new Map(items.map(item => [item.id, item.completed]));
        setJobs(items);
        setJobsError('');
        if (items.some(item => jobIsActive(item) || item.running > 0)) timer = window.setTimeout(readJobs, 2200);
      } catch (error) {
        if (!controller.signal.aborted) setJobsError(translationError(error));
      }
    };
    void readJobs();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [chosen, revision]);

  async function calculate(event: FormEvent) {
    event.preventDefault();
    if (!canEstimate) return;
    setAction('estimate');
    setActionError('');
    setEstimate(null);
    try {
      const value = await apiPostJson<TranslationEstimate>(translationApi + '/estimate', request);
      setEstimate({ key, value });
    } catch (error) { setActionError(translationError(error)); }
    finally { setAction(''); }
  }

  function selectJob(id: string) {
    setChosen(id);
    rememberJob(id);
  }
  async function changeJob(operation: 'start' | 'pause' | 'resume' | 'retry') {
    if (action || (operation === 'start' ? !preview?.needs_translation : !job)) return;
    setAction(operation);
    setActionError('');
    try {
      const path = operation === 'start' ? '/jobs' : '/jobs/' + encodeURIComponent(job!.id) + '/' + operation;
      const next = await apiPostJson<LanguageJob>(translationApi + path, operation === 'start' ? request : {});
      selectJob(next.id);
      counts.current.set(next.id, next.completed);
      if (operation === 'start' && next.completed > 0) window.dispatchEvent(new Event(translationUpdated));
      setJobs(current => [next, ...(current ?? []).filter(item => item.id !== next.id)]);
      if (operation === 'start') setEstimate(null);
    } catch (error) {
      setActionError(translationError(error));
      if (operation === 'start') setEstimate(null);
    } finally { setAction(''); setRevision(value => value + 1); }
  }

  return <div className="translation-batch-body">
    <form onSubmit={calculate} className="translation-options">
      <label>翻译范围<select value={scope} onChange={event => setScope(event.target.value as TranslationScope)}>
        <option value="recent">最近的论文版本</option>
        <option value="selected" disabled={!selected.size}>已选版本（{formatNumber(selected.size)}）</option>
        <option value="all">整个文献库</option>
      </select></label>
      {scope === 'recent' && <label>最近版本数<input type="number" min={1} max={1000000} required value={limit} onChange={event => setLimit(event.target.value)} /></label>}
      <label>每次请求版本数<input type="number" min={1} max={32} required value={batchSize} onChange={event => setBatchSize(event.target.value)} /></label>
      <label>每次请求字符预算<input type="number" min={1000} max={64000} required value={charLimit} onChange={event => setCharLimit(event.target.value)} /></label>
      <button className="button secondary" type="submit" disabled={!canEstimate}>{action === 'estimate' ? '正在预估…' : '预估翻译范围'}</button>
    </form>
    <p className="translation-hint">使用当前 DSH 模型翻译题名和摘要。预估在本地完成，开始后按批次调用模型。</p>
    {preview && <section className="translation-estimate" aria-label="本次翻译预估">
      <h3>本次预估</h3>
      <dl className="translation-numbers">
        <div><dt>实际匹配版本</dt><dd>{formatNumber(preview.total)}</dd></div>
        <div><dt>待翻译</dt><dd>{formatNumber(preview.needs_translation)}</dd></div>
        <div><dt>命中缓存</dt><dd>{formatNumber(preview.cached)}</dd></div>
        <div><dt>无需翻译</dt><dd>{formatNumber(preview.skipped)}</dd></div>
        <div><dt>待译原文字符</dt><dd>{formatNumber(preview.input_chars)}</dd></div>
        <div><dt>预计模型请求</dt><dd>{formatNumber(preview.estimated_requests)}</dd></div>
      </dl>
      <p className="translation-hint">预计输入 {formatNumber(preview.estimated_input_tokens)} token，输出 {formatNumber(preview.estimated_output_tokens)} token。</p>
      {preview.unique_inputs !== undefined && <p className="translation-hint">去重后预计提交 {formatNumber(preview.unique_inputs)} 份不同原文。</p>}
      {Boolean(preview.oversized) && <p className="translation-error">{formatNumber(preview.oversized)} 个版本超过当前字符预算。可提高预算后重新预估。</p>}
      {preview.projection_100k && preview.total < 100000 && <details className="translation-auto-settings"><summary>按本次样本折算 10 万版本</summary>
        <p className="translation-hint">按当前 {formatNumber(preview.projection_100k.based_on_versions)} 个版本的字符量和缓存比例折算，约 {formatNumber(preview.projection_100k.estimated_requests)} 次请求、{formatNumber(preview.projection_100k.estimated_input_tokens)} 输入 token、{formatNumber(preview.projection_100k.estimated_output_tokens)} 输出 token。</p>
      </details>}
      {preview.needs_translation > (preview.oversized ?? 0)
        ? <button className="button primary" type="button" disabled={Boolean(action)} onClick={() => changeJob('start')}>{action === 'start' ? '正在提交…' : '开始翻译'}</button>
        : <p className="translation-hint">{preview.needs_translation ? '当前预算内没有可提交的版本。' : '本次范围没有待译版本。'}</p>}
    </section>}
    {actionError && <p className="translation-error" role="alert">{actionError}</p>}
    <section className="translation-jobs" aria-label="翻译任务">
      <div className="translation-actions"><h3>翻译任务</h3><button className="button ghost" type="button" disabled={Boolean(action)} onClick={() => setRevision(value => value + 1)}>刷新任务</button></div>
      {jobsError && <p className="translation-error" role="alert">{jobsError}</p>}
      {!jobs && !jobsError && <p role="status" className="translation-hint">正在读取任务…</p>}
      {jobs?.length === 0 && <p className="translation-hint">尚无翻译任务。</p>}
      {job && <>
        <label className="translation-version">查看任务<select value={job.id} onChange={event => selectJob(event.target.value)}>
          {jobs?.map(item => <option value={item.id} key={item.id}>{item.id.slice(-8)} · {jobLabels[item.status]} · {formatNumber(item.total)} 个版本</option>)}
        </select></label>
        <div className="translation-progress">
          <strong>{jobLabels[job.status]}</strong><span>{formatNumber(processed)} / {formatNumber(job.total)} 个版本</span>
          <progress value={processed} max={Math.max(job.total, 1)} aria-label="翻译任务进度" />
        </div>
        <dl className="translation-numbers">
          <div><dt>已完成（含缓存）</dt><dd>{formatNumber(job.completed)}</dd></div>
          <div><dt>排队中</dt><dd>{formatNumber(job.pending)}</dd></div>
          <div><dt>处理中</dt><dd>{formatNumber(job.running)}</dd></div>
          <div><dt>跳过</dt><dd>{formatNumber(job.skipped)}</dd></div>
          <div><dt>失败</dt><dd>{formatNumber(job.failed)}</dd></div>
        </dl>
        {job.error && <p className="translation-error">{job.error}</p>}
        <div className="translation-actions">
          {jobIsActive(job) && <button className="button secondary" type="button" disabled={Boolean(action)} onClick={() => changeJob('pause')}>暂停任务</button>}
          {job.status === 'paused' && <button className="button secondary" type="button" disabled={Boolean(action)} onClick={() => changeJob('resume')}>继续任务</button>}
          {!jobIsActive(job) && job.failed > 0 && <button className="button secondary" type="button" disabled={Boolean(action)} onClick={() => changeJob('retry')}>重试失败项</button>}
        </div>
      </>}
    </section>
  </div>;
}

export default function TranslationWorkspace({ selected }: { selected: Set<string> }) {
  const [open, setOpen] = useState(false);
  return <section className="section-block translation-workspace" aria-label="文献库中文翻译">
    <header className="translation-entry"><h2>中文翻译</h2>
      <button className="button secondary" type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>手动批次与任务</button>
    </header>
    <AutomaticTranslation />
    {open && <BatchTranslation selected={selected} />}
  </section>;
}
