import { PaperSummary } from '../types';
import { DownloadStatus, EmptyState, VersionBadge } from './primitives';

export default function PaperTable({ papers, selected, onToggle, onOpen, onDownload }: {
  papers: PaperSummary[];
  selected?: Set<string>;
  onToggle?: (id: string, value: boolean) => void;
  onOpen: (id: string) => void;
  onDownload?: (paper: PaperSummary) => void;
}) {
  if (!papers.length) return <EmptyState />;
  return <div className="table-shell"><table className="paper-table">
    <thead><tr>{selected && <th aria-label="选择" />}<th>论文</th><th>版本</th><th>来源 / 日期</th><th>材料 / 主题</th><th>PDF</th><th aria-label="操作" /></tr></thead>
    <tbody>{papers.map((paper) => <tr key={paper.paper_version_id}>
      {selected && <td><input type="checkbox" checked={selected.has(paper.paper_version_id)} onChange={(event) => onToggle?.(paper.paper_version_id, event.target.checked)} aria-label={`选择 ${paper.title}`} /></td>}
      <td><strong>{paper.favorite && <span className="favorite-mark" title="已收藏">★ </span>}<button className="paper-title-button" type="button" onClick={() => onOpen(paper.canonical_paper_id)}>{paper.title}</button></strong><small>{paper.authors.slice(0, 3).join(' · ') || '作者未提供'}</small></td>
      <td><VersionBadge type={paper.version_type} /></td>
      <td>{paper.journal || paper.source || '—'}<small>{paper.publication_date || paper.submitted_date || '—'}</small></td>
      <td><div className="cell-tags">{paper.materials.slice(0, 2).concat(paper.topics.slice(0, 1)).map((item) => <span className="tag" key={item}>{item}</span>)}</div></td>
      <td><DownloadStatus downloaded={paper.downloaded} status={paper.download_status} extractionStatus={paper.extraction_status} /></td>
      <td>{!paper.downloaded && <button className="mini-button" type="button" onClick={() => onDownload?.(paper)}>立即下载</button>}</td>
    </tr>)}</tbody>
  </table></div>;
}
