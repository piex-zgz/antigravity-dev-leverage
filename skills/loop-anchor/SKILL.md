---
name: loop-anchor
description: >-
  Native-first anti-drift and low-ROI stop-loss adapter. Activate whenever
  running /goal, /teamwork, /swarm, multi-phase implementation, or when a
  fix/review/challenger loop repeats on the same file or module (>= 3 rounds).
  Reuses existing project memory or native /teamwork state instead of creating
  duplicate ledgers, enforces phase-boundary rereads, schedules low-cost
  drift-watcher audits, and stops diminishing-return fix loops early.
---

# Loop Anchor

长任务跑久了容易因上下文噪音累积而出现原则遵循度下降、微操跑偏或悄悄改写目标。本 Skill 是**原生优先的状态适配器**：优先复用既有项目记忆或原生 `/teamwork` 状态，不另造第二套台账。

## 1. 状态锚点路由顺序（严禁并行建两套台账）

按以下顺序探测并使用第一个命中的位置：

1. **项目既有工作记录（最高优先）**：例如仓库 `AGENTS.md` 指定的 `CURRENT_WORK.md`、`approved_reference/` 或项目约定的状态文件。直接遵循该仓库的读写与恢复纪律，不另建 `STATE.md`。
2. **原生 `/teamwork` 状态目录**：若当前运行在 `/teamwork` 模式下，直接复用其 `<appDataDir>/scratch/teamwork/<run_id>/.agents/teamwork/` 下的 `BRIEFING.md`、`DISPATCH.md`、`progress.md` 与 `DEAD_ENDS.md`，让位给原生机制。
3. **跨对话持久兜底目录（仅当 1、2 均不存在时）**：在 `<appDataDir>/scratch/loop-anchor/<task-slug>/`（跨对话持久，不绑死单次对话目录）创建两份精简文件：
   - `PRINCIPLES.md`（不可变原则：终极目标、范围边界、硬约束、Gate 三问，外加 **当前所处阶段 `Phase 0/1/2`、核心结构假设与证伪条件**；未过 Phase 1 真成品确认不得直接进入大并发放大）
   - `STATE.md`（滚动状态快照，**严格控制在 80 行以内**：当前阶段、下一步唯一动作、带实证证据的已完成项、近期投入产出摘要与阻塞风险；历史细节定期归档，不让状态文件膨胀到上千行）

## 2. 阶段重读、战略止损与干净切会话
- 每进入新阶段、经历上下文压缩或跨会话恢复后，**先用 `read_file` 重读锚点文件**，不得用口头记忆或摘要替代。
- **上下文变重时导出原始交接包（防旧上下文干扰）**：当会话上下文体检亮红灯（`Context Health RED`）或切大方向时，运行脚本导出**原始记录交接包**（自动读取 `transcript_full.jsonl`、脱敏去 query 参数、默认写进当前会话 `scratch/handoff.md`，不另造仓库台账、不由旧会话重写总结）：
  ```bash
  python3 ~/.gemini/config/plugins/dev-leverage/scripts/context_tools.py handoff \
    --transcript <transcript_path> --repo <repo_path>
  ```
  切入新会话后，先读交接包与仓库现状，**大改前先确认三件事**（下一步具体改动 + 明确 1 条不做什么 + 1 个真实样本验证目标），确认后再放开执行。
- **微观与战略双重止损（硬红线）**：
  - **微观止损**：对同一非核心模块或边缘细节连续修补 >= 3 轮且真实数据收益趋近于 0，立即停机上报冻结范围。
  - **战略止损**：连续 2 轮优化净收益骤降（或引入回归、导致仓外绕路脚本膨胀）时，立即停止继续叠补丁，摊开累计投入与真实产出账交由用户拍板「止损切换」或「简化重写」。

## 3. 周期性独立复查（直查真实改动、原话与成品）
- 每完成 **2~3 个阶段**、跨多文件大规模扩张后以及**最终交付前**：
  1. 先导出本轮隔离增量、风险摘要与用户原话时间线（不可省略 `--repo` 与 `--baseline`）：
     ```bash
     python3 ~/.gemini/config/plugins/dev-leverage/scripts/diff_baseline.py report \
       --repo <repo_path> \
       --baseline <baseline_json> \
       --write-diff <scratch_dir>/current.diff \
       --write-report <scratch_dir>/current-report.md
     python3 ~/.gemini/config/plugins/dev-leverage/scripts/context_tools.py handoff \
       --transcript <transcript_path> --user-only --out <scratch_dir>/user-intent.md
     ```
  2. 调用 `invoke_subagent(TypeName="drift-watcher", Model="flash", ...)`（重大里程碑或最终交付前改用 `Model="inherit"`），把**三份原始证据**一并交给它直查：① `current-report.md` + `current.diff`（真实 Git 增量）；② `user-intent.md`（用户原话）；③ 原始输入样本、真实终稿产物、`stderr` 日志与外部参照基线。由其独立核查分桶集中度/漏报并抽查 3~5 条溯源，产出 ≤1 屏人工决策卡。
- 收到 `drift-watcher` 结论后：
  - `Aligned`：继续推进。
  - `Mild Drift`：按具体偏离与漏报点纠偏并更新状态记录，再继续。
  - `Off-Track`：**立即停止新增改动**，把漏报/流失事实、边际 ROI 与“删减重写 / 切新会话 / 止损”选项交给用户拍板。
