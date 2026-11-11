import { paperText } from '../paperText';
import { useLayoutEffect, useRef, useState } from 'react';
import { PaperSummary } from '../types';
import { DownloadStatus, EmptyState, VersionBadge } from './primitives';

export default function PaperTable({ papers, selected, onToggle, onOpen, onDownload }: {
  papers: PaperSummary[];
  selected?: Set<string>;
  onToggle?: (id: string, value: boolean) => void;
  onOpen: (id: string) => void;
  onDownload?: (paper: PaperSummary) => void;
}) {
  const grid = useRef<HTMLDivElement>(null);
  const [lanes, setLanes] = useState(1);
  const focus = useRef<{ element: HTMLElement; version: string; column: number } | null>(null);

  useLayoutEffect(() => {
    if (!grid.current) return;
    const container = grid.current;
    let previous = 1;
    function measure() {
      // Use the browser's resolved auto-fill tracks, including the current zoom and font size.
      const count = getComputedStyle(container).gridTemplateColumns.split(/\s+/).length;
      if (count === previous) return;
      previous = count;
      const element = document.activeElement;
      const cell = element instanceof HTMLElement ? element.closest('td') : null;
      const version = cell?.closest('tr')?.dataset.version;
      if (element instanceof HTMLElement && cell && version && container.contains(cell)) {
        focus.current = { element, version, column: cell.cellIndex };
      }
      setLanes(count);
    }
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const target = focus.current;
    focus.current = null;
    if (!target || target.element.isConnected || document.activeElement !== document.body) return;
    const row = grid.current?.querySelector<HTMLTableRowElement>(`tr[data-version="${CSS.escape(target.version)}"]`);
    row?.cells[target.column]?.querySelector<HTMLElement>('input, button')?.focus({ preventScroll: true });
  }, [lanes]);

  const count = Math.min(lanes, papers.length);
  const groups = Array.from({ length: count }, (_, index) => {
    const start = Math.ceil(index * papers.length / count);
    const end = Math.ceil((index + 1) * papers.length / count);
    return { start, end, items: papers.slice(start, end) };
  });
  return <div className="paper-tables" ref={grid}>
    {!papers.length && <EmptyState />}
    {groups.map((group, index) => (
      <div className="table-shell" key={index}>
        <table className="paper-table">
          {groups.length > 1 && <caption>本页第 {group.start + 1}–{group.end} 条</caption>}
          <thead><tr>{selected && <th aria-label="选择" />}<th>论文</th><th>版本</th><th>来源 / 日期</th><th>材料 / 主题</th><th>PDF</th><th aria-label="操作" /></tr></thead>
          <tbody>{group.items.map((paper) => <tr key={paper.paper_version_id} data-version={paper.paper_version_id}>
            {selected && <td><input type="checkbox" checked={selected.has(paper.paper_version_id)} onChange={(event) => onToggle?.(paper.paper_version_id, event.target.checked)} aria-label={`选择 ${paperText(paper.translation_zh?.title_zh || paper.title)}`} /></td>}
            <td><strong>{paper.favorite && <span className="favorite-mark" title="已收藏">★ </span>}<button className="paper-title-button" type="button" title={paper.translation_zh ? paperText(paper.title) : undefined} onClick={() => onOpen(paper.canonical_paper_id)}>{paperText(paper.translation_zh?.title_zh || paper.title)}</button></strong><small>{paper.authors.slice(0, 3).join(' · ') || '作者未提供'}</small></td>
            <td><VersionBadge type={paper.version_type} /></td>
            <td>{paper.journal || paper.source || '—'}<small>{paper.publication_date || paper.submitted_date || '—'}</small></td>
            <td><div className="cell-tags">{paper.materials.slice(0, 2).concat(paper.topics.slice(0, 1)).map((item) => <span className="tag" key={item}>{item}</span>)}</div></td>
            <td><DownloadStatus downloaded={paper.downloaded} status={paper.download_status} extractionStatus={paper.extraction_status} /></td>
            <td>{!paper.downloaded && <button className="mini-button" type="button" onClick={() => onDownload?.(paper)}>立即下载</button>}</td>
          </tr>)}</tbody>
        </table>
      </div>
    ))}
  </div>;
}
