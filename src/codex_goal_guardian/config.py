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
    app_server_url: str | None = None
    recovery_mode: str = "cli_turn"
    allowed_sources: tuple[str, ...] = ("cli", "exec")
    max_thread_age_seconds: int = 86_400
    thread_limit: int = 50
    resume_grace_seconds: float = 2.0
    start_recovery_turn: bool = True
    model_capacity_retry_limit: int = 10
    model_capacity_backoff_initial_seconds: int = 15
    model_capacity_backoff_max_seconds: int = 600
    model_capacity_fallback_models: tuple[str, ...] = ()
    desktop_thread_ids: tuple[str, ...] = ()


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
    recovery_mode = str(data.get("recovery_mode", "cli_turn")).strip().lower()
    if recovery_mode not in {"cli_turn", "desktop_goal_state"}:
        raise ValueError(
            "target recovery_mode must be cli_turn or desktop_goal_state"
        )
    app_server_url = _optional_string(data.get("app_server_url"))
    if recovery_mode == "desktop_goal_state" and app_server_url is None:
        raise ValueError(
            "desktop_goal_state target requires app_server_url so the "
            "Guardian and Desktop app share one runtime"
        )
    model_capacity_retry_limit = int(
        data.get("model_capacity_retry_limit", 10)
    )
    model_capacity_backoff_initial_seconds = int(
        data.get("model_capacity_backoff_initial_seconds", 15)
    )
    model_capacity_backoff_max_seconds = int(
        data.get("model_capacity_backoff_max_seconds", 600)
    )
    if model_capacity_retry_limit < 1:
        raise ValueError("model_capacity_retry_limit must be at least 1")
    if model_capacity_backoff_initial_seconds < 1:
        raise ValueError(
            "model_capacity_backoff_initial_seconds must be at least 1"
        )
    if (
        model_capacity_backoff_max_seconds
        < model_capacity_backoff_initial_seconds
    ):
        raise ValueError(
            "model_capacity_backoff_max_seconds must be greater than or "
            "equal to model_capacity_backoff_initial_seconds"
        )
    return TargetConfig(
        name=str(data["name"]),
        command=tuple(command),
        codex_home=str(data["codex_home"]),
        app_server_url=app_server_url,
        recovery_mode=recovery_mode,
        allowed_sources=_allowed_sources(data.get("allowed_sources")),
        max_thread_age_seconds=int(data.get("max_thread_age_seconds", 86_400)),
        thread_limit=int(data.get("thread_limit", 50)),
        resume_grace_seconds=float(data.get("resume_grace_seconds", 2.0)),
        start_recovery_turn=bool(data.get("start_recovery_turn", True)),
        model_capacity_retry_limit=model_capacity_retry_limit,
        model_capacity_backoff_initial_seconds=(
            model_capacity_backoff_initial_seconds
        ),
        model_capacity_backoff_max_seconds=(
            model_capacity_backoff_max_seconds
        ),
        model_capacity_fallback_models=_model_capacity_fallback_models(
            data.get("model_capacity_fallback_models")
        ),
        desktop_thread_ids=_desktop_thread_ids(
            data.get("desktop_thread_ids")
        ),
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


def _allowed_sources(value: Any) -> tuple[str, ...]:
    if value is None:
        return ("cli", "exec")
    if not isinstance(value, list):
        raise ValueError("target allowed_sources must be a non-empty string array")
    normalized = tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )
    if not normalized or len(normalized) != len(value):
        raise ValueError("target allowed_sources must be a non-empty string array")
    return normalized


def _model_capacity_fallback_models(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(
            "model_capacity_fallback_models must be a string array"
        )
    normalized = tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )
    if len(normalized) != len(value):
        raise ValueError(
            "model_capacity_fallback_models must contain non-empty strings"
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError("model_capacity_fallback_models must be unique")
    if any(len(item) > 128 for item in normalized):
        raise ValueError(
            "model_capacity_fallback_models values must be at most 128 characters"
        )
    return normalized


def _desktop_thread_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("target desktop_thread_ids must be a string array")
    normalized = tuple(
        dict.fromkeys(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )
    if (
        len(normalized) != len(value)
        or any(len(thread_id) > 128 for thread_id in normalized)
    ):
        raise ValueError(
            "target desktop_thread_ids must contain unique non-empty "
            "values up to 128 characters"
        )
    return normalized
