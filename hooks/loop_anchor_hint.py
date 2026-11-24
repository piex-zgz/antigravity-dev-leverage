#!/usr/bin/env python3
"""Lightweight conditional PreInvocation hook to wake up `loop-anchor` only when needed.

Zero injection on subagent sessions and normal short turns. Injects a one-shot
pointer to `skills/loop-anchor/SKILL.md` only when one of five concrete signals fires:
  A. `lastUserInput` invokes `/goal`, `/teamwork`, or `/swarm` (once per session).
  B1. Same workspace file edited >= 4 times in recent transcript steps.
  B2. >= 3 of the last 4 git commits (within 6 hours) in a workspace repo touch
      the same non-doc source file (catches cross-subagent `/teamwork` loops).
  C. Empirically validated context health (`context_tools.py` incremental scan:
     compaction generation `G`, context fill vs compaction cliff, parsed non-zero
     shell exits, tool-actions-per-UI ratio) reaches YELLOW or RED.
  D. Macro multi-file expansion (>= 8 distinct tracked files edited in recent
     transcript steps), reminding to check end-to-end output and marginal ROI
     (with step cooldown so later phases can fire again without nagging).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import context_tools  # type: ignore

SKILL_PATH = PLUGIN_ROOT / "skills" / "loop-anchor" / "SKILL.md"
CONTEXT_TOOL_PATH = SCRIPTS_DIR / "context_tools.py"
TAIL_READ_BYTES = 512_000
LONG_TASK_RE = re.compile(r"(?:^|\s)/(goal|teamwork|swarm)\b", re.IGNORECASE)
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
IGNORE_BASENAMES = {"README.md", "AGENTS.md", "GEMINI.md", "CHANGELOG.md", "progress.md", "DEAD_ENDS.md"}


def state_path_for(payload: dict[str, Any]) -> Path:
    conv_id = str(payload.get("conversationId") or "unknown")
    art_dir = payload.get("artifactDirectoryPath")
    base = Path(art_dir) / "scratch" / "dev-leverage" if isinstance(art_dir, str) and art_dir else Path("/tmp/dev-leverage-sessions")
    return base / f"loop-anchor-{conv_id}.json"


def load_state(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(p: Path, data: dict[str, Any]) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def workspace_roots(payload: dict[str, Any]) -> list[Path]:
    raw: list[str] = [wp for wp in (payload.get("workspacePaths") or []) if isinstance(wp, str) and wp]
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd and cwd not in raw:
        raw.append(cwd)
    roots: list[Path] = []
    seen: set[str] = set()
    for r in raw:
        p = Path(r).expanduser()
        if p.is_dir():
            k = str(p.resolve())
            if k not in seen:
                seen.add(k)
                roots.append(p.resolve())
    return roots


def resolve_tracked_code_path(path_str: str, roots: list[Path]) -> str | None:
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception:
        return None
    s = str(p)
    if "/.gemini/" in s or "/scratch/" in s or s.startswith("/tmp/") or p.name in IGNORE_BASENAMES:
        return None
    for r in roots:
        try:
            p.relative_to(r)
            return s
        except ValueError:
            continue
    return None


def _head_has_explicit_user_input(transcript_path: str | None, max_lines: int = 20) -> bool:
    """Fast O(1) subagent guard avoiding full-file scans when a subagent transcript lacks USER_INPUT."""
    if not transcript_path:
        return False
    p = Path(transcript_path)
    if not p.is_file():
        return False
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fp:
            for idx, raw_line in enumerate(fp):
                if idx >= max_lines:
                    break
                if '"USER_INPUT"' in raw_line or '"USER_EXPLICIT"' in raw_line:
                    try:
                        st = json.loads(raw_line)
                        if st.get("type") == "USER_INPUT" or st.get("source") == "USER_EXPLICIT":
                            return True
                    except Exception:
                        pass
    except OSError:
        return False
    return False


def _read_tail_lines(path: Path, max_lines: int = 120, max_bytes: int = TAIL_READ_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fp:
            if size > max_bytes:
                fp.seek(size - max_bytes)
                fp.readline()  # discard partial first line after mid-file seek
            raw = fp.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return lines[-max_lines:]


def inspect_recent_edits(
    transcript_path: str | None, roots: list[Path]
) -> tuple[tuple[str, int] | None, int]:
    """Read tail 120 transcript lines in O(1) -> (single_file_churn, distinct_edited_files)."""
    if not transcript_path or not roots:
        return (None, 0)
    p = Path(transcript_path)
    if not p.is_file():
        return (None, 0)

    recent_list = _read_tail_lines(p, max_lines=120)
    recent_80_offset = max(0, len(recent_list) - 80)
    file_steps: dict[str, set[int]] = {}
    macro_files: set[str] = set()
    for idx, line in enumerate(recent_list):
        if '"tool_calls"' not in line:
            continue
        try:
            st = json.loads(line)
        except Exception:
            continue
        step_id = st.get("step_index")
        step_key = step_id if isinstance(step_id, int) else idx
        for tc in st.get("tool_calls") or []:
            if tc.get("name") not in context_tools.EDIT_TOOLS:
                continue
            args = tc.get("args") or {}
            target = context_tools.parse_arg_path(args.get("Path") or args.get("path"))
            resolved = resolve_tracked_code_path(target, roots) if target else None
            if resolved:
                macro_files.add(resolved)
                if idx >= recent_80_offset:
                    file_steps.setdefault(resolved, set()).add(step_key)

    churn_hit: tuple[str, int] | None = None
    if file_steps:
        top_file, steps = max(file_steps.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if len(steps) >= 4:
            churn_hit = (top_file, len(steps))
    return (churn_hit, len(macro_files))


def detect_transcript_edit_churn(transcript_path: str | None, roots: list[Path]) -> tuple[str, int] | None:
    return inspect_recent_edits(transcript_path, roots)[0]


def find_repos(roots: list[Path], limit: int = 8) -> list[Path]:
    repos: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidates = [root]
        try:
            candidates.extend(
                sorted(c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS)[:16]
            )
        except OSError:
            pass
        for d in candidates:
            if (d / ".git").exists():
                k = str(d)
                if k not in seen:
                    seen.add(k)
                    repos.append(d)
                    if len(repos) >= limit:
                        return repos
    return repos


def load_baseline_heads(payload: dict[str, Any]) -> dict[str, str]:
    """Read initial repo HEAD SHAs recorded by session_start.py for this conversation."""
    conv_id = str(payload.get("conversationId") or "")
    art_dir = payload.get("artifactDirectoryPath")
    if not conv_id or not isinstance(art_dir, str) or not art_dir:
        return {}
    bp = Path(art_dir) / "scratch" / "dev-leverage" / f"baseline-{conv_id}.json"
    if not bp.is_file():
        return {}
    try:
        raw = json.loads(bp.read_text(encoding="utf-8"))
        repos = raw.get("repos") if isinstance(raw, dict) else None
        if isinstance(repos, dict):
            return {
                str(k): str(v.get("head") or "")
                for k, v in repos.items()
                if isinstance(v, dict) and v.get("head")
            }
    except Exception:
        pass
    return {}


def detect_git_commit_churn(
    roots: list[Path],
    baseline_heads: dict[str, str] | None = None,
    min_head_ts: float = 0.0,
) -> tuple[str, str, str, int] | None:
    """Return (repo_name, head_sha, rel_file, hit_count) if >= 3 of last 4 recent commits hit same source file."""
    base_heads = baseline_heads or {}
    for repo in find_repos(roots):
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "log", "-n", "4", "--since=6 hours ago", "--format=COMMIT %H %ct", "--name-only"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        commits: list[tuple[str, int, set[str]]] = []
        cur_sha = ""
        cur_ts = 0
        cur_files: set[str] = set()
        for raw_line in proc.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("COMMIT "):
                if cur_sha:
                    commits.append((cur_sha, cur_ts, cur_files))
                parts = line.split()
                cur_sha = parts[1] if len(parts) >= 2 else ""
                cur_ts = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
                cur_files = set()
            elif cur_sha:
                cur_files.add(line)
        if cur_sha:
            commits.append((cur_sha, cur_ts, cur_files))
        if len(commits) < 3:
            continue
        head_sha, head_ts, head_files = commits[0]
        # Skip pre-session HEADs so newly opened conversations never alert on earlier sessions' commits.
        init_head = base_heads.get(str(repo.resolve())) or base_heads.get(str(repo))
        if init_head and head_sha == init_head:
            continue
        if min_head_ts > 0.0 and head_ts > 0 and head_ts < min_head_ts:
            continue
        file_hits: Counter[str] = Counter()
        for _, _, fset in commits:
            for f in sorted(fset):
                name = Path(f).name
                if name in IGNORE_BASENAMES or f.startswith("tests/") or "/tests/" in f or name.startswith("test_"):
                    continue
                if f in head_files:
                    file_hits[f] += 1
        if not file_hits:
            continue
        # Deterministic tie-break: highest hit count first, then lexicographical path order.
        top_file, hits = min(file_hits.items(), key=lambda kv: (-kv[1], kv[0]))
        if hits >= 3:
            return (repo.name, head_sha[:8], top_file, hits)
    return None


def lookup_conversation_title(conv_id: str) -> str:
    if not conv_id or conv_id == "unknown":
        return "当前会话"
    db = Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"
    if not db.is_file():
        return conv_id[:8]
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            row = conn.execute(
                "SELECT title FROM conversation_summaries WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            if row and isinstance(row[0], str) and row[0].strip():
                return row[0].strip()[:40]
        finally:
            conn.close()
    except Exception:
        pass
    return conv_id[:8]


def _claim_global_notify_key(dedup_key: str) -> bool:
    """Return True if this global notification key has not fired before (prevents multi-session banner spam)."""
    p_str = os.environ.get("DEV_LEVERAGE_NOTIFY_DEDUP")
    p = Path(p_str) if p_str else (Path.home() / ".gemini" / "antigravity" / "scratch" / "dev-leverage" / "notify-dedup.json")
    data = load_state(p)
    seen = data.get("keys")
    if not isinstance(seen, list):
        seen = []
    if dedup_key in seen:
        return False
    seen.append(dedup_key)
    data["keys"] = seen[-128:]
    save_state(p, data)
    return True


def notify_macos(title: str, subtitle: str, body: str, global_dedup_key: str | None = None) -> bool:
    """Fire-and-forget macOS notification banner (zero LLM pollution; opt-out via DEV_LEVERAGE_NOTIFY=0)."""
    if sys.platform != "darwin" or os.environ.get("DEV_LEVERAGE_NOTIFY", "1") == "0":
        return False
    if global_dedup_key and not _claim_global_notify_key(global_dedup_key):
        return False
    try:
        subprocess.Popen(
            [
                "osascript",
                "-e",
                "on run {t, sub, msg}\ndisplay notification msg with title t subtitle sub\nend run",
                "--",
                title,
                subtitle,
                body,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def emit(msg: str) -> int:
    print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}, ensure_ascii=False))
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("{}")
        return 0

    st_path = state_path_for(payload)
    state = load_state(st_path)
    last_input = payload.get("lastUserInput") or ""
    roots = workspace_roots(payload)
    transcript_path = payload.get("transcriptPath")

    # Fast O(1) subagent guard before incremental health scan so long subagents never rescan from byte 0.
    cached_health = state.get("health_cache") if isinstance(state.get("health_cache"), dict) else None
    if not (cached_health and cached_health.get("is_main")):
        if not _head_has_explicit_user_input(transcript_path):
            print("{}")
            return 0
    prev_offset = int(cached_health.get("offset") or 0) if cached_health else 0
    health_rep, new_health_cache = context_tools.inspect_health_incremental(
        transcript_path, cached_health
    )
    if not health_rep["is_main"]:
        print("{}")
        return 0
    state["health_cache"] = new_health_cache
    if not state.get("session_start_ts"):
        state["session_start_ts"] = time.time()
    total_steps = health_rep["total_steps"]

    # Signal A: explicit long-task command in user input (once per conversation)
    if isinstance(last_input, str) and not state.get("prompt_hint_sent"):
        m = LONG_TASK_RE.search(last_input)
        if m:
            state["prompt_hint_sent"] = True
            save_state(st_path, state)
            return emit(
                f"[dev-leverage] 检测到长任务指令 `/{m.group(1)}`：请先用 `read_file` 读取 `{SKILL_PATH}`，"
                "遵循阶段重读与“同一非核心/非安全边界模块连续返工 >= 3 轮即停机上报”止损红线（派发子代理时一并写进 brief）。"
            )

    inv_num = payload.get("invocationNum")
    if isinstance(inv_num, int) and inv_num < 4:
        if new_health_cache.get("offset") != prev_offset:
            save_state(st_path, state)
        print("{}")
        return 0

    edit_hit, distinct_files = inspect_recent_edits(transcript_path, roots) if roots else (None, 0)

    # Signal B1: same file edited >= 4 times in recent transcript steps (per-file bounded bucket map)
    if edit_hit:
        target, count = edit_hit
        bucket = count // 4
        edit_buckets = state.get("edit_buckets")
        if not isinstance(edit_buckets, dict):
            edit_buckets = {}
        if int(edit_buckets.get(target) or 0) < bucket:
            edit_buckets[target] = bucket
            if len(edit_buckets) > 32:
                edit_buckets = dict(list(edit_buckets.items())[-32:])
            state["edit_buckets"] = edit_buckets
            save_state(st_path, state)
            conv_label = lookup_conversation_title(str(payload.get("conversationId") or ""))
            notify_macos(
                "Dev Leverage 返工止损",
                conv_label,
                f"同一文件 {Path(target).name} 近期连续修改 {count} 次，建议人工介入检查",
            )
            return emit(
                f"[dev-leverage Loop Stop-Loss] 检测到最近步骤内同一文件 `{Path(target).name}` 已被连续修改 {count} 次。"
                f"请立即读取 `{SKILL_PATH}` 核查是否已触发“低收益返工止损（>= 3 轮）”，若是请停止自动修补并上报冻结范围。"
            )

    # Signal B2: >= 3 of last 4 recent git commits hit the same source file (throttled to <= once / 60s)
    if roots:
        now = time.time()
        if now - float(state.get("last_git_check_ts") or 0.0) >= 60.0:
            state["last_git_check_ts"] = now
            git_hit = detect_git_commit_churn(
                roots,
                baseline_heads=load_baseline_heads(payload),
                min_head_ts=float(state.get("session_start_ts") or 0.0),
            )
            if git_hit:
                repo_name, head_sha, rel_file, hits = git_hit
                # Key by (repo, head_sha) only so a single HEAD never fires twice even on tied files.
                key = f"git:{repo_name}:{head_sha}"
                if state.get("last_git_key") != key:
                    state["last_git_key"] = key
                    save_state(st_path, state)
                    conv_label = lookup_conversation_title(str(payload.get("conversationId") or ""))
                    notify_macos(
                        "Dev Leverage 提交返工告警",
                        conv_label,
                        f"{repo_name} 近 4 次提交有 {hits} 次改动 {Path(rel_file).name}，建议停机核查",
                        global_dedup_key=key,
                    )
                    return emit(
                        f"[dev-leverage Loop Stop-Loss] 检测到仓库 `{repo_name}` 最近 4 个提交中有 {hits} 个连续修改同一文件 `{rel_file}`（HEAD `{head_sha}`）。"
                        f"请先读取 `{SKILL_PATH}` 核查是否陷入低收益对抗/修补循环，若是请立即停机上报用户决定是否冻结范围。"
                    )
            save_state(st_path, state)

    # Signal C: Empirically validated context health (GREEN silent, YELLOW once/gen, RED step-cooldown + 1 banner/gen)
    h_level = health_rep["level"]
    g = health_rep["compaction_generation"]
    last_hk = str(state.get("last_health_key") or "")
    metrics_brief = (
        f"G={g}, 水位 {int(health_rep['fill_ratio'] * 100)}% ({health_rep['latest_effective_tokens']:,}/{health_rep['compaction_cliff']:,}), "
        f"近 40 步 Shell 报错 {health_rep['recent_cmd_failures']} 次, 本代工具密度 {health_rep['actions_per_ui']}/轮"
    )
    if h_level == "RED":
        red_cooldown = context_tools.env_int("DEV_LEVERAGE_RED_COOLDOWN", 120)
        last_red_step = int(state["last_red_step"]) if state.get("last_red_step") is not None else -red_cooldown
        last_red_gen = state.get("last_red_gen")
        if last_red_gen != g or (total_steps - last_red_step) >= red_cooldown:
            should_notify = last_red_gen != g
            state["last_health_key"] = f"RED:{g}"
            state["last_red_gen"] = g
            state["last_red_step"] = total_steps
            save_state(st_path, state)
            if should_notify:
                conv_label = lookup_conversation_title(str(payload.get("conversationId") or ""))
                notify_macos(
                    "Dev Leverage 上下文红灯",
                    conv_label,
                    f"G={g} · 水位 {int(health_rep['fill_ratio'] * 100)}% · 近 40 步报错 {health_rep['recent_cmd_failures']} 次，建议导出交接包切新会话",
                )
            handoff_cmd = f'python3 "{CONTEXT_TOOL_PATH}" handoff --transcript "{transcript_path}"'
            return emit(
                f"[dev-leverage Context Health RED] 当前会话已进入高噪音/多代压缩状态（{metrics_brief}）。"
                f"推进下一阶段前，建议先运行 `{handoff_cmd}` 导出原话与事实交接包切换干净会话，或派 `drift-watcher` 做零历史实证探针（见 `{SKILL_PATH}`）。"
            )
    elif h_level == "YELLOW":
        alert_key = f"YELLOW:{g}"
        if not last_hk.startswith((f"YELLOW:{g}", f"RED:{g}")):
            state["last_health_key"] = alert_key
            save_state(st_path, state)
            return emit(
                f"[dev-leverage Context Health YELLOW] 当前会话上下文开始变重（{metrics_brief}）。"
                f"若正在推进多文件执行，可按需 `/compact` 或把噪音探查下放给子代理；切换大方向前见 `{SKILL_PATH}`。"
            )

    # Signal D: Macro multi-file expansion in recent steps (cooldown in steps so later phases still alert)
    macro_files = context_tools.env_int("DEV_LEVERAGE_MACRO_FILES", 8)
    macro_cooldown = context_tools.env_int("DEV_LEVERAGE_MACRO_COOLDOWN", 80)
    last_macro_step = int(state.get("last_macro_step") if state.get("last_macro_step") is not None else -macro_cooldown)
    if roots and distinct_files >= macro_files and (total_steps - last_macro_step) >= macro_cooldown:
        state["last_macro_step"] = total_steps
        save_state(st_path, state)
        return emit(
            f"[dev-leverage Macro Annealing] 近期已跨 {distinct_files} 个源码文件扩张改动。"
            f"继续叠加修改前，建议暂停核验真实端到端成品与进出留存核对，并确认边际收益未见顶（见 `{SKILL_PATH}`）。"
        )

    if new_health_cache.get("offset") != prev_offset:
        save_state(st_path, state)
    print("{}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)
