# Lab Paper Intake / 全自动论文摄取

Lab Paper Intake 是本地优先的论文筛选与交付应用。它可以：

- 用自然语言规划学术检索；
- 查询 OpenAlex、arXiv 和 Crossref 的真实元数据；
- 从 CondMat Trend Radar 导入 CSV/JSON 候选任务；
- 规范化 DOI、arXiv ID 和标题并去重；
- 只解析和下载合法开放获取 PDF；
- 选择论文并导出 CSV、BibTeX、RIS、Markdown 和 Zotero 导入包。

## 启动

建议从工作区根目录统一启动：

```powershell
.\paper.cmd start --only intake
```

也可以在本目录单独启动：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

默认地址是 <http://127.0.0.1:8501>。

## 导入趋势雷达任务

网页侧栏会自动显示最新雷达任务。点击“载入最新雷达任务”即可加入论文库。

侧栏“本地论文库”会显示已保存数量。点击“载入全部”可在刷新或重启后恢复论文列表；“清空当前视图”不会删除 `data/papers.db` 中的任何记录。

命令行方式：

```powershell
.\.venv\Scripts\python.exe -m paper_intake.cli import-tasks
.\.venv\Scripts\python.exe -m paper_intake.cli import-tasks "D:\path\download_tasks.json"
```

导入是幂等的：同一个 `task_id` 不会重复创建论文。系统同时兼容当前子项目任务箱和旧版根目录任务箱。

成功导入后会在任务目录的 `.receipts/` 中写入不可变源文件的 SHA-256 回执。生成引用包时会为雷达任务追加 OA 已解析、PDF 已下载和引用包已导出事件。可以随时查看队列状态：

```powershell
.\.venv\Scripts\python.exe -m paper_intake.cli queue-status
```

## 数据库管理

```powershell
.\.venv\Scripts\python.exe -m paper_intake.cli db-status
.\.venv\Scripts\python.exe -m paper_intake.cli db-backup
.\.venv\Scripts\python.exe -m paper_intake.cli db-migrate
.\.venv\Scripts\python.exe -m paper_intake.cli db-restore "D:\path\papers_backup.sqlite" --confirm
```

数据库会跨搜索批次合并 DOI、arXiv ID 或无冲突规范标题相同的论文，同时保留每次来源 observation 和旧 ID alias。迁移默认先备份；备份附带 SHA-256 manifest，并在写入后验证 SQLite 完整性。
## 配置

程序依次读取：

1. 进程环境变量；
2. `config/.env`；
3. 本项目 `.env`；
4. 工作区根目录 `.env`。

支持的主要配置：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=
UNPAYWALL_EMAIL=your_email@example.com

PAPER_INTAKE_DATA_DIR=lab_paper_intake\data
PAPER_INTAKE_INBOX=lab_paper_intake\data\inbox\trend_radar_download_tasks
PAPER_INTAKE_REQUEST_TIMEOUT_SECONDS=30
```

DeepSeek 是可选项。缺少 key 或模型时，应用使用无 LLM 检索规划和空摘要，不会伪造论文事实。

## 数据目录

- `data/papers.db`：本地论文库；
- `data/inbox/trend_radar_download_tasks/`：雷达任务；
- `data/pdfs/`：已下载合法 OA PDF；
- `data/exports/`：引用和 Zotero 导出包。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 合法 PDF 策略

只使用 arXiv、Unpaywall、OpenAlex 或论文元数据明确提供的开放获取地址。不实现 Sci-Hub、付费墙绕过、凭据共享或 Google Scholar 抓取。
