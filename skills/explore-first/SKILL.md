---
name: explore-first
description: >-
  Anti-premature-concrete exploration protocol for unfamiliar codebases, new
  datasets/domains, or major redesigns. Separates read-only reconnaissance from
  disposable thin vertical slices before committing to production architecture
  or /goal loops. Skip on small, well-scoped edits.
---

# Explore First

在陌生环境里，AI 写代码极快反而容易过早浇筑混凝土，把早期粗糙假设焊成局部极小值势阱。本 Skill 用三段轻量节奏延迟抽象，先让真实世界给反馈，再规模化施工。

> **何时跳过**：边界清晰的单点修 Bug、已有成熟架构下的小功能、纯文档小改直接执行，不套本流程。

## Phase 0：只读地形测绘（禁止写业务代码与敲定细化方案）
1. 盘清客观全貌：目录结构、输入样本类型与异类（边缘前缀/格式）、已有成品或参考基线、真实消费者与消费方式。
2. 列出当前**最大的 3 个未知数（Unknowns）**与 2–3 条差异明显的备选路线（例如「从零重建」vs「锚定已有基线做定向增量」）。
3. 面对复杂陌生域时，并行派出 2–3 个互相隔离的只读侦察视角（样本异类 / 最终消费者与已有资产 / 沉默流失与成本风险），破除单一上下文盲区。
4. 向用户汇报 1 页内地形盘点与路线选项，**确认真实目标与主路线后再动手**。

## Phase 1：可抛弃细管（Disposable Thin Vertical Slice）
1. 挑 **2–3 个最代表性且最别扭的真实样本**（必含异类样本，不专挑好跑的）。
2. 用最薄、明确标为**允许整段扔掉**的代码（不搭多层框架、不补全套抽象、不先做成本微调），直接打穿从原始输入到**最终读者真正消费的成品形态**。
3. 附一张简明**进出留存核对表**（输入多少 → 最终成品保留多少 → 消失部分去向）。
4. 把**真实成品原文/截图**（而非口头摘要）交给用户花 2 分钟审阅，拍板「方向对味」还是「换路子重写」。

## Phase 2：冻结契约，工程放大
1. 仅在 Phase 1 真成品获认可后，将成品形态与核心事实留存口径冻结为项目的确定性外循环门禁（离线金丝雀测试 + 1 页数据流契约）。
2. 此后再搭建正式架构、并发执行器与 `/goal` 长跑；项目后续踩到的专属坑沉淀进该仓库的确定性代码/测试，不污染全局插件。
