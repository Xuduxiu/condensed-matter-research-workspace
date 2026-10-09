# Windows 交付说明

## 构建

在项目根目录运行：

```bat
build_exe.bat
```

构建完成后输出目录是：

```text
dist\LabPaperIntake\
```

主程序是：

```text
dist\LabPaperIntake\LabPaperIntake.exe
```

构建脚本不会把源码项目里的真实 `.env`、`deepseek_api.txt`、历史 `data/exports`、历史 `data/pdfs` 或 `papers.db` 打进交付包。

## 发给师姐

把整个 `dist\LabPaperIntake\` 文件夹发给师姐，不要只发单独 exe。

如果需要在师姐电脑启用 DeepSeek 摘要，在师姐电脑上复制：

```text
dist\LabPaperIntake\config\.env.example
```

为：

```text
dist\LabPaperIntake\config\.env
```

然后在本机填写 DeepSeek API key。不要公开分享带 API key 的 `config\.env`。

## 使用

1. 安装并打开 Zotero 桌面端。
2. 在 Zotero 设置中确认允许本机其他应用通信。
3. 双击 `LabPaperIntake.exe`。
4. 浏览器会自动打开本地 Streamlit 页面。
5. 输入检索词并运行检索。
6. 勾选论文。
7. 点击“生成 Zotero 导入包”，再点击“打开 RIS 文件”，或在 Zotero 中 File -> Import 选择 `selected_papers.ris`。

## Zotero Local API

本软件只使用 Zotero Local API：

```text
http://localhost:23119/api/
```

不需要 Zotero API key，不需要 Zotero user ID，也不使用 Zotero Web API。v0.3 仅使用 Local API 做连接检测，不通过 `/api` 直接写入 Zotero。

如果连接失败，先确认 Zotero 桌面端已经打开，并且允许本机应用通信。Zotero 未连接时，软件仍可检索并生成 RIS 导入包；用户仍可在 Zotero 中手动 File -> Import。

## DeepSeek 配置

打包后的读取优先级是：

1. `dist\LabPaperIntake\config\.env`
2. exe 同级 `.env`
3. 系统环境变量

未配置 DeepSeek API key 时，软件仍然可以运行，并进入 no-LLM 模式：可检索、导出、导入 Zotero 元数据，但不会生成新的中文 LLM 摘要。

## 安全边界

本软件不会实现 Sci-Hub，不会绕过付费墙。PDF 下载和 Zotero 附件只使用合法开放获取 URL 或本地导出包里的 PDF。
