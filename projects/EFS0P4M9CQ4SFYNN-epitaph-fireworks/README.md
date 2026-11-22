# 墓碑礼花 · Epitaph Fireworks

**平衡二元环的最近邻、次近邻不同对数：完整分类与 Lean 4 证明。**

项目 XID-16：`EFS0P4M9CQ4SFYNN`。发起与维护：Hengtong Xu（[@Xuduxiu](https://github.com/Xuduxiu)）。数学推导、实现与核验使用了 AI 协作，最终交付以可复现的源码和验证结果为准。

**状态：首个完整定理已完成，Lean 内核验证通过。** 2026-11-22（北京时间）完成了一次清理构建、公理依赖审计和独立有限枚举。具体输出见 [验证记录](verification/result.json)、[构建日志](verification/build.log) 和 [公理报告](verification/axioms.log)。

这是一个独立推导并完整形式化的小型组合数学项目。当前不主张数学原创优先权或首次形式化；相关已有文献及边界见 [数学说明](docs/PROOF.md)。

## 问题只有一句话

把 `m` 个 0 和 `m` 个 1 排成一个环。相邻位置不同的次数 `D1`，以及隔一个位置不同的次数 `D2`，究竟能取哪些组合？

下标按环长取模。每个起始位置计算一次，因此长度 2 的环 `01` 有 `D1 = 2`、`D2 = 0`；这里不是对无向位置对去重后的计数。

## 已证明的完整答案

对任意正整数 `m`，必须有 `D1 = 2r`，其中 `1 ≤ r ≤ m`，而且

\[
\begin{cases}
D_2=0,&r=m,\\[2mm]
D_2\in\{4,6,8,\ldots,4\min(r,m-r)\},&1\le r<m.
\end{cases}
\]

**每个实际平衡环都满足这组条件；每个满足条件的数对都有一个实际平衡环实现。** 证明没有环长上限。

例如，12 个位置、0 和 1 各 6 个时：

| `D1` | 全部可能的 `D2` |
|---:|---|
| 2 | 4 |
| 4 | 4、6、8 |
| 6 | 4、6、8、10、12 |
| 8 | 4、6、8 |
| 10 | 4 |
| 12 | 0 |

其中 `000000111111 -> (2,4)`，`001100110011 -> (6,12)`，`010101010101 -> (12,0)`。任意大小的非空平衡环都不可能有 `D2 = 2`。

## 主定理与语义

```lean
theorem balanced_binary_ring_spectrum
    {m h k : Nat} (hm : 0 < m) :
    Realizes m h k ↔ BalancedSpectrum m h k
```

`Realizes` 量化实际的 `List Bool`。颜色数量用 `List.count` 计算，`cyclicD1`、`cyclicD2` 逐个比较实际符号，并用原词末端提供周期历史。它们没有被定义成块长公式。

[Verification.lean](Verification.lean) 重新展开两色数量约束和结论公式，检查它们与主定理的类型匹配。核心证明还覆盖任意词的块长分解、旋转不变、实际词计数和每个允许值的构造，因而没有附加“输入已经具有块表示”之类的限制。

## 证明为何成立

一个平衡环有 `r` 个 0 块和 `r` 个 1 块，各组块长都是总和为 `m` 的正整数。因此 `D1 = 2r`。

设两种颜色中长度至少为 2 的块数分别为 `q0`、`q1`，直接计数可得 `D2 = 2(q0 + q1)`。当 `r = m` 时所有块长为 1，`D2 = 0`。当 `r < m` 时，每种颜色的长块数恰好可取 `1` 到 `min(r,m-r)` 中的全部整数。分别构造两种颜色的块并交替展开，便得到完整可达区间。

详细双向证明及构造见 [docs/PROOF.md](docs/PROOF.md)。

## 复现

依赖只有 **Lean 4.19.0 自带的 Std** 和 Python 3；没有 Mathlib 或额外软件包。

安装 [Lean/Elan](https://github.com/leanprover/elan) 后，在本目录执行：

```bash
elan toolchain install leanprover/lean4:v4.19.0
python3 scripts/verify.py
```

`lean-toolchain` 固定编译器版本。完整脚本依次检查源码、清理旧构建、重新构建全部证明、用 `lean --trust=0` 执行独立陈述与公理审计，最后运行 Python 的有限诊断。任何步骤失败都会以非零状态退出，并将 `verification/result.json` 标为失败。

如果只需编译和查看公理依赖：

```bash
lake build
lake env lean --trust=0 -DwarningAsError=true Verification.lean
```

本仓库新增了范围限定在该项目的 [GitHub Actions 工作流](../../.github/workflows/epitaph-fireworks-lean.yml)。它下载固定版本的官方 Lean 包、核对 SHA-256，并运行同一个验证脚本。远端工作流状态应以实际 Actions 运行记录为准。

## 验证结果

| 项目 | 结果 |
|---|---|
| 全部证明从清理后的构建开始编译 | PASS |
| 警告作为错误 | PASS |
| 任意实际平衡词的完整充要条件 | PASS |
| 显式展开的主陈述类型检查 | PASS |
| 两个站点的边界情形 | PASS |
| 关键定理公理依赖检查 | PASS |
| 长度 2–18 的全部 66,196 个平衡词 | PASS，有限诊断 |
| 另一种实现的独立证明内核 | 未运行，不作此声称 |

主定理的公理依赖恰为 `propext`、`Quot.sound`、`Classical.choice`。没有新增公理、未完成证明或 `native_decide`。有限枚举与普遍形式化证明分别记录，不以枚举替代普遍证明。

## 文件地图

| 文件 | 内容 |
|---|---|
| [Fireworks/Composition.lean](Fireworks/Composition.lean) | 正整数块长的完整长块数量谱与显式构造 |
| [Fireworks/BinaryRing.lean](Fireworks/BinaryRing.lean) | 实际布尔词、周期计数及展开后的统计定理 |
| [Fireworks/Decomposition.lean](Fireworks/Decomposition.lean) | 任意平衡词到正块长表示的完备性 |
| [Fireworks/Rotation.lean](Fireworks/Rotation.lean) | 两个计数的循环旋转不变性 |
| [Fireworks/Spectrum.lean](Fireworks/Spectrum.lean) | 分类公式与块长存在性的等价关系 |
| [Fireworks/Classification.lean](Fireworks/Classification.lean) | 完整主定理、推论和实际例子 |
| [Verification.lean](Verification.lean) | 单独书写的目标陈述与公理审计入口 |
| [scripts/verify.py](scripts/verify.py) | 一次命令完成全部验收并记录源文件哈希 |
| [scripts/enumerate.py](scripts/enumerate.py) | 独立的直接下标枚举 |
| [manifest.json](manifest.json) | 不可变项目清单与 16 位标识算法 |
| [docs/NEXT.md](docs/NEXT.md) | 更一般组分、三阶关联，以及明确未解决的后续问题 |

## 交给 Codex 继续

先读 [AGENTS.md](AGENTS.md)，复现现有结果并核对定理定义。第一阶段已经完成；继续工作应另立里程碑，不要把现有已完成证明重新标成待完成。后续可以先推广到不等组分二元环。

若希望转入文献中明确列出的开放问题，[docs/NEXT.md](docs/NEXT.md) 记录了二元词的“两刀洗牌平方猜想”及原始来源。其状态为 **OPEN / NOT_ATTEMPTED**，不计入本项目已验证成果。
