from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HealthConfig:
    url: str = "https://chatgpt.com/backend-api/codex"
    proxy_url: str | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None
    timeout_seconds: float = 8.0
    required_consecutive_successes: int = 2
    required_consecutive_failures: int = 2


@dataclass(frozen=True)
class TargetConfig:
    name: str
    command: tuple[str, ...]
    codex_home: str
    max_thread_age_seconds: int = 86_400
    thread_limit: int = 50
    resume_grace_seconds: float = 2.0
    start_recovery_turn: bool = True


@dataclass(frozen=True)
class GuardianConfig:
    state_path: str
    log_path: str
    health: HealthConfig
    targets: tuple[TargetConfig, ...]
    recovery_prompt: str


DEFAULT_RECOVERY_PROMPT = (
    "Network connectivity has recovered. Reconcile the previous turn's terminal "
    "state first. Do not repeat commands or mutations already recorded as "
    "successful. Then continue the active Goal autonomously. If user input or "
    "approval is required, stop and ask once."
)


def load_config(path: str | Path) -> GuardianConfig:
    source = Path(path).expanduser()
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != 1:
        raise ValueError("config schema_version must be 1")

    health_data = payload.get("health", {})
    health = HealthConfig(
        url=str(health_data.get("url", HealthConfig.url)),
        proxy_url=_optional_string(health_data.get("proxy_url")),
        tcp_host=_optional_string(health_data.get("tcp_host")),
        tcp_port=_optional_int(health_data.get("tcp_port")),
        timeout_seconds=float(
            health_data.get("timeout_seconds", HealthConfig.timeout_seconds)
        ),
        required_consecutive_successes=int(
            health_data.get(
                "required_consecutive_successes",
                HealthConfig.required_consecutive_successes,
            )
        ),
        required_consecutive_failures=int(
            health_data.get(
                "required_consecutive_failures",
                HealthConfig.required_consecutive_failures,
            )
        ),
    )
    if health.required_consecutive_successes < 1:
        raise ValueError("required_consecutive_successes must be at least 1")
    if health.required_consecutive_failures < 1:
        raise ValueError("required_consecutive_failures must be at least 1")

    targets = tuple(_target_from_dict(item) for item in payload.get("targets", []))
    if not targets:
        raise ValueError("config must define at least one target")
    names = [target.name for target in targets]
    if len(names) != len(set(names)):
        raise ValueError("target names must be unique")

    return GuardianConfig(
        state_path=str(payload["state_path"]),
        log_path=str(payload["log_path"]),
        health=health,
        targets=targets,
        recovery_prompt=str(payload.get("recovery_prompt", DEFAULT_RECOVERY_PROMPT)),
    )


def _target_from_dict(data: dict[str, Any]) -> TargetConfig:
    command = data.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise ValueError("target command must be a non-empty string array")
    return TargetConfig(
        name=str(data["name"]),
        command=tuple(command),
        codex_home=str(data["codex_home"]),
        max_thread_age_seconds=int(data.get("max_thread_age_seconds", 86_400)),
        thread_limit=int(data.get("thread_limit", 50)),
        resume_grace_seconds=float(data.get("resume_grace_seconds", 2.0)),
        start_recovery_turn=bool(data.get("start_recovery_turn", True)),
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
