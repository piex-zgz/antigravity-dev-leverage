#!/usr/bin/env python3
"""PreToolUse guard blocking bare `codex exec` shell invocations.

Forces local Codex coding runs through `scripts/codex_worker.py` (clean isolated
CODEX_HOME, session resume support, and non-destructive liveness inspection) while
allowing wrapper runners (`codex_worker.py`, custom dispatchers), `codex login status`, etc.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WORKER_SCRIPT = PLUGIN_ROOT / "scripts" / "codex_worker.py"
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
WRAPPER_CMDS = {"env", "nohup", "command", "exec", "time"}
# Only consider shell commands before heredoc bodies so quoted docs/commits never false-trigger.
HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z0-9_]+)['\"]?")


def _strip_heredocs(cmd: str) -> str:
    lines = cmd.splitlines()
    kept: list[str] = []
    delimiter: str | None = None
    for line in lines:
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        kept.append(line)
        m = HEREDOC_START_RE.search(line)
        if m:
            delimiter = m.group(1)
    return "\n".join(kept)


def _split_segments(cmd: str) -> list[list[str]]:
    lexer = shlex.shlex(_strip_heredocs(cmd), posix=True, punctuation_chars=";&|\n")
    lexer.whitespace_split = True
    segments: list[list[str]] = []
    current: list[str] = []
    try:
        for tok in lexer:
            if tok in {";", "&&", "||", "|", "&", "\n"}:
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(tok)
    except ValueError:
        # Malformed shell quoting: never block blindly on parse error.
        return []
    if current:
        segments.append(current)
    return segments


def _segment_invokes_bare_codex_exec(tokens: list[str]) -> bool:
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        base = Path(tok).name
        if base in WRAPPER_CMDS:
            idx += 1
            continue
        if base == "timeout" and idx + 1 < n:
            idx += 2
            continue
        break
    if idx >= n:
        return False
    exe = Path(tokens[idx]).name
    if exe != "codex":
        return False
    # Allow `codex exec --help` / `codex --version` / `codex login status`.
    rest = tokens[idx + 1 :]
    if "--help" in rest or "-h" in rest:
        return False
    for tok in rest:
        if tok.startswith("-"):
            continue
        return tok == "exec"
    return False


def is_bare_codex_exec(command_line: str) -> bool:
    if not command_line or "codex" not in command_line:
        return False
    for seg in _split_segments(command_line):
        if _segment_invokes_bare_codex_exec(seg):
            return True
    return False


def main() -> int:
    try:
        payload: dict[str, Any] = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"decision": "allow"}))
        return 0

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict) or tool_call.get("name") != "run_command":
        print(json.dumps({"decision": "allow"}))
        return 0

    args = tool_call.get("args") or {}
    cmd_line = str(args.get("CommandLine") or args.get("commandLine") or "")
    if is_bare_codex_exec(cmd_line):
        reason = (
            "Blocked bare `codex exec` to prevent ~/.codex hook/auto_review pollution and "
            "unsupervised stalls. Use the deterministic worker wrapper instead:\n"
            f"  python3 {WORKER_SCRIPT} start --workdir <DIR> --worker-id <ID> --prompt-file <FILE>\n"
            f"  python3 {WORKER_SCRIPT} watch --worker-id <ID>\n"
            "(Add `--with-user-config` only when you explicitly need user ~/.codex MCPs/plugins.)"
        )
        print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
        return 0

    print(json.dumps({"decision": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
