import { paperText } from '../paperText';
import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPostJson } from '../api';
import { LanguageJob, translationApi, translationError, translationUpdated, VersionTranslation } from '../translations';
import { PaperDetail } from '../types';
import '../styles/translations.css';

const labels: Record<VersionTranslation['status'], string> = {
  missing: '未翻译', queued: '等待翻译', running: '翻译中', ready: '译文已保存', failed: '翻译失败', skipped: '无需翻译',
};

function VersionText({ version }: { version: PaperDetail['versions'][number] }) {
  const path = translationApi + '/versions/' + encodeURIComponent(version.id);
  const [result, setResult] = useState<VersionTranslation | null>(null);
  const [fault, setFault] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [compare, setCompare] = useState(false);
  const [revision, setRevision] = useState(0);
  const previousStatus = useRef<VersionTranslation['status'] | undefined>(undefined);
  const abstract = String(version.abstract ?? '').trim();

  useEffect(() => {
    const refresh = (event: Event) => {
      const updated = (event as CustomEvent<string>).detail;
      if (!updated) setRevision(value => value + 1);
    };
    window.addEventListener(translationUpdated, refresh);
    return () => window.removeEventListener(translationUpdated, refresh);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    const read = async () => {
      try {
        const next = await apiGet<VersionTranslation>(path, { force: true, signal: controller.signal });
        if (controller.signal.aborted) return;
        setResult(next);
        if (next.status === 'ready' && previousStatus.current && previousStatus.current !== 'ready') {
          window.dispatchEvent(new CustomEvent(translationUpdated, { detail: version.id }));
        }
        previousStatus.current = next.status;
        setSubmitted(false);
        setFault('');
        if (next.status === 'queued' || next.status === 'running') timer = window.setTimeout(read, 1800);
        else if (next.status === 'missing') timer = window.setTimeout(read, 20000);
      } catch (error) {
        if (!controller.signal.aborted) setFault(translationError(error));
      }
    };
    void read();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [path, revision, version.id, version.title, abstract]);

  async function submit() {
    setSubmitting(true);
    setFault('');
    try {
      await apiPostJson<LanguageJob>(path, {});
      setSubmitted(true);
      setRevision(value => value + 1);
    } catch (error) {
      setFault(translationError(error));
    } finally { setSubmitting(false); }
  }

  const active = result?.status === 'running' || result?.status === 'queued';
  const canSubmit = result?.status === 'missing' || result?.status === 'failed';
  const hasTranslation = result && result.status !== 'skipped' && (result.status === 'ready' ||
    (result.title_zh && result.title_zh !== version.title) || (result.abstract_zh && result.abstract_zh !== abstract));
  return <>
    <div className="translation-actions">
      <span role="status">{submitted ? '任务已提交' : result ? labels[result.status] : fault ? '状态未读取' : '正在读取译文…'}</span>
      {canSubmit && <button type="button" className="button secondary" disabled={submitting || submitted} onClick={submit}>
        {submitting ? '正在提交…' : result.status === 'failed' ? '重试翻译' : abstract ? '翻译标题和摘要' : '只翻译标题'}
      </button>}
      {hasTranslation && <button type="button" className="button secondary" aria-pressed={compare} onClick={() => setCompare(value => !value)}>{compare ? '收起原文对照' : '中英对照'}</button>}
      {(fault || active) && <button type="button" className="button ghost" onClick={() => setRevision(value => value + 1)}>刷新翻译状态</button>}
    </div>
    {fault && <p role="alert" className="translation-error">{fault}</p>}
    {result?.error && <p className="translation-error">{result.error}</p>}
    {!abstract && <p className="translation-hint">此版本没有摘要，可翻译标题。</p>}
    {result?.status === 'skipped' && <p className="translation-hint">这个版本没有需要翻译的英文字段。</p>}
    {hasTranslation && <div className="translation-reading">
      <div lang="zh-CN"><h4>中文译文</h4>
        {result.title_zh && <strong>{paperText(result.title_zh)}</strong>}
        {result.abstract_zh && <p>{paperText(result.abstract_zh)}</p>}
        {!result.title_zh && !result.abstract_zh && <p>此版本没有可显示的译文。</p>}
      </div>
      {compare && <div><h4>该版本原文</h4><strong>{paperText(version.title)}</strong>{abstract && <p>{paperText(abstract)}</p>}</div>}
    </div>}
  </>;
}

export default function PaperTranslation({ versions }: { versions: PaperDetail['versions'] }) {
  const [choice, setChoice] = useState('');
  const version = versions.find(item => item.id === choice) ?? versions[0];
  return <section className="detail-section paper-translation" aria-label="中文翻译">
    <h3>中文翻译</h3>
    {versions.length > 1 && <label className="translation-version">论文版本
      <select value={version.id} onChange={event => setChoice(event.target.value)}>
        {versions.map(item => <option key={item.id} value={item.id}>{[item.source, item.publication_date, paperText(item.title)].filter(Boolean).join(' · ')}</option>)}
      </select>
    </label>}
    {version ? <VersionText key={version.id} version={version} /> : <p className="translation-hint">没有可翻译的论文版本。</p>}
  </section>;
}
