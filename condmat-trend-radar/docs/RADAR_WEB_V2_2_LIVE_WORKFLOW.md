# Radar Web v2.2：实时研究工作流

日期：2026-07-15

## 本轮结论

v2.2 把原先互相分离的“扫描、摘要、材料、下载、Zotero”改成同一条可观察工作流：

1. arXiv cond-mat 快速增量扫描；
2. 仅同步本轮新增或更新的版本与 FTS；
3. 从真实论文标题和摘要抽取材料并保存证据；
4. 滚动补全缺失摘要；
5. 立即下载合法开放 PDF；
6. 生成带本地 PDF `file:///` 链接和附件副本的 Zotero 包。

## 用户反馈对应修复

### 下载不再只是入队

- 单篇主操作为“立即下载 PDF + Zotero”。
- API 同步返回最终状态：`completed`、`already_available` 或明确失败原因。
- 文献库批量操作最多逐篇立即处理 5 篇，之后生成一个合并 Zotero 包。
- 旧队列仍保留为“处理遗留队列”，不再是主入口。

真实验收：arXiv `2607.08710v1` 首次下载成功，PDF 头为 `%PDF-`；RIS 含 `file:///G:/...pdf`；ZIP 完整性检查通过且包含 `pdf/001_...pdf`。

### 摘要补全

- 优先使用本地已有版本；随后依次尝试 arXiv、Crossref、OpenAlex、Semantic Scholar。
- 详情页缺摘要时提供“补全摘要与材料”。
- 实时扫描默认滚动尝试 3 篇，单个来源失败不会阻断扫描。
- 成功后同步更新 `papers`、`paper_versions`、FTS 和材料证据。

真实验收：`doi:10.1103/mqr4-wnny` 从 0 补到 1021 字符，来源 `semantic_scholar`，FTS 同步为 1021 字符；最近自动扫描也成功补全 1/3 篇。

当前限制：Semantic Scholar 无密钥公共额度会偶发 429；可在本地秘密配置中设置 `SEMANTIC_SCHOLAR_API_KEY`。部分出版社记录本身没有摘要，系统会如实保留“摘要未提供”。

### 实时扫描真正增量化

- 自动和首页手动实时扫描只走 arXiv cond-mat 快速通道。
- 完整 OpenAlex 核心期刊刷新移到系统页，避免阻塞实时通道。
- 版本同步使用 `updated_at` 复合索引，仅处理本轮记录。
- FTS 只刷新本轮版本 ID。
- 材料只处理本轮更新论文。
- 预印本—见刊全库匹配延后到完整刷新。
- 页面按运行 ID 轮询并显示阶段和百分比。

真实基准：无摘要回填时 4.15 秒完成；默认补 3 篇摘要时 9.17 秒；最近含 3 个遗留下载任务的自动扫描约 29 秒。旧全量流程记录为约 9 分 44 秒。

### 材料雷达来自论文

- 化学式识别不再依赖固定材料清单；元素合法性校验支持动态材料实体。
- 固定注册表只用于名称归一化和材料家族元数据。
- 每条新证据保存论文、字段、片段、检测器和置信度。
- 支持 MathML、LaTeX、HTML 和空格下标，如 `SrTiO 3` → `SrTiO3`。
- 重扫会替换旧自动证据，避免错误永久累积。
- 综合视图要求至少 2 篇论文证据；突发视图保留单篇新材料；页面最多渲染 300 行。

真实库当前：1652 个有自动论文证据的材料，其中 488 个至少有 2 篇论文证据；18620 条论文—材料链接。浏览器验证 graphene 详情能显示证据来源、98% 置信度、论文标题和片段。

### PDF 与 Zotero 联动

每个联动包包含：

- `selected_papers.ris`（含 `L1  - file:///...pdf`）；
- `selected_papers.bib`（含本地文件 URI）；
- `selected_papers.csv`；
- `selected_summary.md`；
- `manifest.json`；
- `pdf/` 下的可用附件；
- `zotero_package.zip`。

页面生成后持续显示 RIS 和完整 ZIP 下载链接，不会因详情刷新而消失。

## 数据库与性能索引

新增或确认的索引：

- `idx_papers_updated_at`；
- `idx_papers_mode_updated`；
- `idx_paper_materials_material`。

Schema 标记：`radar_live_workflow_v3`。当前生产库为 `G:\condmat-trend-radar\condmat_radar.sqlite`。

## 验证

- Python `unittest`：22/22 通过。
- Python `compileall`：通过。
- TypeScript `tsc --noEmit`：通过。
- ESLint：通过，0 warnings。
- Vite production build：通过，49 modules transformed。
- 浏览器：API 在线；自动扫描完成；材料页 300/488；动态材料 1652；论文摘要可见；PDF + Zotero 显示 1 篇 / 1 个 PDF 附件及两个可下载链接。