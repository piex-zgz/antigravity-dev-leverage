#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${DEV_LEVERAGE_REPO_URL:-https://github.com/piex-zgz/antigravity-dev-leverage.git}"
CONFIG_ROOT="${HOME}/.gemini/config"
PLUGIN_DIR="${CONFIG_ROOT}/plugins/dev-leverage"
CONFIG_JSON="${CONFIG_ROOT}/config.json"
PLUGIN_KEY="dev-leverage"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: Python 3 is required." >&2
  exit 1
fi

update_config_json() {
  local action="$1"
  python3 - "$CONFIG_JSON" "$PLUGIN_KEY" "$action" <<'PY'
import json
import os
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
plugin_key = sys.argv[2]
action = sys.argv[3]

cfg_path.parent.mkdir(parents=True, exist_ok=True)
mode = 0o600
cfg = {}
if cfg_path.is_file():
    mode = cfg_path.stat().st_mode & 0o777
    raw = cfg_path.read_text(encoding="utf-8").strip()
    if raw:
        cfg = json.loads(raw)

plugins = cfg.setdefault("plugins", {})
if not isinstance(plugins, dict):
    plugins = {}
    cfg["plugins"] = plugins

if action == "enable":
    entry = plugins.get(plugin_key)
    if not isinstance(entry, dict):
        entry = {}
    entry["enabled"] = True
    plugins[plugin_key] = entry
elif action == "disable":
    plugins.pop(plugin_key, None)

tmp = cfg_path.with_name(f"{cfg_path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
os.chmod(tmp, mode)
tmp.replace(cfg_path)
PY
}

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$PLUGIN_DIR"
  update_config_json disable
  echo "Uninstalled Dev Leverage from ${PLUGIN_DIR} and cleaned config entry."
  exit 0
fi

mkdir -p "${CONFIG_ROOT}/plugins"

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -f "${SCRIPT_DIR}/plugin.json" && -f "${SCRIPT_DIR}/hooks.json" ]]; then
  if [[ "$SCRIPT_DIR" != "$PLUGIN_DIR" ]]; then
    rm -rf "$PLUGIN_DIR"
    mkdir -p "$PLUGIN_DIR"
    rsync -a \
      --exclude='.git' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='.DS_Store' \
      "${SCRIPT_DIR}/" "${PLUGIN_DIR}/"
  fi
  echo "Installed Dev Leverage from local checkout -> ${PLUGIN_DIR}"
else
  if [[ -d "${PLUGIN_DIR}/.git" ]]; then
    git -C "$PLUGIN_DIR" pull --ff-only
  else
    rm -rf "$PLUGIN_DIR"
    git clone --depth 1 "$REPO_URL" "$PLUGIN_DIR"
  fi
  echo "Installed Dev Leverage from ${REPO_URL} -> ${PLUGIN_DIR}"
fi

update_config_json enable

cat <<'EOF'

Done! Dev Leverage is installed and enabled.

What activates automatically in your next Antigravity turn:
  - Core rules (rules/AGENTS.md, 21 lines)
  - Lifecycle hooks (session baseline snapshot, context health & rework stop-loss, stop evidence gate, codex exec guard)
  - On-demand skills (explore-first, gate-verify, walkthrough, loop-anchor, rsi-retro, orchestrate)
  - Independent read-only review subagents (entropy-reviewer, drift-watcher)
EOF
