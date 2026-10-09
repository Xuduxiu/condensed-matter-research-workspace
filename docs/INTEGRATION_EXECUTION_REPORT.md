# Integration Execution Report

执行日期：2026-07-13（Asia/Shanghai）

## 已完成

- CondMat Radar 已确立为唯一日常主应用；根控制器 `paper.cmd start` 默认只启动 Radar。
- 新增 42 条增量 schema 语句，建立论文版本、作者、主题、材料、文件、全文、下载队列、监控、游标、每日运行、分析结果、迁移账本和人工复核结构。
- 正式迁移 Radar 既有 185,103 条记录为 185,103 个 legacy 版本。
- Intake 76 条 canonical 记录全部迁移：4 条 DOI 精确关联，72 条 library-only 新记录，72 条凝聚态语料资格人工复核。
- 保留 Intake 211 条旧观测、135 条旧别名、165 条外部身份、12 条搜索历史和 42 条 LLM 缓存。
- 29 份旧 PDF 文件按 SHA-256 合并为 9 个唯一文件；29 条原路径全部保留；9 份全文已用 PyMuPDF 提取。
- FTS5 已重建，共 185,179 个论文版本；正文独有词检索验证通过。
- 下载队列支持合法 OA 解析、PDF 头和大小验证、哈希去重、指数退避、中断恢复和单实例锁。
- Zotero 使用稳定的手动 RIS 导入包，不声明未实现的直接写入。
- 每日更新 dry-run 和一次 `--apply --skip-network` 正式运行均通过；旧成功游标不会在失败时推进。
- Windows 每日任务安装脚本已提供：`condmat-trend-radar/scripts/install_daily_task.ps1`。

## 生产验证

- 生产库：`G:\condmat-trend-radar\condmat_radar.sqlite`
- 迁移前备份：`G:\condmat-trend-radar\backups\integration\condmat_radar_before_unified_20260713T120234Z.sqlite`
- 备份大小：5,738,872,832 bytes
- 备份 SHA-256：`A2E6F09322853B1269A9761283E8A7643A80BBA5F887798CD0487987E4BB5202`
- 备份检查：`quick_check=ok`，外键错误 0，论文数 185,103
- 当前 canonical papers：185,175
- 当前 paper versions：185,179
- 作者：10,419；主题：5,529；材料：28
- 唯一 PDF：9；旧路径来源：29；全文记录：9
- FTS5：185,179
- 待人工复核：74（72 条语料资格 + 2 条标题版本候选）
- 原 `term_month_stats`：422,156，未改变
- 原 `published_preprint_stats`：12,788，未改变
- 当前 `quick_check=ok`，外键错误 0，孤立版本 0，重复 source/version 0，重复 PDF hash 0
- 生产迁移重复执行：schema 新增 0、Radar version 新增 0、作者新增 0、Intake canonical 新增 0、人工复核新增 0

## 测试

- Radar unittest：15 passed
- Intake pytest：65 passed
- Radar backend/scripts compileall：passed
- React TypeScript + Vite production build：passed
- 新版 API 临时端口端到端验证：library status、全文 search、daily dry-run passed
- Radar 环境依赖：`httpx 0.28.1`、`PyMuPDF 1.28.0`

## 未自动执行的外部动作

- 8000 端口仍运行旧 API 进程；遵循不主动终止该进程的约束，新代码需在你允许的维护窗口重启后生效。
- Windows 定时任务安装脚本已就绪，但尚未替你选择每天执行时间并注册系统任务。
- 未执行真实联网的 OpenAlex/arXiv 每日增量；只完成只读 dry-run 和不联网正式流水线验证。

## STATUS

`PARTIAL`

代码、数据迁移、PDF/全文、API、UI、测试和回滚备份均完成；剩余项是需要用户维护窗口或时间选择的外部运行态切换，不是数据或代码阻塞。
