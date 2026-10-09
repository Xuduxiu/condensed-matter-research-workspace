# Lab Paper Intake 使用说明

## 推荐流程

1. 在工作区根目录运行 `.\paper.cmd start`。
2. 在趋势雷达中选择研究概念并生成下载任务，或直接运行：
   `.\paper.cmd flow --concept ZrTe5 --limit 50`。
3. 打开 <http://127.0.0.1:8501>。
4. 在左侧“趋势雷达任务箱”点击“载入最新雷达任务”。
5. 检查论文标题、DOI、相关性理由和 PDF 状态，调整勾选。
6. 选择是否把合法开放获取 PDF 下载到导出包。
7. 点击“生成 Zotero 导入包”。
8. 打开生成的 `selected_papers.ris`，或在 Zotero 中选择 `File -> Import`。

导出包还包含：

- `selected_papers.csv`
- `selected_papers.bib`
- `selected_summary.md`
- `manifest.json`
- `pdfs/`
- 对应 ZIP 文件

## 直接检索

不经过趋势雷达时，可以在页面顶部输入研究问题并点击“开始检索”。系统会查询真实学术元数据源，并在配置允许时生成 LLM 摘要。

## Zotero 说明

当前源码通过 RIS 文件导入 Zotero。Zotero Local API 仅用于连接检查，不会直接写入资料库，因此不需要 Zotero API key。

## DeepSeek 配置

复制工作区根目录 `.env.example` 为 `.env`，填写：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=
```

不要公开分享包含真实 API key 的 `.env`、`config/.env` 或旧版 `deepseek_api.txt`。

## PDF 范围

程序只下载合法开放获取 PDF。没有 OA 地址的论文会保留为“仅元数据”，不会尝试绕过付费墙。