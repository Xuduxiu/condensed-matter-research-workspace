import { ReactNode, useEffect } from 'react';
import { DetailSelection } from '../types';
import MaterialDetailPanel from './MaterialDetailPanel';
import PaperDetailPanel from './PaperDetailPanel';
import Sidebar, { AppRoute } from './Sidebar';
import TopBar from './TopBar';

type Props = {
  route: AppRoute;
  children: ReactNode;
  detail: DetailSelection;
  onNavigate: (route: AppRoute) => void;
  onCloseDetail: () => void;
  onSelectPaper: (id: string) => void;
  onSelectMaterial: (id: string) => void;
};

export default function AppShell({ route, children, detail, onNavigate, onCloseDetail, onSelectPaper, onSelectMaterial }: Props) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && detail) onCloseDetail();
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [detail, onCloseDetail]);

  return (
    <div className={`app-shell ${detail ? 'detail-open' : ''}`}>
      <Sidebar active={route} onNavigate={onNavigate} />
      <main className="workspace" id="main-content">
        <TopBar route={route} hasDetail={Boolean(detail)} onCloseDetail={onCloseDetail} />
        <div className="workspace-scroll">{children}</div>
      </main>
      {detail && <button className="detail-backdrop" type="button" aria-label="关闭详情" onClick={onCloseDetail} />}
      <aside className={`detail-panel ${detail ? 'is-open' : ''}`} aria-label="详情面板" aria-hidden={!detail}>
        {detail?.kind === 'paper' && (
          <PaperDetailPanel
            id={detail.id}
            onClose={onCloseDetail}
            onSelectMaterial={onSelectMaterial}
          />
        )}
        {detail?.kind === 'material' && (
          <MaterialDetailPanel
            id={detail.id}
            onClose={onCloseDetail}
            onSelectPaper={onSelectPaper}
          />
        )}
      </aside>
    </div>
  );
}
