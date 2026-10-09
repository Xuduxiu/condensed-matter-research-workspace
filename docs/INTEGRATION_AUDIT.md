# Integration Audit

审计时间：2026-07-13（Asia/Shanghai）<br>
审计范围：`condmat-trend-radar`、`lab_paper_intake`、根控制器、两套 SQLite、现有 PDF/导出目录、外部 Radar 数据目录。<br>
审计方式：只读扫描、SQLite `mode=ro`、文件哈希；本阶段未删除、移动或覆盖任何旧数据。

## 结论

`condmat-trend-radar` 应成为唯一主程序。它已有更完整的发现、摄取、趋势、材料、预印本/见刊分离统计、FastAPI 和 React UI；`lab_paper_intake` 的可靠价值集中在搜索编排、合法 OA 解析、PDF 下载、RIS/BibTeX/Markdown/CSV、manifest、zip 和 Zotero 映射，应迁入 Radar 内部模块。根目录 `paper_workspace.py` 当前是临时编排层，不应演变为第三个业务项目。

当前不能直接把 Intake 的 `papers` 表覆盖进 Radar：两者语料边界不同，Radar 是严格凝聚态趋势语料，Intake 包含大量“dry electrode”等跨领域检索结果。可靠策略是先建立统一文献库和 paper/version 模型，再用 `condmat_view_eligible` 控制是否进入趋势统计。

## 项目入口与技术栈

| 项目 | 当前入口 | 技术栈 | 当前角色 |
| --- | --- | --- | --- |
| CondMat Radar API | `backend/api/main.py`，默认 `127.0.0.1:8000` | Python 3.11、FastAPI、Pydantic、SQLite | 主后端 |
| CondMat Radar UI | `frontend/src/main.tsx` / `App.tsx`，Vite 5173 | React 18、TypeScript、Vite、ECharts | 目标唯一 UI |
| Radar CLI/更新 | `backend/update_all.py`、`backend/ingest_*`、`scripts/update_daily.ps1` | Python、PowerShell | 摄取与趋势更新 |
| Paper Intake UI | `app.py` / `launcher.py`，Streamlit 8501 | Streamlit、Pandas、httpx、PyMuPDF | 待降级为旧 UI；能力迁入 Radar |
| Paper Intake CLI | `paper_intake/cli.py` | Python、SQLite | 迁移期数据库维护入口 |
| 根控制器 | `paper.cmd` / `paper_workspace.py` | Python/Batch | 临时启动、诊断和迁移编排，不是目标业务层 |

## 数据库位置与结构

### Radar 主库

- 当前生产路径：`G:\condmat-trend-radar\condmat_radar.sqlite`
- 大小：5,738,872,832 bytes（状态页约 5.34 GiB）
- 论文：185,103；真实 181,945；mock 3,158
- 时间范围：真实记录 2015-01 至 2026-07
- 关键表：
  - `papers` 185,103
  - `paper_terms` 47,068
  - `term_month_stats` 422,156
  - `monthly_corpus_stats` 2,087
  - `concept_lifecycle` 92
  - `published_preprint_stats` 12,788
  - `ingest_runs` 11，`ingest_chunks` 211，`update_runs` 8
  - `paper_chunks` 0
- 当前 `papers` 已有 DOI、OpenAlex、arXiv、`linked_published_paper_id`、OA、下载和 Zotero 状态字段，但仍把 canonical work 与 version 混在一行；现存 `linked_published_paper_id` 非空记录为 0。
- 当前无 `paper_versions`、作者关系表、文件哈希表、下载任务表、监控查询表、持久来源游标表、人工复核表和 FTS5 索引。
- 当前 `user_version=0`、journal mode 为 `delete`；完整只读检查已通过，`quick_check=ok`、外键错误 0。
- 另有本地或旧位置数据库：
  - `condmat-trend-radar/data/processed/condmat_trends.sqlite`，约 70 MB
  - `condmat-trend-radar/data/condmat_radar.sqlite`，约 242 KB
  - `G:\condmat-trend-radar\processed/condmat_trends.sqlite`，约 242 KB
  - `G:\condmat-trend-radar\data\processed/condmat_trends.sqlite`，约 152 KB
  这些不是当前配置命中的生产库，正式迁移不得自动覆盖，应登记为 legacy candidates。

