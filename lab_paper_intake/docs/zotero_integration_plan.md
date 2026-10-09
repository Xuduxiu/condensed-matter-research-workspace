# Zotero Integration Plan

## v0.3 当前方案：RIS 导入包

- 使用 Zotero Local API 做连接检测。
- 生成 `selected_papers.ris`。
- 用户通过 Zotero `File -> Import` 导入。
- 优点：不需要 Zotero API key，不需要 Python，稳定。
- 限制：不能自动创建 Zotero collection / note / attachment。

## v0.4 可选路线 A：Zotero Web API

- 使用 Zotero Web API key。
- 支持创建 collection、item、note、tags、附件。
- 缺点：需要用户配置 Zotero API key。

## v0.4 可选路线 B：Zotero 插件

- 写 Zotero 插件，暴露本地写入桥。
- LabPaperIntake 调用本地插件接口。
- 优点：不需要 Web API key。
- 缺点：需要维护 Zotero 插件。

## v0.4 可选路线 C：Connector API

- 调研 Zotero Connector save endpoint。
- 可能只能保存有限 item，collection/note/tag/PDF 不一定完整。
- 只有验证后再采用。
