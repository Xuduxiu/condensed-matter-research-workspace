import { useEffect, useState } from 'react';
import { apiGet } from '../api';

export type AppRoute = '/dashboard' | '/discover' | '/library' | '/materials' | '/monitors' | '/system';

const nav: Array<{ route: AppRoute; label: string }> = [
  { route: '/dashboard', label: '工作台' },
  { route: '/discover', label: '发现' },
  { route: '/library', label: '文献库' },
  { route: '/materials', label: '材料' },
  { route: '/monitors', label: '监控' },
  { route: '/system', label: '系统' },
];

export default function WorkspaceNav({ active, onNavigate }: { active: AppRoute; onNavigate: (route: AppRoute) => void }) {
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
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
    <header className="desk-masthead">
      <div className="masthead-inner">
        <span className="desk-brand"><span className="desk-book" aria-hidden="true" />凝态文献台</span>
        <nav className="desk-navigation" aria-label="文献导航">
          {nav.map((item) => (
            <button
              type="button"
              key={item.route}
              aria-current={active === item.route ? 'page' : undefined}
              onClick={() => onNavigate(item.route)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="desk-connection" title={`每日任务 · ${runStatus}`}>
          <span data-connected={apiOnline} aria-hidden="true" />
          {apiOnline === null ? '连接中' : apiOnline ? '本地库在线' : '本地库离线'}
          {runStatus === 'running' && <small>扫描中</small>}
          {runStatus === 'failed' && <small>上次扫描失败</small>}
        </div>
      </div>
    </header>
  );
}
