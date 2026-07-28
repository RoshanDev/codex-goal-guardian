#!/usr/bin/env sh
set -eu

resolved_script="$(readlink -f "$0")"
runtime_root="$(CDPATH='' cd -- "$(dirname -- "$resolved_script")/.." && pwd)"
install_root="$(dirname -- "$runtime_root")"
config_path="${CODEX_GOAL_GUARDIAN_CONFIG:-$install_root/config.json}"
export PYTHONPATH="$runtime_root/src${PYTHONPATH:+:$PYTHONPATH}"

if [ "$#" -eq 0 ]; then
    set -- run-once --config "$config_path" --json
fi

exec python3 -m codex_goal_guardian "$@"
