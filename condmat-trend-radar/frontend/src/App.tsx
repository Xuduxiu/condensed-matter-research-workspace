import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import AppShell from './components/AppShell';
import { AppRoute } from './components/Sidebar';
import Dashboard from './pages/Dashboard';
import { DetailSelection } from './types';

const Discover = lazy(() => import('./pages/Discover'));
const Library = lazy(() => import('./pages/Library'));
const Materials = lazy(() => import('./pages/Materials'));
const Monitors = lazy(() => import('./pages/Monitors'));
const System = lazy(() => import('./pages/System'));

const routes = new Set<AppRoute>(['/dashboard', '/discover', '/library', '/materials', '/monitors', '/system']);

function currentRoute(): AppRoute {
  const path = window.location.pathname.replace(/\/$/, '') || '/dashboard';
  return routes.has(path as AppRoute) ? path as AppRoute : '/dashboard';
}

export default function App() {
  const [route, setRoute] = useState<AppRoute>(currentRoute);
  const [detail, setDetail] = useState<DetailSelection>(null);
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
  const selectPaper = useCallback((id: string) => setDetail({ kind: 'paper', id }), []);
  const selectMaterial = useCallback((id: string) => setDetail({ kind: 'material', id }), []);
  const closeDetail = useCallback(() => setDetail(null), []);

  return <AppShell route={route} detail={detail} onNavigate={navigate} onCloseDetail={closeDetail} onSelectPaper={selectPaper} onSelectMaterial={selectMaterial}>
    <Suspense fallback={<div className="route-loading" role="status">Loading workspace…</div>}>
      {route === '/dashboard' && <Dashboard onSelectPaper={selectPaper} onSelectMaterial={selectMaterial} />}
      {route === '/discover' && <Discover onSelectPaper={selectPaper} />}
      {route === '/library' && <Library onSelectPaper={selectPaper} />}
      {route === '/materials' && <Materials onSelectMaterial={selectMaterial} />}
      {route === '/monitors' && <Monitors onSelectPaper={selectPaper} />}
      {route === '/system' && <System />}
    </Suspense>
  </AppShell>;
}
