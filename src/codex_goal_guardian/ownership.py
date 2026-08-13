from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, Sequence

from .config import TargetConfig


CREATE_NO_WINDOW = 0x08000000
_WINDOWS_PROCESS_CACHE: tuple[float, list[dict[str, object]]] | None = None
_WINDOWS_PROCESS_CACHE_SECONDS = 5.0


def cli_process_is_running(target: TargetConfig) -> bool:
    """Return whether another process is running the configured Codex CLI."""
    if os.name == "nt":
        return _windows_cli_process_is_running(target.command)
    return _proc_cli_process_is_running(target.command)


def desktop_uses_shared_app_server(target: TargetConfig) -> bool:
    """Return whether Codex Desktop has left its embedded App Server."""
    if os.name != "nt":
        return True
    if (
        not target.app_server_url
        or _windows_user_environment_value("CODEX_APP_SERVER_WS_URL")
        != target.app_server_url
    ):
        return False

    return _desktop_processes_use_shared_app_server(
        _windows_process_records()
    )


def _windows_user_environment_value(name: str) -> str | None:
    """Read a persisted per-user environment value without process inheritance."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (ImportError, OSError):
        return None
    return value if isinstance(value, str) else None


def _desktop_processes_use_shared_app_server(
    records: Iterable[dict[str, object]],
) -> bool:
    app_running = False
    embedded_app_server_running = False
    for record in records:
        line = " ".join(
            str(record.get(key) or "")
            for key in ("ExecutablePath", "CommandLine")
        )
        normalized = line.replace("\\", "/").casefold()
        if "/windowsapps/openai.codex_" not in normalized:
            continue
        if "/app/resources/codex.exe" in normalized and "app-server" in normalized:
            embedded_app_server_running = True
        elif (
            "/app/chatgpt.exe" in normalized
            or "/app/codex.exe" in normalized
        ):
            app_running = True
    return app_running and not embedded_app_server_running


def _proc_cli_process_is_running(command: Sequence[str]) -> bool:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        raise RuntimeError("process ownership probe requires /proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        arguments = [
            item.decode("utf-8", errors="replace")
            for item in raw.split(b"\0")
            if item
        ]
        if _is_app_server_arguments(arguments):
            continue
        if _arguments_match_command(arguments, command):
            return True
    return False


def _windows_cli_process_is_running(command: Sequence[str]) -> bool:
    records = _windows_process_records()
    for record in records:
        if int(record.get("ProcessId") or -1) == os.getpid():
            continue
        line = " ".join(
            str(record.get(key) or "")
            for key in ("ExecutablePath", "CommandLine")
        )
        if _is_app_server_command_line(line):
            continue
        if _command_line_matches(line, command):
            return True
    return False


def _windows_process_records() -> list[dict[str, object]]:
    global _WINDOWS_PROCESS_CACHE
    now = time.monotonic()
    if (
        _WINDOWS_PROCESS_CACHE is not None
        and now - _WINDOWS_PROCESS_CACHE[0] <= _WINDOWS_PROCESS_CACHE_SECONDS
    ):
        return [dict(record) for record in _WINDOWS_PROCESS_CACHE[1]]
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise RuntimeError("powershell is required for process ownership probe")
    try:
        completed = subprocess.run(
            (
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Select-Object ProcessId,ExecutablePath,CommandLine | "
                    "ConvertTo-Json -Compress"
                ),
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        if _WINDOWS_PROCESS_CACHE is not None:
            return [dict(record) for record in _WINDOWS_PROCESS_CACHE[1]]
        raise
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"Windows process ownership probe failed: {detail[:500]}"
        )
    try:
        payload = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Windows process ownership probe returned invalid JSON"
        ) from error
    records = payload if isinstance(payload, list) else [payload]
    normalized = [record for record in records if isinstance(record, dict)]
    _WINDOWS_PROCESS_CACHE = (now, normalized)
    return [dict(record) for record in normalized]


def _arguments_match_command(
    arguments: Iterable[str], command: Sequence[str]
) -> bool:
    actual = {_normalize_token(item) for item in arguments if item}
    expected = {_normalize_token(item) for item in _command_signature(command)}
    return bool(expected) and expected.issubset(actual)


def _command_line_matches(line: str, command: Sequence[str]) -> bool:
    normalized_line = _normalize_token(line)
    expected = [
        _normalize_token(item) for item in _command_signature(command)
    ]
    return bool(expected) and all(
        _command_line_contains(normalized_line, item) for item in expected
    )


def _is_app_server_arguments(arguments: Iterable[str]) -> bool:
    return any(item.casefold() == "app-server" for item in arguments)


def _is_app_server_command_line(line: str) -> bool:
    return bool(
        re.search(r"(?i)(?:^|\s|[\"'])app-server(?:$|\s|[\"'])", line)
    )


def _command_line_contains(line: str, expected: str) -> bool:
    if "/" in expected:
        return expected in line
    executable_suffix = ""
    if not expected.endswith((".exe", ".cmd", ".bat")):
        executable_suffix = r"(?:\.(?:exe|cmd|bat))?"
    pattern = (
        rf"(?<![a-z0-9_.-]){re.escape(expected)}"
        rf"{executable_suffix}(?![a-z0-9_.-])"
    )
    return re.search(pattern, line) is not None


def _command_signature(command: Sequence[str]) -> tuple[str, ...]:
    for item in reversed(command):
        if item.lower().endswith((".js", ".py")):
            return (item,)
    return (command[0],) if command else ()


def _normalize_token(value: str) -> str:
    normalized = value.strip().strip('"').replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.casefold()
    return normalized
