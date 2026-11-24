#!/usr/bin/env python3
"""Offline unit tests for codex_worker.py, codex_exec_guard.py, and loop_anchor_hint.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
HOOKS_DIR = PLUGIN_ROOT / "hooks"
WORKER = SCRIPTS_DIR / "codex_worker.py"
GUARD = HOOKS_DIR / "codex_exec_guard.py"

for entry in (str(SCRIPTS_DIR), str(HOOKS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import codex_exec_guard
import codex_worker
import loop_anchor_hint


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Tester"], check=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)


def _write_fake_codex(path: Path, script_body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + script_body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class CodexExecGuardTests(unittest.TestCase):
    def test_blocks_bare_codex_exec_variants(self) -> None:
        blocked = [
            'codex exec -m gpt-5-codex "do work"',
            'cd /tmp/wt && codex exec -C . -',
            'CODEX_HOME=/tmp/x env codex exec -s workspace-write "hi"',
            '/opt/homebrew/bin/codex exec resume abc-123 "fix"',
            'timeout 600 codex exec "run"',
        ]
        for cmd in blocked:
            with self.subTest(cmd=cmd):
                self.assertTrue(codex_exec_guard.is_bare_codex_exec(cmd))

    def test_allows_safe_wrappers_and_non_exec_commands(self) -> None:
        allowed = [
            f'python3 {WORKER} start --workdir /tmp --prompt "hi"',
            'python3 -I runner/dispatch.py --work /tmp --plan p.json',
            'git commit -m "fix bare codex exec timeout"',
            'codex exec --help',
            'codex login status',
            'codex --version',
            "cat <<'EOF'\ncodex exec -m o3\nEOF\n",
            'python3 -I -m unittest discover -s tests',
        ]
        for cmd in allowed:
            with self.subTest(cmd=cmd):
                self.assertFalse(codex_exec_guard.is_bare_codex_exec(cmd))

    def test_hook_cli_json_contract(self) -> None:
        deny_payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": 'codex exec "hi"'}},
        }
        proc = subprocess.run(
            [sys.executable, "-I", str(GUARD)],
            input=json.dumps(deny_payload),
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("codex_worker.py", out["reason"])

        allow_payload = {
            "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
        }
        proc2 = subprocess.run(
            [sys.executable, "-I", str(GUARD)],
            input=json.dumps(allow_payload),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(proc2.stdout)["decision"], "allow")


class LoopAnchorStepDedupTests(unittest.TestCase):
    def test_parallel_edits_in_one_step_do_not_trigger_false_churn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "app.py"
            target.write_text("x = 1\n", encoding="utf-8")
            transcript = root / "transcript.jsonl"
            single_step = {
                "step_index": 10,
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(target)}} for _ in range(5)
                ],
            }
            transcript.write_text(json.dumps(single_step) + "\n", encoding="utf-8")
            self.assertIsNone(loop_anchor_hint.detect_transcript_edit_churn(str(transcript), [root]))

            four_steps = [
                {"step_index": i, "tool_calls": [{"name": "edit_file", "args": {"Path": str(target)}}]}
                for i in range(4)
            ]
            transcript.write_text(
                "".join(json.dumps(s) + "\n" for s in four_steps),
                encoding="utf-8",
            )
            hit = loop_anchor_hint.detect_transcript_edit_churn(str(transcript), [root])
            self.assertEqual(hit, (str(target), 4))


class CodexWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_git_repo(self.repo)
        self.src_codex = self.root / "user_codex"
        self.src_codex.mkdir()
        (self.src_codex / "auth.json").write_text('{"token":"demo"}\n', encoding="utf-8")
        (self.src_codex / "config.toml").write_text('approvals_reviewer = "auto_review"\n', encoding="utf-8")
        (self.src_codex / "hooks.json").write_text('{"bad": true}\n', encoding="utf-8")

    def _run_worker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(WORKER), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_start_watch_and_resume_with_isolated_codex_home(self) -> None:
        fake = _write_fake_codex(
            self.root / "fake_codex.py",
            """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
