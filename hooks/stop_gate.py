#!/usr/bin/env python3
"""Stop hook for dev-leverage.

Surgical end-of-turn Gate check (main sessions only):
- Never blocks errors, user cancels, subagent sessions, or non-idle turns.
- Never loops: checks `continuationActive` and stops after at most one reminder per turn.
- Triggers ONLY if THIS turn actually edited/wrote files inside `workspacePaths` AND the
  assistant's final response lacks a concise verification / evidence summary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EVIDENCE_MARKERS = (
    "验收证据卡",
    "Gate 检查明细",
    "Verification Card",
    "实证依据",
    "验证方式",
    "验证结果",
    "测试通过",
    "已验证",
    "未验证",
    "BLOCKED",
    "PASS WITH NOTES",
    "实跑",
    "退出码",
    "exit code",
)
EDIT_TOOL_NAMES = {"edit_file", "write_file"}


def parse_arg_path(raw_val: Any) -> str | None:
    if not isinstance(raw_val, str) or not raw_val:
        return None
    s = raw_val.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            s = json.loads(s)
        except Exception:
            s = s[1:-1]
    return s


def is_workspace_file(path_str: str, workspace_roots: list[Path]) -> bool:
    try:
        p = Path(path_str).expanduser().resolve()
    except Exception:
        return False
    s = str(p)
    if "/.gemini/" in s or "/scratch/" in s or s.startswith("/tmp/"):
        return False
    for root in workspace_roots:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def analyze_transcript(transcript_path: Path, workspace_roots: list[Path]) -> tuple[bool, list[str], str]:
    """Return (is_main_session, edited_workspace_files_in_turn, last_assistant_text)."""
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, [], ""

    steps: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            steps.append(json.loads(line))
        except Exception:
            continue

    if not steps:
        return False, [], ""

    last_user_idx = -1
    for i, st in enumerate(steps):
        if st.get("source") == "USER_EXPLICIT" or st.get("type") == "USER_INPUT":
            last_user_idx = i

    if last_user_idx < 0:
        return False, [], ""

    edited: list[str] = []
    last_text = ""
    for st in steps[last_user_idx + 1 :]:
        if st.get("type") != "PLANNER_RESPONSE":
            continue
        content = st.get("content")
        if isinstance(content, str) and content.strip():
            last_text = content
        for tc in st.get("tool_calls") or []:
            if tc.get("name", "") not in EDIT_TOOL_NAMES:
                continue
            args = tc.get("args") or {}
            target = parse_arg_path(args.get("Path") or args.get("path"))
            if target and is_workspace_file(target, workspace_roots):
                if target not in edited:
                    edited.append(target)

    return True, edited, last_text


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("{}")
        return 0

    if payload.get("continuationActive"):
        print("{}")
        return 0
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        print("{}")
        return 0
    if payload.get("fullyIdle") is False:
        print("{}")
        return 0

    tpath = payload.get("transcriptPath")
    if not isinstance(tpath, str) or not tpath:
        print("{}")
        return 0

    roots: list[Path] = []
    for wp in payload.get("workspacePaths") or []:
        if isinstance(wp, str) and wp:
            p = Path(wp).expanduser()
            if p.is_dir():
                roots.append(p.resolve())
    if not roots:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd and Path(cwd).is_dir():
            roots.append(Path(cwd).resolve())

    if not roots:
        print("{}")
        return 0

    is_main, edited, last_text = analyze_transcript(Path(tpath), roots)
    if not is_main or not edited:
        print("{}")
        return 0

    if any(marker in last_text for marker in EVIDENCE_MARKERS):
        print("{}")
        return 0

    shown = ", ".join(Path(f).name for f in edited[:5])
    if len(edited) > 5:
        shown += f" 等共 {len(edited)} 个文件"

    reason = (
        f"[dev-leverage 交付提醒] 本轮修改了工作区文件（{shown}），但回复里还没写验证结果。"
        "请核验本次改动，用大白话说清四件事（结论 / 验证结果 / 复用了什么与删了什么 / 缺口与待决），底层参数和路径放括号或文末，再结束本轮。"
    )
    print(json.dumps({"decision": "continue", "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)
