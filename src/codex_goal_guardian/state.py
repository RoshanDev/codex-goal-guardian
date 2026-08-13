from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, MutableMapping


SCHEMA_VERSION = 1


class StateCorruptionError(RuntimeError):
    """Raised when persisted Guardian state cannot be trusted."""


@dataclass(frozen=True)
class HealthTransition:
    recover_now: bool
    outage_generation: int
    outage_started_at: int | None
    health: str
    consecutive_healthy: int
    consecutive_unhealthy: int


def default_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "targets": {}}


def _target_state(state: MutableMapping[str, Any], target_name: str) -> dict[str, Any]:
    targets = state.setdefault("targets", {})
    target = targets.setdefault(
        target_name,
        {
            "health": "unknown",
            "consecutive_healthy": 0,
            "consecutive_unhealthy": 0,
            "unhealthy_started_at": None,
            "outage_generation": 0,
            "outage_started_at": None,
            "recovery_pending": False,
            "recovered": {},
            "desktop_request_generation": 0,
            "desktop_recovery_requests": {},
            "desktop_direct_recoveries": {},
            "desktop_active_observations": {},
            "model_capacity_recoveries": {},
            "delegated_cli_recoveries": {},
        },
    )
    target.setdefault("health", "unknown")
    target.setdefault("consecutive_healthy", 0)
    target.setdefault("consecutive_unhealthy", 0)
    target.setdefault("unhealthy_started_at", None)
    target.setdefault("outage_generation", 0)
    target.setdefault("outage_started_at", None)
    target.setdefault("recovery_pending", False)
    target.setdefault("recovered", {})
    target.setdefault("desktop_request_generation", 0)
    target.setdefault("desktop_recovery_requests", {})
    target.setdefault("desktop_direct_recoveries", {})
    target.setdefault("desktop_active_observations", {})
    target.setdefault("model_capacity_recoveries", {})
    target.setdefault("delegated_cli_recoveries", {})
    return target


def transition_health(
    state: MutableMapping[str, Any],
    target_name: str,
    healthy: bool,
    required_successes: int,
    *,
    required_failures: int = 2,
    now: int | None = None,
) -> HealthTransition:
    if required_successes < 1:
        raise ValueError("required_successes must be at least 1")
    if required_failures < 1:
        raise ValueError("required_failures must be at least 1")

    timestamp = int(time.time() if now is None else now)
    target = _target_state(state, target_name)
    recover_now = False

    if not healthy:
        target["consecutive_unhealthy"] = (
            int(target["consecutive_unhealthy"]) + 1
        )
        if target["consecutive_unhealthy"] == 1:
            target["unhealthy_started_at"] = timestamp
        target["consecutive_healthy"] = 0
        if target["consecutive_unhealthy"] >= required_failures:
            if target["health"] != "down":
                target["outage_generation"] = (
                    int(target["outage_generation"]) + 1
                )
                target["outage_started_at"] = (
                    target["unhealthy_started_at"] or timestamp
                )
                target["recovery_pending"] = False
            target["health"] = "down"
    elif target["health"] == "down":
        target["consecutive_unhealthy"] = 0
        target["unhealthy_started_at"] = None
        target["consecutive_healthy"] = int(target["consecutive_healthy"]) + 1
        if target["consecutive_healthy"] >= required_successes:
            target["health"] = "up"
            recover_now = True
    else:
        target["consecutive_unhealthy"] = 0
        target["unhealthy_started_at"] = None
        target["health"] = "up"
        target["consecutive_healthy"] = required_successes

    return HealthTransition(
        recover_now=recover_now,
        outage_generation=int(target["outage_generation"]),
        outage_started_at=target["outage_started_at"],
        health=str(target["health"]),
        consecutive_healthy=int(target["consecutive_healthy"]),
        consecutive_unhealthy=int(target["consecutive_unhealthy"]),
    )


def was_recovered(
    state: MutableMapping[str, Any],
    target_name: str,
    outage_generation: int,
    thread_id: str,
) -> bool:
    target = _target_state(state, target_name)
    generation = target["recovered"].get(str(outage_generation), {})
    return thread_id in generation


def recovery_record(
    state: MutableMapping[str, Any],
    target_name: str,
    outage_generation: int,
    thread_id: str,
) -> dict[str, Any] | None:
    target = _target_state(state, target_name)
    value = target["recovered"].get(str(outage_generation), {}).get(thread_id)
    return value if isinstance(value, dict) else None


