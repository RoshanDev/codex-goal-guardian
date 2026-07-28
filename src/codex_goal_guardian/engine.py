from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Callable, Optional

from .app_server import AppServerClient
from .config import GuardianConfig, TargetConfig
from .health import HealthResult, probe_health
from .state import (
    StateStore,
    is_recovery_pending,
    mark_recovered,
    recovery_record,
    recovery_records,
    set_recovery_pending,
    transition_health,
)


_TERMINAL_RECOVERY_ACTIONS = {
    "thread_resumed",
    "thread_resumed_active",
    "thread_resumed_goal_changed",
    "turn_started",
}
_NETWORK_ERROR_MARKERS = (
    "connection",
    "disconnected",
    "eof",
    "network",
    "reconnect",
    "socket",
    "stream",
    "timed out",
    "timeout",
    "transport",
    "websocket",
)


def looks_like_network_failure(thread: dict[str, Any]) -> bool:
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return False
    error = turns[-1].get("error")
    if not isinstance(error, dict):
        return False
    text = " ".join(
        str(error.get(key, ""))
        for key in ("message", "additionalDetails", "codexErrorInfo")
    ).lower()
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def thread_eligibility(
    thread: dict[str, Any],
    goal: Optional[dict[str, Any]],
    target: TargetConfig,
    *,
    now: int,
    already_recovered: bool,
) -> tuple[bool, str]:
    if already_recovered:
        return False, "already_recovered"
    if not isinstance(goal, dict):
        return False, "goal_missing"

    goal_status = str(goal.get("status", "missing"))
    if goal_status != "active":
        return False, f"goal_{goal_status}"

    thread_status = _thread_status(thread)
    if thread_status == "active":
        return False, "thread_active"
    if thread_status not in {"idle", "systemError", "notLoaded"}:
        return False, f"thread_{thread_status or 'missing'}"

    updated_at = _timestamp_seconds(thread.get("updatedAt"))
    if updated_at is None:
        return False, "thread_updated_at_missing"
    if now - updated_at > target.max_thread_age_seconds:
        return False, "thread_stale"

    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return False, "turn_missing"
    turn_status = str(turns[-1].get("status", "missing"))
    if turn_status == "inProgress":
        return False, "turn_in_progress"
    if turn_status not in {"completed", "failed", "interrupted"}:
        return False, f"turn_{turn_status}"
    return True, "eligible"


