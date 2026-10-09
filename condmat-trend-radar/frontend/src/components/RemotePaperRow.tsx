import { formatDate } from '../hooks';
import { RemotePaper } from '../types';
import { StatusBadge, VersionBadge } from './primitives';

type Props = {
  paper: RemotePaper;
  busy?: boolean;
  onSave: () => void;
  onDownload: () => void;
  onOpenLocal: () => void;
};

export default function RemotePaperRow({ paper, busy = false, onSave, onDownload, onOpenLocal }: Props) {
  return <article className="remote-paper-row">
    <div className="remote-source-rail">
      <VersionBadge type={paper.version_type} />
      {paper.sources.map((source) => <span key={source}>{source}</span>)}
    </div>
    <div className="remote-paper-main">
      <div className="remote-paper-badges">
        {paper.is_local && <StatusBadge tone="success">本地已有</StatusBadge>}
        {paper.is_open_access && <StatusBadge tone="accent">开放获取</StatusBadge>}
        {!paper.condmat_evidence && <StatusBadge tone="warning">范围待复核</StatusBadge>}
      </div>
      <h3>{paper.title}</h3>
      <p className="paper-authors">{paper.authors.join(' · ') || '作者未提供'}</p>
      <p className="remote-abstract">{paper.abstract || '摘要未提供。'}</p>
      <div className="paper-meta">
        <span>{paper.journal || '来源未提供'}</span>
        <span>{formatDate(paper.publication_date || paper.submitted_date)}</span>
        {paper.doi && <code>{paper.doi}</code>}
        {paper.arxiv_id && <code>arXiv:{paper.arxiv_id}</code>}
      </div>
    </div>
    <div className="remote-paper-actions">
      {paper.is_local
        ? <button className="button secondary" type="button" onClick={onOpenLocal}>打开本地记录</button>
        : <button className="button secondary" type="button" disabled={busy} onClick={onSave}>{busy ? '保存中…' : '保存到文献库'}</button>}
      <button className="button primary" type="button" disabled={busy || paper.is_local && paper.downloaded} onClick={onDownload}>
        {busy ? '处理中…' : paper.is_local ? '立即下载 + Zotero' : '保存并立即下载'}
      </button>
      {paper.url && <a className="button ghost" href={paper.url} target="_blank" rel="noreferrer">来源页面</a>}
    </div>
  </article>;
}
