# Condensed Matter Research Workspace

本地优先的凝聚态论文研究工作区：发现论文、分析趋势、管理全文、下载合法可获取的 PDF，并导出到 Zotero。

主应用 **Condensed Matter Radar** 使用 FastAPI + React + TypeScript + SQLite，在同一界面中提供论文发现、材料与主题统计、版本关联、下载队列、监控条件、全文检索和人工复核。`lab_paper_intake` 保留为旧版工具、迁移来源与回归对照。

## 功能

- 从 OpenAlex、Crossref、arXiv 获取真实论文元数据。
- 分析概念、材料、方法、期刊及预印本/正式发表趋势。
- 按 DOI、arXiv ID 关联版本，模糊匹配进入人工复核。
- 使用 SQLite FTS5 检索本地资料库，按 SHA-256 去重 PDF。
- 持久化下载队列、失败重试、每日增量更新和监控条件。
- 导出 CSV、BibTeX、Markdown、Zotero 兼容 RIS 与附件包。
- 支持中英文界面；可选接入 DeepSeek 辅助分析。

## Windows 源码安装

需要 **Python 3.11+、Node.js 22、Git 和 pnpm 10**。以下命令在 PowerShell 中执行；无需激活虚拟环境。

```powershell
git clone https://github.com/Xuduxiu/condensed-matter-research-workspace.git
cd condensed-matter-research-workspace

py -3.11 -m venv condmat-trend-radar/.venv
.\condmat-trend-radar\.venv\Scripts\python.exe -m pip install -r condmat-trend-radar/requirements.txt

npm.cmd install --global pnpm@10
pnpm.cmd --dir condmat-trend-radar/frontend install --frozen-lockfile

Copy-Item .env.example .env
# 按需编辑 .env，填写自己的邮箱和 API Key。
.\paper.cmd start
```

如果安装的是其他 Python 3.11 以上版本，可将 `py -3.11` 替换为对应的解释器。已有 `.env` 时请保留并按需补充配置，不要覆盖真实配置。

- 应用：<http://127.0.0.1:5173>
- API：<http://127.0.0.1:8000>
- API 文档：<http://127.0.0.1:8000/docs>

`paper.cmd start` 默认启动 Radar API 与前端。后端入口是 `python -m backend.api.main`，包含应用的扫描调度逻辑。新安装从空资料库开始，通过界面获取真实数据。

## 配置与数据

根目录 [.env.example](.env.example) 提供源码开发配置；复制为 `.env` 后填写本机参数。Crossref 与 arXiv 的元数据入口无需 API Key；OpenAlex、Unpaywall 和可选 AI 服务按各自配置使用。

示例配置将源码模式的数据保存在 `condmat-trend-radar/data/`，可通过 `CONDMAT_RADAR_DATA_DIR`、`CONDMAT_RADAR_DB` 等变量指定其他磁盘。数据库、论文 PDF、导出文件、日志、真实 `.env` 和 API Key 均不纳入版本控制。历史文档中的 `G:` 路径是原部署记录，不是新安装要求。

只使用合法开放获取来源，或用户已获授权的机构 IP 访问；不提供付费墙绕过、浏览器 Cookie 导入或 SSO 自动登录。机构授权附件保留访问来源与再分发限制标记。

## 常用命令

在仓库根目录运行：

```powershell
.\paper.cmd start
.\paper.cmd status
.\paper.cmd doctor
```

需要旧版 Intake 时，单独安装后显式启动：

```powershell
py -3.11 -m venv lab_paper_intake/.venv
.\lab_paper_intake\.venv\Scripts\python.exe -m pip install -r lab_paper_intake/requirements.txt
.\paper.cmd start --only intake
```

旧数据迁移、导入及版本关联脚本见 [主应用说明](condmat-trend-radar/README.md)。迁移脚本默认 dry-run，正式写入需显式 `--apply` 并保留备份。

## 验证

```powershell
Push-Location condmat-trend-radar
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Pop-Location

pnpm.cmd --dir condmat-trend-radar/frontend run typecheck
pnpm.cmd --dir condmat-trend-radar/frontend run lint
pnpm.cmd --dir condmat-trend-radar/frontend run build

# 安装旧版 Intake 依赖后：
Push-Location lab_paper_intake
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
```

GitHub Actions 执行两个 Python 测试集与前端检查。单元测试和构建结果不代表外部数据源的实时可用性；联网配额、机构授权和真实下载需在目标环境验证。

## 项目结构与文档

| 路径 | 用途 |
| --- | --- |
| `paper.cmd` / `paper.ps1` / `paper_workspace.py` | Windows 统一启动与诊断入口 |
| `condmat-trend-radar/backend/` | API、检索、趋势统计、下载和调度 |
| `condmat-trend-radar/frontend/` | React 界面 |
| `condmat-trend-radar/scripts/` | 迁移、导入与维护工具 |
| `lab_paper_intake/` | 旧版 Intake 与兼容性测试 |
| `docs/` | 架构、迁移和维护记录 |

- [主应用与 API](condmat-trend-radar/README.md)
- [维护说明](docs/MAINTENANCE.md)
- [目标架构](docs/TARGET_ARCHITECTURE.md)
- [数据迁移方案](docs/DATA_MIGRATION_PLAN.md)
- [Windows 打包与安装](condmat-trend-radar/docs/WINDOWS_INSTALL_HANDOFF.md)
- [发布验证记录](docs/PUBLICATION_CHECKS.md)

既有审计与验收报告保留为历史记录，其中的数据量、时间与测试计数以各报告所述场景为准。
