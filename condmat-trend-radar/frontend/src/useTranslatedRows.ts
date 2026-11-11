import { useEffect, useState } from 'react';
import { apiQueryJson } from './api';
import { translationApi, translationUpdated, VersionTranslation } from './translations';
import { PaperSummary } from './types';

const emptyRows: PaperSummary[] = [];
function needsText(row: PaperSummary) {
  const view = row.translation_zh;
  if (view?.status === 'ready' || view?.status === 'skipped') return false;
  return Boolean((row.title && !view?.title_zh) || (row.abstract && !view?.abstract_zh));
}

/** Patch only the displayed local result page; never repeat a search or start translation. */
export function useTranslatedRows(rows: PaperSummary[] | undefined, enabled: boolean) {
  const [snapshot, setSnapshot] = useState<{ source: PaperSummary[]; translated: PaperSummary[] }>();
  useEffect(() => {
    if (!enabled || !rows?.length) return;
    const source = rows;
    const pending = new Set(source.filter(needsText).map(row => row.paper_version_id));
    if (!pending.size) return;
    const changes = new Map<string, VersionTranslation>();
    const originals = new Map(source.map(row => [row.paper_version_id, row.translation_zh]));
    let timer: number | undefined, request: AbortController | undefined;
    let lastRead = 0, delay = 5000, stopped = false;
    function schedule(ms: number) {
      window.clearTimeout(timer);
      if (!stopped && pending.size && !document.hidden && !request) timer = window.setTimeout(read, ms);
    }
    async function read() {
      if (stopped || document.hidden || request) return;
      const controller = new AbortController();
      request = controller;
      lastRead = Date.now();
      try {
        const ids = [...pending];
        // Both list page sizes are capped at 100, the server's lookup bound.
        const reply = await apiQueryJson<{ items: Record<string, VersionTranslation> }>(translationApi + '/lookup', { version_ids: ids }, controller.signal);
        if (stopped || controller.signal.aborted) return;
        let changed = false;
        for (const id of ids) {
          const entry = reply.items[id];
          if (!entry || entry.version_id !== id) continue;
          if (entry.status === 'ready' || entry.status === 'skipped') pending.delete(id);
          const prev = changes.get(id) ?? originals.get(id);
          if (prev?.source_hash === entry.source_hash && prev.title_zh === entry.title_zh && prev.abstract_zh === entry.abstract_zh && prev.status === entry.status) continue;
          changes.set(id, entry);
          changed = true;
        }
        if (changed) setSnapshot({ source, translated: source.map(row => {
          const translation = changes.get(row.paper_version_id);
          return translation ? { ...row, translation_zh: translation } : row;
        }) });
        delay = changed ? 12000 : Math.min(45000, Math.max(10000, delay * 2));
      } catch {
        // A failed read cannot invalidate a previously displayed translation.
        delay = 45000;
      } finally {
        if (request === controller) { request = undefined; schedule(delay); }
      }
    }
    const wake = (event: Event) => {
      const id = (event as CustomEvent<string>).detail;
      if (id && !pending.has(id)) return;
      schedule(Math.max(100, (id ? 750 : 10000) - (Date.now() - lastRead)));
    };
    const visibility = () => {
      if (document.hidden) { window.clearTimeout(timer); request?.abort(); request = undefined; }
      else schedule(Math.max(200, 2000 - (Date.now() - lastRead)));
    };
    window.addEventListener(translationUpdated, wake);
    document.addEventListener('visibilitychange', visibility);
    schedule(delay);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      request?.abort();
      window.removeEventListener(translationUpdated, wake);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [rows, enabled]);
  return rows && rows === snapshot?.source ? snapshot.translated : rows ?? emptyRows;
}
