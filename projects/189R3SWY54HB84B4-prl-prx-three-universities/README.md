# 三校 PRL/PRX 参与与通讯单位逐年统计（2016—2026）

**XID-16: `189R3SWY54HB84B4`** · 独立科研统计项目 · **数据状态：NOT_COLLECTED**

## 当前状态

**本仓库的 66 行表格是空审计模板，不是论文数量统计成果。** 原始 2016—2025 年 10 × 3 × 2 = **60 行**全部为空；补上 2026 年 3 × 2 = **6 行**，总计 **66 行**。每行的 `participation_count`、`corresponding_count` 与 `unknown_corr_count` 目前均留空；空值不能当作 0。完成了代码和 GitHub 项目登记 **不等于完成了数据采集**。

统计单位是每年 × 学校 × 期刊的一个组合：11 年 × 3 校 × 2 刊 = 66 行，参与和通讯是每行的两个核心数值（合计 132 个核心数值）。

2026 年归类为未完结年度，首次统计截止日 **2026-11-06**（含），标识 `YTD_PROVISIONAL`。2016—2025 是完整自然年度，不能把 2026 年截点值与往年全年值直接作同比。

## 统计口径（严格）

- **学校**：南方科技大学（ROR `049tv2d57`）、东京大学（`057zh3y96`）、京都大学（`02kpeqv85`）。
- **期刊**：仅 Physical Review Letters（PRL，ISSN `0031-9007` / `1079-7114`）和 Physical Review X（PRX，`2160-3308`）；排除 PRX Quantum 等其他独立刊物。
- **参与**：论文正式作者单位包含该校，按同篇同校一次去重。
- **通讯**：APS 文章中标记通讯作者且其正式所属单位包含该校，按同篇同校一次去重；共同通讯跨校可以双重计入。
- **未知**：通讯作者标识缺失、作者-单位对应不明，记为 `UNKNOWN`，不得当作“非通讯”或 0。
- **年份**：以 APS 正式出版日期为准；OpenAlex 仅用于候选 DOI 检索。勘误、撤稿及非研究性文种单独排除并记理由。

## 运行候选数据采集

```bash
python -m pip install requests
export OPENALEX_API_KEY=YOUR_VERIFIED_FREE_KEY
export APS_STATS_AS_OF=2026-11-06
python scripts/prl_prx_three_universities_2016_2026.py
```

OpenAlex 在正式跨页检索时需要有效 API Key，且每页最多 100 条。脚本只提供候选统计，**绝不能把 OpenAlex 的通讯标记直接宣称为经过 APS 原文核实**。

## 交付与审计门禁

- `data/annual_audit_template_2016_2026.csv`：66 行待填、未采集的年度审计表。
- `scripts/prl_prx_three_universities_2016_2026.py`：OpenAlex 候选 DOI 抓取、初步分类与质量说明。
- `AGENT_EXECUTION.md`：待执行 Agent 任务、质量标准及真实数据交付要求。
- 必须产出逐篇 DOI、正式出版日期、作者—机构关系、通讯作者、APS 原文来源与核验日期，再由经核验明细汇总至 66 行年度表。
- 比较南科大优势时只采用实证支持的指标；需完整报告不支持优势的结果。

**现阶段不存在可用于正式汇报的 2016—2026 逐年参与/通讯篇数。**

历史：2026-11-06 初次登记 2016—2025；同日追加 2026 YTD，保留同一个 XID-16。

官方参考：[APS](https://journals.aps.org/) · [OpenAlex API Authentication](https://help.openalex.org/api/authentication/) · [OpenAlex corresponding-author limitations](https://blog.openalex.org/a-big-improvement-to-our-corresponding-author-data/)
