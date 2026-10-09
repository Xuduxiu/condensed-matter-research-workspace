# Radar Web v2.1 · 实施报告

## STATUS

PASS。Radar Web v2.1 已完成统一远程检索、个人工作台、下载队列闭环、安全 PDF 浏览、生产数据库迁移以及真实浏览器验收。

## 本轮交付

- OpenAlex、Crossref、arXiv 并行检索，按 DOI、arXiv ID、标题 + 首位作者合并重复项
- 本地 + 远程联合搜索；远程结果只有在“保存”或“加入下载队列”时才写入数据库
- 收藏、稍后阅读、论文笔记、专题创建/删除、批量加入和专题筛选
- 下载队列立即处理、失败任务重试、合法开放获取 PDF 校验与内联浏览
- 论文详情直接添加专题、打开来源、导出 RIS 和安全打开本地 PDF
- 系统页新增个人工作台 schema、计数、队列状态和操作审计
- 远程首屏限制为 18 条，避免三源检索阻塞本地工作流；远程记录 ID 使用稳定 SHA-1

## 数据库

生产数据库：`G:\condmat-trend-radar\condmat_radar.sqlite`

新增迁移：`radar_workbench_v2`（version 2）。迁移只新增以下表，不修改或删除旧趋势分析与统一资料库表：

- `paper_user_state`
- `paper_collections`
- `collection_papers`
- `user_action_log`
- `workbench_schema_migrations`

空的默认用户状态会被自动删除，避免仅查看或反复切换状态产生垃圾记录。

## 新增 API

- `GET /api/library/remote-search`
- `POST /api/library/remote/import`
- `GET /api/library/workbench/status`
- `GET/POST /api/library/user-state/{canonical_paper_id}`
- `GET/POST /api/library/collections`
- `POST /api/library/collections/{collection_id}/papers`
- `POST /api/library/collections/{collection_id}/remove`
- `POST /api/library/collections/{collection_id}/delete`
- `POST /api/library/downloads/{task_id}/retry`
- `POST /api/library/downloads/run`
- `GET /api/library/files/{file_id}/content`
- `GET /api/library/actions`

## VALIDATION

- Python compileall：PASS
- Backend unittest：PASS，18 / 18
- TypeScript strict：PASS
- ESLint：PASS，0 warnings / 0 errors
- Vite production build：PASS，49 modules；CSS 34.76 kB；JS 210.43 kB
- 真实三源检索：PASS；OpenAlex / Crossref / arXiv 均可独立返回，重复项可合并
- 真实数据库状态回归：PASS；收藏、稍后阅读、专题和专题成员均恢复为 0
- 安全 PDF 接口：PASS；返回 `application/pdf`、`inline` 且文件头为 `%PDF-`
- 桌面浏览器：PASS；Library 与 System 实际数据加载，无横向溢出，API 在线
- 真实下载队列：调度链路 PASS；测试论文没有合法开放获取 PDF，任务按策略进入 `permanent_failed`，未绕过版权限制

机器可读报告：`work/integration_reports/radar_v2_1_contract.json`

## 仍未实现

- 监控编辑、启停、立即执行和命中历史
- 作者机构与研究团队可靠聚合
- FTS 重建时间持久化
- Windows 计划任务注册状态
- 完整日志读取与下载取消
- PDF 标注和阅读进度同步

## 下一阶段建议

优先补齐“监控命中 → 自动保存 → 合法 OA 下载 → 稍后阅读/专题”的自动流水线，其次增加可取消下载、批量重试和 PDF 阅读器标注。

## Radar Web v2.2 live workflow（2026-07-15）

本轮将实时扫描、摘要回填、论文派生材料、立即 PDF 下载和 Zotero 附件导出整合为一条可观察工作流。完整实现、生产数据计数、性能基准和浏览器验收见 `RADAR_WEB_V2_2_LIVE_WORKFLOW.md`。