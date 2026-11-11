import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import AppShell from './components/AppShell';
import { AppRoute } from './components/Sidebar';
import Dashboard from './pages/Dashboard';
import { DetailSelection } from './types';
import { applyDshTheme } from './dshTheme';

const Discover = lazy(() => import('./pages/Discover'));
const Library = lazy(() => import('./pages/Library'));
const Materials = lazy(() => import('./pages/Materials'));
const Monitors = lazy(() => import('./pages/Monitors'));
const System = lazy(() => import('./pages/System'));

const routes = new Set<AppRoute>(['/dashboard', '/discover', '/library', '/materials', '/monitors', '/system']);

function embeddedParentOrigin(): string | undefined {
  if (window.parent === window) return;
  // Chromium can omit the referrer on a navigation from the DSH desktop scheme.
  if (!document.referrer) return 'dsh-app://app';
  const parent = new URL(document.referrer);
  if (parent.protocol === 'dsh-app:' && parent.host === 'app') return 'dsh-app://app';
  if (parent.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(parent.hostname)) return parent.origin;
}

function currentRoute(): AppRoute {
  const path = window.location.pathname.replace(/\/$/, '') || '/dashboard';
  return routes.has(path as AppRoute) ? path as AppRoute : '/dashboard';
}

export default function App() {
  const [route, setRoute] = useState<AppRoute>(currentRoute);
  const [detail, setDetail] = useState<DetailSelection>(null);
  const [projectId, setProjectId] = useState<string>();
  useEffect(() => {
    if (!routes.has(window.location.pathname as AppRoute)) window.history.replaceState({}, '', '/dashboard');
    const pop = () => { setRoute(currentRoute()); setDetail(null); };
    window.addEventListener('popstate', pop);
    return () => window.removeEventListener('popstate', pop);
  }, []);
  const navigate = useCallback((next: AppRoute) => {
    if (next !== route) window.history.pushState({}, '', next);
    setRoute(next); setDetail(null); window.scrollTo(0, 0);
  }, [route]);
  useEffect(() => {
    const origin = embeddedParentOrigin();
    if (!origin) return;
    document.title = '凝态文献台';
    const receive = (event: MessageEvent) => {
      if (event.origin !== origin || event.source !== window.parent) return;
      const message = event.data;
      if (message?.type === 'dsh-paper-desk:theme') {
        if (applyDshTheme(message.theme, message.scheme, message.tokens)) {
          window.parent.postMessage({ type: 'dsh-paper-desk:theme-applied', theme: message.theme }, origin);
        }
        return;
      }
      if (message?.type !== 'dsh-paper-desk:navigate' || !routes.has(message.route)) return;
      if (typeof message.projectId === 'string' && /^[0-9A-HJKMNP-TV-Z]{16}$/.test(message.projectId)) {
        setProjectId(message.projectId);
      }
      if (message.route === route) {
        window.parent.postMessage({ type: 'dsh-paper-desk:ready', route }, origin);
      } else navigate(message.route);
    };
    window.addEventListener('message', receive);
    window.parent.postMessage({ type: 'dsh-paper-desk:ready', route }, origin);
    return () => window.removeEventListener('message', receive);
  }, [route, navigate]);
  const selectPaper = useCallback((id: string) => setDetail({ kind: 'paper', id }), []);
  const selectMaterial = useCallback((id: string) => setDetail({ kind: 'material', id }), []);
  const closeDetail = useCallback(() => setDetail(null), []);

  return <AppShell route={route} detail={detail} onNavigate={navigate} onCloseDetail={closeDetail} onSelectPaper={selectPaper} onSelectMaterial={selectMaterial}>
    <Suspense fallback={<div className="route-loading" role="status">正在载入页面…</div>}>
      {route === '/dashboard' && <Dashboard onSelectPaper={selectPaper} onSelectMaterial={selectMaterial} />}
      {route === '/discover' && <Discover onSelectPaper={selectPaper} />}
      {route === '/library' && <Library onSelectPaper={selectPaper} />}
      {route === '/materials' && <Materials onSelectMaterial={selectMaterial} />}
      {route === '/monitors' && <Monitors onSelectPaper={selectPaper} />}
      {route === '/system' && <System projectId={projectId} />}
    </Suspense>
  </AppShell>;
}
