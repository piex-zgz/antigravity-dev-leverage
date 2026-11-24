# Dev Leverage (`v0.4.0`) — Antigravity Engineering Harness Plugin

围绕 **陌生环境探索防过早浇筑、Context 增量体检与确定性交接、不可伪造三锚点验收 Gate、与零历史实证审稿子代理** 设计的 Antigravity 工程 Harness 插件。

设计哲学：**零业务假设的全局薄底座（Layer 1）**——只沉淀跨项目通用的工程纪律、确定性脚本与只读审稿角色；具体业务仓库的专属契约、金丝雀回归测试与高成本操作硬锁留在各自仓库内（Layer 2），优先级始终高于本全局插件。

> **配套可视化侧边栏**：如果你想在 Antigravity 右侧边栏常驻实时查看水位进度条、一键导出交接包以及离线扫描近 7/30 天历史摩擦轨迹，可搭配安装 [antigravity-context-hud](https://github.com/piex-zgz/antigravity-context-hud)。

---

## 快速安装

依赖 Python 3（纯标准库，零第三方依赖）。

### 方式 1：一键安装（推荐）

自动安装到 `~/.gemini/config/plugins/dev-leverage`，并以原子方式在 `~/.gemini/config/config.json` 中启用插件（保留 `0600` 权限与其他配置）：

```bash
curl -fsSL https://raw.githubusercontent.com/piex-zgz/antigravity-dev-leverage/main/install.sh | bash
```

### 方式 2：使用 Antigravity CLI 或 Git Clone

```bash
# Antigravity CLI
agy plugin install https://github.com/piex-zgz/antigravity-dev-leverage

# 或直接拉取到全局插件目录
git clone https://github.com/piex-zgz/antigravity-dev-leverage.git ~/.gemini/config/plugins/dev-leverage
```

### 方式 3：项目级共享

将仓库内容放入任意项目根目录的 `.agents/plugins/dev-leverage/`，团队拉代码后仅在该工作区生效。

### 卸载

```bash
~/.gemini/config/plugins/dev-leverage/install.sh --uninstall
```

---

## 组件一览

### 1. 常驻极简规则（`rules/AGENTS.md`）
严控 ≤ 26 行（当前 25 行）：先探清再施工、优先复用与删减、输入输出对账、独立诚实与大白话汇报、四段式交还格式，以及主代理编排与复用子代理边界。

### 2. 确定性生命周期 Hooks（`hooks.json`）
- **首轮上下文初始化（`PreInvocation` → `hooks/session_start.py`）**：仅对主会话首次调用生效（对子代理零 I/O、零注入）。自动探测工作区内的项目记忆入口（`AGENTS.md`、`GEMINI.md`、`.agents/rules/`、`CURRENT_WORK.md`），只注入路径指针，并为工作区 Git 仓库记录初始改动基线。
- **微观返工与上下文体检提示（`PreInvocation` → `hooks/loop_anchor_hint.py`）**：日常短任务与子代理零注入；通过字节偏移增量缓存保持亚毫秒热开销，仅在 `/goal`·`/teamwork`·`/swarm`、单文件反复修补（≥4 次或近 4 提交命中 ≥3 次）、基于实证指标的上下文健康红/黄灯（压缩代数 `G`、512K 压缩水位、精准解析出的非零 Shell 报错数、本代工具密度）或跨多文件大规模扩张（≥8 源码文件）时按阶段触发一次提醒；触发**红灯**或**返工止损**时在 macOS 上同步弹出非阻塞系统横幅直达用户（设 `DEV_LEVERAGE_NOTIFY=0` 可关闭）。
- **收工证据门禁（`Stop` → `hooks/stop_gate.py`）**：仅对主会话生效；当本轮实际编辑了工作区文件却未在回复中附实证/验收摘要时，拦停提醒一次。
- **防裸调 Codex 护栏（`PreToolUse` → `hooks/codex_exec_guard.py`）**：匹配 `run_command`，硬拦截直接裸跑 `codex exec`（放行 `codex_worker.py`、自定义封装脚本、`codex login status` 等），强制走隔离监督通道。

### 3. 确定性脚本（`scripts/`）
- **`scripts/context_tools.py`**：增量会话体检（`health`）与零改写确定性交接导出（`handoff`，默认读取完整 `transcript_full.jsonl`、按 `(line_no, step_index)` 锚定、去 URL query 参数并做密钥脱敏，支持 `--user-only` 为审稿人导出不可伪造的用户原话锚点）。
- **`scripts/diff_baseline.py`**：在脏仓库中隔离本轮改动集与风险分档（支持 `--baseline`、`--range <base>..<head>` 与 `--files`），封堵 `--files` 静默覆盖基线与大新文件静默截断，自动输出「Surface & Risk Summary」与紧凑 `Repo File Index`（仓库文件清单）。
- **`scripts/codex_worker.py`**：本地 Codex 编码执行器（`start` / `watch` / `status` / `resume` / `stop`）。默认构建干净独立 `CODEX_HOME`，持久化 `thread_id` 支持 `resume` 续聊，`watch` 仅按活跃度异常唤醒不盲杀。
- **`scripts/doc_style_check.py`**：扫描 Markdown 文档中的自我辩白/元叙述与套话句式。

### 4. 按需 Skills（`skills/`）
- **`explore-first`**：陌生环境/大重构防过早浇筑协议（只读多视角测绘 → 可抛弃细管直出真成品 → 冻结契约再工程放大；日常小修自动跳过）。
- **`gate-verify`**：基线隔离 diff + 表面风险清单 + 用户原话锚点 + 外循环进出留存核对 + 漏报清单 + 减法证据，输出 1 分钟验收证据卡。
- **`walkthrough`**：支持精炼 Markdown、单文件 HTML，以及按需输出飞书文档（经 `lark-cli`，不可用时自动回退）。
- **`loop-anchor`**：原生优先适配器 + 零改写交接与可证伪落点 + 零历史实证探针 + 微观/战略双重止损。
- **`rsi-retro`**：3 步根因复盘骨架（已压缩会话强制派零历史只读子代理查原始日志；既有规则一票否决；允许输出 `0~1` 条并优先沉淀为项目确定性门禁）。
- **`orchestrate`**：极薄编排选型（单子代理 vs `/teamwork` vs `/swarm` vs `codex_worker.py`）与调研赛马流程。

### 5. 独立只读审稿子代理（`agents/`）
- **`entropy-reviewer`**（`tools: [read_file, run_command]`，严格只读约束）：主力代码与文档审稿人——负责**逻辑/并发/安全与脱敏漏洞、减法与冗余治理、仓规/测试诚实度**，以及**陌生读者视角去黑话与防晕字审查**；修完复核优先用 `send_message` 复用原代理。
- **`drift-watcher`**（`tools: [read_file, run_command]`，严格只读约束）：外部真值与复盘核查员——**Mode A** 用打包只读脚本直查原始日志、外部基线、真实终稿分布与用户原话（抽 3~5 条溯源，查漏报与过度删减）；**Mode B** 负责已压缩长会话的独立复盘（`rsi-retro`）。

---

## 环境变量（可选）

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `DEV_LEVERAGE_COMPACTION_CLIFF` | `512000` | 上下文水位分母（tokens） |
| `DEV_LEVERAGE_NOTIFY` | `1` | 设为 `0` 关闭 macOS 红灯/返工止损系统横幅 |
| `DEV_LEVERAGE_RED_COOLDOWN` | `120` | 红灯提醒冷却步数 |
| `DEV_LEVERAGE_MACRO_FILES` | `8` | 宏观多文件扩张告警阈值 |
| `DEV_LEVERAGE_MACRO_COOLDOWN` | `80` | 宏观扩张提醒冷却步数 |

---

## 测试验证

纯 Python 3 标准库，无需安装任何依赖：

```bash
python3 -I -m unittest discover -s tests -v
```
