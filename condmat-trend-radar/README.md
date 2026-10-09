# CondMat Radar：统一论文发现、趋势与资料库

CondMat Radar 是本工作区唯一日常主应用。后端使用 FastAPI + SQLite，前端使用 React + TypeScript + Vite。原 `lab_paper_intake` 的可靠能力已迁入 `backend/downloader`、`backend/library` 与 `backend/scheduler`，旧项目只作为迁移来源和回滚对照保留。

## 功能

- OpenAlex、Crossref、arXiv 元数据发现与凝聚态过滤
- 概念、材料、方法、期刊、预印本/正式发表趋势分析
- canonical paper 与 `paper_versions` 分离
- DOI、arXiv、journal-reference DOI 精确关联；标题候选进入人工复核
- 论文元数据、作者、主题、材料、PDF、全文、下载状态统一检索
- SHA-256 PDF 去重，保留所有旧文件路径来源
- SQLite FTS5 本地全文检索
- 持久化下载队列、指数退避、失败恢复与单实例锁
- 保存监控条件、每日增量更新、成功游标与运行报告
- CSV、BibTeX、Markdown 与 Zotero 兼容 RIS 包
- 中英文界面；英文标题、DOI、arXiv ID 和专业术语保持原文

## 启动

从工作区根目录运行：

```powershell
.\paper.cmd start
```

或分别启动：

```powershell
cd condmat-trend-radar
.venv\Scripts\python.exe -m backend.api.main

cd frontend
npm.cmd run dev
```

打开 <http://127.0.0.1:5173>。

## Windows 一键安装包

无需 Python/Node 的 Windows x64 发布包由以下命令生成：

```powershell
.\deployment\windows\build_release.ps1
```

公开 ZIP 不含任何密钥或历史数据；给组内电脑配置 API 时，用 `create_private_handoff.ps1` 另建不压缩的私人交接目录。目标电脑默认安装到 `%LOCALAPPDATA%\Programs\CondMatRadar`，数据与配置独立保存在 `%LOCALAPPDATA%\CondMatRadar`。完整流程见 [Windows 安装与交接](docs/WINDOWS_INSTALL_HANDOFF.md)。

## 统一迁移脚本

以下脚本默认 dry-run；正式写入必须显式使用 `--apply`：

```powershell
.venv\Scripts\python.exe scripts\audit_legacy_projects.py
.venv\Scripts\python.exe scripts\migrate_legacy_databases.py
.venv\Scripts\python.exe scripts\import_existing_pdfs.py
.venv\Scripts\python.exe scripts\rebuild_search_index.py
.venv\Scripts\python.exe scripts\link_preprints_and_publications.py
.venv\Scripts\python.exe scripts\run_daily_update.py --skip-network
.venv\Scripts\python.exe scripts\verify_migration.py
```

配置示例见 [config/example.yaml](config/example.yaml)。真实密钥只放在环境变量或 `.env.local`，日志与状态接口只显示脱敏信息。

## 新 API

- `GET /api/library/status`
- `GET /api/library/search`
- `POST /api/library/downloads`
- `GET /api/library/downloads`
- `POST /api/library/monitors`
- `GET /api/library/monitors`
- `GET /api/library/reviews`
- `POST /api/library/reviews/{review_id}`
- `POST /api/library/export/zotero`
- `GET /api/daily/status`
- `POST /api/daily/run`
- `POST /api/daily/stop/{run_id}`

原趋势、热力图、材料、生命周期、共现网络和论文接口继续保留。

## 验证

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q backend scripts
cd frontend
npm.cmd run build
```

生产迁移前必须保留已通过 `quick_check` 与外键检查的 SQLite 备份。迁移规则、回滚路径和数据边界见工作区根目录 `docs/`。

最新完整实装与生产验收见 [Radar Web v2.3 全链路报告](docs/RADAR_WEB_V2_3_HARDENING.md)。
