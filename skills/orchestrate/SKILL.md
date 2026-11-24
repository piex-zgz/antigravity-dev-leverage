---
name: orchestrate
description: >-
  Lightweight orchestration guide for choosing between single subagents,
  research racing, native /teamwork, and native /swarm in Antigravity.
---

# Orchestrate

本 Skill 不重复实现派发框架，只定义 **Antigravity 原生编排能力** 的选型边界与调研赛马流程（子代理模型分工以常驻规则为准）。

## 1. 编排模式选型

| 场景 | 推荐方式 | 关键取舍 |
| :--- | :--- | :--- |
| 单点只读调研、文件核验、独立审稿、阶段盯盘 | 直接 `invoke_subagent` | 轻量、上下文纯净、按角色选模型 |
| 开放性技术调研 / 高风险方案选型（防单视角盲区） | **调研赛马**（见第 2 节） | 并行多路独立取证再合成超集 |
| 多文件协同开发、需要分阶段交接与风险分档验收 | 建议用户启用原生 **`/teamwork`** | 原生跨对话状态与 `DEAD_ENDS` 记录 |
| 大规模多角色并行协作、共享消息板与播报/盯盘 | 建议用户启用原生 **`/swarm`** | 原生共享板、narrator 与 attention 排序 |
| 多分支/多代理并行开发合并收尾 | 单代理专项**收拢合并** | 合并后把各分支新建的零散测试归并回对应模块测试，把临时复制的常量/夹具提回公共处 |
| 长程无人值守闭环推进 | 建议用户启用原生 **`/goal`** + `loop-anchor` | 原生持久推进配合外置锚点防漂移 |
| 调用本地 Codex (`codex`) 并行编码协作 | `scripts/codex_worker.py` (`start`/`watch`/`resume`/`stop`) | 干净隔离 `CODEX_HOME`、支持 `resume` 续聊与无损活跃度唤醒（裸 `codex exec` 由 `PreToolUse` Hook 硬拦截） |

## 2. 调研赛马（Research Racing）
1. 并行启动 **2 个**独立只读子代理（按需分配不同 `Model` 或切入角度：如一路深挖核心源码/细节，一路广扫外围约束/对照文档），明确各自边界与完成条件。
2. 汇总两路一手证据，**合成超集结论**（保留有源码/实测支撑的发现，显式标出分歧点并亲自抽查裁决）。
3. 重要方案可交 `entropy-reviewer` 以 Mode B 复核信源与逻辑盲区。

## 3. 子代理回报契约
派发任务时要求按极简结构回报：**结论 / 一手证据（精确路径行号或输出） / 风险与未核实项**；主代理只吸收已核实事实，不盲转猜测。
