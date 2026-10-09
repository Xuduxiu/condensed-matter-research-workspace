import { AppRoute } from './Sidebar';

const pageMeta: Record<AppRoute, { title: string; description: string }> = {
  '/dashboard': { title: '今日总览', description: '从发现到归档的每日研究态势' },
  '/discover': { title: '发现论文', description: '统一检索本地元数据与 PDF 全文' },
  '/library': { title: '文献库', description: '论文版本、PDF、队列与导出' },
  '/materials': { title: '材料雷达', description: '材料热度、突发变化与长期趋势' },
  '/monitors': { title: '监控', description: '持续跟踪关键词、材料、期刊与作者' },
  '/system': { title: '系统', description: '数据库、索引、任务与迁移状态' },
};

export default function TopBar({ route, hasDetail, onCloseDetail }: { route: AppRoute; hasDetail: boolean; onCloseDetail: () => void }) {
  const meta = pageMeta[route];
  return (
    <header className="topbar">
      <div><h1>{meta.title}</h1><p>{meta.description}</p></div>
      <div className="topbar-actions">
        <span className="environment-pill">LOCAL-FIRST · SQLITE</span>
        {hasDetail && <button className="icon-button" type="button" aria-label="关闭详情面板" onClick={onCloseDetail}>×</button>}
      </div>
    </header>
  );
}
