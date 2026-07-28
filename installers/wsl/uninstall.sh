#!/usr/bin/env bash
set -euo pipefail

dry_run=0
purge_data=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) dry_run=1 ;;
        --purge-data) purge_data=1 ;;
        -h|--help)
            printf 'Usage: uninstall.sh [--dry-run] [--purge-data]\n'
            exit 0
            ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

data_root="${XDG_DATA_HOME:-$HOME/.local/share}"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
state_root="${XDG_STATE_HOME:-$HOME/.local/state}"
install_root="$data_root/codex-goal-guardian"
config_dir="$config_root/codex-goal-guardian"
guardian_state_dir="$state_root/codex-goal-guardian"
unit_dir="$config_root/systemd/user"
service_path="$unit_dir/codex-goal-guardian.service"
timer_path="$unit_dir/codex-goal-guardian.timer"

if [ "$dry_run" -eq 1 ]; then
    printf '[Codex Goal Guardian] dry-run: disable codex-goal-guardian.timer\n'
    printf '[Codex Goal Guardian] dry-run: remove %s\n' "$install_root"
    if [ "$purge_data" -eq 1 ]; then
        printf '[Codex Goal Guardian] dry-run: purge %s and %s\n' \
            "$config_dir" "$guardian_state_dir"
    fi
    exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now codex-goal-guardian.timer >/dev/null 2>&1 || true
fi
rm -f -- "$service_path" "$timer_path"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
fi
rm -rf -- "$install_root"

if [ "$purge_data" -eq 1 ]; then
    rm -rf -- "$config_dir" "$guardian_state_dir"
else
    printf '[Codex Goal Guardian] preserved configuration and state under %s and %s\n' \
        "$config_dir" "$guardian_state_dir"
fi
