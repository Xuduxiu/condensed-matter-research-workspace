# CondMat Radar Windows 安装与交接

版本：2.4.0<br>
目标：Windows 10/11 x64，目标电脑无需预装 Python、Node 或 npm。

## 交付物边界

程序、数据和密钥严格分开：

- `CondMatRadar_v2.4.0_Windows_x64.zip`：公开程序包，不含任何真实 API、数据库、PDF、日志或个人操作记录。
- `PRIVATE_Sister_Install_Kit_*`：只用于当面交接，额外含你的本地 API 配置；安装成功后应从目标电脑和 U 盘删除。
- 目标电脑的配置与数据库位于 `%LOCALAPPDATA%\CondMatRadar`，重复安装或升级不会覆盖它们。

没有直接复制当前 9 GB 生产库，因为其中含收藏、监控、操作历史、AI 缓存，并且 37 份 PDF 使用 `G:\` 绝对路径。直接复制会把个人状态和失效附件路径一并带过去。师姐电脑默认从干净数据库开始，由实时扫描取得真实论文。

## 给师姐电脑安装

1. 将整个 `PRIVATE_Sister_Install_Kit_*` 文件夹复制到师姐电脑本地磁盘。
2. 双击 `一键安装_使用我的API.cmd`。
3. 等待浏览器自动打开 `http://127.0.0.1:8765/`。
4. 在“系统”页确认：数据库正常、实时扫描运行、DeepSeek 和 OpenAlex 显示已配置。
5. 安装确认后，删除私人交接文件夹及移动介质上的副本。

安装不请求管理员权限，不开放局域网端口，也不创建防火墙规则。

## 日常使用

- 桌面 `CondMat Radar`：启动或重新打开页面；重复双击不会启动第二个服务。
- 开始菜单 `CondMat Radar > Stop CondMat Radar`：关闭本机后台服务。
- 关闭浏览器标签不会自动停止后台扫描。
- 重新运行安装脚本即可升级程序，配置和数据保留。

## API 配置

本机密钥文件：

```text
%LOCALAPPDATA%\CondMatRadar\config\.env.local
```

支持：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL_FAST=deepseek-v4-flash
DEEPSEEK_MODEL_PRO=deepseek-v4-pro
OPENALEX_API_KEY=
OPENALEX_MAILTO=
UNPAYWALL_EMAIL=
SEMANTIC_SCHOLAR_API_KEY=
```

Crossref、arXiv 不需要 key；Zotero 当前通过 RIS/ZIP 与本地 PDF 附件联动，不需要 Zotero Web API key。

暂用你的 API 意味着两台电脑共享配额与账单。后续换成师姐自己的 key 时，编辑上述文件并从开始菜单停止、再重新打开程序即可。不要把 `.env.local` 发到群聊、邮件或公开网盘。

## 数据与磁盘

默认数据目录：

```text
%LOCALAPPDATA%\CondMatRadar\data
```

建议至少预留 25 GB，长期使用建议 40 GB。安装器支持命令行 `-DataDir "D:\CondMatRadarData"`，但必须在首次使用前指定，且写入的是目标电脑解析后的绝对路径。

首次启动会显式创建完整 SQLite schema 和工作台表，再启动实时 arXiv cond-mat 扫描，因此不会依赖“先点某个页面”才能初始化数据库。

## 卸载

Windows 设置中的“已安装的应用”可卸载 CondMat Radar。默认只移除程序和快捷方式，保留配置与数据库。若需要彻底清理，可从 PowerShell 运行：

```powershell
& "$env:LOCALAPPDATA\Programs\CondMatRadar\uninstall.ps1" -RemoveUserData
```

## 验收项目

- 程序仅监听 `127.0.0.1:8765`。
- `/openapi.json` 标题为 `Condensed Matter Trend Radar`。
- 首页、发现、文献库、材料雷达、监控、系统均可打开。
- 新数据库 `quick_check=ok`、外键违规为 0。
- 系统页只显示 API 已配置状态，不显示完整密钥。
- 公共 ZIP 二次扫描不含 `.env`、`.env.local`、`deepseek_api.txt`、SQLite 或历史 PDF。
- 真实 DeepSeek 请求会消耗你的配额；默认雷达简报仍由用户手动触发，不会后台自动调用付费 AI。