def recovery_records(
    state: MutableMapping[str, Any],
    target_name: str,
    outage_generation: int,
) -> dict[str, dict[str, Any]]:
    target = _target_state(state, target_name)
    values = target["recovered"].get(str(outage_generation), {})
    if not isinstance(values, dict):
        return {}
    return {
        str(thread_id): record
        for thread_id, record in values.items()
        if isinstance(record, dict)
    }


def is_recovery_pending(
    state: MutableMapping[str, Any], target_name: str
) -> bool:
    return bool(_target_state(state, target_name)["recovery_pending"])


def set_recovery_pending(
    state: MutableMapping[str, Any], target_name: str, pending: bool
) -> None:
    _target_state(state, target_name)["recovery_pending"] = bool(pending)


def enqueue_desktop_recovery_request(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    normalized_thread_id = thread_id.strip()
    if not normalized_thread_id or len(normalized_thread_id) > 128:
        raise ValueError("thread_id must be a non-empty value up to 128 characters")

    target = _target_state(state, target_name)
    requests = target["desktop_recovery_requests"]
    if not isinstance(requests, dict):
        raise StateCorruptionError(
            "Guardian desktop recovery requests must be an object"
        )
    existing = requests.get(normalized_thread_id)
    if isinstance(existing, dict) and existing.get("status") == "pending":
        return {**existing, "thread_id": normalized_thread_id, "coalesced": True}

    generation = int(target["desktop_request_generation"]) + 1
    target["desktop_request_generation"] = generation
    request = {
        "generation": generation,
        "requested_at": int(time.time() if now is None else now),
        "status": "pending",
    }
    requests[normalized_thread_id] = request
    _prune_desktop_recovery_requests(requests)
    return {**request, "thread_id": normalized_thread_id, "coalesced": False}


def pending_desktop_recovery_requests(
    state: MutableMapping[str, Any], target_name: str
) -> dict[str, dict[str, Any]]:
    requests = _target_state(state, target_name)["desktop_recovery_requests"]
    if not isinstance(requests, dict):
        raise StateCorruptionError(
            "Guardian desktop recovery requests must be an object"
        )
    return {
        str(thread_id): dict(request)
        for thread_id, request in requests.items()
        if isinstance(request, dict) and request.get("status") == "pending"
    }


def finish_desktop_recovery_request(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    *,
    expected_generation: int,
    action: str,
    now: int | None = None,
) -> bool:
    requests = _target_state(state, target_name)["desktop_recovery_requests"]
    if not isinstance(requests, dict):
        raise StateCorruptionError(
            "Guardian desktop recovery requests must be an object"
        )
    request = requests.get(thread_id)
    if (
        not isinstance(request, dict)
        or request.get("status") != "pending"
        or int(request.get("generation", -1)) != expected_generation
    ):
        return False
    request.update(
        {
            "status": "finished",
            "action": action,
            "finished_at": int(time.time() if now is None else now),
        }
    )
    _prune_desktop_recovery_requests(requests)
    return True


def desktop_direct_recovery_record(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
) -> dict[str, Any] | None:
    records = _target_state(state, target_name)["desktop_direct_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian desktop direct recovery records must be an object"
        )
    value = records.get(thread_id)
    return dict(value) if isinstance(value, dict) else None


def mark_desktop_direct_recovery(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    *,
    turn_id: str,
    action: str = "goal_state_reactivated",
    recovery_turn_id: str | None = None,
    app_server_url: str | None = None,
    now: int | None = None,
) -> None:
    records = _target_state(state, target_name)["desktop_direct_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian desktop direct recovery records must be an object"
        )
    records[thread_id] = {
        "turn_id": turn_id,
        "action": action,
        "recovery_turn_id": recovery_turn_id,
        "app_server_url": app_server_url,
        "recorded_at": int(time.time() if now is None else now),
    }


def desktop_active_observation(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
) -> dict[str, Any] | None:
    records = _target_state(state, target_name)[
        "desktop_active_observations"
    ]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian desktop active observations must be an object"
        )
    value = records.get(thread_id)
    return dict(value) if isinstance(value, dict) else None


