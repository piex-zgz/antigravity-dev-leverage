#!/usr/bin/env python3
"""Deterministic Antigravity transcript health checker and Channel-A handoff exporter.

Zero third-party dependencies. Built strictly on empirically validated transcript signals:
  - `health`: incremental byte-offset scan tracking compaction count `G`, latest effective
    context tokens (`input_tokens + cache_read_tokens`) vs compaction cliff, parsed shell
    non-zero exit failures (fixing `status == "DONE"` blind spot), and tool-actions-per-UI.
  - `handoff`: pure deterministic Channel-A extraction from `transcript_full.jsonl` (when
    available), anchored by `(line_no, step_index)`, with query-stripped URLs and secret
    redaction before clipping or writing.

Usage:
  context_tools.py health --transcript <path> [--state <json>] [--json]
  context_tools.py handoff --transcript <path> [--repo <dir> ...] [--out <file>] [--user-only] [--stdout] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_COMPACTION_CLIFF = 512_000
RECENT_WINDOW = 40
EDIT_TOOLS = {"edit_file", "write_file"}

STRIP_META_RE = re.compile(
    r"<(?:ADDITIONAL_METADATA|ORCHESTRATOR|CONTEXT_SUMMARY)>.*?</(?:ADDITIONAL_METADATA|ORCHESTRATOR|CONTEXT_SUMMARY)>",
    re.DOTALL,
)
# Anchor to Antigravity's real wire header at char 0 (handles normal + truncated outputs + trailing period)
# so exit-0 commands printing foreign logs or read_file on source code cannot false-positive.
GENERIC_CMD_FAIL_RE = re.compile(
    r"\A(?:Created At: [^\r\n]*\r?\nCompleted At: [^\r\n]*\r?\n)?"
    r"(?:The tool's output was truncated[^\r\n]*\r?\n\r?\n"
    r"Here is a preview with the middle of the output truncated:\r?\n)?"
    r"(?:Created At: [^\r\n]*\r?\nCompleted At: [^\r\n]*\r?\n)*"
    r"Command exited with code ([1-9]\d*)\.?(?:\r?\n|$)"
)
BG_TASK_FAIL_RE = re.compile(
    r'Task id "[^"]+/task-\d+" finished with result:\r?\n'
    r"(?:The tool's output was truncated[^\r\n]*\r?\n\r?\n"
    r"Here is a preview with the middle of the output truncated:\r?\n)?"
    r"Command exited with code ([1-9]\d*)\.?(?:\r?\n|$)"
)
URL_RE = re.compile(r"""https?://[^\s"'<>\\`\]\)}）》，。；：、]+""")

NOISY_URL_HOSTS = {
    "vertexaisearch.cloud.google.com",
    "daily-cloudcode-pa.googleapis.com",
    "www.w3.org",
    "schemas.openxmlformats.org",
    "example.com",
}

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), "[REDACTED_JWT]"),
    (
        re.compile(
            r"""(?i)(["']?(?:disposable_login_token|access_token|refresh_token|id_token|csrf_token|api[_-]?key|client_secret|flow_id|user_code|password|passwd|secret|ticket|token)["']?\s*[:=]\s*)(["']?)[^\s&"'`,;)}\]]+\2"""
        ),
        r"\1\2[REDACTED]\2",
    ),
    (re.compile(r"(?i)\b(Bearer|Token|Basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{12,}|AKIA[0-9A-Z]{16})\b"), "[REDACTED_KEY]"),
]


def env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name) or "")
        return v if v > 0 else default
    except ValueError:
        return default


def _clean_netloc(parts: Any) -> str:
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port is not None else host


def _scrub_url_inline(match: re.Match[str]) -> str:
    raw = match.group(0)
    stripped = raw.rstrip(".,;:!?)\\]}\"'")
    trailing = raw[len(stripped) :]
    try:
        parts = urlsplit(stripped)
    except Exception:
        return "[REDACTED_URL]" + trailing
    if not parts.hostname:
        return raw
    # Strip userinfo@ and all query/fragment parameters so unlisted signed URL tokens cannot leak.
    return urlunsplit((parts.scheme, _clean_netloc(parts), parts.path, "", "")) + trailing


def redact_secrets(text: str) -> str:
    out = URL_RE.sub(_scrub_url_inline, text)
    for pat, repl in SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def parse_arg_path(val: Any) -> str | None:
    if not isinstance(val, str) or not val.strip():
        return None
    s = val.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            s = json.loads(s)
        except Exception:
            s = s[1:-1]
    return s if isinstance(s, str) and s.strip() else None


