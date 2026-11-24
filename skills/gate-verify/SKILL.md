---
name: gate-verify
description: >-
  Define and run Gate-first verification with scoped diff baselines before
  declaring engineering changes, refactors, technical research, or design
  documents complete, producing a concise 1-minute verification evidence card.
---

# Gate Verify

将验收标准、验证工具和复查证据结构化落地，让用户能在约 1 分钟内完成可信复查。

## 执行步骤

### 0. 隔离本轮改动基线（脏仓库必做）
在存在历史未提交改动的仓库里，**严禁直接把整个 `git diff` 当成本轮改动**：
1. 优先使用首轮注入的基线文件路径，运行确定性脚本生成本轮增量报告与隔离 diff 文件：
   ```bash
   python3 ~/.gemini/config/plugins/dev-leverage/scripts/diff_baseline.py report \
     --repo <repo_path> \
     --baseline <baseline_json> \
     --write-diff <scratch_dir>/scoped.diff \
     --write-report <scratch_dir>/baseline-report.md
   ```
2. 若无基线文件，改用 `--files <本轮实际编辑的文件列表>` 生成隔离 diff；两者均不可得时，在证据卡中按脚本输出标注 `full_dirty_unisolated`（或 `non_git`）。注意：`baseline-report.md` 顶部会自动输出「Surface & Risk Summary」以及 `--files` 遗漏告警、大新文件截断告警，不得静默忽略。

### 1. 读取仓库专属约定（Space/Project 层优先）
- 读取目标仓库的 `AGENTS.md`、`GEMINI.md`、`.agents/rules/*.md` 以及真实构建/测试配置（如 `Makefile`、`pyproject.toml`、`package.json`、`go.mod`）。
- 严格复用仓库既有测试、lint、类型检查与验收命令，不臆造不存在的脚本。

### 2. 按任务类型执行默认 Gate

#### A. 后端 / 工程代码与重构
1. **本轮影响面**：基于第 0 步的 `baseline-report.md`，确认改动规模、风险分档（`Low` / `Medium` / `High`）及受影响消费者（若存在“历史脏文件在本轮二次编辑”的重叠提示，显式说明）。
2. **构建与自动化验证**：实跑相关单元测试、集成测试、类型检查与 lint，保留精确命令、退出码与关键统计。
3. **输入输出数量核对（防单测全绿但成品做废）**：凡涉及过滤、归并、压缩、迁移、识别等多阶段链路，必附「输入多少 → 最终成品保留多少 → 少掉的部分去哪了」对账数据；**严禁用仓外临时脚本遮盖主链路缺陷**，发现主链路失效须直接暴露。
4. **复用与删减检查**：核对是否复用了既有权威实现、是否删除了被替代的旧路径/死代码/失效测试；无必要不新增表、字段、参数或兼容层。不靠压缩格式、合并语句凑体量指标；先做减法，确需扩展时说明真实业务收益。
5. **独立审稿（Medium / High 风险必做）**：
   - 常规逻辑/Bug 审查：配合内置代码审查能力。
   - **减法与用户意图对齐审查**：先用 `context_tools.py handoff --transcript <path> --user-only --out <scratch_dir>/user-intent.md` 导出用户原始诉求，再调用 `invoke_subagent(TypeName="entropy-reviewer", Model="inherit", ...)`，传入 `scoped.diff`、`baseline-report.md`、`user-intent.md`、相关源码路径及仓库 `AGENTS.md`/`.agents/rules/*.md`。若涉及数据处理/批量管线交付，另派 `drift-watcher` 直查真实终稿分桶分布并抽查 3~5 条回溯原始输入。处理其 Blocker/Major 意见后再复验。

#### B. 技术调研 / 方案与批量分析文档
1. **一手信源核验、对抗抽核与漏报清单**：核心事实必须锚定源码路径+行号、实测命令输出或权威文档链接；明确区分“已证实”“推断”“未核实”。若结论基于批量脚本、LLM 汇总或与外部基线对比，**不得只凭格式/引用行号等机械校验放行**，必须回原始一手数据对抗抽核，并**单列漏报/盲区清单**（严禁未逐项核对即宣称 100%）。
2. **文档审美与去套话检查**：运行确定性扫描脚本检查自我辩白、元叙述和 AI 套话：
   ```bash
   python3 ~/.gemini/config/plugins/dev-leverage/scripts/doc_style_check.py <doc1.md> [doc2.md ...]
   ```
3. **独立文档复核（重要方案必做）**：调用 `entropy-reviewer`（`Model="inherit"`）以 Mode B 审查文档路径，重点核查无据推断、遗漏反例与结论可落地性。

#### C. 页面 / HTML / Explorer 类产物（如涉及）
- 涉及网页、静态 HTML 报告或前端页面验收时，优先调用内置 `browser` 子代理打开页面并截图验证版式与控制台无报错，以截图作为复查证据，不以纯源码目视替代。

### 3. 输出「1 分钟验收证据卡」

```markdown
## 验收证据卡

- **结论**：PASS / PASS WITH NOTES / BLOCKED
- **风险分档**：Low / Medium / High（范围隔离方式：`baseline_delta` / `explicit_files` / `explicit_files_over_baseline` / `full_dirty_unisolated` / `non_git`）
- **改动规模**：<原样引用 `baseline-report.md` 的文件数与 `+X / -Y` 行（干净已提交里程碑可附 `git diff --shortstat <base>..HEAD`），涉及模块>

### 请你看这 1~3 处（最高价值人工复查点）
1. [file.py](file:///abs/path/file.py#L10-L40) — <为什么这里最值得看 / 核心风险或取舍>

### 复用与删减（减法证据）
- **复用**：<复用了哪些既有接口/模块>
- **删减**：<移除了哪些旧路径/冗余逻辑；如无则写“无”>

### Gate 检查明细
| 检查项 | 状态 | 验证方式 / 精确证据 |
| :--- | :--- | :--- |
| <标准 1> | PASS / FAIL / SKIPPED | `<精确命令或核验方式>` → <退出码与关键输出> |

### 独立审稿与残留风险
- **独立审稿结论**：<原样粘贴 `entropy-reviewer` / `drift-watcher` 原始 `Verdict` 与关键减法/阻断意见，不得降级改写；未触发须说明原因>
- **未验证范围与残留风险**：<如实列出，无则写“无”>
```