def save_desktop_active_observation(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    record: MutableMapping[str, Any],
    *,
    now: int | None = None,
) -> None:
    records = _target_state(state, target_name)[
        "desktop_active_observations"
    ]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian desktop active observations must be an object"
        )
    records[thread_id] = {
        **dict(record),
        "recorded_at": int(time.time() if now is None else now),
    }
    _prune_timestamped_records(records)


def clear_desktop_active_observation(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
) -> bool:
    records = _target_state(state, target_name)[
        "desktop_active_observations"
    ]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian desktop active observations must be an object"
        )
    return records.pop(thread_id, None) is not None


def model_capacity_recovery_record(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
) -> dict[str, Any] | None:
    records = _target_state(state, target_name)["model_capacity_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian model capacity recovery records must be an object"
        )
    value = records.get(thread_id)
    return dict(value) if isinstance(value, dict) else None


def save_model_capacity_recovery(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    record: MutableMapping[str, Any],
    *,
    now: int | None = None,
) -> None:
    records = _target_state(state, target_name)["model_capacity_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian model capacity recovery records must be an object"
        )
    records[thread_id] = {
        **dict(record),
        "recorded_at": int(time.time() if now is None else now),
    }
    _prune_timestamped_records(records)


def delegated_cli_recovery_record(
    state: MutableMapping[str, Any], target_name: str, thread_id: str
) -> dict[str, Any] | None:
    records = _target_state(state, target_name)["delegated_cli_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian delegated CLI recovery records must be an object"
        )
    value = records.get(thread_id)
    return dict(value) if isinstance(value, dict) else None


def save_delegated_cli_recovery(
    state: MutableMapping[str, Any],
    target_name: str,
    thread_id: str,
    record: MutableMapping[str, Any],
    *,
    now: int | None = None,
) -> None:
    records = _target_state(state, target_name)["delegated_cli_recoveries"]
    if not isinstance(records, dict):
        raise StateCorruptionError(
            "Guardian delegated CLI recovery records must be an object"
        )
    records[thread_id] = {
        **dict(record),
        "recorded_at": int(time.time() if now is None else now),
    }
    _prune_timestamped_records(records)


def _prune_timestamped_records(
    records: MutableMapping[str, Any], *, limit: int = 64
) -> None:
    if len(records) <= limit:
        return
    oldest = sorted(
        (int(value.get("recorded_at", 0)), str(key))
        for key, value in records.items()
        if isinstance(value, dict)
    )
    for _, key in oldest[:-limit]:
        records.pop(key, None)


def _prune_desktop_recovery_requests(
    requests: MutableMapping[str, Any], *, completed_limit: int = 32
) -> None:
    finished = sorted(
        (
            int(request.get("finished_at", request.get("requested_at", 0))),
            str(thread_id),
        )
        for thread_id, request in requests.items()
        if isinstance(request, dict) and request.get("status") == "finished"
    )
    for _, thread_id in finished[:-completed_limit]:
        requests.pop(thread_id, None)


def mark_recovered(
    state: MutableMapping[str, Any],
    target_name: str,
    outage_generation: int,
    thread_id: str,
    *,
    action: str,
    turn_id: str | None,
    now: int | None = None,
) -> None:
    target = _target_state(state, target_name)
    recovered = target["recovered"]
    generation = recovered.setdefault(str(outage_generation), {})
    generation[thread_id] = {
        "action": action,
        "turn_id": turn_id,
        "recorded_at": int(time.time() if now is None else now),
    }

    generation_numbers = sorted(
        (int(key), key) for key in recovered if str(key).isdigit()
    )
    for _, old_key in generation_numbers[:-8]:
        recovered.pop(old_key, None)


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateCorruptionError(
                f"Guardian state is unreadable; preserved at {self.path}: {exc}"
            ) from exc
        if not isinstance(state, dict):
            raise StateCorruptionError("Guardian state root must be an object")
        if state.get("schema_version") != SCHEMA_VERSION:
            raise StateCorruptionError(
                f"Unsupported Guardian state schema: {state.get('schema_version')!r}"
            )
        if not isinstance(state.get("targets"), dict):
            raise StateCorruptionError("Guardian state targets must be an object")
        return state

    def save(self, state: MutableMapping[str, Any]) -> None:
        if state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("refusing to persist an unsupported state schema")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def singleton_supervisor(path: str | Path) -> Iterator[bool]:
    """Try to hold one runtime-wide supervisor lease without waiting."""
    lock_path = Path(path).expanduser().with_name(
        Path(path).expanduser().name + ".supervisor.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
