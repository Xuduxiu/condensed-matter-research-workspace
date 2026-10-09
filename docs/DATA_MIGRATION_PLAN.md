# Data Migration Plan

## 目标与边界

统一数据库由 CondMat Radar 拥有。新安装默认可使用 `condmat-trend-radar/data/radar.db`；当前生产继续通过 `CONDMAT_RADAR_DB` 指向 `G:\condmat-trend-radar\condmat_radar.sqlite`，不为了路径美观复制或移动 5.34 GiB 主库。

`lab_paper_intake/data/papers.db`、所有旧 PDF、旧 run folder、Radar 旧数据库候选和趋势表永远作为只读 legacy source 保留。迁移只新增目标记录，不删除源数据。

## 统一实体

目标 schema 至少包括：

- `papers`：canonical research work；保留现有 Radar ID 兼容层。
- `paper_versions`：arXiv 修订版、preprint、published、repository/imported 版本。
- `authors`、`paper_authors`。
- `topics`、`paper_topics`。
- `materials`、`material_aliases`、`paper_materials`。
- `paper_files`、`paper_file_sources`。
- `download_tasks`、`download_attempts`。
- `monitor_queries`。
- `source_cursors`。
- `daily_runs`、`daily_run_steps`。
- `analysis_results`。
- `migration_records`。
- `paper_merge_events`。
- `manual_review_items`。
- `library_fts`（FTS5，索引 canonical/version 标题、摘要和已提取 PDF 文本）。

现有 `paper_terms`、`term_month_stats`、`monthly_corpus_stats`、`concept_lifecycle` 和 `published_preprint_stats` 原样保留并继续作为趋势历史来源。

## 身份和版本规则

自动关联优先级：

1. DOI 完全一致：置信度 1.00。
2. 去版本号后的 arXiv ID 完全一致：0.99；arXiv version 单独写入 version。
3. arXiv `journal_ref` 或 DOI 明确指向正式版：0.98。
4. 标准化标题高度相似且外部 ID 不冲突：候选，不直接低置信合并。
5. 标题 + 第一作者 + 作者集合 + 年份联合：达到阈值才自动关联。
6. 其他情况写入 `manual_review_items`，保留原始 payload。

每次自动合并写 `paper_merge_events`：rule、confidence、source/target、时间、reversible、原始 payload/hash。不得只凭标题相似自动合并低置信记录。

## 分阶段迁移

### Phase 0：冻结证据与备份

- 两库执行只读 `quick_check`、外键检查和统计快照。
- 使用 SQLite backup API 备份生产 Radar 和 Intake；5.34 GiB Radar 备份需要足够磁盘空间，因此正式执行前必须确认目标盘容量。
- 记录源文件大小、mtime、SHA-256（大库可记录分块 hash/backup hash）。
- 检查 API/日更是否有写入者；不能在活动写入期间切换。

### Phase 1：纯新增 schema

- 在数据库副本运行版本化 migration。
- 只创建新表、索引和 FTS，不重写现有趋势表。
- `migration_records` 以 `(migration_name, source_fingerprint, source_record_id)` 唯一，保证重复执行幂等。

### Phase 2：映射现有 Radar 论文

- 为 185,103 个现有 `papers` 生成至少一个 `paper_versions`。
- DOI 行映射为 published；仅 arXiv 行映射为 preprint/arxiv；其他映射为 metadata version。
- 解析 `authorships_json` 到作者关系表；无法解析时保留 JSON 并写 review。
- 从 `paper_terms` 将 `term_type=material` 映射到 materials，concept 映射 topics。
- 保留所有原 `papers.id`，不改变旧 API 查询结果。

### Phase 3：导入 Intake 元数据

- 先按 DOI/arXiv 精确匹配；当前预计 4 条 DOI 匹配。
- 其余 72 条创建 library-only canonical/version 或 review item，默认 `condmat_view_eligible=0`，不进入趋势统计。
- 导入 211 条 observation 和 135 个 alias 为迁移 provenance，不覆盖 Radar 更可靠字段。
- 旧 search runs 和 LLM cache 只登记迁移记录；缓存不直接混入 Radar LLM cache，避免 provider/schema 冲突。

