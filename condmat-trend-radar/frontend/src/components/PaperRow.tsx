import { PaperSummary } from '../types';
import { DownloadStatus, VersionBadge } from './primitives';

type Props = {
  paper: PaperSummary;
  selected?: boolean;
  selectable?: boolean;
  onSelect?: (selected: boolean) => void;
  onOpen: () => void;
  onDownload?: () => void;
  onMonitor?: () => void;
  dense?: boolean;
};

export default function PaperRow({ paper, selected, selectable, onSelect, onOpen, onDownload, onMonitor, dense = false }: Props) {
  const materialTags = [...new Map(paper.materials.filter(Boolean).map((item) => [item.toLocaleLowerCase(), item])).values()].slice(0, 3);
  const materialNames = new Set(materialTags.map((item) => item.toLocaleLowerCase()));
  const topicTags = [...new Map(paper.topics.filter(Boolean).map((item) => [item.toLocaleLowerCase(), item])).values()]
    .filter((item) => !materialNames.has(item.toLocaleLowerCase()))
    .slice(0, 3);
  return (
    <article className={`paper-row ${dense ? 'dense' : ''}`}>
      {selectable && <label className="row-check"><input type="checkbox" checked={selected} onChange={(event) => onSelect?.(event.target.checked)} aria-label={`选择 ${paper.title}`} /></label>}
      <div className="paper-body">
        <div className="paper-heading"><VersionBadge type={paper.version_type} />{paper.favorite && <span className="favorite-mark" title="已收藏">★</span>}<h3><button className="paper-title-button" type="button" onClick={onOpen}>{paper.title || '未提供标题'}</button></h3></div>
        <p className="paper-meta">
          <span>{paper.authors.length ? paper.authors.slice(0, 4).join(' · ') : '作者未提供'}{paper.authors.length > 4 ? ` 等 ${paper.authors.length} 人` : ''}</span>
          <b>·</b><span>{paper.journal || paper.source || '来源未提供'}</span><b>·</b><time>{paper.publication_date || paper.submitted_date || String(paper.year || '日期未提供')}</time>
        </p>
        {!dense && <p className="paper-abstract">{paper.abstract || '摘要未提供。'}</p>}
        <div className="paper-tags">
          {materialTags.map((item) => <span className="tag material" key={`material:${item}`}>{item}</span>)}
          {topicTags.map((item) => <span className="tag" key={`topic:${item}`}>{item}</span>)}
          {!materialTags.length && !topicTags.length && <span className="muted">材料与主题未提供</span>}
        </div>
      </div>
      <div className="paper-side">
        <DownloadStatus downloaded={paper.downloaded} status={paper.download_status} extractionStatus={paper.extraction_status} />
        <span className="paper-id mono">{paper.doi ? `DOI ${paper.doi}` : paper.arxiv_id ? `arXiv ${paper.arxiv_id}` : 'ID —'}</span>
        <div className="row-actions">
          {!paper.downloaded && <button className="mini-button primary" type="button" onClick={onDownload} aria-label={`下载 ${paper.title}`}>立即下载</button>}
          <button className="mini-button" type="button" onClick={onMonitor} aria-label={`监控 ${paper.title}`}>监控</button>
          <button className="mini-button" type="button" onClick={onOpen}>详情</button>
        </div>
      </div>
    </article>
  );
}
