# Condensed Matter Research Workspace

本地优先的凝聚态论文研究工作区：发现论文、分析趋势、管理全文、下载合法可获取的 PDF，并导出到 Zotero。

主应用 **Condensed Matter Radar** 使用 FastAPI + React + TypeScript + SQLite，在同一界面中提供论文发现、材料与主题统计、版本关联、下载队列、监控条件、全文检索和人工复核。`lab_paper_intake` 保留为旧版工具、迁移来源与回归对照。

## 在 DeepSeek Harness 中使用

在 DSH 顶栏“编辑”右侧打开“文献”，**凝态文献台（CondMat Paper Desk）** 会铺满标题栏下方的窗口。工作台、发现、文献库、材料、监控和系统共用一个导航栏。点击标题栏的“返回 DSH”，即可恢复进入前的会话和侧栏布局。检索、PDF 下载和 Zotero 导出共用论文主应用的 SQLite 资料库和队列。项目 XID-16 为 `TH7DP0X71X9BTRRT`，可在“系统”页查看。

文献台沿用 DSH「外观」里的主题。当前视觉主题插件提供**罗德岛 · 白昼、罗德岛 · 黑夜、终末地 · 协议玻璃、XLab · 实验室**。在文献台右上角点「外观」即可使用同一个宿主选择器，已打开的页面和统计图会立即换色。启用“跟随系统”时，文献台跟随 DSH 解析后的深浅色。在浏览器独立运行时使用文献台自己的配色。[四套主题桌面实拍](docs/dsh-paper-desk-themes.png)

布局按窗口里实际可用的宽度调整。超宽屏上，发现和文献库把筛选放在左侧，论文卡片和统计图自动增列。表格空间充足时，当前页按原顺序分为并排的表，每栏都有表头和记录范围。打开论文详情会为阅读栏留出位置，缩窄窗口或提高显示缩放时，主区会重新排版。[3440 × 1440 桌面预览](docs/dsh-paper-desk-ultrawide.png)

### 自动中文翻译

在「文献库 → 中文翻译」开启自动翻译后，当前资料库的真实论文版本会分批处理，新导入的标题和摘要也会自动加入队列。译文保存在本机 SQLite，列表优先显示已有中文，详情中可查看原文。没有摘要时先译标题，后续补入摘要再处理。相同原文复用已有译文，PDF 正文、阅读笔记和聊天记录不参与翻译。

翻译使用 DSH 当前选定的模型和供应商。调用以论文元数据为输入，不需要为文献台复制密钥。DSH 退出期间，待办和进度仍在数据库中；下次启动 DSH 时，已开启自动翻译的资料库会恢复处理。可在同一页查看队列、模型状态和用量。执行前的 token 数是估算，实际用量以供应商返回值为准。

在下方源码安装步骤准备好 Python 及前端依赖后，从仓库根目录执行：

```powershell
pnpm.cmd --dir condmat-trend-radar/frontend run build
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\dsh-paper-desk\install.ps1
```

安装后查看 DSH 顶栏。如果已有窗口仍显示旧版界面，可在“应用”菜单中刷新页面。插件链接到当前仓库，移动目录后需要重新运行安装命令。首次打开文献页面时，DSH 按需启动本机论文服务，退出宿主时释放服务。在 DSH 插件管理中可停用“凝态文献台”。隔离验收库的桌面实拍可查看[宽窗口](docs/dsh-paper-desk.png)和[窄窗口](docs/dsh-paper-desk-narrow.png)。

插件回归入口为 `node --test dsh-paper-desk/test/*.test.mjs`。安装 Playwright 和 Chromium 后，可运行 `node dsh-paper-desk/test/workspace_e2e.mjs` 验证六个页面及读写流程。已安装 DSH 的 Windows 电脑还可运行 `node dsh-paper-desk/test/desktop_e2e.mjs` 验证真实桌面菜单、安装和返回操作。联网验收使用 `condmat-trend-radar\.venv\Scripts\python.exe dsh-paper-desk\test\live_sources.py`，在临时资料库查询 OpenAlex、Crossref、arXiv，下载一份公开 PDF 并检查 Zotero 压缩包。

桌面已安装视觉主题插件时，运行 `node dsh-paper-desk/test/desktop_e2e.mjs --themes` 可在隔离配置中检验四套原生主题、跟随系统和已打开图表的实时配色。

同一桌面测试支持 `--ultrawide`。它会调整隔离 Electron 窗口的 CSS 视口，检验超宽屏六页布局、分栏阅读顺序、勾选状态及详情开合。Windows 显示缩放和应用缩放会影响同一物理屏幕的可用 CSS 宽度。

`node dsh-paper-desk/test/desktop_e2e.mjs --translation-runtime` 用隔离的 DSH 配置验证模型接入、自动翻译开关和重启续跑。`node dsh-paper-desk/test/translations_e2e.mjs` 验证翻译界面。`condmat-trend-radar\.venv\Scripts\python.exe dsh-paper-desk\test\translation_scale.py` 用十万个合成版本验证队列规模、去重和增量更新。这三项测试均不产生模型推理费用。

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