out_path = Path(args[args.index("-o") + 1])
codex_home = Path(os.environ["CODEX_HOME"])
assert (codex_home / "auth.json").is_file()
assert not (codex_home / "config.toml").exists()
assert not (codex_home / "hooks.json").exists()
assert "--ignore-user-config" in args
assert "--ignore-rules" in args
is_resume = len(args) >= 2 and args[1] == "resume"
tid = args[2] if is_resume else "thread-abc-123"
print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
if is_resume:
    Path("resumed.txt").write_text(prompt, encoding="utf-8")
    out_path.write_text("Resumed OK", encoding="utf-8")
else:
    Path("created.txt").write_text(prompt, encoding="utf-8")
    out_path.write_text("Initial OK", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
        )
        state = self.root / "state-normal"
        r_start = self._run_worker(
            "start",
            "--workdir", str(self.repo),
            "--state-dir", str(state),
            "--codex-bin", str(fake),
            "--source-codex-home", str(self.src_codex),
            "--prompt", "first task",
        )
        self.assertEqual(r_start.returncode, 0, r_start.stderr)

        r_watch = self._run_worker("watch", "--state-dir", str(state), "--poll-interval", "0.05")
        self.assertEqual(r_watch.returncode, 0, r_watch.stderr + r_watch.stdout)
        rep = json.loads(r_watch.stdout)
        self.assertEqual(rep["status"], "done")
        self.assertEqual(rep["thread_id"], "thread-abc-123")
        self.assertEqual(rep["last_message_preview"], "Initial OK")
        self.assertEqual((self.repo / "created.txt").read_text(encoding="utf-8"), "first task")

        r_res = self._run_worker("resume", "--state-dir", str(state), "--prompt", "second task")
        self.assertEqual(r_res.returncode, 0, r_res.stderr)
        r_watch2 = self._run_worker("watch", "--state-dir", str(state), "--poll-interval", "0.05")
        self.assertEqual(r_watch2.returncode, 0, r_watch2.stderr + r_watch2.stdout)
        rep2 = json.loads(r_watch2.stdout)
        self.assertEqual(rep2["last_message_preview"], "Resumed OK")
        self.assertEqual((self.repo / "resumed.txt").read_text(encoding="utf-8"), "second task")

    def test_watch_exits_2_without_killing_worker_on_stall_and_reconnect_storm(self) -> None:
        fake = _write_fake_codex(
            self.root / "fake_stalling_codex.py",
            """
import json, sys, time
print(json.dumps({"type": "thread.started", "thread_id": "thread-stall-1"}), flush=True)
for _ in range(3):
    print("ERROR: Reconnecting... waiting for network", flush=True)
    time.sleep(0.05)
time.sleep(30)
""",
        )
        state = self.root / "state-stall"
        r_start = self._run_worker(
            "start",
            "--workdir", str(self.repo),
            "--state-dir", str(state),
            "--codex-bin", str(fake),
            "--source-codex-home", str(self.src_codex),
            "--prompt", "stall test",
        )
        self.assertEqual(r_start.returncode, 0, r_start.stderr)

        r_watch = self._run_worker(
            "watch",
            "--state-dir", str(state),
            "--max-reconnects", "3",
            "--stall-after", "60",
            "--poll-interval", "0.05",
        )
        self.assertEqual(r_watch.returncode, 2, r_watch.stderr + r_watch.stdout)
        rep = json.loads(r_watch.stdout)
        self.assertEqual(rep["status"], "needs_inspection")
        self.assertTrue(rep["worker_alive"])
        self.assertEqual(rep["thread_id"], "thread-stall-1")
        self.assertTrue(codex_worker.pid_alive(rep["pid"]))

        # Now explicitly stop the worker and verify clean shutdown while keeping thread_id.
        r_stop = self._run_worker("stop", "--state-dir", str(state), "--grace-seconds", "2")
        self.assertEqual(r_stop.returncode, 0, r_stop.stderr)
        stopped = json.loads(r_stop.stdout)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["thread_id"], "thread-stall-1")
        self.assertFalse(codex_worker.pid_alive(rep["pid"]))

    def test_active_long_running_worker_is_not_interrupted_past_stall_window(self) -> None:
        fake = _write_fake_codex(
            self.root / "fake_active_codex.py",
            """
import json, sys, time
from pathlib import Path
args = sys.argv[1:]
out_path = Path(args[args.index("-o") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-active"}), flush=True)
for i in range(6):
    print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": f"step {i}"}}), flush=True)
    time.sleep(0.6)
out_path.write_text("Long active task finished", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
        )
        state = self.root / "state-active"
        r_start = self._run_worker(
            "start",
            "--workdir", str(self.repo),
            "--state-dir", str(state),
            "--codex-bin", str(fake),
            "--source-codex-home", str(self.src_codex),
            "--prompt", "active task",
        )
        self.assertEqual(r_start.returncode, 0, r_start.stderr)
        # Total runtime (~4.5s) > 2x stall_after (2.0s), yet continuous progress keeps watch happy.
        r_watch = self._run_worker(
            "watch",
            "--state-dir", str(state),
            "--stall-after", "2.0",
            "--poll-interval", "0.1",
        )
        self.assertEqual(r_watch.returncode, 0, r_watch.stderr + r_watch.stdout)
        rep = json.loads(r_watch.stdout)
        self.assertEqual(rep["status"], "done")
        self.assertEqual(rep["last_message_preview"], "Long active task finished")

    def test_worktree_allow_git_commit_adds_common_git_dir(self) -> None:
        wt = self.root / "wt-branch"
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "feat-x", str(wt)],
            check=True,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)],
                check=False,
            )
        )
        cfg = {
            "workdir": str(wt),
            "codex_bin": "codex",
            "model": "gpt-5-codex",
            "effort": "high",
            "sandbox": "workspace-write",
            "allow_git_commit": True,
            "last_message_path": str(self.root / "last.md"),
        }
        cmd = codex_worker.build_codex_command(cfg)
        self.assertIn("--add-dir", cmd)
        added = cmd[cmd.index("--add-dir") + 1]
        self.assertEqual(Path(added).resolve(), (self.repo / ".git").resolve())

    def test_with_user_config_opt_in_uses_source_home(self) -> None:
        cfg = {
            "workdir": str(self.repo),
            "codex_bin": "codex",
            "model": "gpt-5-codex",
            "effort": "high",
            "sandbox": "workspace-write",
            "with_user_config": True,
            "last_message_path": str(self.root / "last.md"),
        }
        cmd = codex_worker.build_codex_command(cfg)
        self.assertNotIn("--ignore-user-config", cmd)
        self.assertNotIn("--ignore-rules", cmd)
        home = codex_worker.prepare_codex_home(
            self.root / "state-optin",
            with_user_config=True,
            source_codex_home=self.src_codex,
        )
        self.assertEqual(home, self.src_codex.resolve())

    def test_slow_resume_and_reused_start_do_not_return_stale_status(self) -> None:
        fake = _write_fake_codex(
            self.root / "fake_slow_codex.py",
            """
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if "--version" in args:
    print("codex-cli 0.169.0")
    sys.exit(0)
assert "OPENAI_API_KEY" not in os.environ
assert "CODEX_API_KEY" not in os.environ
out_path = Path(args[args.index("-o") + 1])
is_resume = len(args) >= 2 and args[1] == "resume"
tid = args[2] if is_resume else "thread-race-1"
print(json.dumps({"type": "thread.started", "thread_id": tid}), flush=True)
time.sleep(0.35)
out_path.write_text("ResumeDone" if is_resume else "StartDone", encoding="utf-8")
print(json.dumps({"type": "turn.completed"}), flush=True)
""",
        )
        (self.src_codex / "models_cache.json").write_text(
            json.dumps({"client_version": "0.169.0", "models": [{"id": "gpt-5-codex"}]}),
            encoding="utf-8",
        )
        state = self.root / "state-race"
        env = dict(os.environ, OPENAI_API_KEY="sk-leak", CODEX_API_KEY="ck-leak")
        r1 = subprocess.run(
            [sys.executable, "-I", str(WORKER), "start", "--workdir", str(self.repo),
             "--state-dir", str(state), "--codex-bin", str(fake),
             "--source-codex-home", str(self.src_codex), "--prompt", "p1"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(r1.returncode, 0, r1.stderr)
        p1 = json.loads(r1.stdout)["pid"]
        self.assertTrue((state / "codex_home" / "models_cache.json").is_file())
        w1 = self._run_worker("watch", "--state-dir", str(state), "--poll-interval", "0.05")
        self.assertEqual(w1.returncode, 0, w1.stderr + w1.stdout)
        self.assertFalse((state / "codex_home" / "auth.json").exists())

        r2 = subprocess.run(
            [sys.executable, "-I", str(WORKER), "resume", "--state-dir", str(state),
             "--prompt", "p2"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        j2 = json.loads(r2.stdout)
        self.assertNotEqual(j2["pid"], p1)
        self.assertEqual(j2["state"], "running")
        w2 = self._run_worker("watch", "--state-dir", str(state), "--poll-interval", "0.05")
        self.assertEqual(w2.returncode, 0, w2.stderr + w2.stdout)
        self.assertEqual(json.loads(w2.stdout)["last_message_preview"], "ResumeDone")

        r3 = subprocess.run(
            [sys.executable, "-I", str(WORKER), "start", "--workdir", str(self.repo),
             "--state-dir", str(state), "--codex-bin", str(fake),
             "--source-codex-home", str(self.src_codex), "--prompt", "p3"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(r3.returncode, 0, r3.stderr)
        j3 = json.loads(r3.stdout)
        self.assertNotEqual(j3["pid"], j2["pid"])
        self.assertEqual(j3["state"], "running")
        w3 = self._run_worker("watch", "--state-dir", str(state), "--poll-interval", "0.05")
        self.assertEqual(w3.returncode, 0, w3.stderr + w3.stdout)
        self.assertEqual(json.loads(w3.stdout)["last_message_preview"], "StartDone")


class TestLoopAnchorMacroAnnealing(unittest.TestCase):
    HOOK = PLUGIN_ROOT / "hooks" / "loop_anchor_hint.py"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_hook(self, payload: dict, extra_env: dict[str, str] | None = None) -> dict:
        env = dict(os.environ)
        env["DEV_LEVERAGE_NOTIFY"] = "0"
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, "-I", str(self.HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return json.loads(proc.stdout or "{}")

    def test_short_turn_has_zero_injection(self) -> None:
        tr = self.root / "short.jsonl"
        tr.write_text('{"step_index": 1, "type": "USER_INPUT"}\n', encoding="utf-8")
        out = self._run_hook(
            {
                "conversationId": "conv-short",
                "artifactDirectoryPath": str(self.root / "art"),
                "workspacePaths": [str(self.ws)],
                "transcriptPath": str(tr),
                "invocationNum": 2,
                "lastUserInput": "普通问答",
            }
        )
        self.assertEqual(out, {})

    def test_context_health_and_macro_expansion_fire_once_per_state(self) -> None:
        tr = self.root / "macro.jsonl"
        lines = [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
            json.dumps({"step_index": 1, "type": "CHECKPOINT"}),
            json.dumps({"step_index": 2, "type": "CHECKPOINT"}),
            json.dumps({"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
            json.dumps(
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "input_tokens": 10000,
                    "cache_read_tokens": 370000,
                    "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest"}} for _ in range(16)],
                }
            ),
            json.dumps({"step_index": 5, "type": "GENERIC", "status": "DONE", "content": "Command exited with code 1"}),
        ]
        for i in range(10):
            f = self.ws / f"mod_{i}.py"
            f.write_text("x = 1\n", encoding="utf-8")
            lines.append(
                json.dumps(
                    {
                        "step_index": 6 + i,
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [{"name": "edit_file", "args": {"Path": str(f)}}],
                    }
                )
            )
        tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = {
            "conversationId": "conv-macro",
            "artifactDirectoryPath": str(self.root / "art"),
            "workspacePaths": [str(self.ws)],
            "transcriptPath": str(tr),
            "invocationNum": 6,
            "lastUserInput": "继续改下一个模块",
        }
        # First call fires Signal C (Context Health RED: G=2, fill=74%, high tool density)
        out1 = self._run_hook(payload, {"DEV_LEVERAGE_MACRO_FILES": "8"})
        msg1 = out1["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("Context Health RED", msg1)
        self.assertIn("G=2", msg1)
        self.assertIn("context_tools.py", msg1)

        # Second call in same RED bucket fires Signal D (10 distinct files >= 8)
        out2 = self._run_hook(payload, {"DEV_LEVERAGE_MACRO_FILES": "8"})
        msg2 = out2["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("跨 10 个源码文件", msg2)

        # Third call within cooldown stays silent (no nagging)
        out3 = self._run_hook(payload, {"DEV_LEVERAGE_MACRO_FILES": "8"})
        self.assertEqual(out3, {})

    def test_green_session_has_zero_signal_c_injection(self) -> None:
        tr = self.root / "green.jsonl"
        lines = [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
            json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "input_tokens": 5000,
                    "cache_read_tokens": 45000,
                    "tool_calls": [{"name": "read_file", "args": {"Path": "README.md"}}],
                }
            ),
        ]
        tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Subtractive proof: legacy DEV_LEVERAGE_HORIZON_STEPS no longer triggers Signal C on a GREEN session.
        out = self._run_hook(
            {
                "conversationId": "conv-green",
                "artifactDirectoryPath": str(self.root / "art"),
                "workspacePaths": [str(self.ws)],
                "transcriptPath": str(tr),
                "invocationNum": 10,
                "lastUserInput": "聊聊架构设计",
            },
            {"DEV_LEVERAGE_HORIZON_STEPS": "2"},
        )
        self.assertEqual(out, {})

    def test_subagent_session_has_zero_injection(self) -> None:
        tr = self.root / "subagent.jsonl"
        lines = [
            json.dumps({"step_index": 0, "type": "SYSTEM_MESSAGE", "source": "SYSTEM"}),
            json.dumps({"step_index": 1, "type": "CHECKPOINT"}),
            json.dumps({"step_index": 2, "type": "CHECKPOINT"}),
            json.dumps(
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "input_tokens": 10000,
                    "cache_read_tokens": 400000,
                    "tool_calls": [{"name": "run_command"}] * 16,
                }
            ),
            json.dumps({"step_index": 4, "type": "GENERIC", "status": "DONE", "content": "Command exited with code 1"}),
        ]
        for i in range(10):
            f = self.ws / f"sub_{i}.py"
            f.write_text("y = 2\n", encoding="utf-8")
            lines.append(
                json.dumps(
                    {
                        "step_index": 5 + i,
                        "type": "PLANNER_RESPONSE",
                        "tool_calls": [{"name": "edit_file", "args": {"Path": str(f)}}],
                    }
                )
            )
        tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self._run_hook(
            {
                "conversationId": "conv-subagent",
                "artifactDirectoryPath": str(self.root / "art"),
                "workspacePaths": [str(self.ws)],
                "transcriptPath": str(tr),
                "invocationNum": 10,
                "lastUserInput": "/goal subagent task",
            },
            {"DEV_LEVERAGE_MACRO_FILES": "8"},
        )
        self.assertEqual(out, {})

    def test_macos_notification_helper_respects_opt_out_and_passes_safe_argv(self) -> None:
        from unittest import mock

        with mock.patch.object(loop_anchor_hint.sys, "platform", "darwin"):
            with mock.patch.dict(os.environ, {"DEV_LEVERAGE_NOTIFY": "0"}):
                with mock.patch.object(loop_anchor_hint.subprocess, "Popen") as popen:
                    self.assertFalse(loop_anchor_hint.notify_macos("T", "Sub", "Body"))
                    popen.assert_not_called()

            with mock.patch.dict(os.environ, {"DEV_LEVERAGE_NOTIFY": "1"}):
                with mock.patch.object(loop_anchor_hint.subprocess, "Popen") as popen:
                    self.assertTrue(
                        loop_anchor_hint.notify_macos('Title "quoted"', "评测分析底座", "G=10 · 水位 91%")
                    )
                    popen.assert_called_once()
                    argv = popen.call_args[0][0]
                    self.assertEqual(argv[0], "osascript")
                    self.assertIn("--", argv)
                    self.assertEqual(argv[-3:], ['Title "quoted"', "评测分析底座", "G=10 · 水位 91%"])

    def test_git_commit_churn_deterministic_tie_break_and_skips_presession_head(self) -> None:
        repo = self.root / "tied_repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Create 3 commits touching BOTH compare.py and rules.py so they tie at 3 hits each.
        for i in range(3):
            (repo / "rules.py").write_text(f"r = {i}\n", encoding="utf-8")
            (repo / "compare.py").write_text(f"c = {i}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "rules.py", "compare.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"round {i}"], check=True)
        head_full = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

        # Lexicographical tie-break must always pick compare.py over rules.py.
        hit = loop_anchor_hint.detect_git_commit_churn([repo])
        self.assertEqual(hit, ("tied_repo", head_full[:8], "compare.py", 3))

        # Baseline matching current HEAD (brand-new session) must NOT fire on pre-session commits.
        self.assertIsNone(
            loop_anchor_hint.detect_git_commit_churn([repo], baseline_heads={str(repo.resolve()): head_full})
        )
        # Commit older than session_start_ts must also be skipped.
        self.assertIsNone(
            loop_anchor_hint.detect_git_commit_churn([repo], min_head_ts=time.time() + 60.0)
        )

    def test_red_step_cooldown_avoids_bucket_double_fire_and_caps_banner_once_per_gen(self) -> None:
        tr = self.root / "red_cd.jsonl"
        # Build G=2 transcript reaching 119 gen_steps (just below 120 bucket boundary) at 75% fill.
        lines = [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
            json.dumps({"step_index": 1, "type": "CHECKPOINT"}),
            json.dumps({"step_index": 2, "type": "CHECKPOINT"}),
            json.dumps({"step_index": 3, "type": "USER_INPUT", "source": "USER_EXPLICIT"}),
        ]
        for i in range(118):
            lines.append(json.dumps({"step_index": 4 + i, "type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 375000}))
        tr.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = {
            "conversationId": "conv-red-cd",
            "artifactDirectoryPath": str(self.root / "art"),
            "workspacePaths": [str(self.ws)],
            "transcriptPath": str(tr),
            "invocationNum": 10,
            "lastUserInput": "继续",
        }
        out1 = self._run_hook(payload, {"DEV_LEVERAGE_RED_COOLDOWN": "120"})
        self.assertIn("Context Health RED", out1["injectSteps"][0]["systemMessage"]["systemMessage"])

        # Append 5 steps crossing gen_steps=120 -> old `// 120` double-fired here; new step cooldown stays silent!
        with tr.open("a", encoding="utf-8") as fp:
            for j in range(5):
                fp.write(json.dumps({"step_index": 122 + j, "type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 375000}) + "\n")
        out2 = self._run_hook(payload, {"DEV_LEVERAGE_RED_COOLDOWN": "120"})
        self.assertEqual(out2, {})

        # After full 120-step cooldown, RED systemMessage re-fires, while macOS banner stays capped to gen 2.
        with tr.open("a", encoding="utf-8") as fp:
            for j in range(120):
                fp.write(json.dumps({"step_index": 127 + j, "type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 375000}) + "\n")
        out3 = self._run_hook(payload, {"DEV_LEVERAGE_RED_COOLDOWN": "120"})
        self.assertIn("Context Health RED", out3["injectSteps"][0]["systemMessage"]["systemMessage"])
        st_file = self.root / "art" / "scratch" / "dev-leverage" / "loop-anchor-conv-red-cd.json"
        st = json.loads(st_file.read_text(encoding="utf-8"))
        self.assertEqual(st["last_red_gen"], 2)
        self.assertEqual(st["last_red_step"], 247)


if __name__ == "__main__":
    unittest.main()
