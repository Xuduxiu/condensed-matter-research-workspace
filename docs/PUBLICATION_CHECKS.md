# 公开仓库发布验证

验证日期：2026-10-09（Asia/Shanghai）。

本次整理保留现有业务实现，补充源码安装说明、示例配置、Git 忽略规则、换行规则和 GitHub Actions。

## 本地已执行

| 检查 | 结果 |
| --- | --- |
| Radar：`python -m unittest discover -s tests -v` | 188 项通过，161.758 秒 |
| Intake：`python -m pytest -q -p no:cacheprovider --basetemp <独立临时目录>` | 65 项通过，6.53 秒 |
| 前端：`npm run typecheck` | 通过 |
| 前端：`npm run lint` | 通过，零警告要求满足 |
| 前端：`npm run build` | 通过，609 个模块；统计图表 chunk 仍有大于 500 kB 的体积提示 |

本地验证使用 Windows、Python 3.12.14、Node.js 24.14.0 和现有项目虚拟环境。首次在受限沙箱中运行时，Python 临时目录及 Node 路径访问失败；随后以正常本机权限运行，并为 Intake 指定新的独立临时目录后通过。该失败属于验证环境问题，没有据此修改业务代码。

## 发布范围

纳入源码、测试、配置模板和维护文档。

不纳入真实密钥、`.env`、生产数据库、下载的论文 PDF、导出包、日志、虚拟环境、依赖缓存、构建产物、私人安装交接目录、历史运行截图或临时修复脚本。本机原文件保留。

## 验证边界

- GitHub Actions 使用 Windows / Python 3.11 运行后端与 Intake 测试，Ubuntu / Node.js 22 / pnpm 10 执行前端安装与检查；远端结果以 [Actions 页面](https://github.com/Xuduxiu/condensed-matter-research-workspace/actions) 为准。
- 本次未重新执行真实联网扫描、机构下载、生产数据迁移或 Windows 安装包验收。
- 其他历史报告中的数据库规模、线上成功数量与服务状态，不作为本次发布验证结果。
