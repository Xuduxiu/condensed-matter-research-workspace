# Radar Web v2 · 启动审查

日期：2026-07-13<br>
范围：`condmat-trend-radar` 前端与其直接依赖的 API；未重写采集、迁移、下载器和分析管线。

## 结论

旧前端适合作为分析原型，不适合作为统一论文工作台。它使用 React 18、TypeScript、Vite、Tailwind 指令和 ECharts，但没有真实 URL 路由：`App.tsx` 仅在 `research / library / system` 三个状态间切换。论文分析、下载、队列、监控、系统状态彼此嵌在少数大页面中，详情必须跳到局部页面或缺失，无法形成“发现 → 详情 → 下载 → 文献库 → 材料 → 监控”的连续工作流。

Radar Web v2 因此采用独立信息架构和组件体系，同时保留旧页面文件与全部兼容 API。

## 当前技术栈

- React 18.3 + React DOM 18.3
- TypeScript（strict）
- Vite 5
- 原项目包含 ECharts 5；v2 的详情趋势使用轻量 SVG，避免为小图重复初始化大图表实例
- CSS：旧前端主要依赖一个持续追加的 `globals.css`；没有现成组件库
- 路由：旧版无路由库，v2 使用 History API 提供六个稳定 URL，避免为本次重构额外引入路由依赖
- 请求：旧版 `fetch` 包装无取消、缓存、错误分类；v2 新 client 支持 AbortSignal、短时查询缓存、脱敏错误和离线/权限分类

## 旧页面与入口

旧主入口：

- `ResearchRadar.tsx`：内嵌 Overview / Heatmap / Lifecycle / Compare / Papers / ConceptDetail / Cooccurrence
- `Library.tsx`：旧统一库、队列、监控和导出混合页
- `SystemData.tsx`：旧系统与采集状态页

仍在源码中但不再进入 v2 主导航的旧页面：

- `Overview.tsx`
- `Heatmap.tsx`
- `Lifecycle.tsx`
- `Compare.tsx`
- `Papers.tsx`
- `ConceptDetail.tsx`
- `Cooccurrence.tsx`
- `ResearchRadar.tsx`
- `SystemData.tsx`

这些文件没有删除，便于比对与后续按 Git 历史清理；TypeScript v2 构建只纳入新入口实际可达的文件。

## 已有后端能力

旧趋势分析 API（保留）：

- `/api/overview`
- `/api/heatmap`
- `/api/concept/*`
- `/api/lifecycle`
- `/api/cooccurrence`
- `/api/papers`
- `/api/published-preprint/*`
- `/api/config/status`
- CSV / JSON 导出与旧下载中心桥接 API

统一资料库 API（继续使用）：

- `GET /api/library/status`
- `GET /api/library/search`
- `GET/POST /api/library/downloads`
- `GET/POST /api/library/monitors`
- `GET/POST /api/library/reviews`
- `POST /api/library/export/zotero`
- `GET /api/daily/status`
- `POST /api/daily/run`
- `POST /api/daily/stop/{run_id}`

## 数据审查

生产数据库：`G:\condmat-trend-radar\condmat_radar.sqlite`

- Canonical papers：185,175
- Paper versions：185,179
- 真实 canonical papers：182,017
- 真实 versions：182,021
- 遗留 mock papers：3,158
- 通过凝聚态视图筛选的真实 versions：14,737
- Unique PDFs：9
- FTS entries：185,179
- Materials：28
- Pending manual reviews：74

v2 的首页和发现页默认只展示 `data_mode=real AND condmat_view_eligible=1`，避免旧 mock 与非凝聚态来源污染科研入口。文献库显式使用全部真实版本，系统页如实报告 mock 数量用于审计。

## 必要后端缺口与本次补口

原 `/api/library/search` 只返回当前页 `count`，没有总数、稳定排序、富详情和材料趋势聚合，无法满足 18.5 万条数据分页。本次只新增 `/api/ui-v2/*` 只读聚合接口：

- `/api/ui-v2/dashboard`
- `/api/ui-v2/papers`
- `/api/ui-v2/papers/{canonical_paper_id}`
- `/api/ui-v2/materials`
- `/api/ui-v2/materials/{material_id}`
- `/api/ui-v2/system`

写操作仍复用已有下载、监控、RIS 和每日任务 API，没有复制队列或业务逻辑。

## 未接入能力

- 统一远程搜索与本地/远程去重
- 收藏数据模型与 API
- 用户专题 CRUD
- 安全打开本地 PDF 的 API
- 监控编辑、启停、立即执行与独立命中记录
- 可靠的作者隶属关系 → 研究团队聚合
- FTS 重建时间持久化
- Windows 计划任务注册状态查询
- 完整日志读取 API

正式界面对以上能力使用“—”“未提供”或禁用按钮，并说明原因，不生成模拟数据。
## 2026-07-14 · v2.1 缺口回填

启动审查中列出的“统一远程搜索、本地/远程去重、收藏、专题、安全 PDF 打开”已经完成。新增工作台 schema 为纯增量迁移；远程搜索不自动入库；PDF 只通过受控文件 ID 内联返回。

当前剩余缺口：

- 监控编辑、启停、立即执行与命中历史
- 作者隶属关系和研究团队聚合
- FTS 重建时间持久化
- Windows 计划任务注册状态
- 完整日志读取、下载取消和 PDF 标注