### Phase 4：统一 PDF 文献库

目标目录：

```text
data/library/
├── pdf/
├── supplements/
├── metadata/
└── thumbnails/
```

- 扫描 29 个 PDF，校验 `%PDF-`、MIME、size、SHA-256、页数。
- 当前预期：9 个唯一文件、20 个重复副本、0 个坏 PDF。
- 目标库按哈希只复制一个 physical canonical file；旧 29 个文件全部保留原位。
- 文件名优先 DOI，其次 `arxiv_id_version`，否则 version ID；实际唯一约束是 SHA-256。
- `paper_file_sources` 保存每个 legacy path、run id、source URL 和迁移时间。
- 7 个已下载但无法与 Radar 精确匹配的 Intake paper 不丢弃，进入 library-only 或 manual review。

### Phase 5：材料与趋势历史

- 原 `paper_terms/term_month_stats/published_preprint_stats/concept_lifecycle` 不搬空、不重算覆盖。
- 建立 `materials/material_aliases/paper_materials` 作为规范关系层，先从 29 个现有材料项播种。
- 规范化 chemical display alias（如 NbSe2、NbSe₂、niobium diselenide），同时把 phase/thickness/composition 作为独立维度；不把 1T、2H、monolayer、bulk 静默折叠。
- 新热点指标在旧历史上增量计算；half-life 保留为 legacy 指标，不再作为唯一排序。

### Phase 6：FTS 与搜索

- 建立 FTS5 索引 canonical/version 标题、摘要和 `paper_chunks` 文本。
- 搜索顺序：本地 metadata/FTS → OpenAlex/arXiv/Crossref → 统一归并。
- 结果状态从统一库计算，不从前端猜测。

### Phase 7：日更与切换

- 新 `scheduler/daily_update.py` 使用单实例锁、`daily_runs`、step checkpoint 和 `source_cursors`。
- 游标仅在对应 source step 完整成功后推进；失败记录 error/next retry，不推进。
- `--dry-run` 不写业务表，不调用下载；手动“立即更新”调用同一 service。
- Radar API/React 接入 library/downloader/scheduler；Streamlit 不再作为正常启动目标。

## 幂等与回滚

- 每个迁移脚本必须支持 `--dry-run --verbose --log-file`，失败返回非零。
- 所有写入使用事务；每批有 migration record 和 source fingerprint。
- 文件复制使用同目录临时文件、hash 校验和原子替换。
- 目标 schema 是 additive；回滚先恢复数据库备份，或按 migration batch 删除新关系记录。源库和源文件不受影响。
- 冲突只进入 `manual_review_items`，禁止“取一个看起来更好”的静默覆盖。

## 脚本与职责

- `scripts/audit_legacy_projects.py`：只读统计和风险报告。
- `scripts/migrate_legacy_databases.py`：schema + Radar/Intake metadata 迁移。
- `scripts/import_existing_pdfs.py`：hash、校验、copy、source refs。
- `scripts/rebuild_search_index.py`：FTS 重建。
- `scripts/link_preprints_and_publications.py`：版本关联和 review queue。
- `scripts/run_daily_update.py`：统一日更入口。
- `scripts/verify_migration.py`：迁移前后计数、完整性、抽样和幂等复跑。

## 当前 dry-run 预期基线

| 指标 | 预期 |
| --- | ---: |
| Radar legacy papers | 185,103 |
| Intake canonical papers | 76 |
| DOI exact overlap | 4 |
| Intake unmatched | 72 |
| Intake downloaded canonical papers | 9 |
| PDF files scanned | 29 |
| Unique PDF hashes | 9 |
| Duplicate PDF copies | 20 |
| Material terms | 29 |
| Trend history records | 422,156 |
| Published/preprint records | 12,788 |

正式迁移结果必须由脚本重新计算，不得把这些审计数硬编码为成功条件。

## 正式迁移门

只有以下条件同时满足才写生产库：副本迁移通过、幂等复跑零新增、回滚演练通过、磁盘空间足够、旧 API 没有活动写事务、两库备份完成、29 个 PDF 都有 hash/validation 结果。任何一项不满足时保持 PARTIAL，不删除旧数据。