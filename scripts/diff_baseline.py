#!/usr/bin/env python3
"""Deterministic git baseline snapshot & delta reporter for dirty repositories.

Usage:
  diff_baseline.py snapshot --repo <repo_dir> --out <baseline.json>
  diff_baseline.py report --repo <repo_dir> [--baseline <baseline.json>] \
      [--files f1 f2 ...] [--write-diff <diff.patch>] [--write-report <report.md>] \
      [--format json|markdown]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

UNTRACKED_DIFF_CAP = 60_000
LARGE_FILE_JUMP_LOC = 150
NON_CODE_SURFACE_EXTS = {".md", ".rst", ".txt", ".adoc", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".svg", ".html"}

TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|fixtures)/|(?:^|/)(?:test_[^/]+|[^/]+_test\.[^/]+|[^/]+\.(?:test|spec)\.[^/]+|conftest\.py)$",
    re.IGNORECASE,
)
SENSITIVE_CONFIG_RE = re.compile(
    r"(?:^|/)(?:pytest\.ini|pyproject\.toml|package\.json|go\.mod|Cargo\.toml|hooks\.json|AGENTS\.md|GEMINI\.md)$|(?:^|/)(?:hooks|\.agents)/",
    re.IGNORECASE,
)
ASSERT_LINE_RE = re.compile(r"\b(?:assert[A-Z]\w*|assert|expect|pytest\.raises)\b")
TEST_SKIP_RE = re.compile(r"(?:pytest\.mark\.(?:skip|xfail)|pytest\.xfail\s*\(|@unittest\.skip|skipTest\s*\(|\.(?:skip|todo)\s*\()")
SURFACE_CLI_RE = re.compile(r"""add_(?:argument|parser)\s*\(\s*["']([^"']+)["']""")
SURFACE_DEF_RE = re.compile(
    r"^(?:(?:async\s+)?def\s+([a-zA-Z][a-zA-Z0-9_]*)\s*\(|class\s+([A-Z][a-zA-Z0-9_]*)\b|export\s+(?:async\s+)?(?:function|class|const|interface|type)\s+([a-zA-Z0-9_]+)|func\s+([A-Z][a-zA-Z0-9_]*)\s*\()"
)
SURFACE_ENV_RE = re.compile(r"""(?:os\.environ\.get|os\.getenv)\s*\(\s*["']([A-Z0-9_]+)["']|process\.env\.([A-Z0-9_]+)""")


def run_git(repo: Path, args: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        return proc.returncode, proc.stdout
    except Exception:
        return 1, ""


def is_git_repo(path: Path) -> bool:
    code, out = run_git(path, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "UNREADABLE"


def parse_porcelain(repo: Path) -> dict[str, str]:
    """Return {rel_path: XY_status} from git status --porcelain."""
    code, out = run_git(repo, ["status", "--porcelain", "-uall"])
    if code != 0:
        return {}
    entries: dict[str, str] = {}
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        if raw_path.startswith('"') and raw_path.endswith('"'):
            raw_path = raw_path[1:-1]
        entries[raw_path] = xy
    return entries


def staged_blob_map(repo: Path) -> dict[str, str]:
    """Single git call returning {rel_path: status:staged_blob_sha}."""
    code, out = run_git(repo, ["diff", "--cached", "--raw", "-z"])
    if code != 0 or not out:
        return {}
    result: dict[str, str] = {}
    parts = out.split("\x00")
    i = 0
    while i < len(parts):
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        fields = meta.split()
        new_sha = fields[3] if len(fields) >= 4 else ""
        status = fields[4] if len(fields) >= 5 else ""
        if status.startswith(("R", "C")) and i + 2 < len(parts):
            path = parts[i + 2]
            i += 3
        elif i + 1 < len(parts):
            path = parts[i + 1]
            i += 2
        else:
            break
        result[path] = f"{status}:{new_sha}"
    return result


def file_fingerprint(
    repo: Path, rel_path: str, xy: str, staged_blobs: dict[str, str]
) -> dict[str, str]:
    full = repo / rel_path
    work_hash = sha256_file(full) if full.is_file() else "MISSING"
    return {
        "status": xy,
        "work_hash": work_hash,
        "staged": staged_blobs.get(rel_path, ""),
    }


def capture_snapshot(repo: Path, max_files: int = 1000) -> dict[str, Any]:
    repo = repo.resolve()
    _, head = run_git(repo, ["rev-parse", "HEAD"])
    _, branch = run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    porcelain = parse_porcelain(repo)
    if len(porcelain) > max_files:
        return {
            "repo": str(repo),
            "head": head.strip(),
            "branch": branch.strip(),
            "dirty_count": len(porcelain),
            "truncated": True,
            "files": {},
        }
    staged_blobs = staged_blob_map(repo)
    files: dict[str, dict[str, str]] = {}
    for rel, xy in sorted(porcelain.items()):
        files[rel] = file_fingerprint(repo, rel, xy, staged_blobs)
    return {
        "repo": str(repo),
        "head": head.strip(),
        "branch": branch.strip(),
        "dirty_count": len(files),
        "truncated": False,
        "files": files,
    }


def committed_files_since(repo: Path, base_head: str, cur_head: str) -> list[str]:
    if not base_head or not cur_head or base_head == cur_head:
        return []
    code, out = run_git(repo, ["diff", "--name-only", f"{base_head}..{cur_head}"])
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def numstat_for_files(
    repo: Path, rel_paths: list[str], porcelain: dict[str, str], diff_base_ref: str
) -> dict[str, tuple[int, int]]:
    """Compute exact (added, deleted) lines against diff_base_ref without double-counting staged+unstaged."""
    stats: dict[str, tuple[int, int]] = {}
    tracked = [p for p in rel_paths if porcelain.get(p) != "??"]
    if tracked:
        code, out = run_git(repo, ["diff", diff_base_ref, "--numstat", "--", *tracked])
        if code == 0:
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    add_s, del_s, p = parts
                    add_n = int(add_s) if add_s.isdigit() else 0
                    del_n = int(del_s) if del_s.isdigit() else 0
                    stats[p] = (add_n, del_n)
    for p in rel_paths:
        if porcelain.get(p) == "??":
            full = repo / p
            if full.is_file():
                try:
                    lines = len(full.read_text(errors="replace").splitlines())
                except OSError:
                    lines = 0
                stats[p] = (lines, 0)
            else:
                stats[p] = (0, 0)
        elif p not in stats:
            stats[p] = (0, 0)
    return stats


def is_test_path(rel_path: str) -> bool:
    return bool(TEST_PATH_RE.search(rel_path.replace("\\", "/")))


def is_sensitive_path(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    return bool(TEST_PATH_RE.search(norm) or SENSITIVE_CONFIG_RE.search(norm))


def collect_scoped_diff(
    repo: Path, rel_paths: list[str], porcelain: dict[str, str], diff_base_ref: str
) -> tuple[str, list[dict[str, Any]]]:
    chunks: list[str] = []
    truncated_untracked: list[dict[str, Any]] = []
    tracked = [p for p in rel_paths if porcelain.get(p) != "??"]
    if tracked:
        _, diff_out = run_git(repo, ["diff", diff_base_ref, "--", *tracked])
        if diff_out.strip():
            chunks.append(f"# === Diff against {diff_base_ref} ===\n" + diff_out)
    for p in rel_paths:
        if porcelain.get(p) == "??":
            full = repo / p
            if full.is_file():
                try:
                    raw_body = full.read_text(errors="replace")
                    total_bytes = len(raw_body.encode("utf-8", errors="replace"))
                    total_lines = len(raw_body.splitlines())
                    if len(raw_body) > UNTRACKED_DIFF_CAP:
                        body = (
                            raw_body[:UNTRACKED_DIFF_CAP]
                            + f"\n... [TRUNCATED: showed {UNTRACKED_DIFF_CAP} chars of {total_bytes} bytes / {total_lines} lines — read full file directly]"
                        )
                        truncated_untracked.append(
                            {
                                "path": p,
                                "shown_chars": UNTRACKED_DIFF_CAP,
                                "total_bytes": total_bytes,
                                "total_lines": total_lines,
                            }
                        )
                    else:
                        body = raw_body
                except OSError:
                    body = "[unreadable file]"
                chunks.append(f"# === Untracked File: {p} ===\n{body}\n")
    return "\n".join(chunks), truncated_untracked


def analyze_surface_risks(
    repo: Path,
    changed: list[str],
    stats: dict[str, tuple[int, int]],
    porcelain: dict[str, str],
    diff_base_ref: str,
    diff_text: str,
    committed_during_session: list[str],
) -> dict[str, Any]:
    """Zero-config, language-agnostic surface & risk summary from the scoped diff."""
    added_tracked: set[str] = set()
    if changed and (committed_during_session or any("A" in porcelain.get(p, "") for p in changed)):
        code, out = run_git(repo, ["diff", "--diff-filter=A", "--name-only", diff_base_ref, "--", *changed])
        if code == 0:
            added_tracked = {line.strip() for line in out.splitlines() if line.strip()}

    new_files: list[dict[str, Any]] = []
    large_file_jumps: list[dict[str, Any]] = []
    sensitive_paths: list[str] = []
    for p in changed:
        add_n, del_n = stats.get(p, (0, 0))
        if porcelain.get(p) == "??" or "A" in porcelain.get(p, "") or p in added_tracked:
            new_files.append({"path": p, "added": add_n})
        if add_n >= LARGE_FILE_JUMP_LOC:
            large_file_jumps.append({"path": p, "added": add_n, "deleted": del_n})
        if is_sensitive_path(p):
            sensitive_paths.append(p)

    # Single-pass scan over diff_text (covers both unified git diff hunks and untracked new files).
    test_assert_delta: dict[str, list[int]] = {}  # path -> [added_asserts, deleted_asserts, added_skips]
    new_surface: list[str] = []
    seen_surface: set[str] = set()
    cur_file = ""
    in_untracked = False

    def _is_comment_or_blank(text: str) -> bool:
        stripped = text.strip()
        return not stripped or stripped.startswith(("#", "//", "/*", "*"))

    def _scan_added_line(rel: str, text: str) -> None:
        if _is_comment_or_blank(text):
            return
        if is_test_path(rel):
            entry = test_assert_delta.setdefault(rel, [0, 0, 0])
            if ASSERT_LINE_RE.search(text):
                entry[0] += 1
            if TEST_SKIP_RE.search(text):
                entry[2] += 1
            return
        if Path(rel).suffix.lower() in NON_CODE_SURFACE_EXTS:
            return
        for m in SURFACE_CLI_RE.finditer(text):
            tag = f"{rel}: cli `{m.group(1)}`"
            if tag not in seen_surface:
                seen_surface.add(tag)
                new_surface.append(tag)
        m_def = SURFACE_DEF_RE.match(text)
        if m_def:
            sym = next((g for g in m_def.groups() if g), "")
            if sym and not sym.startswith("_"):
                tag = f"{rel}: `{sym}`"
                if tag not in seen_surface:
                    seen_surface.add(tag)
                    new_surface.append(tag)
        for m_env in SURFACE_ENV_RE.finditer(text):
            env_k = m_env.group(1) or m_env.group(2)
            if env_k:
                tag = f"{rel}: env `{env_k}`"
                if tag not in seen_surface:
                    seen_surface.add(tag)
                    new_surface.append(tag)

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("# === Untracked File: ") and raw_line.endswith(" ==="):
            in_untracked = True
            cur_file = raw_line[len("# === Untracked File: ") : -len(" ===")].strip()
            continue
        if not in_untracked and raw_line.startswith("diff --git "):
            parts = raw_line.split(" b/", 1)
            if len(parts) == 2:
                cur_file = parts[1].strip().strip('"')
            else:
                m_q = re.search(r'"b/([^"]+)"$', raw_line)
                cur_file = m_q.group(1) if m_q else ""
            continue
        if not cur_file:
            continue
        if in_untracked:
            _scan_added_line(cur_file, raw_line)
            continue
        if raw_line.startswith("+++ ") or raw_line.startswith("--- ") or raw_line.startswith("@@ "):
            continue
        if raw_line.startswith("+"):
            _scan_added_line(cur_file, raw_line[1:])
        elif raw_line.startswith("-") and is_test_path(cur_file):
            del_text = raw_line[1:]
            if not _is_comment_or_blank(del_text) and ASSERT_LINE_RE.search(del_text):
                test_assert_delta.setdefault(cur_file, [0, 0, 0])[1] += 1

    # Flag test files ONLY when net assertions dropped or skip/xfail was added (avoids refactor false positives).
    test_integrity_flags: list[dict[str, Any]] = []
    for rel, (add_a, del_a, add_skip) in sorted(test_assert_delta.items()):
        if add_a < del_a or add_skip > 0:
            test_integrity_flags.append(
                {
                    "path": rel,
                    "added_asserts": add_a,
                    "deleted_asserts": del_a,
                    "added_skips": add_skip,
                }
            )

    return {
        "new_files": new_files,
        "large_file_jumps": large_file_jumps,
        "sensitive_paths_touched": sensitive_paths,
        "test_integrity_flags": test_integrity_flags,
        "new_surface_count": len(new_surface),
        "new_surface_samples": new_surface[:20],
    }


def _compute_baseline_delta(
    repo: Path,
    baseline_path: Path,
    cur_head: str,
    current_porcelain: dict[str, str],
) -> tuple[bool, list[str], int, list[str], str, list[str]]:
    """Return (valid, changed, preexisting_excluded, overlap_files, diff_base_ref, committed_during_session)."""
    raw_base = json.loads(baseline_path.read_text(encoding="utf-8"))
    if "repos" in raw_base:
        base = raw_base["repos"].get(str(repo))
    else:
        base = raw_base if raw_base.get("repo") == str(repo) else None

    if base is None or base.get("truncated"):
        return (False, sorted(current_porcelain.keys()), 0, [], "HEAD", [])

    diff_base_ref = "HEAD"
    committed_during_session: list[str] = []
    base_head = str(base.get("head") or "")
    if base_head and cur_head and base_head != cur_head:
        diff_base_ref = base_head
        committed_during_session = committed_files_since(repo, base_head, cur_head)

    base_files: dict[str, dict[str, str]] = base.get("files", {})
    staged_blobs = staged_blob_map(repo)
    changed_set: set[str] = set(committed_during_session)
    overlap_files: list[str] = []
    preexisting_excluded = 0
    for rel, xy in sorted(current_porcelain.items()):
        if rel not in base_files:
            changed_set.add(rel)
        else:
            cur_fp = file_fingerprint(repo, rel, xy, staged_blobs)
            if cur_fp != base_files[rel]:
                changed_set.add(rel)
                overlap_files.append(rel)
            else:
                preexisting_excluded += 1
    for rel in sorted(base_files.keys()):
        if rel not in current_porcelain and rel not in committed_during_session:
            changed_set.add(rel)
    return (True, sorted(changed_set), preexisting_excluded, overlap_files, diff_base_ref, committed_during_session)


def compute_risk_and_walkthrough(
    changed_files: list[str], total_added: int, total_deleted: int
) -> tuple[str, str, int]:
    modules = {Path(p).parts[0] if len(Path(p).parts) > 1 else "." for p in changed_files}
    total_churn = total_added + total_deleted
    n_files = len(changed_files)
    n_modules = len(modules)

    walkthrough_mode = "html" if (n_files >= 8 or total_churn >= 300 or n_modules >= 3) else "markdown"

    if n_files >= 8 or total_churn >= 400 or n_modules >= 3:
        risk_tier = "High"
    elif n_files >= 3 or total_churn >= 80:
        risk_tier = "Medium"
    else:
        risk_tier = "Low"

    return risk_tier, walkthrough_mode, n_modules


def build_report(
    repo: Path,
    baseline_path: Path | None,
    explicit_files: list[str] | None,
    write_diff: Path | None,
) -> dict[str, Any]:
    repo = repo.resolve()
    if not is_git_repo(repo):
        return {
            "repo": str(repo),
            "is_git_repo": False,
            "isolation_mode": "non_git",
            "note": "Target directory is not a git repository; pass explicit file paths for review.",
            "changed_files": explicit_files or [],
        }

    _, cur_head_raw = run_git(repo, ["rev-parse", "HEAD"])
    cur_head = cur_head_raw.strip()
    current_porcelain = parse_porcelain(repo)
    preexisting_excluded = 0
    overlap_files: list[str] = []
    diff_base_ref = "HEAD"
    committed_during_session: list[str] = []
    excluded_baseline_files: list[str] = []
    excluded_sensitive_files: list[str] = []

    if explicit_files:
        isolation_mode = "explicit_files"
        norm_files: list[str] = []
        for f in explicit_files:
            fp = Path(f)
            candidate = fp.resolve() if fp.is_absolute() else (repo / fp).resolve()
            try:
                norm_files.append(str(candidate.relative_to(repo)))
            except ValueError:
                norm_files.append(os.path.normpath(f))
        changed = sorted(set(norm_files))
        baseline_applied = False
        if baseline_path and baseline_path.is_file():
            valid, base_changed, pre_ex, ov, base_ref, comm = _compute_baseline_delta(
                repo, baseline_path, cur_head, current_porcelain
            )
            if valid:
                baseline_applied = True
                diff_base_ref = base_ref
                committed_during_session = comm
                preexisting_excluded = pre_ex
                overlap_files = [p for p in ov if p in changed]
                excluded_baseline_files = sorted(set(base_changed) - set(changed))
                if excluded_baseline_files:
                    isolation_mode = "explicit_files_over_baseline"
                    excluded_sensitive_files = [p for p in excluded_baseline_files if is_sensitive_path(p)]
        if not baseline_applied:
            other_dirty = sorted(set(current_porcelain.keys()) - set(changed))
            preexisting_excluded = len(other_dirty)
            excluded_sensitive_files = [p for p in other_dirty if is_sensitive_path(p)]
    elif baseline_path and baseline_path.is_file():
        valid, changed, preexisting_excluded, overlap_files, diff_base_ref, committed_during_session = (
            _compute_baseline_delta(repo, baseline_path, cur_head, current_porcelain)
        )
        isolation_mode = "baseline_delta" if valid else "full_dirty_unisolated"
    else:
        isolation_mode = "full_dirty_unisolated"
        changed = sorted(current_porcelain.keys())

    stats = numstat_for_files(repo, changed, current_porcelain, diff_base_ref)
    total_added = sum(a for a, _ in stats.values())
    total_deleted = sum(d for _, d in stats.values())
    risk_tier, walkthrough_mode, n_modules = compute_risk_and_walkthrough(
        changed, total_added, total_deleted
    )

    diff_text, truncated_untracked = (
        collect_scoped_diff(repo, changed, current_porcelain, diff_base_ref) if changed else ("", [])
    )
    surface_risks = analyze_surface_risks(
        repo, changed, stats, current_porcelain, diff_base_ref, diff_text, committed_during_session
    )

    diff_out_str = None
    if write_diff is not None:
        write_diff.parent.mkdir(parents=True, exist_ok=True)
        write_diff.write_text(diff_text or "# (no scoped diff)\n", encoding="utf-8")
        diff_out_str = str(write_diff)

    return {
        "repo": str(repo),
        "is_git_repo": True,
        "isolation_mode": isolation_mode,
        "diff_base_ref": diff_base_ref,
        "committed_files_in_session": len(committed_during_session),
        "preexisting_dirty_excluded": preexisting_excluded,
        "preexisting_dirty_edited_again": overlap_files,
        "excluded_baseline_files": excluded_baseline_files,
        "excluded_sensitive_files": excluded_sensitive_files,
        "truncated_untracked_files": truncated_untracked,
        "total_repo_dirty": len(current_porcelain),
        "scoped_file_count": len(changed),
        "scoped_module_count": n_modules,
        "lines_added": total_added,
        "lines_deleted": total_deleted,
        "risk_tier": risk_tier,
        "recommended_walkthrough": walkthrough_mode,
        "diff_file": diff_out_str,
        "surface_risks": surface_risks,
        "changed_files": [
            {
                "path": p,
                "status": current_porcelain.get(p, "COMMITTED" if p in committed_during_session else "CLEAN/REVERTED"),
                "added": stats.get(p, (0, 0))[0],
                "deleted": stats.get(p, (0, 0))[1],
            }
            for p in changed
        ],
    }


def format_markdown(rep: dict[str, Any]) -> str:
    if not rep.get("is_git_repo", True):
        return f"- Repo: `{rep['repo']}` (`non_git`)\n- Note: {rep.get('note', '')}"
    lines = [
        f"### Scoped Diff Baseline Report (`{Path(rep['repo']).name}`)",
        f"- **Isolation Mode**: `{rep['isolation_mode']}` (base ref `{rep['diff_base_ref']}`, excluded {rep['preexisting_dirty_excluded']} pre-existing dirty files out of {rep['total_repo_dirty']})",
        f"- **Scoped Scale**: {rep['scoped_file_count']} files ({rep['committed_files_in_session']} committed in session), {rep['scoped_module_count']} modules, +{rep['lines_added']} / -{rep['lines_deleted']} lines (total churn {rep['lines_added'] + rep['lines_deleted']})",
        f"- **Risk Tier**: `{rep['risk_tier']}` | **Recommended Walkthrough**: `{rep['recommended_walkthrough']}`",
    ]
    def _join_capped(items: list[str], limit: int) -> str:
        text = ", ".join(items[:limit])
        if len(items) > limit:
            text += f" (+{len(items) - limit} more)"
        return text

    if rep.get("excluded_baseline_files"):
        lines.append(
            f"- **WARNING (`--files` omitted {len(rep['excluded_baseline_files'])} baseline-changed files)**: "
            + _join_capped([f"`{p}`" for p in rep["excluded_baseline_files"]], 15)
        )
    if rep.get("excluded_sensitive_files"):
        lines.append(
            "- **WARNING (excluded dirty test/config/hook files)**: "
            + _join_capped([f"`{p}`" for p in rep["excluded_sensitive_files"]], 10)
        )
    if rep.get("truncated_untracked_files"):
        trunc_desc = ", ".join(
            f"`{t['path']}` ({t['shown_chars']}c/{t['total_bytes']}B, {t['total_lines']}L)"
            for t in rep["truncated_untracked_files"]
        )
        lines.append(f"- **WARNING (untracked files truncated in diff — read directly)**: {trunc_desc}")
    if rep.get("preexisting_dirty_edited_again"):
        lines.append(
            "- **Overlap Caveat**: Pre-existing dirty files further edited this session (diff reflects full file delta against base ref): "
            + _join_capped([f"`{p}`" for p in rep["preexisting_dirty_edited_again"]], 10)
        )
    if rep.get("diff_file"):
        lines.append(f"- **Scoped Diff File**: `{rep['diff_file']}`")

    sr = rep.get("surface_risks") or {}
    sr_lines: list[str] = []
    if sr.get("new_files"):
        sr_lines.append(
            "- **New Files**: " + _join_capped([f"`{x['path']}` (+{x['added']})" for x in sr["new_files"]], 15)
        )
    if sr.get("large_file_jumps"):
        sr_lines.append(
            "- **Large File Jumps (>=150 +LOC)**: "
            + _join_capped([f"`{x['path']}` (+{x['added']}/-{x['deleted']})" for x in sr["large_file_jumps"]], 10)
        )
    if sr.get("sensitive_paths_touched"):
        sr_lines.append(
            "- **Tests / Config / Hooks Touched**: "
            + _join_capped([f"`{p}`" for p in sr["sensitive_paths_touched"]], 15)
        )
    if sr.get("test_integrity_flags"):
        sr_lines.append(
            "- **Test Integrity Alerts (net assert drop or skip/xfail added)**: "
            + ", ".join(
                f"`{t['path']}` (+{t['added_asserts']}/-{t['deleted_asserts']} asserts, +{t['added_skips']} skip)"
                for t in sr["test_integrity_flags"]
            )
        )
    if sr.get("new_surface_samples"):
        shown = sr["new_surface_samples"][:12]
        extra = sr["new_surface_count"] - len(shown)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        sr_lines.append(
            f"- **New Public/CLI/Env Surface ({sr['new_surface_count']})**: "
            + "; ".join(shown)
            + suffix
        )
    if sr_lines:
        lines.append("\n#### Surface & Risk Summary")
        lines.extend(sr_lines)

    if rep["changed_files"]:
        lines.append("\n| File | Status | + | - |\n| :--- | :--- | ---: | ---: |")
        for item in rep["changed_files"][:30]:
            lines.append(
                f"| `{item['path']}` | `{item['status']}` | {item['added']} | {item['deleted']} |"
            )
        if len(rep["changed_files"]) > 30:
            lines.append(f"| ... (+{len(rep['changed_files']) - 30} more) | | | |")
    else:
        lines.append("- **Scoped Changes**: None")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scoped git baseline helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Capture initial dirty-state baseline")
    p_snap.add_argument("--repo", required=True)
    p_snap.add_argument("--out", required=True)

    p_rep = sub.add_parser("report", help="Compare current repo state against baseline")
    p_rep.add_argument("--repo", required=True)
    p_rep.add_argument("--baseline", default=None)
    p_rep.add_argument("--files", nargs="*", default=None)
    p_rep.add_argument("--write-diff", default=None)
    p_rep.add_argument("--write-report", default=None)
    p_rep.add_argument("--format", choices=["json", "markdown"], default="markdown")

    args = parser.parse_args()
    if args.cmd == "snapshot":
        repo = Path(args.repo)
        if not is_git_repo(repo):
            print(json.dumps({"ok": False, "reason": "not_a_git_repo"}))
            return 0
        snap = capture_snapshot(repo)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "out": str(out), "dirty_count": snap["dirty_count"]}))
        return 0

    rep = build_report(
        repo=Path(args.repo),
        baseline_path=Path(args.baseline) if args.baseline else None,
        explicit_files=args.files,
        write_diff=Path(args.write_diff) if args.write_diff else None,
    )
    rendered = json.dumps(rep, ensure_ascii=False, indent=2) if args.format == "json" else format_markdown(rep)
    if args.write_report:
        rp = Path(args.write_report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