def resolve_full_transcript(path: Path) -> Path:
    if path.name == "transcript.jsonl":
        full = path.with_name("transcript_full.jsonl")
        if full.is_file():
            return full
    return path


def extract_user_request(raw_content: str) -> str:
    start_tag = "<USER_REQUEST>"
    end_tag = "</USER_REQUEST>"
    i = raw_content.find(start_tag)
    j = raw_content.rfind(end_tag)
    if i != -1 and j != -1 and j > i:
        return raw_content[i + len(start_tag) : j].strip()
    return STRIP_META_RE.sub("", raw_content).strip()


def sanitize_url(raw_url: str) -> str | None:
    cleaned = raw_url.rstrip(".,;:!?)\\]}\"'")
    try:
        parts = urlsplit(cleaned)
    except Exception:
        return None
    host = (parts.hostname or "").lower()
    if not host or host in NOISY_URL_HOSTS or host.endswith(".googleapis.com"):
        return None
    return urlunsplit((parts.scheme, _clean_netloc(parts), parts.path, "", ""))


def extract_clean_urls(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in URL_RE.finditer(text):
        u = sanitize_url(m.group(0))
        if u and u not in seen:
            seen.add(u)
            found.append(u)
    return found


def _fresh_health_state(path: str, inode: int) -> dict[str, Any]:
    return {
        "path": path,
        "inode": inode,
        "offset": 0,
        "is_main": False,
        "total_steps": 0,
        "first_step_index": None,
        "checkpoints": 0,
        "latest_effective_tokens": 0,
        "peak_effective_tokens": 0,
        "latest_model": "",
        "gen_steps": 0,
        "gen_user_turns": 0,
        "gen_tool_actions": 0,
        "total_cmd_failures": 0,
        "recent_cmd_fails": [],
    }


def _should_parse_health_line(line: str, is_main: bool, first_step_seen: bool) -> bool:
    if not first_step_seen:
        return True
    if not is_main and ('"USER_INPUT"' in line or '"USER_EXPLICIT"' in line):
        return True
    return (
        '"CHECKPOINT"' in line
        or '"USER_INPUT"' in line
        or '"PLANNER_RESPONSE"' in line
        or "Command exited with code " in line
    )


def inspect_health_incremental(
    transcript_path: str | Path | None,
    cache_state: dict[str, Any] | None = None,
    compaction_cliff: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Incrementally scan transcript.jsonl via byte offset -> (health_report, updated_cache)."""
    cliff = compaction_cliff or env_int("DEV_LEVERAGE_COMPACTION_CLIFF", DEFAULT_COMPACTION_CLIFF)
    if not transcript_path:
        empty = _fresh_health_state("", 0)
        return _build_health_report(empty, cliff), empty
    p = Path(transcript_path)
    if not p.is_file():
        empty = _fresh_health_state(str(p), 0)
        return _build_health_report(empty, cliff), empty

    try:
        st_info = p.stat()
    except OSError:
        empty = _fresh_health_state(str(p), 0)
        return _build_health_report(empty, cliff), empty

    resolved = str(p.resolve())
    inode = int(st_info.st_ino)
    file_size = int(st_info.st_size)

    if (
        isinstance(cache_state, dict)
        and cache_state.get("path") == resolved
        and cache_state.get("inode") == inode
        and isinstance(cache_state.get("offset"), int)
        and 0 <= cache_state["offset"] <= file_size
    ):
        state = dict(cache_state)
        state["recent_cmd_fails"] = list(cache_state.get("recent_cmd_fails") or [])
    else:
        state = _fresh_health_state(resolved, inode)

    recent_fails: list[int] = state["recent_cmd_fails"]
    offset = int(state["offset"])

    try:
        with p.open("rb") as fp:
            if offset > 0:
                fp.seek(offset)
            while True:
                raw_bytes = fp.readline()
                if not raw_bytes:
                    break
                # Stop before a partial uncommitted line at EOF so next turn resumes cleanly.
                if not raw_bytes.endswith(b"\n"):
                    break
                offset += len(raw_bytes)
                line = raw_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                state["total_steps"] += 1
                state["gen_steps"] += 1
                step_failed = 0

                if _should_parse_health_line(line, bool(state["is_main"]), state["first_step_index"] is not None):
                    try:
                        obj = json.loads(line)
                    except Exception:
                        obj = None
                    if isinstance(obj, dict):
                        if state["first_step_index"] is None and isinstance(obj.get("step_index"), int):
                            state["first_step_index"] = obj["step_index"]
                        st_type = obj.get("type")
                        st_src = obj.get("source")
                        if st_type == "USER_INPUT" or st_src == "USER_EXPLICIT":
                            state["is_main"] = True
                        if st_type == "USER_INPUT":
                            state["gen_user_turns"] += 1
                        elif st_type == "CHECKPOINT":
                            state["checkpoints"] += 1
                            state["latest_effective_tokens"] = 0
                            state["gen_steps"] = 0
                            state["gen_user_turns"] = 0
                            state["gen_tool_actions"] = 0
                            recent_fails.clear()
                        elif st_type == "PLANNER_RESPONSE":
                            tcs = obj.get("tool_calls") or []
                            if isinstance(tcs, list):
                                state["gen_tool_actions"] += len(tcs)
                            in_tok = obj.get("input_tokens")
                            cache_tok = obj.get("cache_read_tokens")
                            if isinstance(in_tok, int) or isinstance(cache_tok, int):
                                eff = (in_tok if isinstance(in_tok, int) else 0) + (
                                    cache_tok if isinstance(cache_tok, int) else 0
                                )
                                if eff > 0:
                                    state["latest_effective_tokens"] = eff
                                    if eff > state["peak_effective_tokens"]:
                                        state["peak_effective_tokens"] = eff
                            model = obj.get("model")
                            if isinstance(model, str) and model:
                                state["latest_model"] = model
                        elif st_type == "GENERIC":
                            content = obj.get("content") or ""
                            if isinstance(content, str) and GENERIC_CMD_FAIL_RE.search(content):
                                step_failed = 1
                                state["total_cmd_failures"] += 1
                        elif st_type == "SYSTEM_MESSAGE":
                            content = obj.get("content") or ""
                            if isinstance(content, str) and BG_TASK_FAIL_RE.search(content):
                                step_failed = 1
                                state["total_cmd_failures"] += 1

                recent_fails.append(step_failed)
                if len(recent_fails) > RECENT_WINDOW:
                    del recent_fails[: len(recent_fails) - RECENT_WINDOW]
    except OSError:
        pass

    state["offset"] = offset
    state["recent_cmd_fails"] = recent_fails
    return _build_health_report(state, cliff), state


def _build_health_report(state: dict[str, Any], cliff: int) -> dict[str, Any]:
    g = int(state.get("checkpoints") or 0)
    eff = int(state.get("latest_effective_tokens") or 0)
    peak = int(state.get("peak_effective_tokens") or 0)
    first_idx = state.get("first_step_index")
    is_forked = isinstance(first_idx, int) and first_idx > 0
    gen_ui = int(state.get("gen_user_turns") or 0)
    gen_actions = int(state.get("gen_tool_actions") or 0)
    actions_per_ui = round(gen_actions / max(1, gen_ui), 1)
    recent_fails = sum(state.get("recent_cmd_fails") or [])
    fill_ratio = round(eff / cliff, 3) if cliff > 0 else 0.0

    reasons: list[str] = []
    if (
        (g >= 2 and (fill_ratio >= 0.70 or recent_fails >= 4 or (fill_ratio >= 0.45 and actions_per_ui >= 40.0)))
        or (g >= 1 and recent_fails >= 6)
        or (fill_ratio >= 0.85 and (recent_fails >= 4 or actions_per_ui >= 25.0))
    ):
        level = "RED"
        if g >= 2:
            reasons.append(f"G={g} multi-compaction")
        if fill_ratio >= 0.70:
            reasons.append(f"context fill {int(fill_ratio * 100)}% ({eff:,}/{cliff:,})")
        if (g >= 2 and fill_ratio >= 0.45 and actions_per_ui >= 40.0) or (fill_ratio >= 0.85 and actions_per_ui >= 25.0):
            reasons.append(f"high tool density ({actions_per_ui} actions/turn)")
        if recent_fails >= 4:
            reasons.append(f"{recent_fails} shell failures in last {RECENT_WINDOW} steps")
    elif (
        g >= 2
        or fill_ratio >= 0.75
        or (is_forked and fill_ratio >= 0.55)
        or (g >= 1 and actions_per_ui >= 20.0)
        or recent_fails >= 5
    ):
        level = "YELLOW"
        if g >= 2:
            reasons.append(f"G={g} (early details compressed multiple times)")
        if fill_ratio >= 0.75 or (is_forked and fill_ratio >= 0.55):
            reasons.append(f"context fill {int(fill_ratio * 100)}% ({eff:,}/{cliff:,})")
        if g >= 1 and actions_per_ui >= 20.0:
            reasons.append(f"high tool density ({actions_per_ui} actions/turn)")
        if recent_fails >= 5:
            reasons.append(f"{recent_fails} shell failures in last {RECENT_WINDOW} steps")
    else:
        level = "GREEN"

    return {
        "level": level,
        "reasons": reasons,
        "is_main": bool(state.get("is_main")),
        "is_forked": is_forked,
        "first_step_index": first_idx,
        "total_steps": int(state.get("total_steps") or 0),
        "compaction_generation": g,
        "latest_effective_tokens": eff,
        "peak_effective_tokens": peak,
        "compaction_cliff": cliff,
        "fill_ratio": fill_ratio,
        "latest_model": str(state.get("latest_model") or ""),
        "gen_steps": int(state.get("gen_steps") or 0),
        "gen_user_turns": gen_ui,
        "gen_tool_actions": gen_actions,
        "actions_per_ui": actions_per_ui,
        "recent_cmd_failures": recent_fails,
        "total_cmd_failures": int(state.get("total_cmd_failures") or 0),
    }


def format_health_markdown(rep: dict[str, Any]) -> str:
    fork_tag = f" (forked from step {rep['first_step_index']})" if rep["is_forked"] else ""
    lines = [
        f"### Context Health: `{rep['level']}`{fork_tag}",
        f"- **Compaction Generation (`G`)**: `{rep['compaction_generation']}` | **Total Steps**: {rep['total_steps']} (current gen: {rep['gen_steps']})",
        f"- **Context Fill**: {rep['latest_effective_tokens']:,} / {rep['compaction_cliff']:,} ({int(rep['fill_ratio'] * 100)}%, peak {rep['peak_effective_tokens']:,}, model `{rep['latest_model'] or 'unknown'}`)",
        f"- **Execution Density (current gen)**: {rep['gen_tool_actions']} tool actions / {rep['gen_user_turns']} user turns (`{rep['actions_per_ui']}` actions/turn)",
        f"- **Parsed Shell Failures**: {rep['recent_cmd_failures']} in last {RECENT_WINDOW} steps (session total: {rep['total_cmd_failures']})",
    ]
    if rep["reasons"]:
        lines.append("- **Triggers**: " + "; ".join(rep["reasons"]))
    return "\n".join(lines)


def _is_inside_git_repo(path: Path) -> bool:
    check_dir = path.parent if not path.is_dir() else path
    while not check_dir.exists() and check_dir.parent != check_dir:
        check_dir = check_dir.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(check_dir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except Exception:
        return False


def _git_repo_summary(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    out: dict[str, Any] = {"repo": str(repo), "status": [], "recent_commits": []}
    try:
        p1 = subprocess.run(
            ["git", "-C", str(repo), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if p1.returncode == 0:
            out["status"] = [line for line in p1.stdout.splitlines() if line.strip()][:40]
        p2 = subprocess.run(
            ["git", "-C", str(repo), "log", "-n", "5", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if p2.returncode == 0:
            out["recent_commits"] = [line for line in p2.stdout.splitlines() if line.strip()]
    except Exception:
        pass
    return out


def default_handoff_path(transcript_path: Path, user_only: bool = False) -> Path:
    resolved = transcript_path.resolve()
    fname = "handoff-user-only.md" if user_only else "handoff.md"
    # Expected layout: <brain>/<conv-id>/.system_generated/logs/transcript*.jsonl
    if resolved.parent.name == "logs" and resolved.parent.parent.name == ".system_generated":
        return resolved.parent.parent.parent / "scratch" / fname
    return Path("/tmp/dev-leverage-handoffs") / f"{resolved.stem}-{fname}"


def export_handoff(
    transcript_path: Path,
    repos: list[Path] | None = None,
    user_only: bool = False,
    max_user_chars: int = 800,
) -> str:
    """Pure Channel-A deterministic export (0 LLM rewriting, secret-redacted before clipping)."""
    src = resolve_full_transcript(transcript_path)
    user_entries: list[dict[str, Any]] = []
    checkpoints: list[dict[str, Any]] = []
    file_edits: Counter[str] = Counter()
    urls: list[str] = []
    seen_urls: set[str] = set()
    subagent_count = 0
    total_lines = 0
    last_step_index: int | None = None
    last_user_sig: tuple[Any, str] | None = None

    with src.open("r", encoding="utf-8", errors="replace") as fp:
        for line_no, raw_line in enumerate(fp, start=1):
            line = raw_line.strip()
            if not line:
                continue
            total_lines = line_no
            # Fast substring prefilter skips multi-MB GENERIC tool-output lines that handoff never reads.
            if user_only:
                if '"USER_INPUT"' not in line:
                    continue
            elif '"USER_INPUT"' not in line and '"CHECKPOINT"' not in line and '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            step_idx = obj.get("step_index")
            if isinstance(step_idx, int):
                last_step_index = step_idx
            st_type = obj.get("type")
            content = obj.get("content") or ""
            created_at = str(obj.get("created_at") or "")[:19].replace("T", " ")

            if st_type == "USER_INPUT" and isinstance(content, str):
                req = redact_secrets(extract_user_request(content))
                if not req:
                    continue
                sig = (step_idx, req)
                if sig == last_user_sig:
                    continue
                # If the same step_index was edited/retried in place with an extended text, replace prior draft.
                if (
                    user_entries
                    and step_idx is not None
                    and user_entries[-1]["step_index"] == step_idx
                    and req.startswith(user_entries[-1]["raw_prefix"])
                ):
                    user_entries.pop()
                last_user_sig = sig
                for u in extract_clean_urls(req):
                    if u not in seen_urls:
                        seen_urls.add(u)
                        urls.append(u)
                clipped = req if len(req) <= max_user_chars else req[:max_user_chars].rstrip() + " ... [clipped]"
                user_entries.append(
                    {
                        "line_no": line_no,
                        "step_index": step_idx,
                        "created_at": created_at,
                        "text": clipped,
                        "raw_prefix": req[:200],
                    }
                )
            elif st_type == "CHECKPOINT" and not user_only:
                checkpoints.append({"line_no": line_no, "step_index": step_idx, "created_at": created_at})
            elif st_type == "PLANNER_RESPONSE" and not user_only:
                if isinstance(content, str) and content:
                    for u in extract_clean_urls(content):
                        if u not in seen_urls:
                            seen_urls.add(u)
                            urls.append(u)
                for tc in obj.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name")
                    if name == "invoke_subagent":
                        subagent_count += 1
                    if name in EDIT_TOOLS:
                        args = tc.get("args") or {}
                        p_str = parse_arg_path(args.get("Path") or args.get("path"))
                        if p_str and "/.gemini/antigravity/brain/" not in p_str and "/scratch/" not in p_str and not p_str.startswith("/tmp/"):
                            file_edits[p_str] += 1

    lines: list[str] = []
    if user_only:
        lines.append("# Verbatim User Intent Timeline (Raw Extract)")
        lines.append(f"- **Source**: `{src}` ({total_lines} lines, {len(user_entries)} user turns)")
        lines.append("")
        for entry in user_entries:
            flat = " ".join(entry["text"].splitlines())
            lines.append(f"- `(L{entry['line_no']}, step={entry['step_index']}, {entry['created_at']})` {flat}")
        return "\n".join(lines) + "\n"

    health_rep, _ = inspect_health_incremental(src)

    lines.append("# Session Handoff (Raw Transcript Extract — Zero LLM Rewriting)")
    lines.append("")
    lines.append(f"- **Full Transcript Pointer**: `{src}` (lines `1..{total_lines}`, last `step_index={last_step_index}`)")
    lines.append(
        f"- **Session Health**: `{health_rep['level']}` | `G={health_rep['compaction_generation']}` | "
        f"Context `{health_rep['latest_effective_tokens']:,}/{health_rep['compaction_cliff']:,}` | "
        f"Shell fails `{health_rep['recent_cmd_failures']}` (recent {RECENT_WINDOW}) / `{health_rep['total_cmd_failures']}` total"
    )
    if subagent_count:
        lines.append(f"- **Subagents Invoked**: {subagent_count} (check repo `git status`/`log` below for subagent edits)")

    sec = 1
    lines.append(f"\n## {sec}. Verbatim User Requests & Corrections (`<USER_REQUEST>` only)")
    if not user_entries:
        lines.append("- (none)")
    else:
        for entry in user_entries:
            flat = " ".join(entry["text"].splitlines())
            lines.append(f"- `(L{entry['line_no']}, step={entry['step_index']}, {entry['created_at']})` {flat}")

    sec += 1
    lines.append(f"\n## {sec}. Compaction History Index")
    if not checkpoints:
        lines.append(f"- `G0`: lines `1..{total_lines}` (no compaction)")
    else:
        prev_line = 1
        for idx, cp in enumerate(checkpoints):
            lines.append(
                f"- `G{idx}`: lines `{prev_line}..{cp['line_no'] - 1}` → **CHECKPOINT** at `(L{cp['line_no']}, step={cp['step_index']}, {cp['created_at']})`"
            )
            prev_line = cp["line_no"] + 1
        if prev_line <= total_lines:
            lines.append(f"- `G{len(checkpoints)}` (current): lines `{prev_line}..{total_lines}`")

    sec += 1
    lines.append(f"\n## {sec}. Touched Workspace Files (Direct Tool Edits)")
    if not file_edits:
        lines.append("- (no direct workspace `edit_file`/`write_file` edits recorded outside scratch)")
    else:
        for fpath, cnt in file_edits.most_common(30):
            lines.append(f"- `{fpath}` ({cnt} edits)")
        if len(file_edits) > 30:
            lines.append(f"- ... (+{len(file_edits) - 30} more files)")

    if repos:
        sec += 1
        lines.append(f"\n## {sec}. Current Git Ground Truth")
        for r in repos:
            summary = _git_repo_summary(r)
            lines.append(f"### Repo `{summary['repo']}`")
            if summary["status"]:
                lines.append("- **Working Tree**:")
                for s in summary["status"]:
                    lines.append(f"  - `{s}`")
            else:
                lines.append("- **Working Tree**: clean")
            if summary["recent_commits"]:
                lines.append("- **Recent Commits**:")
                for c in summary["recent_commits"]:
                    lines.append(f"  - `{c}`")

    if urls:
        sec += 1
        lines.append(f"\n## {sec}. Referenced External Links (Query Strings Stripped)")
        for u in urls[:25]:
            lines.append(f"- {u}")
        if len(urls) > 25:
            lines.append(f"- ... (+{len(urls) - 25} more)")

    sec += 1
    lines.append(f"\n## {sec}. New-Session First-Turn Checklist")
    lines.append(
        "Read this raw handoff and inspect the current repo files directly (read exact lines from "
        f"`{src}` only if a specific past detail is ambiguous). Before making broad edits, propose:\n"
        "1. **Exact next concrete diff/plan** (target files and change);\n"
        "2. **1 explicit non-goal** (what you will NOT build or change);\n"
        "3. **1 real-sample verification target** to prove the direction works."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity context health and Channel-A handoff tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_health = sub.add_parser("health", help="Compute empirically validated session context health")
    p_health.add_argument("--transcript", required=True, help="Path to transcript.jsonl")
    p_health.add_argument("--state", default=None, help="Optional incremental cache JSON path")
    p_health.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")

    p_handoff = sub.add_parser("handoff", help="Export deterministic Channel-A handoff card")
    p_handoff.add_argument("--transcript", required=True, help="Path to transcript.jsonl or transcript_full.jsonl")
    p_handoff.add_argument("--repo", action="append", default=[], help="Workspace git repo to include status/commits")
    p_handoff.add_argument("--out", default=None, help="Output file path (defaults to conversation scratch/handoff.md)")
    p_handoff.add_argument("--user-only", action="store_true", help="Emit only verbatim <USER_REQUEST> timeline")
    p_handoff.add_argument("--stdout", action="store_true", help="Print output to stdout instead of writing file")
    p_handoff.add_argument("--force", action="store_true", help="Allow writing inside a git repository")

    args = parser.parse_args()

    if args.cmd == "health":
        cache: dict[str, Any] | None = None
        state_path = Path(args.state) if args.state else None
        if state_path and state_path.is_file():
            try:
                cache = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                cache = None
        rep, new_cache = inspect_health_incremental(args.transcript, cache)
        if state_path:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(new_cache, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.as_json else format_health_markdown(rep))
        return 0

    tr_path = Path(args.transcript)
    if not tr_path.is_file():
        print(f"error: transcript not found: {tr_path}", file=sys.stderr)
        return 1

    repos = [Path(r) for r in args.repo if r]
    rendered = export_handoff(tr_path, repos=repos, user_only=args.user_only)
    if args.stdout:
        print(rendered, end="")
        return 0

    out_path = Path(args.out) if args.out else default_handoff_path(tr_path, user_only=args.user_only)
    if _is_inside_git_repo(out_path) and not args.force:
        print(
            f"error: refusing to write transcript handoff inside a git repo ({out_path}); "
            "use a scratch path or pass --force",
            file=sys.stderr,
        )
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out_path), "bytes": len(rendered.encode("utf-8"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
