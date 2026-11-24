#!/usr/bin/env python3
"""Offline unit tests for scripts/diff_baseline.py and scripts/context_tools.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import context_tools
import diff_baseline


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert 1 == 1\n    assert 2 == 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)


class DiffBaselineHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_git_repo(self.repo)

    def test_explicit_files_cannot_silently_mask_baseline_and_sensitive_edits(self) -> None:
        base_file = self.root / "baseline.json"
        snap = diff_baseline.capture_snapshot(self.repo)
        base_file.write_text(json.dumps(snap), encoding="utf-8")

        (self.repo / "src" / "app.py").write_text(
            "def existing():\n    return 2\n\ndef new_public_api():\n    return 3\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_app.py").write_text(
            "import pytest\n@pytest.mark.skip('flaky')\ndef test_ok():\n    assert 1 == 1\n",
            encoding="utf-8",
        )

        # Passing --files with only src/app.py must still surface excluded baseline test edit.
        rep = diff_baseline.build_report(
            self.repo,
            baseline_path=base_file,
            explicit_files=["src/app.py"],
            write_diff=self.root / "scoped.diff",
        )
        self.assertEqual(rep["isolation_mode"], "explicit_files_over_baseline")
        self.assertEqual(rep["excluded_baseline_files"], ["tests/test_app.py"])
        self.assertEqual(rep["excluded_sensitive_files"], ["tests/test_app.py"])
        md = diff_baseline.format_markdown(rep)
        self.assertIn("WARNING (`--files` omitted 1 baseline-changed files)", md)
        self.assertIn("tests/test_app.py", md)

    def test_surface_risks_flags_net_assert_drop_and_skip_without_refactor_fp(self) -> None:
        base_file = self.root / "baseline.json"
        base_file.write_text(json.dumps(diff_baseline.capture_snapshot(self.repo)), encoding="utf-8")

        # Refactoring tests (removing 2 asserts, adding 3 asserts) must NOT trigger test_integrity_flags.
        (self.repo / "tests" / "test_app.py").write_text(
            "def test_refactored():\n    assert 1 == 1\n    assert 2 == 2\n    assert 3 == 3\n",
            encoding="utf-8",
        )
        rep_clean = diff_baseline.build_report(self.repo, base_file, None, None)
        self.assertEqual(rep_clean["surface_risks"]["sensitive_paths_touched"], ["tests/test_app.py"])
        self.assertEqual(rep_clean["surface_risks"]["test_integrity_flags"], [])

        # Dropping assertions net-negative + adding new public/CLI surface must trigger cleanly.
        (self.repo / "tests" / "test_app.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
        (self.repo / "src" / "extra.py").write_text(
            'import argparse, os\n'
            'def run_extra():\n'
            '    p = argparse.ArgumentParser()\n'
            '    p.add_argument("--secret-mode")\n'
            '    return os.getenv("EXTRA_FLAG")\n',
            encoding="utf-8",
        )
        rep_alert = diff_baseline.build_report(self.repo, base_file, None, None)
        sr = rep_alert["surface_risks"]
        self.assertEqual(len(sr["test_integrity_flags"]), 1)
        self.assertEqual(sr["test_integrity_flags"][0]["path"], "tests/test_app.py")
        self.assertEqual(sr["new_files"], [{"path": "src/extra.py", "added": 5}])
        samples_text = " ".join(sr["new_surface_samples"])
        self.assertIn("cli `--secret-mode`", samples_text)
        self.assertIn("`run_extra`", samples_text)
        self.assertIn("env `EXTRA_FLAG`", samples_text)

    def test_large_untracked_file_truncation_is_explicitly_reported(self) -> None:
        big = self.repo / "src" / "huge.py"
        big.write_text("x = 1\n" * 12000, encoding="utf-8")  # 72,000 bytes > 60,000 cap
        rep = diff_baseline.build_report(self.repo, None, None, self.root / "big.diff")
        self.assertEqual(len(rep["truncated_untracked_files"]), 1)
        self.assertEqual(rep["truncated_untracked_files"][0]["path"], "src/huge.py")
        md = diff_baseline.format_markdown(rep)
        self.assertIn("WARNING (untracked files truncated in diff", md)
        self.assertIn("Large File Jumps", md)

    def test_commented_out_assert_invalid_baseline_fallback_and_doc_filtering(self) -> None:
        # Commenting out an assertion must still flag test_integrity_flags (net assert drop).
        (self.repo / "tests" / "test_app.py").write_text(
            "def test_ok():\n    assert 1 == 1\n    # assert 2 == 2\n",
            encoding="utf-8",
        )
        (self.repo / "docs.md").write_text("```python\ndef fake_doc_func(): pass\n```\n", encoding="utf-8")
        rep = diff_baseline.build_report(self.repo, None, None, None)
        sr = rep["surface_risks"]
        self.assertEqual(len(sr["test_integrity_flags"]), 1)
        self.assertEqual(sr["test_integrity_flags"][0]["deleted_asserts"], 1)
        self.assertEqual(sr["test_integrity_flags"][0]["added_asserts"], 0)
        # Markdown docs must not pollute new_surface_samples.
        self.assertEqual(sr["new_surface_samples"], [])

        # Invalid/truncated baseline + relative --files path must still warn on excluded dirty test file.
        trunc_base = self.root / "trunc.json"
        trunc_base.write_text(json.dumps({"repo": str(self.repo), "truncated": True, "files": {}}), encoding="utf-8")
        rep2 = diff_baseline.build_report(self.repo, trunc_base, ["./docs.md"], None)
        self.assertEqual(rep2["changed_files"][0]["path"], "docs.md")
        self.assertEqual(rep2["excluded_sensitive_files"], ["tests/test_app.py"])

    def test_commit_range_ignores_dirty_tree_and_emits_repo_file_index(self) -> None:
        base_sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (self.repo / "src" / "new_mod.py").write_text("def handle_range():\n    return 42\n", encoding="utf-8")
        (self.repo / "src" / "app.py").write_text("def existing():\n    return 99\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "feat range"], check=True)

        # Dirty working tree + untracked file must NOT bleed into --range report.
        (self.repo / "tests" / "test_app.py").write_text("def test_dirty():\n    assert False\n", encoding="utf-8")
        (self.repo / "untracked_noise.py").write_text("x = 1\n", encoding="utf-8")

        diff_out = self.root / "range.diff"
        rep = diff_baseline.build_report(self.repo, None, None, diff_out, commit_range=f"{base_sha}..HEAD")
        self.assertEqual(rep["isolation_mode"], "commit_range")
        self.assertEqual([c["path"] for c in rep["changed_files"]], ["src/app.py", "src/new_mod.py"])
        self.assertEqual(rep["surface_risks"]["new_files"], [{"path": "src/new_mod.py", "added": 2}])
        self.assertIn("handle_range", diff_out.read_text(encoding="utf-8"))
        self.assertNotIn("untracked_noise", diff_out.read_text(encoding="utf-8"))
        self.assertEqual(rep["repo_file_index"]["total_tracked"], 3)
        self.assertIn("tests/test_app.py", rep["repo_file_index"]["files"])
        md = diff_baseline.format_markdown(rep)
        self.assertIn("#### Repo File Index (3 tracked files)", md)
        self.assertIn("`tests/`: `test_app.py`", md)

        # Single ref normalizes to <ref>..HEAD; invalid range returns exit code 2 via CLI.
        rep_single = diff_baseline.build_report(self.repo, None, None, None, commit_range=base_sha)
        self.assertEqual(rep_single["diff_base_ref"], f"{base_sha}..HEAD")
        self.assertEqual(rep_single["scoped_file_count"], 2)

        bad = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS_DIR / "diff_baseline.py"), "report", "--repo", str(self.repo), "--range", "no_such_ref..HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("invalid_range", bad.stdout)


class ContextToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_checkpoint_clears_tokens_and_fails_and_wire_regex_blocks_exit0_quotes(self) -> None:
        tr = self.root / "cp.jsonl"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>hi</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 480000},
            # Real Antigravity GENERIC failure with trailing period + timestamps.
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Created At: 2026-11-23T10:00:00+08:00\nCompleted At: 2026-11-23T10:00:01+08:00\nCommand exited with code 1.\n\nCommand output:\nfail"},
            # Real Antigravity truncated GENERIC failure.
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "The tool's output was truncated due to being too long.\n\nHere is a preview with the middle of the output truncated:\nCreated At: 2026-11-23T10:00:02+08:00\nCompleted At: 2026-11-23T10:00:03+08:00\nCommand exited with code 2.\n"},
            # Real Antigravity background task SYSTEM_MESSAGE failure (truncated variant).
            {"step_index": 4, "type": "SYSTEM_MESSAGE", "content": '<SYSTEM_MESSAGE>\n[Message] sender=abc/task-7 content=Task id "abc/task-7" finished with result:\nThe tool\'s output was truncated.\n\nHere is a preview with the middle of the output truncated:\nCommand exited with code 1.\n</SYSTEM_MESSAGE>'},
            # False-positive guards: (a) read_file quoting error, (b) exit-0 command printing foreign exit-1 log, (c) subagent report quoting exit-1.
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "content": 'Showing lines 1-5 of 5\n1: Command exited with code 1.'},
            {"step_index": 6, "type": "GENERIC", "status": "DONE", "content": "Created At: 2026-11-23T10:00:04+08:00\nCompleted At: 2026-11-23T10:00:05+08:00\nCommand exited with code 0.\n\nCommand output:\nCommand exited with code 1.\n"},
            {"step_index": 7, "type": "SYSTEM_MESSAGE", "content": "<SYSTEM_MESSAGE>\n[Message] sender=subagent-uuid content=Report:\nCommand exited with code 1.\n</SYSTEM_MESSAGE>"},
        ]
        tr.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        rep_pre, cache_pre = context_tools.inspect_health_incremental(tr)
        self.assertEqual(rep_pre["recent_cmd_failures"], 3)
        self.assertEqual(rep_pre["total_cmd_failures"], 3)

        # Two checkpoints + low-fill normal multi-tool turn (14% fill, 16 actions, 0 fails in G2) must stay YELLOW (not RED).
        post_cp = [
            {"step_index": 8, "type": "CHECKPOINT"},
            {"step_index": 9, "type": "CHECKPOINT"},
            {"step_index": 10, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>next</USER_REQUEST>"},
            {"step_index": 11, "type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 63675, "tool_calls": [{"name": "read_file"}] * 16},
        ]
        with tr.open("a", encoding="utf-8") as fp:
            for item in post_cp:
                fp.write(json.dumps(item) + "\n")
        rep_post, _ = context_tools.inspect_health_incremental(tr, cache_pre)
        self.assertEqual(rep_post["recent_cmd_failures"], 0)
        self.assertEqual(rep_post["total_cmd_failures"], 3)
        self.assertEqual(rep_post["peak_effective_tokens"], 490000)
        self.assertEqual(rep_post["level"], "YELLOW")

        # Crossing 45% fill + >=40 actions/UI at G=2 triggers RED with high tool density reason.
        with tr.open("a", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    {
                        "step_index": 12,
                        "type": "PLANNER_RESPONSE",
                        "input_tokens": 20000,
                        "cache_read_tokens": 220000,
                        "tool_calls": [{"name": "read_file"}] * 25,
                    }
                )
                + "\n"
            )
        rep_hi, _ = context_tools.inspect_health_incremental(tr)
        self.assertEqual(rep_hi["level"], "RED")
        self.assertTrue(any("high tool density" in r for r in rep_hi["reasons"]))

    def test_g1_six_shell_failures_triggers_red_even_with_low_actions_per_ui(self) -> None:
        tr = self.root / "g1_stuck.jsonl"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>a</USER_REQUEST>"},
            {"step_index": 1, "type": "CHECKPOINT"},
            {"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>b</USER_REQUEST>"},
            {"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>c</USER_REQUEST>"},
        ]
        for i in range(6):
            rows.append({"step_index": 4 + i, "type": "GENERIC", "status": "DONE", "content": "Created At: t\nCompleted At: t\nCommand exited with code 1.\n"})
        tr.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
        rep, _ = context_tools.inspect_health_incremental(tr)
        self.assertEqual(rep["compaction_generation"], 1)
        self.assertEqual(rep["recent_cmd_failures"], 6)
        self.assertEqual(rep["level"], "RED")

    def test_redacts_json_quoted_secrets_basic_auth_userinfo_and_unlisted_url_queries(self) -> None:
        raw = (
            '{"access_token": "tok_json_999"} password="pw_quoted_888" Authorization: Basic dXNlcjpwYXNz\n'
            "https://alice:s3cr3t@internal.example.org:8443/path?sig=abc123&ticket=xyz789\n"
            "https://example.org/doc?token=unlisted_tok_555"
        )
        redacted = context_tools.redact_secrets(raw)
        for leaked in ("tok_json_999", "pw_quoted_888", "dXNlcjpwYXNz", "alice:s3cr3t", "sig=abc123", "ticket=xyz789", "unlisted_tok_555"):
            self.assertNotIn(leaked, redacted)
        self.assertIn("https://internal.example.org:8443/path", redacted)
        self.assertIn("https://example.org/doc", redacted)
        self.assertEqual(
            context_tools.sanitize_url("https://alice:s3cr3t@internal.example.org:8443/p?sig=1"),
            "https://internal.example.org:8443/p",
        )
        # Greedy USER_REQUEST slice survives nested literal </USER_REQUEST> text.
        nested = "<USER_REQUEST>explain </USER_REQUEST> tag handling</USER_REQUEST><ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"
        self.assertEqual(context_tools.extract_user_request(nested), "explain </USER_REQUEST> tag handling")

    def test_incremental_health_matches_full_scan_and_parses_shell_failures(self) -> None:
        tr = self.root / "transcript.jsonl"
        part1 = [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>start</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "input_tokens": 5000, "cache_read_tokens": 20000, "model": "m1", "tool_calls": [{"name": "run_command"}]},
            # Real Antigravity wire format: status == "DONE" with trailing period on exit code line.
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Created At: 2026-11-23T10:00:00+08:00\nCompleted At: 2026-11-23T10:00:01+08:00\nCommand exited with code 1.\n\nAssertionError"},
        ]
        tr.write_text("".join(json.dumps(x) + "\n" for x in part1), encoding="utf-8")
        rep1, cache1 = context_tools.inspect_health_incremental(tr)
        self.assertTrue(rep1["is_main"])
        self.assertEqual(rep1["level"], "GREEN")
        self.assertEqual(rep1["recent_cmd_failures"], 1)

        # Append two checkpoints + high fill + 4 real-format shell/bg failures and verify incremental == full scan.
        part2 = [
            {"step_index": 3, "type": "CHECKPOINT"},
            {"step_index": 4, "type": "CHECKPOINT"},
            {"step_index": 5, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>fix</USER_REQUEST>"},
            {"step_index": 6, "type": "PLANNER_RESPONSE", "input_tokens": 20000, "cache_read_tokens": 370000, "model": "m1", "tool_calls": [{"name": "run_command"}] * 16},
            {"step_index": 7, "type": "GENERIC", "status": "DONE", "content": "Created At: t\nCompleted At: t\nCommand exited with code 2.\n"},
            {"step_index": 8, "type": "GENERIC", "status": "DONE", "content": "Created At: t\nCompleted At: t\nCommand exited with code 1.\n"},
            {"step_index": 9, "type": "SYSTEM_MESSAGE", "content": 'Task id "u1/task-9" finished with result:\nCommand exited with code 1.\n'},
            {"step_index": 10, "type": "GENERIC", "status": "DONE", "content": "Created At: t\nCompleted At: t\nCommand exited with code 127.\n"},
        ]
        with tr.open("a", encoding="utf-8") as fp:
            for item in part2:
                fp.write(json.dumps(item) + "\n")

        rep_inc, _ = context_tools.inspect_health_incremental(tr, cache1)
        rep_full, _ = context_tools.inspect_health_incremental(tr, None)
        self.assertEqual(rep_inc, rep_full)
        self.assertEqual(rep_inc["level"], "RED")
        self.assertEqual(rep_inc["compaction_generation"], 2)
        self.assertEqual(rep_inc["recent_cmd_failures"], 4)
        self.assertEqual(rep_inc["total_cmd_failures"], 5)
        self.assertEqual(rep_inc["actions_per_ui"], 16.0)

    def test_handoff_prefers_full_transcript_dedups_and_strips_live_tokens(self) -> None:
        logs = self.root / "brain" / "conv1" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        tr_compact = logs / "transcript.jsonl"
        tr_full = logs / "transcript_full.jsonl"

        tr_compact.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>truncated...</USER_REQUEST>"}) + "\n",
            encoding="utf-8",
        )
        full_rows = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-11-23T10:00:00Z",
                "content": (
                    "<USER_REQUEST>\n"
                    "检查 https://docs.example.org/docx/Doc123?disposable_login_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123456\n"
                    "Bearer secret_token_1234567890\n"
                    "</USER_REQUEST>\n"
                    "<ADDITIONAL_METADATA>noise</ADDITIONAL_METADATA>"
                ),
            },
            # Duplicate retry with identical (step_index, text) must be deduplicated.
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "created_at": "2026-11-23T10:00:01Z",
                "content": (
                    "<USER_REQUEST>\n"
                    "检查 https://docs.example.org/docx/Doc123?disposable_login_token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123456\n"
                    "Bearer secret_token_1234567890\n"
                    "</USER_REQUEST>"
                ),
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "See https://vertexaisearch.cloud.google.com/grounding/abc and https://notes.example.org/articles/123?from=skill",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": '"/workspace/demo/project/app.py"'}},
                    {"name": "edit_file", "args": {"Path": '"/workspace/demo/.gemini/antigravity/brain/x/scratch/tmp.py"'}},
                ],
            },
        ]
        tr_full.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in full_rows), encoding="utf-8")

        md = context_tools.export_handoff(tr_compact)
        self.assertIn(str(tr_full), md)
        self.assertNotIn("disposable_login_token=eyJ", md)
        self.assertNotIn("secret_token_1234567890", md)
        self.assertNotIn("vertexaisearch.cloud.google.com", md)
        self.assertNotIn("?from=skill", md)
        self.assertIn("https://docs.example.org/docx/Doc123", md)
        self.assertIn("https://notes.example.org/articles/123", md)
        self.assertIn("`/workspace/demo/project/app.py` (1 edits)", md)
        self.assertNotIn("/scratch/tmp.py", md)
        # Deduplicated: only 1 bullet for step=0.
        self.assertEqual(md.count("step=0"), 1)

        # Refuses writing inside a git repo without --force.
        repo = self.root / "gitrepo"
        repo.mkdir()
        _init_git_repo(repo)
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS_DIR / "context_tools.py"), "handoff", "--transcript", str(tr_compact), "--out", str(repo / "HANDOFF.md")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertFalse((repo / "HANDOFF.md").exists())


if __name__ == "__main__":
    unittest.main()
