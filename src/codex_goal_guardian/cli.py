from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .app_server import AppServerClient
from .config import GuardianConfig, load_config
from .engine import RecoveryEngine
from .health import probe_health
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-goal-guardian",
        description="Resume eligible active Codex Goals after connectivity recovers.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    doctor = subparsers.add_parser("doctor", help="check runtime compatibility")
    _add_config_argument(doctor)
    _add_json_argument(doctor)

    run_once = subparsers.add_parser(
        "run-once", help="perform one health transition and recovery pass"
    )
    _add_config_argument(run_once)
    run_once.add_argument(
        "--dry-run",
        action="store_true",
        help="list eligible recovery actions without mutating Codex threads",
    )
    _add_json_argument(run_once)

    watch = subparsers.add_parser(
        "watch", help="run recovery checks until interrupted"
    )
    _add_config_argument(watch)
    watch.add_argument(
        "--interval",
        type=_positive_float,
        default=60.0,
        help="seconds between checks (default: 60)",
    )
    watch.add_argument(
        "--dry-run",
        action="store_true",
        help="observe eligible actions without mutating Codex threads",
    )
    _add_json_argument(watch)

    status = subparsers.add_parser("status", help="show persisted Guardian state")
    _add_config_argument(status)
    _add_json_argument(status)

    hook = subparsers.add_parser(
        "hook-record", help="record a privacy-filtered Codex hook event"
    )
    hook.add_argument(
        "--log",
        default=str(default_hook_log_path()),
        help="JSONL path for compact hook evidence",
    )
    _add_json_argument(hook)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command_name == "doctor":
            config = load_config(arguments.config)
            report = doctor_config(config, config_path=arguments.config)
            _emit(report, arguments.json_output)
            return 0 if report["ok"] else 2
        if arguments.command_name == "run-once":
            config = load_config(arguments.config)
            report = RecoveryEngine().run_once(
                config, dry_run=arguments.dry_run
            )
            if _report_worth_logging(report):
                append_json_log(
                    config.log_path, {"kind": "run_once", "report": report}
                )
            _emit(report, arguments.json_output)
            return 2 if _report_has_errors(report) else 0
        if arguments.command_name == "watch":
            return _watch(arguments)
        if arguments.command_name == "status":
            config = load_config(arguments.config)
            report = {
                "state_path": config.state_path,
                "state": StateStore(config.state_path).load(),
            }
            _emit(report, arguments.json_output)
            return 0
        if arguments.command_name == "hook-record":
            event = record_hook_event(arguments.log, sys.stdin.read())
            _emit(event, arguments.json_output)
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        payload = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}"[:1000],
        }
        _emit(payload, getattr(arguments, "json_output", False), error=True)
        return 0 if arguments.command_name == "hook-record" else 2
    parser.error(f"unsupported command: {arguments.command_name}")
    return 2


def doctor_config(
    config: GuardianConfig, *, config_path: str | Path
) -> dict[str, Any]:
    wsl = running_under_wsl()
    health = probe_health(config.health)
    report: dict[str, Any] = {
        "ok": True,
        "version": __version__,
        "config_path": str(Path(config_path).expanduser()),
        "platform": platform.platform(),
        "is_wsl": wsl,
        "python": sys.version.split()[0],
        "health": {
            "healthy": health.healthy,
            "reason": health.reason,
            "status_code": health.status_code,
            "elapsed_ms": health.elapsed_ms,
        },
        "targets": [],
    }
    for target in config.targets:
        target_report = inspect_target(
            target.name,
            target.command,
            target.codex_home,
            is_wsl=wsl,
        )
        report["targets"].append(target_report)
        if target_report["errors"]:
            report["ok"] = False
    return report


def inspect_target(
    name: str,
    command: Sequence[str],
    codex_home: str,
    *,
    is_wsl: bool,
) -> dict[str, Any]:
    requested = str(command[0])
    resolved = _resolve_executable(requested)
    report: dict[str, Any] = {
        "name": name,
        "command": list(command),
        "resolved_executable": resolved,
        "codex_home": str(Path(codex_home).expanduser()),
        "version": None,
        "app_server": False,
        "app_server_rpc": False,
        "app_server_thread_sample_count": None,
        "app_server_goal_rpc": None,
        "app_server_thread_read_rpc": None,
        "app_server_sample_goal_status": None,
        "warnings": [],
        "errors": [],
    }
    if resolved is None:
        report["errors"].append("command_not_found")
        return report
    if is_windows_shim_under_wsl(requested, resolved, is_wsl=is_wsl):
        report["errors"].append("windows_shim_under_wsl")
        return report

    resolved_command = (resolved, *command[1:])
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(Path(codex_home).expanduser())
    version = _run_bounded((*resolved_command, "--version"), environment)
    if version["returncode"] != 0:
        report["errors"].append(f"version_check_failed: {version['detail']}")
    else:
        report["version"] = version["detail"].splitlines()[0][:200]

    app_server = _run_bounded(
        (*resolved_command, "app-server", "--help"), environment
    )
    if app_server["returncode"] == 0:
        report["app_server"] = True
    else:
        report["errors"].append(
            f"app_server_unavailable: {app_server['detail']}"
        )
    if not Path(codex_home).expanduser().exists():
        report["warnings"].append("codex_home_does_not_exist_yet")
    if report["app_server"]:
        try:
            with AppServerClient(
                command=(
                    *resolved_command,
                    "app-server",
                    "--listen",
                    "stdio://",
                ),
                codex_home=codex_home,
                timeout_seconds=20,
            ) as client:
                threads = client.list_threads(limit=1)
                if (
                    threads
                    and isinstance(threads[0], dict)
                    and isinstance(threads[0].get("id"), str)
                ):
                    thread_id = threads[0]["id"]
                    goal = client.get_goal(thread_id)
                    client.read_thread(thread_id, include_turns=False)
                    report["app_server_goal_rpc"] = True
                    report["app_server_thread_read_rpc"] = True
                    if isinstance(goal, dict):
                        report["app_server_sample_goal_status"] = goal.get(
                            "status"
                        )
            report["app_server_rpc"] = True
            report["app_server_thread_sample_count"] = len(threads)
        except Exception as error:
            report["errors"].append(
                f"app_server_rpc_failed: {type(error).__name__}: {error}"[:700]
            )
    return report


