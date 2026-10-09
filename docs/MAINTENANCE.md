# 维护与演进规则

## 组件职责

### Condensed Matter Trend Radar

负责元数据摄取、语料范围、术语抽取、趋势统计、论文排序和下载任务生成。它不拥有 PDF 下载、Zotero 导出或用户论文库。

### Lab Paper Intake

负责自然语言检索、雷达任务导入、去重、开放获取地址解析、合法 PDF 下载、人工选择和引用包导出。它不重复实现趋势统计。

### Workspace Controller

`paper_workspace.py` 只编排两个子系统，不复制业务逻辑。新增业务功能应进入对应子项目，控制器只暴露稳定命令。

## 稳定集成协议

当前任务协议为 `trend-radar-download-task-v1`。至少保留：

- `task_id`
- `title`
- `doi` / `arxiv_id`
- `publication_date`
- `url` / `pdf_url`
- `concepts` / `materials` / `methods`
- `momentum`
- `download_priority`
- `reason`
- `status`

入库端对相同 `task_id` 幂等写入；没有 task ID 时使用 DOI、arXiv ID 或规范化标题生成稳定 ID。协议升级必须新增版本，并保留至少一个旧版本读取器。

导入回执协议为 `paper-intake-task-receipt-v1`。回执独立存放在任务目录 `.receipts/`，包含源文件 SHA-256、导入时间、数据库路径、论文 ID 和计数。写入采用同目录临时文件加原子替换，不修改雷达生成的任务文件。

逐论文生命周期协议为 `paper-intake-task-event-v1`，采用 `.receipts/events/` 下的独立原子 JSON 事件。`oa_resolved`、`downloaded`、`exported` 事件是追加式历史；队列状态按 `task_id` 去重聚合，导出 manifest 只保存计数和错误摘要。

## Intake 数据库契约

- `PRAGMA user_version` 与 `schema_migrations` 是 schema 的共同版本证据；当前版本为 3。
- `papers` 只保存 canonical paper；DOI、arXiv ID 和无外部 ID 冲突的规范标题由 `paper_identities` 唯一映射。
- 每次来源观测写入 `paper_observations`；被合并的旧 ID 写入 `paper_aliases`，不得静默丢弃来源记录。
- `downloaded_pdfs.paper_id` 具有外键，导出或下载流程必须先持久化 paper，再写下载账本。
- 生产迁移必须先运行 `db-backup`，并在副本上完成迁移/恢复演练；不得直接重写 Radar 的大型数据库。
- Radar 使用 `backend.db.status` 提供 light/full 两级只读检查；根控制器聚合两套库，但写入维护命令仍只归 Intake 所有。
- `db-status` 是只读检查；`db-restore` 必须显式 `--confirm`，并通过 checkpoint、锁检查、临时恢复和原子替换。

## 修改后的质量门

每次涉及源码的修改至少运行：

```powershell
.\paper.cmd test
```

如修改真实数据摄取，还应运行小规模、明确限额的网络试验；不得把 mock 结果写成真实科学结论。如修改 PDF 下载，必须验证只使用合法开放获取 URL，并保留失败状态。

## 数据与源码隔离

- 数据库、PDF、任务文件、日志、缓存和发布包不是源码。
- 不覆盖或删除用户数据库、PDF 和历史导出包。
- 大型数据优先通过环境变量放到外部数据盘。
- 配置文件只存本机值，仓库只维护 `.env.example`。
- 日志和状态输出不得打印完整 API key。

## 当前优化路线

### P0：仓库基线与密钥治理

- 把确认后的源码建立首次 Git 提交。
- 将 `deepseek_api.txt` 中仍需使用的值迁移到 `.env` 后手动删除。
- 发布包与运行数据使用独立归档位置，避免仓库继续膨胀。

### P1：任务状态闭环

- 已完成 `imported` 回执及导出时 `oa_resolved`、`downloaded`、`exported` 追加事件。
- 已由 Radar 的只读 `/api/download-center/status` 和下载中心显示批次、入库原因与生命周期进度。
- 继续保持追加式事件和单一写入方，避免两个应用同时改写同一个 JSON 文件。

### P1：长任务后台化

- 把真实元数据摄取、批量摘要和 PDF 下载放入可恢复队列。
- UI 仅提交任务并轮询状态，进程重启后可续跑。
- 对外部 API 增加统一限速、退避、缓存命中率和配额监控。

### P2：统一论文身份层

- 已完成 Intake canonical identity、provenance observation、旧 ID alias、版本化迁移与 schema 检查。
- 已完成备份/恢复命令、SHA-256 manifest、WAL、busy timeout、外键和事务回滚。
- 下一步把相同身份规范抽成共享包供 Radar 读取，并评估全文检索索引；不直接重写 Radar 的大型数据库。

### P2：发布自动化

- `status` / `doctor` 已能识别 Windows EXE 是否落后于当前源码，并区分服务停止与端口冲突。
- 由干净源码构建 Windows 发布包。
- 自动生成 manifest、版本号、SHA-256 和最小启动验证。
- 发布包文档与源码文档分开，防止旧 EXE 被误认为当前源码。
