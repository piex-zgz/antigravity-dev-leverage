#!/usr/bin/env python3
"""One-shot main-session startup hook (registered on PreInvocation).

Why PreInvocation instead of SessionStart:
At SessionStart, transcript.jsonl does not yet contain step 0, so subagents cannot be
reliably distinguished from main user sessions. On PreInvocation, step 0 is already
present (`USER_EXPLICIT`/`USER_INPUT` for main sessions vs `SYSTEM`/`SYSTEM_MESSAGE`
for subagents). Using a per-conversation baseline file guarantees this runs at most
once per main session and does zero git I/O for subagents.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

try:
    import diff_baseline  # type: ignore
except Exception:
    diff_baseline = None  # type: ignore

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "target",
    ".next",
}
MAX_REPOS = 20


def has_explicit_user_input(transcript_path: str | None) -> bool:
    if not transcript_path:
        return False
    p = Path(transcript_path)
    if not p.is_file():
        return False
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:10]:
            if not line.strip():
                continue
            step = json.loads(line)
            if step.get("source") == "USER_EXPLICIT" or step.get("type") == "USER_INPUT":
                return True
        return False
    except Exception:
        return False


def discover_roots(payload: dict[str, Any]) -> list[Path]:
    raw: list[str] = []
    for wp in payload.get("workspacePaths") or []:
        if isinstance(wp, str) and wp:
            raw.append(wp)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd and cwd not in raw:
        raw.append(cwd)
    roots: list[Path] = []
    seen: set[str] = set()
    for r in raw:
        p = Path(r).expanduser()
        if p.is_dir():
            key = str(p.resolve())
            if key not in seen:
                seen.add(key)
                roots.append(p.resolve())
    return roots


def find_named_files_bounded(
    base: Path, target_name: str, max_depth: int = 5, limit: int = 2, max_dirs: int = 200
) -> list[Path]:
    hits: list[Path] = []
    base_depth = len(base.parts)
    visited = 0
    for cur_root, dirs, files in os.walk(base):
        visited += 1
        if visited > max_dirs:
            break
        cur = Path(cur_root)
        depth = len(cur.parts) - base_depth
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")) if depth < max_depth else []
        if target_name in files:
            hits.append(cur / target_name)
            if len(hits) >= limit:
                return hits
    return hits


def find_memory_pointers_and_repos(roots: list[Path]) -> tuple[list[str], list[Path], int]:
    pointers: list[str] = []
    repos: list[Path] = []
    seen_ptr: set[str] = set()
    seen_repo: set[str] = set()

    def add_ptr(p: Path) -> None:
        if p.is_file():
            s = str(p)
            if s not in seen_ptr:
                seen_ptr.add(s)
                pointers.append(s)

    def maybe_add_repo(d: Path) -> None:
        if (d / ".git").exists():
            s = str(d)
            if s not in seen_repo:
                seen_repo.add(s)
                repos.append(d)

    for root in roots:
        maybe_add_repo(root)
        add_ptr(root / "AGENTS.md")
        add_ptr(root / "GEMINI.md")
        rules_dir = root / ".agents" / "rules"
        if rules_dir.is_dir():
            for md in sorted(rules_dir.glob("*.md"))[:5]:
                add_ptr(md)

        try:
            children = sorted(
                c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS
            )
        except OSError:
            children = []
        for child in children[:24]:
            is_repo = (child / ".git").exists()
            if is_repo:
                maybe_add_repo(child)
            if (child / "AGENTS.md").is_file():
                add_ptr(child / "AGENTS.md")
            if is_repo:
                for cw in find_named_files_bounded(child, "CURRENT_WORK.md", max_depth=5, limit=2):
                    add_ptr(cw)

    truncated_repos = max(0, len(repos) - MAX_REPOS)
    return pointers[:12], repos[:MAX_REPOS], truncated_repos


def marker_and_baseline_paths(payload: dict[str, Any]) -> tuple[Path, Path]:
    conv_id = str(payload.get("conversationId") or "unknown")
    art_dir = payload.get("artifactDirectoryPath")
    if isinstance(art_dir, str) and art_dir:
        base_dir = Path(art_dir) / "scratch" / "dev-leverage"
    else:
        base_dir = Path("/tmp/dev-leverage-sessions")
    return base_dir / f"started-{conv_id}.flag", base_dir / f"baseline-{conv_id}.json"


def snapshot_repos(out_path: Path, conv_id: str, repos: list[Path]) -> tuple[str | None, list[str]]:
    if not repos or diff_baseline is None:
        return None, []
    bundle: dict[str, Any] = {"conversationId": conv_id, "repos": {}}
    summaries: list[str] = []
    for repo in repos:
        try:
            snap = diff_baseline.capture_snapshot(repo, max_files=1000)
            bundle["repos"][str(repo)] = snap
            if snap.get("truncated"):
                summaries.append(f"{repo.name} ({snap['branch']}, dirty={snap['dirty_count']} [snapshot skipped >1000])")
            else:
                summaries.append(f"{repo.name} ({snap['branch']}, pre-existing dirty={snap['dirty_count']})")
        except Exception:
            continue
    if not bundle["repos"]:
        return None, []
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(out_path), summaries
    except OSError:
        return None, []


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        print("{}")
        return 0

    # Fast exit after first invocation
    inv_num = payload.get("invocationNum")
    if isinstance(inv_num, int) and inv_num > 1:
        print("{}")
        return 0

    # Strict subagent guard: only run in main conversations with explicit user input
    if not has_explicit_user_input(payload.get("transcriptPath")):
        print("{}")
        return 0

    marker_path, baseline_out = marker_and_baseline_paths(payload)
    if marker_path.is_file():
        print("{}")
        return 0

    # Mark initialized immediately so later invocations never repeat
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("ok\n", encoding="utf-8")
    except OSError:
        pass

    roots = discover_roots(payload)
    if not roots:
        print("{}")
        return 0

    pointers, repos, truncated_repos = find_memory_pointers_and_repos(roots)
    conv_id = str(payload.get("conversationId") or "unknown")
    baseline_path, repo_summaries = snapshot_repos(baseline_out, conv_id, repos)

    if not pointers and not baseline_path:
        print("{}")
        return 0

    lines = ["[dev-leverage Context] Workspace pointers (paths only; read relevant ones before modifying code):"]
    if pointers:
        lines.append("- Project memory / rules: " + ", ".join(pointers))
    if baseline_path:
        lines.append(f"- Git baseline snapshot: {baseline_path}")
        if repo_summaries:
            suffix = f" (+{truncated_repos} more truncated)" if truncated_repos else ""
            lines.append("- Detected repos: " + "; ".join(repo_summaries) + suffix)
        lines.append(
            f"- Tip: Before gate-verify / walkthrough / entropy-reviewer, run `python3 {PLUGIN_ROOT}/scripts/diff_baseline.py report --repo <repo> --baseline {baseline_path} --write-diff <scratch>/scoped.diff --write-report <scratch>/report.md`."
        )

    out = {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "\n".join(lines),
                }
            }
        ]
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("{}")
        sys.exit(0)
