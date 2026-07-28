#!/usr/bin/env bash
set -euo pipefail

dry_run=0
with_systemd=0
force_config=0
codex_command=""
node_command=""
proxy_url=""
tcp_host=""
tcp_port=""

usage() {
    printf '%s\n' \
        "Usage: install.sh [--dry-run] [--with-systemd] [--force-config]" \
        "                  [--codex-command PATH] [--node-command PATH]" \
        "                  [--proxy-url URL]" \
        "                  [--tcp-host HOST] [--tcp-port PORT]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --with-systemd) with_systemd=1 ;;
        --force-config) force_config=1 ;;
        --codex-command|--node-command|--proxy-url|--tcp-host|--tcp-port)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            case "$1" in
                --codex-command) codex_command="$2" ;;
                --node-command) node_command="$2" ;;
                --proxy-url) proxy_url="$2" ;;
                --tcp-host) tcp_host="$2" ;;
                --tcp-port) tcp_port="$2" ;;
            esac
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

source_root="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
state_root="${XDG_STATE_HOME:-$HOME/.local/state}"
install_root="$data_root/codex-goal-guardian"
runtime_root="$install_root/runtime"
config_dir="$config_root/codex-goal-guardian"
guardian_state_dir="$state_root/codex-goal-guardian"
config_path="$config_dir/config.json"
state_path="$guardian_state_dir/state.json"
log_path="$guardian_state_dir/guardian.jsonl"
bin_dir="$install_root/bin"
launcher="$bin_dir/codex-goal-guardian"

if [ -z "$codex_command" ]; then
    codex_command="$(command -v codex || true)"
fi
if [ -z "$codex_command" ] || [ ! -x "$codex_command" ]; then
    printf 'A native executable Codex CLI is required; pass --codex-command.\n' >&2
    exit 1
