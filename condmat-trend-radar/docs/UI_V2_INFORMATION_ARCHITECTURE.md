# Radar Web v2 · 信息架构

## 产品主线

`今日总览 → 发现论文 → 右侧查看详情 → 下载 PDF → 文献库管理 → 材料归因 → 持续监控`

一级导航严格收敛为六项：

| 导航 | 路由 | 核心任务 | 主要数据 |
|---|---|---|---|
| 总览 | `/dashboard` | 每日决策、关注论文、热点变化、运行摘要 | dashboard 聚合、daily runs |
| 发现 | `/discover` | 本地元数据 / PDF 全文检索 | 服务端分页论文搜索 |
| 文献库 | `/library` | 全量真实论文、PDF、队列、复核、RIS | library + system 聚合 |
| 材料雷达 | `/materials` | 热点、预印本、见刊、突发、持续趋势 | materials 聚合与月序列 |
| 监控 | `/monitors` | 创建并检查简单监控规则 | monitor_queries |
| 系统 | `/system` | 数据库、PDF、FTS、下载、每日任务、迁移、日志 | system 聚合 |

下载器、搜索历史、分析中心和任务管理不再是一级入口。下载成为论文行、详情面板和文献库的内联动作；每日任务进入总览与系统；材料分析成为独立雷达页。

## 全局布局

- 左侧：216 px 固定导航，底部显示 API 与每日任务状态
- 中间：自适应工作区，顶部 72 px 页面栏，主体独立滚动
- 右侧：桌面 390–420 px 详情列；1200 px 以下变为覆盖抽屉
- 1366 px 桌面保持三列可用，主区最小 600 px
- 820 px 以下收窄导航为图标栏，表格允许局部横向滚动

## 详情策略

论文和材料不再强制跳转新页。列表行支持：

- 点击打开详情
- Enter 打开详情
- Esc 关闭详情
- 真实按钮元素与 focus-visible

论文详情统一包含元数据、摘要、版本、材料、主题、文件、下载状态与已有操作；材料详情包含实体元数据、24 个月预印本/见刊序列、主题和最近论文。

## 组件体系

壳层：`AppShell`, `Sidebar`, `TopBar`

数据行与详情：`PaperRow`, `PaperTable`, `PaperDetailPanel`, `MaterialRow`, `MaterialDetailPanel`

状态与控制：`StatusBadge`, `MetricStrip`, `FilterBar`, `SearchInput`, `EmptyState`, `ErrorState`, `LoadingSkeleton`, `DownloadStatus`, `VersionBadge`, `TrendIndicator`, `RunSummary`, `ConfirmDialog`

所有页面复用同一套状态、按钮、标签、筛选和密度规则。

## 设计令牌

根 CSS 定义：

- `--background`
- `--surface`
- `--surface-elevated`
- `--border`
- `--text-primary`
- `--text-secondary`
- `--accent`
- `--success`
- `--warning`
- `--danger`
- `--preprint`
- `--publication`

视觉强调只服务状态语义，不使用大面积渐变、玻璃拟态、巨型统计卡或无意义动画。

## 大库性能策略

- 默认 50 条，允许 25 / 50 / 100
- 总数、筛选、排序全部在 SQLite 服务端完成
- 320 ms 搜索防抖
- 路由/筛选变化取消上一请求
- GET 查询短时缓存，写操作后清空缓存
- 论文详情与材料详情缓存 30 秒
- 单页最多 100 条，因此不额外引入虚拟列表依赖
- 正式界面从不请求全部 18.5 万条记录

## 错误与真实性

页面必须显式覆盖 loading、empty、API error、offline、permission、partial data 与下载失败。API 错误只显示短消息，不渲染 traceback 或秘密配置。缺失字段统一使用“—”或“未提供”。远程搜索入口保留但明确标识未接入。