### Intake 库

- 路径：`lab_paper_intake/data/papers.db`
- 大小：1,712,128 bytes
- schema version：3，WAL
- `papers` 76，`paper_observations` 211，`paper_aliases` 135，`paper_identities` 165
- `downloaded_pdfs` 29，`search_runs` 12，`llm_cache` 42
- 重复 DOI/arXiv/规范标题组均为 0；外键错误 0。
- 与 Radar 精确匹配：DOI 4、arXiv 0、标题-only 0；未匹配 72。
- 9 篇已下载论文中，2 篇可按 DOI 对应 Radar，7 篇需要作为 library-only 或人工复核记录导入。

## PDF 和历史导出

- 扫描位置：`lab_paper_intake/data/pdfs` 与 `lab_paper_intake/data/exports/**/pdfs`
- PDF 文件 29；唯一 SHA-256 为 9；重复物理副本 20；重复哈希组 8。
- `data/pdfs` 直接文件 5；旧 run folder 文件 24。
- 29 个文件均以 `%PDF-` 开头；未发现损坏头。
- 旧数据不得删除。统一库只在新 `data/library/pdf` 中为每个哈希保存一个 canonical copy，并保留所有 legacy path 引用。

## 下载流程与远程搜索

### Intake 可复用流程

- `pipeline.py`：自然语言规划 → OpenAlex/arXiv → Crossref fallback → 去重 → OA 解析 → 可选摘要/下载。
- `pdf_resolver.py`：优先 arXiv，其次已有 `pdf_url`、Unpaywall、OpenAlex OA location。
- `pdf_downloader.py`：httpx 流式下载、重定向、最小文件尺寸检查、状态登记、PyMuPDF 文本提取。
- 当前不足：没有持久任务队列、指数退避、MIME/页数/SHA-256 完整验证，也没有永久文件表。

### Radar 现有来源

- OpenAlex：正式摄取、现有记录补全、配额检测；支持 cursor 和 429/Retry-After 退避。
- Crossref：期刊 cursor 分页、恢复检查和 enrichment。
- arXiv：增量/quickstart 客户端与预印本来源。
- 手工 CSV/JSON 导入。
- DeepSeek：证据构建、review/ideas/skeleton 等后续 LLM 工作；配置输出只显示掩码。

不允许引入 Sci-Hub、付费墙绕过或凭据共享；统一下载器继续只接受合法 OA 来源。

## 旧热点材料与趋势历史

历史数据位于 Radar 主库，不应重新只算当前快照：

- `paper_terms`：材料 9,660 条、29 个规范材料、覆盖 6,618 篇论文；概念 28,354，方法 9,054。
- 代表材料包括 graphene、hBN、TMD、MoTe2、WSe2、MnBi2Te4、MoS2、NbSe2、ZrTe5 等。
- `term_month_stats`：422,156 条，覆盖 2015-01 至 2026-07，多 corpus scope/data mode。
- `published_preprint_stats`：12,788 条，92 个 term，已分离见刊热度、预印本增速和 validation gap。
- `concept_lifecycle`：92 条，含 material_system、physics_concept、method、platform_material；现有 half-life 仍可保留，但不再作为热点材料唯一解释指标。
- 未发现持久化用户收藏/监控表；这部分需要新建，不能假定已有状态。

## Zotero 与导出

可直接迁移并保持行为兼容：

- `export_package.py`：单次导出目录、manifest、lifecycle 摘要、zip。
- exporters：CSV、BibTeX、RIS、Markdown。
- `zotero/mapper.py`：文献与 note 映射。
- `zotero/local_client.py`：当前只可靠实现本地 Zotero 健康检查；直接创建 collection/item/note/attachment 的方法仍明确未实现，RIS 包是稳定主路径。
- 旧 run folder PDF 恢复逻辑位于 `export_package.py` 的本地路径解析和复制流程。

