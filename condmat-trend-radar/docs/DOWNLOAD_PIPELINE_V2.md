# PDF 下载与 Zotero 联动口径

## 合法来源与候选顺序

下载器只尝试明确可合法访问的 PDF，不绕过登录、机构订阅或付费墙：

1. 论文及其关联版本的 arXiv 精确版本、最新版本和官方 export 端点；
2. 元数据中明确标记开放获取的 publisher/repository PDF；
3. OpenAlex 的 `best_oa_location`、明确 `is_oa`/license 的 locations；
4. Crossref 中同时存在 Creative Commons 许可和 PDF content-type 的链接；
5. Unpaywall 的 best/all OA locations；
6. 本地元数据没有候选时，以 DOI 实时查询 OpenAlex，再查询 Crossref CC 链接。

候选按上述顺序逐个尝试。一个候选返回 HTML、截断文件、加密文件或网络错误时，不会阻止后续候选继续下载。

## PDF 验证

成功状态必须同时满足：

- 文件大小达到下限且未超过 200 MiB 默认上限；
- 前 1024 字节内存在 PDF signature；
- 文件尾存在 EOF marker；
- PyMuPDF 能解析文档、无需密码、至少有一页且第一页可打开。

只含 `%PDF-` 文件头、实际为错误页或不完整下载的文件不会进入论文库或 Zotero 包。

## 失败分类与重试

- 网络错误、超时、HTTP 429/5xx、HTML 伪 PDF、截断或结构损坏：指数退避重试；达到上限后进入人工复核。
- 401/403/407、451、加密 PDF、超过大小上限、没有合法 OA 候选：直接进入人工复核。
- 404/410：候选全部尝试后标记永久失败，可由用户在元数据更新后手动重试。

每个候选会记录来源、原因、脱敏 URL、HTTP 状态、content-type、最终 URL、接收字节数、SHA-256、失败类别和验证结果。含 token/signature/API key 的查询参数不会写入数据库。

API：

- `GET /api/library/downloads/statistics`：按候选来源统计尝试数、成功数、成功率，并给出失败类别及任务状态分布。
- `GET /api/library/downloads/{task_id}/audit`：查看单个任务的完整候选审计链。

## Zotero 包

每个结构验证成功的 PDF 都会复制到导出目录的 `pdf/`，并通过 RIS `L1` 相对路径关联。ZIP 内包含 RIS、BibTeX、CSV、摘要、导入说明、manifest 和 PDF。

使用时必须先完整解压 `zotero_package.zip`，保持 `selected_papers.ris` 与 `pdf/` 的相对位置，再在 Zotero 中导入 RIS。这样安装目录或电脑变化不会使附件继续指向旧机器的绝对路径。

## 能力边界

“网络上存在”不等于“可合法自动下载”。系统能保证的是：已发现的合法 OA 候选全部轮询、失败不伪装成功、每次结果可审计、成功 PDF 必定进入 Zotero 包；不能承诺绕过付费墙或下载未公开全文。
