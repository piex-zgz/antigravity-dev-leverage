---
name: walkthrough
description: >-
  Generate high-signal walkthrough materials for code changes, PRs/MRs,
  refactors, or complex design/research outputs. Supports concise Markdown
  (default), single-file interactive HTML for large scoped changes, and Feishu
  docs via lark-cli on request.
---

# Walkthrough

以最低阅读成本向他人（或未来的自己）讲清一次变更或调研的全貌，消除冗长 diff 与长文带来的信息交换损耗。

## 形态选择规则（单一事实源：`diff_baseline.py`）

先运行 `~/.gemini/config/plugins/dev-leverage/scripts/diff_baseline.py report --repo <repo> --baseline <baseline_json>`，严格按其输出的 `recommended_walkthrough`（基于**本轮隔离增量**，严禁按脏仓库全量 diff 误判）：

1. **默认形态：精炼 Markdown 走读**（`recommended_walkthrough == "markdown"`，直接输出或写入 Artifact / 指定路径）。
2. **单文件交互 HTML 走读页**：当用户明确要求，或脚本输出 `recommended_walkthrough == "html"`（本轮增量达到大改动阈值：文件 ≥ 8、或总变更行 `added + deleted` ≥ 300、或跨模块 ≥ 3）时生成，零外部 CDN 依赖，写入当前对话 Artifact 目录或用户指定路径并给 `file://` 链接。
3. **飞书走读文档（同步团队时按需触发）**：
   - 当用户明确要求产出飞书文档同步团队/评审时，先确认本机存在 `lark-cli`；若不可用则回退为 Markdown/HTML 并说明原因。
   - 若沙箱无法访问钥匙串/网络，按规范申请提权执行。
   - **铁律**：同一份走读**只创建一次**，创建后必须回读校验标题、正文关键表与链接是否存在；不重复新建多份空白或覆盖版本。

## 必须讲清的三件事（缺一不可）

### 1. 改了什么 & 为什么（What & Why）
- 用 2~4 句话说清背景问题、核心方案、**复用了什么/删掉了什么**，以及**刻意没有做什么**（边界取舍）。
- 调研/方案类：直接给最终判断、适用前提与明确建议（不写自我辩白或元叙述）。

### 2. 关键链路 & 重点看哪（Flow & Hotspots）
- 给出高层结构图或调用链路（Markdown 用 Mermaid，HTML 用内联 SVG 或清晰分步卡片）。
- 只列出 **Top 3~5 个最值得人工细看的文件/章节**，写明关注理由并附精确 `file://` 行号链接。
- 禁止对格式化、import 调整、机械重命名做流水账播报。

### 3. 怎么验证 & 后续从哪接手（Proof & Handoff）
- 若已运行 `gate-verify`，直接复用其验收证据卡与审稿结论，不重复跑检查；否则如实列出已验证与未覆盖项。
- 明确后续开发、Review 或上线接手的入口、依赖与注意事项。

## 单文件 HTML 走读页规范（触发时）
- 零外部依赖，离线/内网打开无白屏。
- 顶部概览条（结论、本轮增量规模、风险分档、验证状态）+ 左侧固定目录 + 主区三段式卡片。
- 支持按风险等级（`High` / `Medium` / `Low`）筛选审查点、一键展开/折叠关键 diff 片段、高对比度清晰排版。
- 生成后在回复中只给 `file://` 链接与 3 行核心摘要，不把整份 HTML 源码贴进对话。