## 调度、游标、锁与恢复

- `scripts/update_daily.ps1/.sh` 只是调用 `backend.update_all`，没有证据表明仓库已安装 Windows Task Scheduler 任务。
- `backend/ingest/lock.py` 已有 PID 文件锁和 stale-lock 检测，但 `update_all.py` 当前没有使用它。
- OpenAlex/Crossref 客户端内部有 cursor/重试，数据库仅有 ingest checkpoint；缺少“只有成功才推进”的统一 `source_cursors`。
- `ingest/task_store.py` 把后台任务状态写 JSON，不是统一数据库任务队列。
- `update_all.py` 默认 `mock_if_empty=True`，且 `force_refresh` 会删除多张核心表；这不适合作为新的每日生产入口。

## 配置与密钥

- Radar 依次读取进程环境、项目 `.env.local/.env`、根 `.env`、`G:/condmat-trend-radar/secrets/.env.local`。
- Intake 读取 `config/.env`、项目 `.env`、根 `.env`。
- 根 `.env.example` 已覆盖 DeepSeek、OpenAlex、Unpaywall 和两项目路径。
- `config_check.py` 仅输出 `has_key` 和掩码，不输出完整密钥。
- 根目录仍有被忽略的旧 `deepseek_api.txt`；不能自动读取、迁移或删除。
- 仍缺少附件要求的 `config/example.yaml` 和统一启动配置校验。

## 测试现状

- Intake：65 项 pytest，覆盖导出、去重、PDF、Zotero 映射、任务回执、数据库迁移/备份恢复等。
- Radar：4 项 unittest，仅覆盖 Intake 状态聚合、数据库只读状态和事务回滚。
- Radar analytics 源码存在大量重复顶层函数定义，后定义会覆盖前定义：
  - `backend/db/database.py` 的 `paper_columns/_paper_payload/upsert_paper`
  - `analytics/stats.py` 多个核心统计函数
  - `analytics/lifecycle.py`
  - `analytics/cooccurrence.py`
  这是当前最大代码维护风险；迁移层应使用新模块和明确接口，不能继续向这些文件追加实现。

## 可复用模块

- Radar：API/React 框架、OpenAlex/Crossref/arXiv 客户端、严格凝聚态过滤、材料词典与抽取、时间序列、见刊/预印本统计、锁、现有 ingest 日志。
- Intake：OA resolver、流式 PDF 下载、PDF 文本提取、批内去重/规范化、RIS/BibTeX/Markdown/CSV、manifest/zip、Zotero 映射、旧 run folder 恢复、数据库备份恢复模式。

## 冲突与迁移风险

1. 两个 `papers` 都是平面模型，但字段语义和语料边界不同；必须先引入 `paper_versions`。
2. 72/76 Intake 记录在 Radar 无可靠匹配，其中有明显非凝聚态记录；不可自动加入热点统计。
3. 29 个 PDF 只有 9 个唯一哈希；不能按文件名去重，也不能删除旧副本。
4. Radar 5.34 GiB 主库正在由旧 API 进程读取；任何正式 schema 写入前必须备份、停止写入者并在副本演练。
5. `linked_published_paper_id` 当前为空，不能声称已有预印本/见刊关联。
6. 当前无 FTS5、来源统一游标、download queue、manual review 和 migration ledger。
7. Radar 核心模块重复定义且测试不足，直接大重写风险高。
8. 根控制器与两个 UI 形成临时三层结构，不符合最终单主程序目标。

## 审计判定

当前状态是可迁移但尚未完成统一：旧数据完整、两库健康、可复用下载/导出逻辑明确；下一步应执行纯新增 schema、dry-run 迁移和副本验证，然后才对生产 Radar 库正式迁移。
