#!/usr/bin/env python3
"""Deterministic supervisor for local `codex exec` coding workers.

Provides subagent-like lifecycle parity (`start` / `watch` / `status` / `resume` / `stop`):
- Clean isolated per-worker CODEX_HOME (copies only auth.json by default; --with-user-config opts in)
- Persists `thread_id` from `--json` events so `resume` continues in-context without cold re-reads
- Non-destructive liveness watch: no hardcoded max-timeout kill cap and never blindly SIGKILLs;
  returns exit code 2 (`needs_inspection`) with a live progress snapshot if progress stalls or
  network reconnects loop, leaving the worker alive for the caller to inspect and decide.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_STATE_ROOT = Path.home() / ".cache" / "dev-leverage" / "codex-workers"
NOISE_MARKERS = (
    "failed to refresh available models",
)
RECONNECT_MARKERS = (
    "Reconnecting...",
    "waiting for network",
)
APPROVAL_TIMEOUT_MARKERS = (
    "automatic permission approval review did not finish",
)


def now_ts() -> float:
    return time.time()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def resolve_git_common_dir(workdir: Path) -> Path | None:
    git_entry = workdir / ".git"
    if not git_entry.is_file():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    raw = Path(proc.stdout.strip())
    return raw.resolve() if raw.is_absolute() else (workdir / raw).resolve()


def git_snapshot(workdir: Path) -> dict[str, Any]:
    if not workdir.is_dir():
        return {"dirty_files": [], "diff_stat": ""}
    try:
        st = subprocess.run(
            ["git", "-C", str(workdir), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        df = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return {"dirty_files": [], "diff_stat": ""}
    dirty = [ln for ln in st.stdout.splitlines() if ln.strip()] if st.returncode == 0 else []
    diff_stat = df.stdout.strip() if df.returncode == 0 else ""
    return {"dirty_files": dirty[:50], "dirty_count": len(dirty), "diff_stat": diff_stat}


def latest_dirty_mtime(workdir: Path) -> float:
    snap = git_snapshot(workdir)
    latest = 0.0
    for entry in snap["dirty_files"]:
        rel = entry[3:].strip()
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip()
        if rel.startswith('"') and rel.endswith('"'):
            rel = rel[1:-1]
        p = workdir / rel
        try:
            if p.exists():
                latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


_SEMVER_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\b")


def _detect_codex_version(codex_bin: str) -> str | None:
    try:
        res = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    m = _SEMVER_RE.search(res.stdout or res.stderr or "")
    return m.group(1) if m else None


def _is_valid_models_cache(path: Path, cli_version: str | None) -> bool:
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or not doc.get("models"):
        return False
    if cli_version and doc.get("client_version") != cli_version:
        return False
    return True


def prepare_codex_home(
    state_dir: Path,
    *,
    with_user_config: bool,
    source_codex_home: Path | None,
    codex_bin: str = "codex",
) -> Path:
    src = (
        source_codex_home.expanduser().resolve()
        if source_codex_home is not None
        else Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()
    )
    if with_user_config:
        return src
    isolated = state_dir / "codex_home"
    isolated.mkdir(parents=True, exist_ok=True)
    auth_src = src / "auth.json"
    auth_dst = isolated / "auth.json"
    if auth_src.is_file():
        shutil.copy2(auth_src, auth_dst)
        try:
            os.chmod(auth_dst, 0o600)
        except OSError:
            pass
    cache_dst = isolated / "models_cache.json"
    cache_src = src / "models_cache.json"
    cli_ver = _detect_codex_version(codex_bin)
    if not _is_valid_models_cache(cache_dst, cli_ver) and _is_valid_models_cache(cache_src, cli_ver):
        try:
            shutil.copy2(cache_src, cache_dst)
            os.chmod(cache_dst, 0o600)
        except OSError:
            pass
    return isolated


def build_codex_command(cfg: dict[str, Any], *, resume_thread_id: str | None = None) -> list[str]:
    codex_bin = str(cfg["codex_bin"])
    model = str(cfg["model"])
    effort = str(cfg["effort"])
    workdir = Path(cfg["workdir"])
    last_msg = str(cfg["last_message_path"])
    with_user_config = bool(cfg.get("with_user_config"))
    sandbox = str(cfg.get("sandbox") or "workspace-write")

    if resume_thread_id:
        cmd = [codex_bin, "exec", "resume", resume_thread_id, "-"]
    else:
        cmd = [codex_bin, "exec", "-C", str(workdir), "-"]

    if model:
        cmd.extend(["-m", model])
    cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    if not with_user_config:
        cmd.extend([
            "--ignore-user-config",
            "--ignore-rules",
            "-c", "features.memories=false",
            "--skip-git-repo-check",
        ])
        if resume_thread_id:
            cmd.extend(["-c", f'sandbox_mode="{sandbox}"'])
        else:
            cmd.extend(["-s", sandbox])
    elif not resume_thread_id:
        cmd.extend(["-s", sandbox, "--skip-git-repo-check"])
    else:
        cmd.append("--skip-git-repo-check")

    if not resume_thread_id:
        add_dirs: list[str] = list(cfg.get("add_dirs") or [])
        if cfg.get("allow_git_commit"):
            common_git = resolve_git_common_dir(workdir)
            if common_git is not None and str(common_git) not in add_dirs:
                add_dirs.append(str(common_git))
        for d in add_dirs:
            cmd.extend(["--add-dir", str(d)])

    cmd.extend(["--json", "-o", last_msg])
    return cmd


def _extract_thread_id(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    for key in ("thread_id", "session_id"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    thread = obj.get("thread")
    if isinstance(thread, dict):
        tid = thread.get("id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    return None


def _summarize_json_event(obj: dict[str, Any]) -> tuple[str, str | None, str | None, str]:
    """Return (phase_hint, cmd_started_or_none, cmd_ended_flag, short_summary)."""
    ev_type = str(obj.get("type") or "event")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    if ev_type == "item.started" and item_type in {"command_execution", "exec", "tool_call"}:
        cmd = str(item.get("command") or item.get("name") or "")[:160]
        return "running_cmd", cmd, None, f"{ev_type}: {cmd or item_type}"
    if ev_type == "item.completed" and item_type in {"command_execution", "exec", "tool_call"}:
        cmd = str(item.get("command") or item.get("name") or "")[:120]
        exit_c = item.get("exit_code")
        return "thinking", None, "ended", f"{ev_type} (rc={exit_c}): {cmd or item_type}"
    if ev_type == "item.completed" and item_type in {"agent_message", "reasoning"}:
        text = " ".join(str(item.get("text") or "").split())[:160]
        return "thinking", None, None, f"{item_type}: {text}"
    if ev_type in {"turn.started", "turn.completed", "thread.started"}:
        return "thinking", None, None, ev_type
    if ev_type == "error":
        msg = " ".join(str(obj.get("message") or obj.get("error") or "").split())[:160]
        return "error", None, None, f"error: {msg}"
    return "thinking", None, None, ev_type


def _run_supervisor(state_dir: Path) -> int:
    cfg_path = state_dir / "config.json"
    status_path = state_dir / "status.json"
    cfg = read_json(cfg_path)
    workdir = Path(cfg["workdir"])
    raw_log = Path(cfg["raw_log_path"])
    prompt_path = Path(cfg["prompt_path"])
    resume_tid = cfg.get("resume_thread_id")
    cmd = build_codex_command(cfg, resume_thread_id=resume_tid)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(cfg["codex_home"])
    if not cfg.get("with_user_config"):
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)

    started = now_ts()
    status: dict[str, Any] = {
        "worker_id": cfg["worker_id"],
        "run_nonce": cfg.get("run_nonce"),
        "state_dir": str(state_dir),
        "workdir": str(workdir),
        "model": cfg["model"],
        "effort": cfg["effort"],
        "mode": "resume" if resume_tid else "start",
        "with_user_config": bool(cfg.get("with_user_config")),
        "codex_home": str(cfg["codex_home"]),
        "command": cmd,
        "supervisor_pid": os.getpid(),
        "pid": None,
        "state": "starting",
        "phase": "starting",
        "anomaly": None,
        "thread_id": resume_tid,
        "started_at": started,
        "updated_at": started,
        "last_meaningful_at": started,
        "last_event_type": "starting",
        "last_event_summary": "launching codex worker",
        "last_cmd": None,
        "cmd_started_at": None,
        "reconnect_streak": 0,
        "noise_count": 0,
        "events_count": 0,
        "exit_code": None,
        "finished_at": None,
        "raw_log_path": str(raw_log),
        "last_message_path": str(cfg["last_message_path"]),
        "tail_events": [],
    }
    atomic_write_json(status_path, status)

    prompt_bytes = prompt_path.read_bytes()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        status["pid"] = proc.pid
        status["state"] = "running"
        status["phase"] = "thinking"
        status["updated_at"] = now_ts()
        atomic_write_json(status_path, status)

        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt_bytes)
                proc.stdin.close()
            except BrokenPipeError:
                pass

        tail_events: list[str] = []
        last_flush = now_ts()
        with raw_log.open("ab") as log_fh:
            assert proc.stdout is not None
            for raw_b in proc.stdout:
                log_fh.write(raw_b)
                log_fh.flush()
                ts = now_ts()
                line = raw_b.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if any(m in line for m in APPROVAL_TIMEOUT_MARKERS):
                    status["anomaly"] = "approval_timeout"
                    status["phase"] = "blocked"
                    status["last_event_type"] = "approval_timeout"
                    status["last_event_summary"] = line[:200]
                    tail_events.append(line[:200])
                    tail_events = tail_events[-25:]
                    status["tail_events"] = tail_events
                    status["updated_at"] = ts
                    atomic_write_json(status_path, status)
                    last_flush = ts
                    continue

                if any(m in line for m in RECONNECT_MARKERS):
                    status["reconnect_streak"] = int(status["reconnect_streak"]) + 1
                    status["phase"] = "reconnecting"
                    status["last_event_type"] = "reconnecting"
                    status["last_event_summary"] = line[:200]
                    status["updated_at"] = ts
                    atomic_write_json(status_path, status)
                    last_flush = ts
                    continue

                if any(m in line for m in NOISE_MARKERS):
                    status["noise_count"] = int(status["noise_count"]) + 1
                    if ts - last_flush >= 2.0:
                        status["updated_at"] = ts
                        atomic_write_json(status_path, status)
                        last_flush = ts
                    continue

                parsed = None
                if line.startswith("{") and line.endswith("}"):
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            parsed = obj
                    except ValueError:
                        parsed = None

                status["events_count"] = int(status["events_count"]) + 1
                status["reconnect_streak"] = 0
                status["anomaly"] = None
                status["last_meaningful_at"] = ts
                status["updated_at"] = ts

                if parsed is not None:
                    tid = _extract_thread_id(parsed)
                    if tid and not status.get("thread_id"):
                        status["thread_id"] = tid
                    phase_hint, cmd_started, cmd_ended, summary = _summarize_json_event(parsed)
                    status["phase"] = phase_hint
                    status["last_event_type"] = str(parsed.get("type") or "json")
                    status["last_event_summary"] = summary
                    if cmd_started is not None:
                        status["last_cmd"] = cmd_started
                        status["cmd_started_at"] = ts
                    elif cmd_ended:
                        status["cmd_started_at"] = None
                    tail_events.append(summary)
                else:
                    status["last_event_type"] = "text"
                    status["last_event_summary"] = line[:200]
                    tail_events.append(line[:200])

                tail_events = tail_events[-25:]
                status["tail_events"] = tail_events
                atomic_write_json(status_path, status)
                last_flush = ts

        rc = proc.wait()
    finally:
        if not cfg.get("with_user_config"):
            (Path(cfg["codex_home"]) / "auth.json").unlink(missing_ok=True)
    finished = now_ts()
    cur = read_json(status_path) if status_path.is_file() else status
    if cur.get("state") != "stopped":
        status["state"] = "done" if rc == 0 else "failed"
    else:
        status["state"] = "stopped"
    status["phase"] = "done"
    status["exit_code"] = rc
    status["finished_at"] = finished
    status["updated_at"] = finished
    status["tail_events"] = tail_events
    atomic_write_json(status_path, status)
    return 0


def _resolve_state_dir(worker_id: str | None, state_dir: str | None) -> Path:
    if state_dir:
        return Path(state_dir).expanduser().resolve()
    if not worker_id:
        raise SystemExit("Either --worker-id or --state-dir is required")
    return (DEFAULT_STATE_ROOT / worker_id).resolve()


def _launch_supervisor(state_dir: Path, *, expected_nonce: str) -> dict[str, Any]:
    script = Path(__file__).resolve()
    subprocess.Popen(
        [sys.executable, "-I", str(script), "_supervise", "--state-dir", str(state_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    status_path = state_dir / "status.json"
    deadline = now_ts() + 5.0
    while now_ts() < deadline:
        if status_path.is_file():
            try:
                st = read_json(status_path)
                if st.get("run_nonce") == expected_nonce and (
                    st.get("pid") is not None or st.get("state") in {"done", "failed"}
                ):
                    return st
            except Exception:
                pass
        time.sleep(0.05)
    return read_json(status_path) if status_path.is_file() else {"state": "starting"}


def _reset_status_before_launch(
    state_dir: Path, cfg: dict[str, Any], run_nonce: str, *, resume_thread_id: str | None = None
) -> None:
    started = now_ts()
    atomic_write_json(state_dir / "status.json", {
        "worker_id": cfg["worker_id"],
        "run_nonce": run_nonce,
        "state_dir": str(state_dir),
        "workdir": cfg["workdir"],
        "model": cfg["model"],
        "effort": cfg["effort"],
        "mode": "resume" if resume_thread_id else "start",
        "with_user_config": bool(cfg.get("with_user_config")),
        "codex_home": cfg["codex_home"],
        "supervisor_pid": None,
        "pid": None,
        "state": "starting",
        "phase": "starting",
        "anomaly": None,
        "thread_id": resume_thread_id,
        "started_at": started,
        "updated_at": started,
        "last_meaningful_at": started,
        "last_event_type": "starting",
        "last_event_summary": "spawning supervisor",
        "last_cmd": None,
        "cmd_started_at": None,
        "reconnect_streak": 0,
        "noise_count": 0,
        "events_count": 0,
        "exit_code": None,
        "finished_at": None,
        "raw_log_path": cfg["raw_log_path"],
        "last_message_path": cfg["last_message_path"],
        "tail_events": [],
    })


def _read_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    if prompt is not None:
        return prompt
    raise SystemExit("Provide either --prompt or --prompt-file")


def cmd_start(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise SystemExit(f"Workdir does not exist: {workdir}")
    worker_id = args.worker_id or f"worker-{int(now_ts())}-{os.getpid()}"
    state_dir = _resolve_state_dir(worker_id, args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    status_path = state_dir / "status.json"
    if status_path.is_file():
        old = read_json(status_path)
        if pid_alive(old.get("pid")):
            raise SystemExit(f"Worker {worker_id} is already running (pid={old.get('pid')})")

    prompt_text = _read_prompt(args.prompt, args.prompt_file)
    prompt_path = state_dir / "prompt.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    codex_home = prepare_codex_home(
        state_dir,
        with_user_config=args.with_user_config,
        source_codex_home=Path(args.source_codex_home) if args.source_codex_home else None,
        codex_bin=args.codex_bin,
    )
    run_nonce = uuid.uuid4().hex
    cfg = {
        "worker_id": worker_id,
        "run_nonce": run_nonce,
        "workdir": str(workdir),
        "codex_bin": args.codex_bin,
        "model": args.model,
        "effort": args.effort,
        "sandbox": args.sandbox,
        "with_user_config": args.with_user_config,
        "source_codex_home": args.source_codex_home,
        "allow_git_commit": args.allow_git_commit,
        "add_dirs": list(args.add_dir or []),
        "codex_home": str(codex_home),
        "prompt_path": str(prompt_path),
        "raw_log_path": str(state_dir / "events.log"),
        "last_message_path": str(state_dir / "last-message.md"),
        "resume_thread_id": None,
    }
    atomic_write_json(state_dir / "config.json", cfg)
    _reset_status_before_launch(state_dir, cfg, run_nonce)
    st = _launch_supervisor(state_dir, expected_nonce=run_nonce)
    print(json.dumps({
        "ok": True,
        "worker_id": worker_id,
        "state_dir": str(state_dir),
        "pid": st.get("pid"),
        "supervisor_pid": st.get("supervisor_pid"),
        "codex_home": str(codex_home),
        "state": st.get("state"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.worker_id, args.state_dir)
    cfg_path = state_dir / "config.json"
    status_path = state_dir / "status.json"
    if not cfg_path.is_file() or not status_path.is_file():
        raise SystemExit(f"Worker state not found in {state_dir}")
    old = read_json(status_path)
    if pid_alive(old.get("pid")):
        raise SystemExit(f"Worker is still running (pid={old.get('pid')}); wait or stop it first")
    thread_id = args.thread_id or old.get("thread_id")
    if not thread_id:
        raise SystemExit("No thread_id recorded for this worker; cannot resume")

    prompt_text = _read_prompt(args.prompt, args.prompt_file)
    prompt_path = state_dir / f"prompt-resume-{int(now_ts())}.txt"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    cfg = read_json(cfg_path)
    prepare_codex_home(
        state_dir,
        with_user_config=bool(cfg.get("with_user_config")),
        source_codex_home=Path(cfg["source_codex_home"]) if cfg.get("source_codex_home") else None,
        codex_bin=str(cfg.get("codex_bin") or "codex"),
    )
    run_nonce = uuid.uuid4().hex
    cfg["run_nonce"] = run_nonce
    cfg["prompt_path"] = str(prompt_path)
    cfg["resume_thread_id"] = thread_id
    if args.model:
        cfg["model"] = args.model
    if args.effort:
        cfg["effort"] = args.effort
    atomic_write_json(cfg_path, cfg)
    _reset_status_before_launch(state_dir, cfg, run_nonce, resume_thread_id=thread_id)
    st = _launch_supervisor(state_dir, expected_nonce=run_nonce)
    out: dict[str, Any] = {
        "ok": True,
        "worker_id": cfg["worker_id"],
        "state_dir": str(state_dir),
        "thread_id": thread_id,
        "pid": st.get("pid"),
        "state": st.get("state"),
    }
    if cfg.get("allow_git_commit") or cfg.get("add_dirs"):
        out["warning"] = (
            "codex exec resume does not support --add-dir; "
            "allow_git_commit/add_dirs are not re-applied on resume"
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _build_report(state_dir: Path, st: dict[str, Any], *, watch_status: str, reason: str = "") -> dict[str, Any]:
    workdir = Path(st["workdir"])
    snap = git_snapshot(workdir)
    now = now_ts()
    started = float(st.get("started_at") or now)
    finished = st.get("finished_at")
    elapsed = round((float(finished) if finished else now) - started, 1)
    last_meaningful = float(st.get("last_meaningful_at") or started)
    silent_s = round(max(0.0, now - last_meaningful), 1)
    last_msg_path = Path(st["last_message_path"])
    last_msg = ""
    if last_msg_path.is_file():
        last_msg = last_msg_path.read_text(encoding="utf-8", errors="replace").strip()[:800]
    return {
        "status": watch_status,
        "reason": reason,
        "worker_id": st.get("worker_id"),
        "state_dir": str(state_dir),
        "workdir": st.get("workdir"),
        "pid": st.get("pid"),
        "worker_alive": pid_alive(st.get("pid")),
        "state": st.get("state"),
        "phase": st.get("phase"),
        "anomaly": st.get("anomaly"),
        "thread_id": st.get("thread_id"),
        "exit_code": st.get("exit_code"),
        "elapsed_s": elapsed,
        "silent_s": silent_s,
        "reconnect_streak": st.get("reconnect_streak", 0),
        "noise_count": st.get("noise_count", 0),
        "events_count": st.get("events_count", 0),
        "last_cmd": st.get("last_cmd"),
        "last_event_summary": st.get("last_event_summary"),
        "git_dirty_count": snap.get("dirty_count", 0),
        "git_dirty_files": snap.get("dirty_files", []),
        "git_diff_stat": snap.get("diff_stat", ""),
        "last_message_preview": last_msg,
        "tail_events": st.get("tail_events", [])[-15:],
        "raw_log_path": st.get("raw_log_path"),
    }


def cmd_status(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.worker_id, args.state_dir)
    status_path = state_dir / "status.json"
    if not status_path.is_file():
        raise SystemExit(f"Worker status not found: {status_path}")
    st = read_json(status_path)
    report = _build_report(state_dir, st, watch_status=str(st.get("state") or "unknown"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.worker_id, args.state_dir)
    status_path = state_dir / "status.json"
    if not status_path.is_file():
        raise SystemExit(f"Worker status not found: {status_path}")

    watch_started = now_ts()
    stall_after = float(args.stall_after)
    cmd_stall = float(args.cmd_stall)
    max_reconnects = int(args.max_reconnects)
    poll = max(0.05, float(args.poll_interval))

    while True:
        st = read_json(status_path)
        state = str(st.get("state") or "")
        alive = pid_alive(st.get("pid")) or pid_alive(st.get("supervisor_pid"))

        if state in {"done", "failed", "stopped"} or not alive:
            rc = st.get("exit_code")
            final_status = "done" if (state == "done" and rc == 0) else (state or "exited")
            report = _build_report(state_dir, st, watch_status=final_status)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if (state == "done" and rc == 0) else 1

        now = now_ts()
        workdir = Path(st["workdir"])
        last_meaningful = float(st.get("last_meaningful_at") or st.get("started_at") or now)
        dirty_mtime = latest_dirty_mtime(workdir)
        effective_progress = max(last_meaningful, dirty_mtime)

        reason = ""
        if st.get("anomaly") == "approval_timeout":
            reason = "approval_timeout detected in codex output; worker left alive for inspection"
        elif int(st.get("reconnect_streak") or 0) >= max_reconnects:
            reason = (
                f"reconnect_streak={st['reconnect_streak']} >= {max_reconnects}; "
                "worker left alive for inspection"
            )
        elif (
            st.get("phase") == "running_cmd"
            and st.get("cmd_started_at") is not None
            and (now - max(float(st["cmd_started_at"]), dirty_mtime)) >= cmd_stall
        ):
            reason = (
                f"single shell command silent for >= {cmd_stall:g}s ({st.get('last_cmd')}); "
                "worker left alive for inspection"
            )
        elif (now - effective_progress) >= stall_after:
            reason = (
                f"no meaningful event or worktree write for >= {stall_after:g}s; "
                "worker left alive for inspection"
            )

        if reason:
            report = _build_report(state_dir, st, watch_status="needs_inspection", reason=reason)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2

        time.sleep(poll)


def cmd_stop(args: argparse.Namespace) -> int:
    state_dir = _resolve_state_dir(args.worker_id, args.state_dir)
    status_path = state_dir / "status.json"
    if not status_path.is_file():
        raise SystemExit(f"Worker status not found: {status_path}")
    st = read_json(status_path)
    pid = st.get("pid")
    st["state"] = "stopped"
    st["updated_at"] = now_ts()
    atomic_write_json(status_path, st)

    if isinstance(pid, int) and pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            os.kill(pid, signal.SIGTERM)
        deadline = now_ts() + float(args.grace_seconds)
        while now_ts() < deadline and pid_alive(pid):
            time.sleep(0.1)
        if pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGKILL)

    time.sleep(0.1)
    st = read_json(status_path)
    st["state"] = "stopped"
    st["finished_at"] = st.get("finished_at") or now_ts()
    atomic_write_json(status_path, st)
    report = _build_report(state_dir, st, watch_status="stopped", reason="stopped by caller")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deterministic supervisor for local codex exec workers")
    sub = p.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start a detached isolated codex exec worker")
    p_start.add_argument("--workdir", required=True)
    p_start.add_argument("--worker-id")
    p_start.add_argument("--state-dir")
    p_start.add_argument("--prompt")
    p_start.add_argument("--prompt-file")
    p_start.add_argument("--model", default=os.environ.get("DEV_LEVERAGE_CODEX_MODEL", ""))
    p_start.add_argument("--effort", default="high")
    p_start.add_argument("--sandbox", default="workspace-write")
    p_start.add_argument("--codex-bin", default="codex")
    p_start.add_argument("--source-codex-home")
    p_start.add_argument("--with-user-config", action="store_true")
    p_start.add_argument("--allow-git-commit", action="store_true")
    p_start.add_argument("--add-dir", action="append", default=[])

    p_watch = sub.add_parser("watch", help="Watch worker liveness; exit 2 without killing on stall")
    p_watch.add_argument("--worker-id")
    p_watch.add_argument("--state-dir")
    p_watch.add_argument("--stall-after", type=float, default=300.0)
    p_watch.add_argument("--cmd-stall", type=float, default=180.0)
    p_watch.add_argument("--max-reconnects", type=int, default=5)
    p_watch.add_argument("--poll-interval", type=float, default=2.0)

    p_status = sub.add_parser("status", help="Inspect current worker progress snapshot")
    p_status.add_argument("--worker-id")
    p_status.add_argument("--state-dir")

    p_resume = sub.add_parser("resume", help="Resume an existing worker session with a new prompt")
    p_resume.add_argument("--worker-id")
    p_resume.add_argument("--state-dir")
    p_resume.add_argument("--thread-id")
    p_resume.add_argument("--prompt")
    p_resume.add_argument("--prompt-file")
    p_resume.add_argument("--model")
    p_resume.add_argument("--effort")

    p_stop = sub.add_parser("stop", help="Gracefully stop a running worker and keep its state")
    p_stop.add_argument("--worker-id")
    p_stop.add_argument("--state-dir")
    p_stop.add_argument("--grace-seconds", type=float, default=5.0)

    p_sup = sub.add_parser("_supervise")
    p_sup.add_argument("--state-dir", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "resume":
        return cmd_resume(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "_supervise":
        return _run_supervisor(Path(args.state_dir).resolve())
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