def is_windows_shim_under_wsl(
    requested: str, resolved: str, *, is_wsl: bool
) -> bool:
    if not is_wsl:
        return False
    candidates = (requested, resolved)
    for candidate in candidates:
        normalized = candidate.replace("\\", "/").lower()
        if normalized.startswith("/mnt/"):
            return True
        if len(normalized) > 2 and normalized[1:3] == ":/":
            return True
        if normalized.endswith((".cmd", ".bat", ".ps1", ".exe")):
            return True
    return False


def running_under_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return "microsoft" in release.lower()


def record_hook_event(path: str | Path, raw_input: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_input) if raw_input.strip() else {}
    except json.JSONDecodeError as error:
        payload = {"reason": f"invalid hook JSON: {error.msg}"}
    if not isinstance(payload, dict):
        payload = {"reason": "hook payload root was not an object"}

    event: dict[str, Any] = {
        "timestamp": int(time.time()),
        "kind": "hook",
    }
    for key in (
        "hook_event_name",
        "session_id",
        "thread_id",
        "turn_id",
        "reason",
        "error",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            event[key] = str(value)[:500]
    append_json_log(path, event)
    return event


def append_json_log(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with StateStore(destination).locked():
        if destination.exists() and destination.stat().st_size >= 5 * 1024 * 1024:
            os.replace(
                destination,
                destination.with_name(destination.name + ".1"),
            )
        with destination.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )


def default_config_path() -> Path:
    configured = os.environ.get("CODEX_GOAL_GUARDIAN_CONFIG")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData/Local"
        return root / "CodexGoalGuardian/config.json"
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "codex-goal-guardian/config.json"


def default_hook_log_path() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) if local_app_data else Path.home() / "AppData/Local"
        return root / "CodexGoalGuardian/hook-events.jsonl"
    state_root = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return state_root / "codex-goal-guardian/hook-events.jsonl"


def _watch(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    engine = RecoveryEngine()
    last_log_fingerprint: str | None = None
    last_logged_at = 0.0
    while True:
        report = engine.run_once(config, dry_run=arguments.dry_run)
        if _report_worth_logging(report):
            fingerprint = _report_log_fingerprint(report)
            current_time = time.monotonic()
            if (
                fingerprint != last_log_fingerprint
                or current_time - last_logged_at >= 300
            ):
                append_json_log(
                    config.log_path, {"kind": "watch", "report": report}
                )
                last_log_fingerprint = fingerprint
                last_logged_at = current_time
        _emit(report, arguments.json_output)
        delay = arguments.interval
        if _report_has_errors(report):
            delay = max(delay, 60)
        time.sleep(delay)


def _run_bounded(
    command: Sequence[str], environment: dict[str, str]
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"returncode": None, "detail": str(error)[:500]}
    detail = (completed.stdout or completed.stderr or "").strip()
    return {"returncode": completed.returncode, "detail": detail[:500]}


def _resolve_executable(command: str) -> str | None:
    expanded = str(Path(command).expanduser())
    if Path(expanded).is_file():
        return str(Path(expanded).resolve())
    return shutil.which(command)


def _report_has_errors(report: dict[str, Any]) -> bool:
    return any(target.get("errors") for target in report.get("targets", []))


def _report_worth_logging(report: dict[str, Any]) -> bool:
    return any(
        target.get("state_changed")
        or target.get("actions")
        or target.get("errors")
        or target.get("status") in {"recovered", "recovery_pending"}
        for target in report.get("targets", [])
    )


def _report_log_fingerprint(report: dict[str, Any]) -> str:
    stable = {
        "healthy": report.get("healthy"),
        "health_reason": report.get("health_reason"),
        "targets": [
            {
                "name": target.get("name"),
                "status": target.get("status"),
                "outage_generation": target.get("outage_generation"),
                "state_changed": target.get("state_changed"),
                "actions": target.get("actions"),
                "errors": target.get("errors"),
            }
            for target in report.get("targets", [])
        ],
    }
    return json.dumps(stable, ensure_ascii=False, sort_keys=True)


def _emit(payload: dict[str, Any], as_json: bool, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if as_json:
        print(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            file=stream,
        )
        return
    if "error" in payload:
        print(payload["error"], file=stream)
        return
    if "targets" in payload:
        health = payload.get("health") or {
            "healthy": payload.get("healthy"),
            "reason": payload.get("health_reason"),
        }
        print(
            f"health={'up' if health.get('healthy') else 'down'} "
            f"({health.get('reason')})",
            file=stream,
        )
        for target in payload["targets"]:
            status = target.get("status") or (
                "ok" if not target.get("errors") else "error"
            )
            print(f"{target.get('name')}: {status}", file=stream)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Guardian JSON configuration path",
    )


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit machine-readable JSON",
    )


def _positive_float(value: str) -> float:
    result = float(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result
