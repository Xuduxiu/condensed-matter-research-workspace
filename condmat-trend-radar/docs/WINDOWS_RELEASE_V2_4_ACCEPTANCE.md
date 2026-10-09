# CondMat Radar v2.4.0 Windows 发布验收

验收日期：2026-07-28<br>
结论：通过，可交给另一台 Windows 10/11 x64 电脑安装。

## 最终交付物

- 公开包：`releases/CondMatRadar_v2.4.0_Windows_x64.zip`
  - 大小：39,230,727 bytes
  - SHA-256：`32f703e9f89cec743568173c93d93100e6667c1c96eeca2e4866225e0e083767`
  - 不含真实 API、数据库、PDF、日志或用户状态。
- 私有交接目录：`releases/PRIVATE_Sister_Install_Kit_20260728_111508`
  - 包含公开程序和一份本地 API 配置。
  - 未压缩，避免误传为普通公开安装包。
  - 安装完成后应从目标电脑、聊天软件和移动介质删除。

## 全链路验收

在全新的模拟 Windows 用户目录中完成了以下实际操作：

1. 从最终 ZIP 解压并运行安装器。
2. 无需管理员、Python、Node 或 npm，程序安装到当前用户的 LocalAppData。
3. 一键私有安装器正确识别 DeepSeek 和 OpenAlex 已配置；未在验收中触发付费 DeepSeek 推理。
4. 启动 `CondMatRadar.exe`，同源网页和 API 在 `127.0.0.1:8765` 返回 HTTP 200，API 版本为 2.4.0。
5. 新数据库首启自动扫描 arXiv cond-mat，取得 50 篇真实论文；材料抽取从论文内容发现 12 个材料并建立 13 条证据关联。
6. 实时扫描调度器运行，默认间隔 900 秒；数据库 `quick_check=ok`，外键违规为 0。
7. `--stop` 可正常关闭服务并移除运行状态文件。
8. 卸载可移除程序和快捷方式，默认保留 API 配置、数据库与论文文件。
9. 在桌面快捷方式被系统策略拒绝时，安装器只给出警告，不再中止；仍可从开始菜单或程序路径启动。

## 代码与构建验证

- Python `compileall`：通过，0 个编译错误。
- 后端单元测试：44/44 通过，0 失败、0 错误、0 跳过。
- 前端 TypeScript、ESLint、生产构建：全部通过，0 warnings；Vite 仅提示 Analytics 懒加载包超过 500 kB。
- Windows PowerShell 脚本语法：4/4 通过。
- 公开 ZIP 文件名与文本二次扫描：未发现 `.env.local`、`deepseek_api.txt`、SQLite、PDF 或疑似真实 API 值。
- 程序停止后的最终环境：无 `CondMatRadar` 残留进程，无 8765 监听。

## 数据与 API 边界

- 没有复制当前约 9 GB 的个人生产数据库，因为其中包含收藏、监控、操作历史、AI 缓存以及 37 条指向 `G:\` 的 PDF 绝对路径。
- 目标电脑使用干净数据库，首次联网启动后立即获取真实论文。
- 当前私有配置包含 DeepSeek、OpenAlex；Semantic Scholar key、`OPENALEX_MAILTO` 和 `UNPAYWALL_EMAIL` 未提供，因此没有伪造。Crossref 和 arXiv 不需要 key；后续补一个联系邮箱可使 Unpaywall 使用更规范。
- 两台电脑暂时共享你的 DeepSeek/OpenAlex 配额和账单。师姐取得自己的 key 后，只需替换 `%LOCALAPPDATA%\CondMatRadar\config\.env.local` 并重启程序。

## 已知非阻断项

- 当前 EXE 没有商业代码签名，Windows SmartScreen 可能显示“未知发布者”；应核对本报告中的 ZIP SHA-256 后再选择继续。
- 若学校策略同时禁止桌面与开始菜单快捷方式，可直接运行 `%LOCALAPPDATA%\Programs\CondMatRadar\app\2.4.0\CondMatRadar.exe`。
- 自定义 `-DataDir` 位于其他磁盘时，普通卸载会保留该数据目录，需由用户确认后手工删除。
