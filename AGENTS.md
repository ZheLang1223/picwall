# AGENTS.md

给在本仓库工作的 AI 代理（Codex、ZCode、Gemini、Claude 等）的约定。

## 汇报 PR

当被问到“现在有哪些 PR”这类问题时，按三类分开列：

1. **我自己的**：每个 PR 一句话说是什么、状态、是否已过独立 review 与结论。
2. **合作者的**（仓库有 write 权限的人）。
3. **外部的**：必须读过代码再评。

合作者和外部的每个 PR 一句话，包含：

- 对应 Issue（没有对应 Issue 的排在后面、降低优先级）；
- 怎么解决的；
- 效果和之前的对比；
- 我的评价和分级：**可合 / 需改 / 需你拍板 / 不合**。

好的汇报长什么样、以及常见的反面例子，见 [docs/agents/pr-report-example.md](docs/agents/pr-report-example.md)。