class RecoveryEngine:
    def __init__(
        self,
        *,
        probe: Callable[[Any], HealthResult] = probe_health,
        client_factory: Optional[Callable[[TargetConfig], Any]] = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probe = probe
        self._client_factory = client_factory or self._default_client
        self._now = now
        self._sleep = sleep

    def run_once(
        self, config: GuardianConfig, *, dry_run: bool = False
    ) -> dict[str, Any]:
        health = self._probe(config.health)
        timestamp = int(self._now())
        report: dict[str, Any] = {
            "timestamp": timestamp,
            "dry_run": dry_run,
            "healthy": health.healthy,
            "health_reason": health.reason,
            "health_status_code": health.status_code,
            "health_elapsed_ms": health.elapsed_ms,
            "targets": [],
        }
        store = StateStore(config.state_path)

        with store.locked():
            state = store.load()
            initial_state = copy.deepcopy(state)
            for target in config.targets:
                previous = copy.deepcopy(
                    state.get("targets", {}).get(target.name, {})
                )
                transition = transition_health(
                    state,
                    target.name,
                    health.healthy,
                    config.health.required_consecutive_successes,
                    required_failures=(
                        config.health.required_consecutive_failures
                    ),
                    now=timestamp,
                )
                target_report: dict[str, Any] = {
                    "name": target.name,
                    "status": "healthy",
                    "outage_generation": transition.outage_generation,
                    "consecutive_healthy": transition.consecutive_healthy,
                    "consecutive_unhealthy": (
                        transition.consecutive_unhealthy
                    ),
                    "state_changed": (
                        previous.get("health") != transition.health
                        or previous.get("consecutive_healthy")
                        != transition.consecutive_healthy
                        or previous.get("outage_generation")
                        != transition.outage_generation
                        or previous.get("consecutive_unhealthy")
                        != transition.consecutive_unhealthy
                    ),
                    "actions": [],
                    "skipped": [],
                    "errors": [],
                }
                report["targets"].append(target_report)

                if not health.healthy:
                    target_report["status"] = "unhealthy"
                    continue
                if transition.health == "down":
                    target_report["status"] = "confirming"
                    continue

                if transition.recover_now:
                    set_recovery_pending(state, target.name, True)
                if not is_recovery_pending(state, target.name):
                    continue

                if transition.recover_now:
                    store.save(state)
                complete = self._recover_target(
                    config,
                    target,
                    transition.outage_generation,
                    state,
                    store,
                    target_report,
                    timestamp,
                    dry_run=dry_run,
                )
                if dry_run:
                    target_report["status"] = "dry_run"
                elif complete:
                    set_recovery_pending(state, target.name, False)
                    target_report["status"] = "recovered"
                else:
                    target_report["status"] = "recovery_pending"

            if state != initial_state:
                store.save(state)
        return report

    def _recover_target(
        self,
        config: GuardianConfig,
        target: TargetConfig,
        outage_generation: int,
        state: dict[str, Any],
        store: StateStore,
        report: dict[str, Any],
        now: int,
        *,
        dry_run: bool,
    ) -> bool:
        try:
            client_context = self._client_factory(target)
            with client_context as client:
                summaries = client.list_threads(limit=target.thread_limit)
                staged = recovery_records(
                    state, target.name, outage_generation
                )
                known_ids = {
                    str(item.get("id"))
                    for item in summaries
                    if isinstance(item, dict) and item.get("id")
                }
                for thread_id, record in staged.items():
                    if (
                        record.get("action") == "thread_resumed"
                        and thread_id not in known_ids
                    ):
                        summaries.append({"id": thread_id})

                had_error = False
                for summary in summaries:
                    if not isinstance(summary, dict):
                        continue
                    thread_id = summary.get("id")
                    if not isinstance(thread_id, str) or not thread_id:
                        report["skipped"].append(
                            {"thread_id": None, "reason": "thread_id_missing"}
                        )
                        continue
                    record = recovery_record(
                        state, target.name, outage_generation, thread_id
                    )
                    record_action = record.get("action") if record else None
                    terminal = record_action in _TERMINAL_RECOVERY_ACTIONS
                    if record_action == "thread_resumed" and target.start_recovery_turn:
                        terminal = False

                    try:
                        thread = client.read_thread(thread_id, include_turns=True)
                        goal = client.get_goal(thread_id)
                        eligible, reason = thread_eligibility(
                            thread,
                            goal,
                            target,
                            now=now,
                            already_recovered=terminal,
                        )
                        if not eligible:
                            report["skipped"].append(
                                {"thread_id": thread_id, "reason": reason}
                            )
                            continue

                        if dry_run:
                            action = (
                                "would_start_turn"
                                if record_action == "thread_resumed"
                                else "would_resume"
                            )
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": action,
                                    "network_failure": looks_like_network_failure(
                                        thread
                                    ),
                                }
                            )
                            continue

                        if record_action != "thread_resumed":
                            client.resume_thread(thread_id)
                            mark_recovered(
                                state,
                                target.name,
                                outage_generation,
                                thread_id,
                                action="thread_resumed",
                                turn_id=None,
                                now=now,
                            )
                            store.save(state)

                        if target.resume_grace_seconds > 0:
                            self._sleep(target.resume_grace_seconds)
                        after_resume = client.read_thread(
                            thread_id, include_turns=True
                        )
                        goal_after_resume = client.get_goal(thread_id)
                        if (
                            not isinstance(goal_after_resume, dict)
                            or goal_after_resume.get("status") != "active"
                        ):
                            mark_recovered(
                                state,
                                target.name,
                                outage_generation,
                                thread_id,
                                action="thread_resumed_goal_changed",
                                turn_id=None,
                                now=now,
                            )
                            store.save(state)
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "thread_resumed_goal_changed",
                                }
                            )
                            continue

                        if _thread_or_turn_active(after_resume):
                            mark_recovered(
                                state,
                                target.name,
                                outage_generation,
                                thread_id,
                                action="thread_resumed_active",
                                turn_id=None,
                                now=now,
                            )
                            store.save(state)
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "thread_resumed_active",
                                }
                            )
                            continue

                        if not target.start_recovery_turn:
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "thread_resumed",
                                }
                            )
                            continue

                        message_id = _recovery_message_id(
                            target.name, outage_generation, thread_id
                        )
                        turn = client.start_turn(
                            thread_id,
                            prompt=config.recovery_prompt,
                            client_user_message_id=message_id,
                        )
                        turn_id = turn.get("id")
                        mark_recovered(
                            state,
                            target.name,
                            outage_generation,
                            thread_id,
                            action="turn_started",
                            turn_id=str(turn_id) if turn_id is not None else None,
                            now=now,
                        )
                        store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "turn_started",
                                "turn_id": turn_id,
                                "client_user_message_id": message_id,
                            }
                        )
                    except Exception as error:
                        had_error = True
                        report["errors"].append(
                            {
                                "thread_id": thread_id,
                                "error": _safe_exception(error),
                            }
                        )
                return not had_error
        except Exception as error:
            report["errors"].append(
                {"thread_id": None, "error": _safe_exception(error)}
            )
            return False

    @staticmethod
    def _default_client(target: TargetConfig) -> AppServerClient:
        return AppServerClient(
            command=target.command + ("app-server", "--listen", "stdio://"),
            codex_home=target.codex_home,
            timeout_seconds=15,
        )


def _thread_status(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict):
        return str(status.get("type", ""))
    return str(status or "")


def _thread_or_turn_active(thread: dict[str, Any]) -> bool:
    if _thread_status(thread) == "active":
        return True
    turns = thread.get("turns")
    return bool(
        isinstance(turns, list)
        and turns
        and turns[-1].get("status") == "inProgress"
    )


def _timestamp_seconds(value: Any) -> int | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 100_000_000_000:
        timestamp //= 1000
    return timestamp


def _recovery_message_id(
    target_name: str, outage_generation: int, thread_id: str
) -> str:
    seed = (
        f"codex-goal-guardian:{target_name}:{outage_generation}:{thread_id}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _safe_exception(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:1000]