fi
case "$codex_command" in
    /mnt/*|*.exe|*.cmd|*.bat|*.ps1)
        printf 'Refusing Windows Codex shim under WSL: %s\n' "$codex_command" >&2
        exit 1
        ;;
esac

codex_real="$(readlink -f "$codex_command")"
guardian_command_0="$codex_command"
guardian_command_1=""
case "$codex_real" in
    *.js)
        if [ -z "$node_command" ]; then
            node_command="$(command -v node || true)"
        fi
        if [ -z "$node_command" ] || [ ! -x "$node_command" ]; then
            printf 'Codex uses a JavaScript entrypoint; pass a native --node-command.\n' >&2
            exit 1
        fi
        node_command="$(readlink -f "$node_command")"
        node_major="$("$node_command" -p 'process.versions.node.split(".")[0]')"
        if [ "$node_major" -lt 18 ]; then
            printf 'Node 18 or newer is required; resolved %s.\n' "$node_command" >&2
            exit 1
        fi
        guardian_command_0="$node_command"
        guardian_command_1="$codex_real"
        ;;
esac

if [ -n "$guardian_command_1" ]; then
    version_output="$("$guardian_command_0" "$guardian_command_1" --version)"
    "$guardian_command_0" "$guardian_command_1" app-server --help >/dev/null
else
    version_output="$("$guardian_command_0" --version)"
    "$guardian_command_0" app-server --help >/dev/null
fi
printf '[Codex Goal Guardian] verified %s via %s\n' \
    "$version_output" "$guardian_command_0"

if [ "$dry_run" -eq 1 ]; then
    printf '[Codex Goal Guardian] dry-run: copy runtime to %s\n' "$runtime_root"
    printf '[Codex Goal Guardian] dry-run: write configuration to %s\n' "$config_path"
    if [ "$with_systemd" -eq 1 ]; then
        printf '[Codex Goal Guardian] dry-run: install codex-goal-guardian.timer\n'
    fi
    exit 0
fi

install -d -m 700 "$install_root" "$config_dir" "$guardian_state_dir" "$bin_dir"
staging="$(mktemp -d "$install_root/.runtime.XXXXXX")"
cleanup() {
    if [ -d "$staging" ]; then
        rm -rf -- "$staging"
    fi
}
trap cleanup EXIT
cp -R -- "$source_root/src" "$staging/src"
cp -R -- "$source_root/scripts" "$staging/scripts"
cp -- "$source_root/pyproject.toml" "$staging/pyproject.toml"
chmod +x "$staging/scripts/run-wsl.sh"
if [ -d "$runtime_root" ]; then
    old_runtime="$install_root/.runtime.previous"
    rm -rf -- "$old_runtime"
    mv -- "$runtime_root" "$old_runtime"
    mv -- "$staging" "$runtime_root"
    rm -rf -- "$old_runtime"
else
    mv -- "$staging" "$runtime_root"
fi
ln -sfn -- "$runtime_root/scripts/run-wsl.sh" "$launcher"

if [ "$force_config" -eq 1 ] || [ ! -f "$config_path" ]; then
    GUARDIAN_CONFIG_PATH="$config_path" \
    GUARDIAN_STATE_PATH="$state_path" \
    GUARDIAN_LOG_PATH="$log_path" \
    GUARDIAN_COMMAND_0="$guardian_command_0" \
    GUARDIAN_COMMAND_1="$guardian_command_1" \
    GUARDIAN_PROXY_URL="$proxy_url" \
    GUARDIAN_TCP_HOST="$tcp_host" \
    GUARDIAN_TCP_PORT="$tcp_port" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

tcp_port = os.environ["GUARDIAN_TCP_PORT"]
payload = {
    "schema_version": 1,
    "state_path": os.environ["GUARDIAN_STATE_PATH"],
    "log_path": os.environ["GUARDIAN_LOG_PATH"],
    "health": {
        "url": "https://chatgpt.com/backend-api/codex",
        "proxy_url": os.environ["GUARDIAN_PROXY_URL"] or None,
        "tcp_host": os.environ["GUARDIAN_TCP_HOST"] or None,
        "tcp_port": int(tcp_port) if tcp_port else None,
        "timeout_seconds": 8,
        "required_consecutive_successes": 2,
        "required_consecutive_failures": 2,
    },
    "targets": [
        {
            "name": "wsl",
            "command": [
                item
                for item in (
                    os.environ["GUARDIAN_COMMAND_0"],
                    os.environ["GUARDIAN_COMMAND_1"],
                )
                if item
            ],
            "codex_home": str(Path.home() / ".codex"),
            "allowed_sources": ["cli", "exec"],
            "max_thread_age_seconds": 86400,
            "thread_limit": 50,
            "resume_grace_seconds": 2,
            "start_recovery_turn": True,
        }
    ],
}
destination = Path(os.environ["GUARDIAN_CONFIG_PATH"])
destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
    chmod 600 "$config_path"
    printf '[Codex Goal Guardian] wrote %s\n' "$config_path"
else
    printf '[Codex Goal Guardian] preserved %s (use --force-config to replace)\n' "$config_path"
fi

if [ "$with_systemd" -eq 1 ]; then
    unit_dir="$config_root/systemd/user"
    install -d -m 700 "$unit_dir"
    service_path="$unit_dir/codex-goal-guardian.service"
    timer_path="$unit_dir/codex-goal-guardian.timer"
    {
        printf '%s\n' \
            '[Unit]' \
            'Description=Codex Goal Guardian recovery check' \
            '' \
            '[Service]' \
            'Type=oneshot' \
            "ExecStart=$launcher run-once --config $config_path --json"
    } >"$service_path"
    {
        printf '%s\n' \
            '[Unit]' \
            'Description=Run Codex Goal Guardian every minute' \
            '' \
            '[Timer]' \
            'OnBootSec=1min' \
            'OnUnitActiveSec=1min' \
            'Persistent=true' \
            'Unit=codex-goal-guardian.service' \
            '' \
            '[Install]' \
            'WantedBy=timers.target'
    } >"$timer_path"
    systemctl --user daemon-reload
    systemctl --user enable --now codex-goal-guardian.timer
    printf '[Codex Goal Guardian] enabled codex-goal-guardian.timer\n'
fi

printf '[Codex Goal Guardian] installation complete\n'
