# 多源论文覆盖与身份归一口径

## 目标与可验证边界

Radar 同时保留 OpenAlex、Crossref 与 arXiv 的来源版本，并用 DOI、base arXiv ID、OpenAlex ID 进行确定性归一。没有外部 ID 时，仅在标题、第一作者和年份同时满足严格规则时自动合并；标题相似但证据不足或 DOI 冲突时进入 `identity_match` 人工复核。

任何公开元数据源都不能代表“全网全部论文”，因此系统不再声称绝对不重不漏。可验证目标是：

### 发现范围与分析资格是两层

- OpenAlex 以 `topics.subfield.id:3104` 建立按日期、cursor 全分页的凝聚态全领域**候选流**，覆盖 article、preprint、review。每条返回记录均保留为来源版本，即使证据暂时不足，也不会在采集层被静默删除。
- 候选只有通过本地文本证据，或满足 OpenAlex 主题强度门槛（主 3104 主题分数至少 0.35，或任一 3104 主题分数至少 0.45），才成为 `condmat_view_eligible=1` 的严格分析样本。其余记录保留为 `openalex-field-candidate-unverified`，可复核但不污染热点分母。
- 严格分析样本还必须通过来源质量门：`primary_location.source.type=repository` 的通用仓储记录默认不进入热点分母；arXiv 可继续按上述科学证据门判断，正规 journal/proceedings location 或明确的正式 Crossref DOI 版本可保留。Zenodo、Figshare、OSF 等仓储记录仍完整保留在 `papers`/`paper_versions` 中用于召回、下载和复核。
- 每次真实采集和严格统计刷新都会执行可重复的历史重分类；它只下调“仅有通用 repository 证据”的 canonical 资格，不删除任何来源版本。可先运行 `python -m backend.db.reclassify_openalex_quality` 查看 dry-run，再用 `--apply` 持久化。
- arXiv 的 9 个 `cond-mat.*` 分类提供独立预印本观测。Crossref 则对配置的 18 本精选期刊做 cursor 全分页和正式出版元数据交叉校验（core 范围可只扫描其中子集）。**18 刊不是全凝聚态期刊列表，Crossref 该流不能单独证明全领域覆盖，也不能作为热点分析的全领域分母。**
- 宽收集解决召回审计，严格资格控制分析精度；两层数量、拒绝原因和分页完成状态必须分别报告。

在这个边界内，可验证目标是：

- 每个来源的原始版本独立保存在 `paper_versions`；
- 相同 DOI/base arXiv ID 只对应一个 canonical paper；
- 统计跨源覆盖率、稳定身份率、来源两两交集和 Jaccard；
- 对带 DOI/arXiv ID 且缺少可核验来源记录的论文建立幂等回补队列；
- 回补结果必须通过请求标识一致性校验，身份冲突不得静默合并。

## 主要指标

- `canonical_count`：当前口径下的 canonical 论文数；
- `cross_source_count/rate`：至少被两个受跟踪来源独立观察到的论文数/比例；
- `stable_identity_count/rate`：具有 DOI、base arXiv ID 或 OpenAlex ID 的论文数/比例；
- `source_pair_overlap`：OpenAlex/Crossref/arXiv 两两交集、并集与 Jaccard；
- `missing_source_gaps`：有精确查询标识、但缺少对应来源观察的数量；
- `queue_status`：回补队列的 queued/completed/not_found/retryable_failed/identity_conflict 状态。

这些值是覆盖下界。`not_found` 可能代表来源范围不包含该论文，而不是本地漏抓。

## 使用

只读审计（默认不恢复历史来源版本、不写审计表、不联网）：

```powershell
.\.venv\Scripts\python.exe scripts\audit_source_coverage.py --scope eligible
```

审计、排队，并最多执行 50 次精确标识回补：

```powershell
.\.venv\Scripts\python.exe scripts\audit_source_coverage.py --scope eligible --apply --queue-missing --backfill-limit 50
```

API：

- `GET /api/library/coverage?refresh=true`（只读计算；不会在 GET 中恢复 18 万条历史版本）
- `POST /api/library/coverage/backfill`，默认 `apply=false`；显式传入 `apply=true` 才会写队列并访问外部来源。

日常扫描只处理本次时间窗口的缺口，实时模式每轮最多回补 12 条；全量模式每轮最多回补 100 条。失败项保留在队列中，后续可恢复执行。