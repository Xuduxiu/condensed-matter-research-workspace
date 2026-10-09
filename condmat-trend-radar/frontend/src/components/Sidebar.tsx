import { useEffect, useState } from 'react';
import { apiGet } from '../api';

export type AppRoute = '/dashboard' | '/discover' | '/library' | '/materials' | '/monitors' | '/system';

const nav: Array<{ route: AppRoute; label: string; sub: string; glyph: string }> = [
  { route: '/dashboard', label: '总览', sub: 'Today', glyph: '⌁' },
  { route: '/discover', label: '发现', sub: 'Discover', glyph: '⌕' },
  { route: '/library', label: '文献库', sub: 'Library', glyph: '▤' },
  { route: '/materials', label: '材料雷达', sub: 'Materials', glyph: '◇' },
  { route: '/monitors', label: '监控', sub: 'Monitors', glyph: '◎' },
  { route: '/system', label: '系统', sub: 'System', glyph: '⚙' },
];

export default function Sidebar({ active, onNavigate }: { active: AppRoute; onNavigate: (route: AppRoute) => void }) {
  const [apiOnline, setApiOnline] = useState(false);
  const [runStatus, setRunStatus] = useState('未运行');

  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;
    const check = async () => {
      controller?.abort();
      controller = new AbortController();
      const [library, daily] = await Promise.allSettled([
        apiGet<{ installed: boolean }>('/api/library/status', { signal: controller.signal, force: true }),
        apiGet<{ runs: Array<{ status: string }> }>('/api/daily/status?limit=1', { signal: controller.signal, force: true }),
      ]);
      if (disposed) return;
      setApiOnline(library.status === 'fulfilled');
      setRunStatus(daily.status === 'fulfilled' ? daily.value.runs[0]?.status ?? '未运行' : '未知');
    };
    void check();
    const timer = window.setInterval(check, 15_000);
    return () => { disposed = true; controller?.abort(); window.clearInterval(timer); };
  }, []);

  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true"><i /><i /><i /></div>
        <div><strong>CondMat Radar</strong><span>RESEARCH WORKBENCH · V2</span></div>
      </div>
      <nav className="nav-list">
        {nav.map((item) => (
          <button
            type="button"
            key={item.route}
            className={`nav-link ${active === item.route ? 'active' : ''}`}
            aria-current={active === item.route ? 'page' : undefined}
            onClick={() => onNavigate(item.route)}
          >
            <span className="nav-glyph" aria-hidden="true">{item.glyph}</span>
            <span className="nav-copy"><strong>{item.label}</strong><small>{item.sub}</small></span>
          </button>
        ))}
      </nav>
      <div className="sidebar-status">
        <div><span className={`status-dot ${apiOnline ? 'online' : 'offline'}`} />API <strong>{apiOnline ? '在线' : '离线'}</strong></div>
        <div><span className={`status-dot ${runStatus === 'running' ? 'warning' : runStatus === 'failed' || runStatus === '未知' ? 'offline' : 'online'}`} />每日任务 <strong>{runStatus}</strong></div>
      </div>
      <div className="sidebar-foot">LOCAL-FIRST · SQLITE</div>
    </aside>
  );
}
