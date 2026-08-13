from __future__ import annotations

import copy
import json
import mmap
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .app_server import AppServerClient, AppServerError
from .config import GuardianConfig, TargetConfig
from .health import HealthResult, probe_health
from .ownership import (
    cli_process_is_running,
    desktop_uses_shared_app_server,
)
from .state import (
    StateStore,
    desktop_direct_recovery_record,
    finish_desktop_recovery_request,
    is_recovery_pending,
    mark_desktop_direct_recovery,
    mark_recovered,
    model_capacity_recovery_record,
    pending_desktop_recovery_requests,
    recovery_record,
    recovery_records,
    save_model_capacity_recovery,
    set_recovery_pending,
    transition_health,
)


_TERMINAL_RECOVERY_ACTIONS = {
    "recovery_skipped_safety",
    "thread_resumed_goal_changed",
    "thread_resumed_stale",
    "thread_resumed_turn_completed",
    "turn_completed",
}
_STAGED_RECOVERY_ACTIONS = {
    "thread_resumed",
    "thread_resumed_active",
    "turn_started",
}
_WAITING_RECOVERY_REASONS = {
    "thread_active",
    "turn_in_progress",
}
_PENDING_DESKTOP_WAKE_ACTIONS = {
    "goal_state_reactivated",
    "resume_requested",
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
_MODEL_CAPACITY_ERROR_MARKER = "selected model is at capacity"
_PROMPT_POLICY_ERROR_MARKER = "invalid prompt: your prompt was flagged"
_PROMPT_POLICY_RECOVERY_PROMPT = (
    "Continue the active Goal from its current recorded state. Do not repeat "
    "completed actions. Follow the user's existing objective and all "
    "applicable policies."
)


def looks_like_network_failure(thread: dict[str, Any]) -> bool:
    turn = _latest_recovery_candidate_turn(thread)
    if turn is None:
        return False
    return _turn_looks_like_network_failure(turn)


def _turn_looks_like_network_failure(turn: dict[str, Any]) -> bool:
    error = turn.get("error")
    if not isinstance(error, dict):
        return False
    return _error_looks_like_network_failure(error)


def _error_looks_like_network_failure(error: dict[str, Any]) -> bool:
    text = _error_text(error)
    return any(marker in text for marker in _NETWORK_ERROR_MARKERS)


def _error_text(error: dict[str, Any]) -> str:
    return " ".join(
        str(error.get(key, ""))
        for key in ("message", "additionalDetails", "codexErrorInfo")
    ).lower()


def _error_looks_like_model_capacity_failure(error: dict[str, Any]) -> bool:
    return _MODEL_CAPACITY_ERROR_MARKER in _error_text(error)


def _error_looks_like_prompt_policy_rejection(error: dict[str, Any]) -> bool:
    return _PROMPT_POLICY_ERROR_MARKER in _error_text(error)


def looks_like_model_capacity_failure(
    thread: dict[str, Any], target: TargetConfig
) -> bool:
    return _model_capacity_failure_turn(thread, target) is not None


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

    updated_at = _latest_activity_timestamp(thread, goal)
    if updated_at is None:
        return False, "thread_updated_at_missing"
    if now - updated_at > target.max_thread_age_seconds:
        return False, "thread_stale"

    thread_status = _thread_status(thread)
    if thread_status == "active":
        return False, "thread_active"
    if thread_status not in {"idle", "systemError", "notLoaded"}:
        return False, f"thread_{thread_status or 'missing'}"

    source = _thread_source(thread)
    if source not in target.allowed_sources:
        return False, f"source_{source or 'missing'}"

    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return False, "turn_missing"
    if _has_in_progress_turn(thread):
        return False, "turn_in_progress"
    last_turn = turns[-1]
    if not isinstance(last_turn, dict):
        return False, "turn_missing"
    turn_status = str(last_turn.get("status", "missing"))
    if turn_status not in {"failed", "interrupted"}:
        return False, f"turn_{turn_status}"
    error = last_turn.get("error")
    if isinstance(error, dict) and _error_looks_like_prompt_policy_rejection(
        error
    ):
        return False, "prompt_policy_rejection"
    if not _turn_looks_like_network_failure(last_turn):
        return False, "turn_not_network_failure"
    return True, "eligible"


def model_capacity_eligibility(
    thread: dict[str, Any],
    goal: Optional[dict[str, Any]],
    target: TargetConfig,
    *,
    now: int,
) -> tuple[bool, str]:
    if not target.model_capacity_fallback_models:
        return False, "model_capacity_recovery_disabled"
    if not isinstance(goal, dict):
        return False, "goal_missing"
    if str(goal.get("status", "missing")) != "active":
        return False, f"goal_{goal.get('status', 'missing')}"

    updated_at = _latest_activity_timestamp(thread, goal)
    if updated_at is None:
        return False, "thread_updated_at_missing"
    if now - updated_at > target.max_thread_age_seconds:
        return False, "thread_stale"

    thread_status = _thread_status(thread)
    if thread_status == "active":
        return False, "thread_active"
    if thread_status not in {"idle", "systemError", "notLoaded"}:
        return False, f"thread_{thread_status or 'missing'}"
    source = _thread_source(thread)
    if source not in target.allowed_sources:
        return False, f"source_{source or 'missing'}"
    if _has_in_progress_turn(thread):
        return False, "turn_in_progress"
    if _model_capacity_failure_turn(thread, target) is None:
        return False, "turn_not_model_capacity_failure"
    return True, "eligible"


def desktop_goal_reactivation_eligibility(
    thread: dict[str, Any],
    goal: Optional[dict[str, Any]],
    target: TargetConfig,
    *,
    now: int,
    already_recovered: bool,
    pending_evidence_turn_id: str | None = None,
) -> tuple[bool, str]:
    if already_recovered:
        return False, "already_recovered"
    if not isinstance(goal, dict):
        return False, "goal_missing"

    goal_status = str(goal.get("status", "missing"))
    recoverable_goal_statuses = {"blocked"}
    if target.delegated_continuity_enabled:
        recoverable_goal_statuses.add("usageLimited")
    if goal_status not in recoverable_goal_statuses:
        return False, f"goal_{goal_status}"

    updated_at = _latest_activity_timestamp(thread, goal)
    if updated_at is None:
        return False, "thread_updated_at_missing"
    if now - updated_at > target.max_thread_age_seconds:
        return False, "thread_stale"

    thread_status = _thread_status(thread)
    if thread_status == "active":
        return False, "thread_active"
    if thread_status not in {"idle", "systemError", "notLoaded"}:
        return False, f"thread_{thread_status or 'missing'}"

    source = _thread_source(thread)
    if source not in target.allowed_sources:
        return False, f"source_{source or 'missing'}"

    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return False, "turn_missing"
    if _has_in_progress_turn(thread):
        return False, "turn_in_progress"
    candidate = _desktop_recovery_candidate_turn(
        thread,
        pending_evidence_turn_id=pending_evidence_turn_id,
    )
    if candidate is None:
        return False, "turn_missing"
    error = _desktop_turn_error(thread, candidate, target)
    if error is None:
        turn_status = str(candidate.get("status", "missing"))
        if (
            target.delegated_continuity_enabled
            and turn_status in {"completed", "failed", "interrupted"}
        ):
            return True, "delegated_continuation"
        if turn_status in {"failed", "interrupted"}:
            return False, "turn_not_network_failure"
        return False, f"turn_{turn_status}"
    if _error_looks_like_prompt_policy_rejection(error):
        if target.prompt_policy_retry_enabled:
            return True, "prompt_policy_retry"
        return False, "prompt_policy_rejection"
    if target.delegated_continuity_enabled:
        turn_status = str(candidate.get("status", "missing"))
        if turn_status in {"completed", "failed", "interrupted"}:
            return True, "delegated_continuation"
    if not _error_looks_like_network_failure(error):
        return False, "turn_not_network_failure"
    return True, "eligible"


class RecoveryEngine:
    def __init__(
        self,
        *,
        probe: Callable[[Any], HealthResult] = probe_health,
        client_factory: Optional[Callable[[TargetConfig], Any]] = None,
        process_probe: Callable[[TargetConfig], bool] = cli_process_is_running,
        desktop_runtime_probe: Callable[
            [TargetConfig], bool
        ] = desktop_uses_shared_app_server,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probe = probe
        self._client_factory = client_factory or self._default_client
        self._process_probe = process_probe
        self._desktop_runtime_probe = desktop_runtime_probe
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

                capacity_status = "disabled"
                if target.model_capacity_fallback_models:
                    capacity_status = self._recover_model_capacity(
                        config,
                        target,
                        state,
                        store,
                        target_report,
                        timestamp,
                        dry_run=dry_run,
                    )

                if target.recovery_mode == "desktop_goal_state":
                    requests = pending_desktop_recovery_requests(
                        state, target.name
                    )
                    watched_thread_ids = target.desktop_thread_ids
                    if not requests and not watched_thread_ids:
                        continue
                    complete = self._recover_desktop_requests(
                        target,
                        requests,
                        watched_thread_ids,
                        config.recovery_prompt,
                        state,
                        store,
                        target_report,
                        timestamp,
                        dry_run=dry_run,
                    )
                    if dry_run:
                        target_report["status"] = "dry_run"
                    elif requests and complete:
                        target_report["status"] = "recovered"
                    elif requests:
                        target_report["status"] = "recovery_pending"
                    elif target_report["errors"]:
                        target_report["status"] = "error"
                    elif target_report["actions"]:
                        target_report["status"] = "recovered"
                    elif capacity_status == "waiting":
                        target_report["status"] = "capacity_waiting"
                    else:
                        target_report["status"] = "monitoring"
                    continue

                if transition.recover_now:
                    set_recovery_pending(state, target.name, True)
                elif (
                    transition.health == "up"
                    and target.start_recovery_turn
                    and _has_staged_recovery(
                        state,
                        target.name,
                        transition.outage_generation,
                    )
                ):
                    set_recovery_pending(state, target.name, True)
                if not is_recovery_pending(state, target.name):
                    if dry_run and capacity_status != "idle":
                        target_report["status"] = "dry_run"
                    elif capacity_status == "waiting":
                        target_report["status"] = "capacity_waiting"
                    elif capacity_status == "error":
                        target_report["status"] = "error"
                    elif capacity_status == "acted":
                        target_report["status"] = "recovered"
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
                    target_report["status"] = (
                        "capacity_waiting"
                        if capacity_status == "waiting"
                        else "recovered"
                    )
                else:
                    target_report["status"] = "recovery_pending"

            if state != initial_state:
                store.save(state)
        return report

    def _recover_model_capacity(
        self,
        config: GuardianConfig,
        target: TargetConfig,
        state: dict[str, Any],
        store: StateStore,
        report: dict[str, Any],
        now: int,
        *,
        dry_run: bool,
    ) -> str:
        if target.recovery_mode == "desktop_goal_state":
            if not target.desktop_thread_ids:
                return "disabled"
            if not self._desktop_runtime_probe(target):
                report["skipped"].append(
                    {
                        "thread_id": None,
                        "reason": "desktop_shared_runtime_not_active",
                    }
                )
                return "idle"
            summaries = [
                {"id": thread_id}
                for thread_id in target.desktop_thread_ids
            ]
        elif self._process_probe(target):
            report["skipped"].append(
                {
                    "thread_id": None,
                    "reason": "model_capacity_cli_process_running",
                }
            )
            return "idle"
        else:
            summaries = None

        capacity_error_context: tuple[
            str, dict[str, Any], str, str | None
        ] | None = None
        try:
            client_context = self._client_factory(target)
            with client_context as client:
                if summaries is None:
                    summaries = client.list_threads(limit=target.thread_limit)
                for summary in summaries:
                    if not isinstance(summary, dict):
                        continue
                    thread_id = summary.get("id")
                    if not isinstance(thread_id, str) or not thread_id:
                        continue
                    thread = client.read_thread(thread_id, include_turns=True)
                    failure_turn = _model_capacity_failure_turn(thread, target)
                    if failure_turn is None:
                        continue
                    goal = client.get_goal(thread_id)
                    eligible, reason = model_capacity_eligibility(
                        thread,
                        goal,
                        target,
                        now=now,
                    )
                    if not eligible:
                        report["skipped"].append(
                            {"thread_id": thread_id, "reason": reason}
                        )
                        return (
                            "waiting"
                            if reason in _WAITING_RECOVERY_REASONS
                            else "idle"
                        )

                    failure_turn_id = str(failure_turn.get("id", ""))
                    if not failure_turn_id or len(failure_turn_id) > 128:
                        report["skipped"].append(
                            {
                                "thread_id": thread_id,
                                "reason": "model_capacity_turn_id_missing",
                            }
                        )
                        return "idle"

                    record = model_capacity_recovery_record(
                        state, target.name, thread_id
                    )
                    if record is None:
                        record = _new_model_capacity_record(
                            failure_turn_id,
                            target,
                            now=now,
                        )
                        if not dry_run:
                            save_model_capacity_recovery(
                                state,
                                target.name,
                                thread_id,
                                record,
                                now=now,
                            )
                            store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "model_capacity_retry_scheduled",
                                "model": "thread_default",
                                "next_retry_at": record["next_retry_at"],
                            }
                        )
                        return "waiting"

                    previous_failure_id = str(
                        record.get("failure_turn_id", "")
                    )
                    recovery_turn_id = str(
                        record.get("recovery_turn_id", "")
                    )
                    if failure_turn_id == recovery_turn_id and (
                        failure_turn_id != previous_failure_id
                    ):
                        record = _schedule_observed_capacity_failure(
                            record,
                            failure_turn_id,
                            target,
                            now=now,
                        )
                        if not dry_run:
                            save_model_capacity_recovery(
                                state,
                                target.name,
                                thread_id,
                                record,
                                now=now,
                            )
                            store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "model_capacity_retry_scheduled",
                                "model": _capacity_model_label(record),
                                "next_retry_at": record["next_retry_at"],
                            }
                        )
                        return "waiting"
                    if failure_turn_id != previous_failure_id:
                        record = _new_model_capacity_record(
                            failure_turn_id,
                            target,
                            now=now,
                        )
                        if not dry_run:
                            save_model_capacity_recovery(
                                state,
                                target.name,
                                thread_id,
                                record,
                                now=now,
                            )
                            store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "model_capacity_retry_scheduled",
                                "model": "thread_default",
                                "next_retry_at": record["next_retry_at"],
                            }
                        )
                        return "waiting"
                    if record.get("action") == "fallbacks_exhausted":
                        report["skipped"].append(
                            {
                                "thread_id": thread_id,
                                "reason": "model_capacity_fallbacks_exhausted",
                            }
                        )
                        return "idle"

                    next_retry_at = int(record.get("next_retry_at", 0) or 0)
                    if now < next_retry_at:
                        report["skipped"].append(
                            {
                                "thread_id": thread_id,
                                "reason": "model_capacity_backoff",
                                "model": _capacity_model_label(record),
                                "next_retry_at": next_retry_at,
                            }
                        )
                        return "waiting"

                    model_index = int(record.get("model_index", 0))
                    attempts_in_model = int(
                        record.get("attempts_in_model", 0)
                    )
                    if attempts_in_model >= target.model_capacity_retry_limit:
                        if model_index >= len(
                            target.model_capacity_fallback_models
                        ):
                            record.update(
                                {
                                    "action": "fallbacks_exhausted",
                                    "next_retry_at": None,
                                }
                            )
                            if not dry_run:
                                save_model_capacity_recovery(
                                    state,
                                    target.name,
                                    thread_id,
                                    record,
                                    now=now,
                                )
                                store.save(state)
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": (
                                        "model_capacity_fallbacks_exhausted"
                                    ),
                                }
                            )
                            return "acted"
                        model_index += 1
                        attempts_in_model = 0

                    model = _capacity_model(target, model_index)
                    attempt_number = attempts_in_model + 1
                    total_attempts = int(record.get("total_attempts", 0)) + 1
                    message_id = _model_capacity_message_id(
                        target.name,
                        thread_id,
                        failure_turn_id,
                        model_index,
                        attempt_number,
                    )
                    if dry_run:
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "would_retry_model_capacity",
                                "model": model or "thread_default",
                                "attempt": attempt_number,
                                "retry_limit": (
                                    target.model_capacity_retry_limit
                                ),
                            }
                        )
                        return "acted"

                    fresh_thread = client.read_thread(
                        thread_id, include_turns=True
                    )
                    fresh_goal = client.get_goal(thread_id)
                    fresh_eligible, fresh_reason = model_capacity_eligibility(
                        fresh_thread,
                        fresh_goal,
                        target,
                        now=now,
                    )
                    fresh_failure = _model_capacity_failure_turn(
                        fresh_thread, target
                    )
                    if (
                        not fresh_eligible
                        or not isinstance(fresh_failure, dict)
                        or fresh_failure.get("id") != failure_turn_id
                    ):
                        report["skipped"].append(
                            {
                                "thread_id": thread_id,
                                "reason": (
                                    fresh_reason
                                    if not fresh_eligible
                                    else "model_capacity_evidence_changed"
                                ),
                            }
                        )
                        return (
                            "waiting"
                            if fresh_reason in _WAITING_RECOVERY_REASONS
                            else "idle"
                        )

                    record.update(
                        {
                            "action": "start_requested",
                            "model_index": model_index,
                            "model": model,
                            "attempts_in_model": attempt_number,
                            "total_attempts": total_attempts,
                            "client_user_message_id": message_id,
                            "recovery_turn_id": None,
                            "next_retry_at": None,
                        }
                    )
                    save_model_capacity_recovery(
                        state,
                        target.name,
                        thread_id,
                        record,
                        now=now,
                    )
                    store.save(state)
                    capacity_error_context = (
                        thread_id,
                        dict(record),
                        failure_turn_id,
                        model,
                    )

                    client.resume_thread(thread_id)
                    if target.resume_grace_seconds > 0:
                        self._sleep(target.resume_grace_seconds)
                    resumed_thread = client.read_thread(
                        thread_id, include_turns=True
                    )
                    resumed_goal = client.get_goal(thread_id)
                    if (
                        not isinstance(resumed_goal, dict)
                        or resumed_goal.get("status") != "active"
                    ):
                        record.update(
                            {"action": "goal_changed", "next_retry_at": None}
                        )
                        save_model_capacity_recovery(
                            state,
                            target.name,
                            thread_id,
                            record,
                            now=now,
                        )
                        store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "model_capacity_goal_changed",
                            }
                        )
                        return "acted"

                    if _thread_or_turn_active(resumed_thread):
                        started_turn_id = _latest_turn_id(resumed_thread)
                    else:
                        turn = client.start_turn(
                            thread_id,
                            prompt=config.recovery_prompt,
                            client_user_message_id=message_id,
                            model=model,
                        )
                        value = turn.get("id")
                        started_turn_id = (
                            str(value) if value is not None else None
                        )
                    record.update(
                        {
                            "action": "turn_started",
                            "recovery_turn_id": started_turn_id,
                        }
                    )
                    save_model_capacity_recovery(
                        state,
                        target.name,
                        thread_id,
                        record,
                        now=now,
                    )
                    store.save(state)
                    report["actions"].append(
                        {
                            "thread_id": thread_id,
                            "action": "model_capacity_retry_started",
                            "turn_id": started_turn_id,
                            "model": model or "thread_default",
                            "attempt": attempt_number,
                            "retry_limit": target.model_capacity_retry_limit,
                            "client_user_message_id": message_id,
                        }
                    )

                    settled_thread, settled_goal = self._wait_until_settled(
                        client, thread_id, target
                    )
                    settled_at = int(self._now())
                    settled_failure = _model_capacity_failure_turn(
                        settled_thread, target
                    )
                    if isinstance(settled_failure, dict) and (
                        started_turn_id is None
                        or settled_failure.get("id") == started_turn_id
                    ):
                        record = _schedule_observed_capacity_failure(
                            record,
                            str(settled_failure.get("id", failure_turn_id)),
                            target,
                            now=settled_at,
                        )
                        save_model_capacity_recovery(
                            state,
                            target.name,
                            thread_id,
                            record,
                            now=settled_at,
                        )
                        store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "model_capacity_retry_failed",
                                "turn_id": started_turn_id,
                                "model": model or "thread_default",
                                "next_retry_at": record["next_retry_at"],
                            }
                        )
                        return "waiting"

                    record.update(
                        {
                            "action": "turn_settled",
                            "next_retry_at": None,
                        }
                    )
                    save_model_capacity_recovery(
                        state,
                        target.name,
                        thread_id,
                        record,
                        now=settled_at,
                    )
                    store.save(state)
                    report["actions"].append(
                        {
                            "thread_id": thread_id,
                            "action": "model_capacity_retry_settled",
                            "turn_id": started_turn_id,
                            "model": model or "thread_default",
                            "goal_status": (
                                settled_goal.get("status")
                                if isinstance(settled_goal, dict)
                                else None
                            ),
                        }
                    )
                    return "acted"
                return "idle"
        except Exception as error:
            if (
                capacity_error_context is not None
                and _MODEL_CAPACITY_ERROR_MARKER in str(error).lower()
            ):
                thread_id, record, failure_turn_id, model = (
                    capacity_error_context
                )
                failed_at = int(self._now())
                record = _schedule_observed_capacity_failure(
                    record,
                    failure_turn_id,
                    target,
                    now=failed_at,
                )
                save_model_capacity_recovery(
                    state,
                    target.name,
                    thread_id,
                    record,
                    now=failed_at,
                )
                store.save(state)
                report["actions"].append(
                    {
                        "thread_id": thread_id,
                        "action": "model_capacity_retry_failed",
                        "turn_id": None,
                        "model": model or "thread_default",
                        "next_retry_at": record["next_retry_at"],
                    }
                )
                return "waiting"
            report["errors"].append(
                {"thread_id": None, "error": _safe_exception(error)}
            )
            return "error"

    def _recover_desktop_requests(
        self,
        target: TargetConfig,
        requests: dict[str, dict[str, Any]],
        watched_thread_ids: tuple[str, ...],
        recovery_prompt: str,
        state: dict[str, Any],
        store: StateStore,
        report: dict[str, Any],
        now: int,
        *,
        dry_run: bool,
    ) -> bool:
        had_error = False
        recovery_waiting = False
        try:
            client_context = self._client_factory(target)
            with client_context as client:
                thread_ids = tuple(
                    dict.fromkeys((*requests, *watched_thread_ids))
                )
                watched = set(watched_thread_ids)
                for thread_id in thread_ids:
                    request = requests.get(thread_id)
                    requested = isinstance(request, dict)
                    generation = (
                        int(request.get("generation", -1))
                        if requested
                        else -1
                    )
                    direct = thread_id in watched
                    direct_record = (
                        desktop_direct_recovery_record(
                            state,
                            target.name,
                            thread_id,
                        )
                        if direct
                        else None
                    )
                    pending_evidence_turn_id = (
                        str(direct_record.get("turn_id"))
                        if (
                            target.start_recovery_turn
                            and isinstance(direct_record, dict)
                            and direct_record.get("app_server_url")
                            == target.app_server_url
                            and direct_record.get("action")
                            in _PENDING_DESKTOP_WAKE_ACTIONS
                            and direct_record.get("turn_id")
                        )
                        else None
                    )
                    try:
                        thread = client.read_thread(
                            thread_id, include_turns=True
                        )
                        goal = client.get_goal(thread_id)

                        if (
                            isinstance(goal, dict)
                            and goal.get("status") == "active"
                        ):
                            if (
                                target.app_server_url is not None
                                and not self._desktop_runtime_probe(target)
                            ):
                                if requested:
                                    recovery_waiting = True
                                report["skipped"].append(
                                    {
                                        "thread_id": thread_id,
                                        "reason": (
                                            "desktop_shared_runtime_not_active"
                                        ),
                                    }
                                )
                                continue
                            if (
                                direct
                                and target.start_recovery_turn
                                and not _thread_or_turn_active(thread)
                            ):
                                evidence_turn_id = (
                                    _desktop_continuity_turn_id(
                                        thread,
                                        target,
                                        pending_evidence_turn_id=(
                                            pending_evidence_turn_id
                                        ),
                                    )
                                )
                                record = desktop_direct_recovery_record(
                                    state,
                                    target.name,
                                    thread_id,
                                )
                                wake_finished = {
                                    "runtime_active",
                                    "turn_started",
                                    "turn_settled",
                                    "goal_changed",
                                }
                                wake_pending = (
                                    isinstance(evidence_turn_id, str)
                                    and (
                                        not isinstance(record, dict)
                                        or record.get("turn_id")
                                        != evidence_turn_id
                                        or record.get("app_server_url")
                                        != target.app_server_url
                                        or record.get("action")
                                        not in wake_finished
                                    )
                                )
                                if wake_pending:
                                    if dry_run:
                                        report["actions"].append(
                                            {
                                                "thread_id": thread_id,
                                                "action": (
                                                    "would_wake_active_goal"
                                                ),
                                                "mode": "direct",
                                                "evidence_turn_id": (
                                                    evidence_turn_id
                                                ),
                                            }
                                        )
                                    else:
                                        self._wake_direct_desktop_goal(
                                            client,
                                            target,
                                            thread_id,
                                            evidence_turn_id,
                                            recovery_prompt,
                                            state,
                                            store,
                                            report,
                                            now,
                                        )
                            if requested and not dry_run:
                                finish_desktop_recovery_request(
                                    state,
                                    target.name,
                                    thread_id,
                                    expected_generation=generation,
                                    action="goal_already_active",
                                    now=now,
                                )
                                store.save(state)
                            if requested:
                                report["actions"].append(
                                    {
                                        "thread_id": thread_id,
                                        "action": "goal_already_active",
                                        "same_runtime_wake_required": not (
                                            direct
                                            and target.start_recovery_turn
                                        ),
                                    }
                                )
                            continue

                        eligible, reason = (
                            desktop_goal_reactivation_eligibility(
                                thread,
                                goal,
                                target,
                                now=now,
                                already_recovered=False,
                                pending_evidence_turn_id=(
                                    pending_evidence_turn_id
                                ),
                            )
                        )
                        if not eligible:
                            if (
                                requested
                                and reason in _WAITING_RECOVERY_REASONS
                            ):
                                recovery_waiting = True
                            elif requested and not dry_run:
                                finish_desktop_recovery_request(
                                    state,
                                    target.name,
                                    thread_id,
                                    expected_generation=generation,
                                    action=f"request_skipped_{reason}",
                                    now=now,
                                )
                                store.save(state)
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": reason,
                                }
                            )
                            continue

                        prompt_policy_retry = reason == "prompt_policy_retry"

                        candidate = _desktop_recovery_candidate_turn(
                            thread,
                            pending_evidence_turn_id=(
                                pending_evidence_turn_id
                            ),
                        )
                        candidate_id = (
                            candidate.get("id")
                            if isinstance(candidate, dict)
                            else None
                        )
                        if direct and (
                            not isinstance(candidate_id, str)
                            or not candidate_id
                        ):
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": "turn_id_missing",
                                }
                            )
                            continue
                        if direct:
                            record = desktop_direct_recovery_record(
                                state,
                                target.name,
                                thread_id,
                            )
                            if (
                                prompt_policy_retry
                                and isinstance(record, dict)
                                and record.get("recovery_turn_id")
                                == candidate_id
                            ):
                                report["skipped"].append(
                                    {
                                        "thread_id": thread_id,
                                        "reason": (
                                            "prompt_policy_retry_exhausted"
                                        ),
                                    }
                                )
                                continue
                            if (
                                isinstance(record, dict)
                                and record.get("turn_id") == candidate_id
                                and record.get("app_server_url")
                                == target.app_server_url
                                and (
                                    not target.start_recovery_turn
                                    or record.get("action")
                                    not in _PENDING_DESKTOP_WAKE_ACTIONS
                                )
                            ):
                                if requested and not dry_run:
                                    finish_desktop_recovery_request(
                                        state,
                                        target.name,
                                        thread_id,
                                        expected_generation=generation,
                                        action=(
                                            "request_skipped_"
                                            "already_recovered_turn"
                                        ),
                                        now=now,
                                    )
                                    store.save(state)
                                report["skipped"].append(
                                    {
                                        "thread_id": thread_id,
                                        "reason": "already_recovered_turn",
                                    }
                                )
                                continue

                        if (
                            target.app_server_url is not None
                            and not self._desktop_runtime_probe(target)
                        ):
                            if requested:
                                recovery_waiting = True
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": (
                                        "desktop_shared_runtime_not_active"
                                    ),
                                }
                            )
                            continue

                        if dry_run:
                            action = {
                                "thread_id": thread_id,
                                "action": "would_reactivate_goal",
                                "network_failure": not prompt_policy_retry,
                                "prompt_policy_retry": prompt_policy_retry,
                                "same_runtime_wake_required": not (
                                    direct and target.start_recovery_turn
                                ),
                            }
                            if direct:
                                action["mode"] = "direct"
                            report["actions"].append(action)
                            continue

                        fresh_thread = client.read_thread(
                            thread_id, include_turns=True
                        )
                        fresh_goal = client.get_goal(thread_id)
                        fresh_eligible, fresh_reason = (
                            desktop_goal_reactivation_eligibility(
                                fresh_thread,
                                fresh_goal,
                                target,
                                now=now,
                                already_recovered=False,
                                pending_evidence_turn_id=(
                                    pending_evidence_turn_id
                                ),
                            )
                        )
                        if not fresh_eligible:
                            if (
                                requested
                                and fresh_reason
                                in _WAITING_RECOVERY_REASONS
                            ):
                                recovery_waiting = True
                            elif requested:
                                finish_desktop_recovery_request(
                                    state,
                                    target.name,
                                    thread_id,
                                    expected_generation=generation,
                                    action=(
                                        "request_skipped_"
                                        f"{fresh_reason}"
                                    ),
                                    now=now,
                                )
                                store.save(state)
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": fresh_reason,
                                    "stage": "pre_mutation",
                                }
                            )
                            continue
                        if prompt_policy_retry != (
                            fresh_reason == "prompt_policy_retry"
                        ):
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": "recovery_evidence_changed",
                                    "stage": "pre_mutation",
                                }
                            )
                            continue

                        fresh_candidate = _desktop_recovery_candidate_turn(
                            fresh_thread,
                            pending_evidence_turn_id=(
                                pending_evidence_turn_id
                            ),
                        )
                        fresh_candidate_id = (
                            fresh_candidate.get("id")
                            if isinstance(fresh_candidate, dict)
                            else None
                        )
                        if direct and (
                            not isinstance(fresh_candidate_id, str)
                            or not fresh_candidate_id
                        ):
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": "turn_id_missing",
                                    "stage": "pre_mutation",
                                }
                            )
                            continue
                        if direct:
                            fresh_record = desktop_direct_recovery_record(
                                state,
                                target.name,
                                thread_id,
                            )
                            if (
                                isinstance(fresh_record, dict)
                                and fresh_record.get("turn_id")
                                == fresh_candidate_id
                                and fresh_record.get("app_server_url")
                                == target.app_server_url
                                and (
                                    not target.start_recovery_turn
                                    or fresh_record.get("action")
                                    not in _PENDING_DESKTOP_WAKE_ACTIONS
                                )
                            ):
                                report["skipped"].append(
                                    {
                                        "thread_id": thread_id,
                                        "reason": "already_recovered_turn",
                                        "stage": "pre_mutation",
                                    }
                                )
                                continue

                        if (
                            target.app_server_url is not None
                            and not self._desktop_runtime_probe(target)
                        ):
                            if requested:
                                recovery_waiting = True
                            report["skipped"].append(
                                {
                                    "thread_id": thread_id,
                                    "reason": (
                                        "desktop_shared_runtime_not_active"
                                    ),
                                    "stage": "pre_mutation",
                                }
                            )
                            continue

                        assert isinstance(fresh_goal, dict)
                        reactivated_goal = client.reactivate_goal(thread_id)
                        _verify_goal_reactivation(
                            fresh_goal, reactivated_goal
                        )
                        confirmed_goal = client.get_goal(thread_id)
                        if not isinstance(confirmed_goal, dict):
                            raise AppServerError(
                                "thread/goal/get did not confirm the active goal"
                            )
                        _verify_goal_reactivation(
                            fresh_goal, confirmed_goal
                        )
                        if requested:
                            finish_desktop_recovery_request(
                                state,
                                target.name,
                                thread_id,
                                expected_generation=generation,
                                action="goal_state_reactivated",
                                now=now,
                            )
                        if direct:
                            assert isinstance(fresh_candidate_id, str)
                            mark_desktop_direct_recovery(
                                state,
                                target.name,
                                thread_id,
                                turn_id=fresh_candidate_id,
                                app_server_url=target.app_server_url,
                                now=now,
                            )
                        store.save(state)
                        action = {
                            "thread_id": thread_id,
                            "action": "goal_state_reactivated",
                            "tokens_used": confirmed_goal.get("tokensUsed"),
                            "time_used_seconds": confirmed_goal.get(
                                "timeUsedSeconds"
                            ),
                            "same_runtime_wake_required": not (
                                direct and target.start_recovery_turn
                            ),
                        }
                        if direct:
                            action["mode"] = "direct"
                            action["evidence_turn_id"] = fresh_candidate_id
                        report["actions"].append(action)
                        if direct and target.start_recovery_turn:
                            assert isinstance(fresh_candidate_id, str)
                            if prompt_policy_retry:
                                self._start_prompt_policy_continuation(
                                    client,
                                    target,
                                    thread_id,
                                    fresh_candidate_id,
                                    state,
                                    store,
                                    report,
                                    now,
                                )
                            else:
                                self._wake_direct_desktop_goal(
                                    client,
                                    target,
                                    thread_id,
                                    fresh_candidate_id,
                                    recovery_prompt,
                                    state,
                                    store,
                                    report,
                                    now,
                                )
                    except Exception as error:
                        had_error = True
                        report["errors"].append(
                            {
                                "thread_id": thread_id,
                                "error": _safe_exception(error),
                            }
                        )
        except Exception as error:
            report["errors"].append(
                {"thread_id": None, "error": _safe_exception(error)}
            )
            return False
        return not had_error and not recovery_waiting

    def _start_prompt_policy_continuation(
        self,
        client: Any,
        target: TargetConfig,
        thread_id: str,
        evidence_turn_id: str,
        state: dict[str, Any],
        store: StateStore,
        report: dict[str, Any],
        now: int,
    ) -> None:
        current_thread = client.read_thread(thread_id, include_turns=True)
        current_goal = client.get_goal(thread_id)
        if (
            not isinstance(current_goal, dict)
            or current_goal.get("status") != "active"
        ):
            report["skipped"].append(
                {"thread_id": thread_id, "reason": "goal_changed_before_wake"}
            )
            return
        if _thread_or_turn_active(current_thread):
            report["skipped"].append(
                {"thread_id": thread_id, "reason": "turn_in_progress"}
            )
            return

        message_id = _desktop_recovery_message_id(
            target.name,
            evidence_turn_id,
            thread_id,
        )
        turn = client.start_turn(
            thread_id,
            prompt=_PROMPT_POLICY_RECOVERY_PROMPT,
            client_user_message_id=message_id,
        )
        recovery_turn_id = turn.get("id")
        normalized_turn_id = (
            str(recovery_turn_id) if recovery_turn_id is not None else None
        )
        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="prompt_policy_turn_started",
            recovery_turn_id=normalized_turn_id,
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)
        report["actions"].append(
            {
                "thread_id": thread_id,
                "action": "prompt_policy_continuation_started",
                "turn_id": normalized_turn_id,
                "client_user_message_id": message_id,
                "mode": "direct",
            }
        )
        _, goal_after_turn = self._wait_until_settled(
            client,
            thread_id,
            target,
        )
        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="prompt_policy_turn_settled",
            recovery_turn_id=normalized_turn_id,
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)
        report["actions"].append(
            {
                "thread_id": thread_id,
                "action": "prompt_policy_continuation_settled",
                "turn_id": normalized_turn_id,
                "goal_status": (
                    goal_after_turn.get("status")
                    if isinstance(goal_after_turn, dict)
                    else None
                ),
                "mode": "direct",
            }
        )

    def _wake_direct_desktop_goal(
        self,
        client: Any,
        target: TargetConfig,
        thread_id: str,
        evidence_turn_id: str,
        recovery_prompt: str,
        state: dict[str, Any],
        store: StateStore,
        report: dict[str, Any],
        now: int,
    ) -> None:
        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="goal_state_reactivated",
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)

        if target.resume_grace_seconds > 0:
            self._sleep(target.resume_grace_seconds)
        before_resume = client.read_thread(thread_id, include_turns=True)
        goal_before_resume = client.get_goal(thread_id)
        if (
            not isinstance(goal_before_resume, dict)
            or goal_before_resume.get("status") != "active"
        ):
            mark_desktop_direct_recovery(
                state,
                target.name,
                thread_id,
                turn_id=evidence_turn_id,
                action="goal_changed",
                app_server_url=target.app_server_url,
                now=now,
            )
            store.save(state)
            report["actions"].append(
                {
                    "thread_id": thread_id,
                    "action": "goal_changed_before_wake",
                    "mode": "direct",
                }
            )
            return
        if _thread_or_turn_active(before_resume):
            mark_desktop_direct_recovery(
                state,
                target.name,
                thread_id,
                turn_id=evidence_turn_id,
                action="runtime_active",
                app_server_url=target.app_server_url,
                now=now,
            )
            store.save(state)
            report["actions"].append(
                {
                    "thread_id": thread_id,
                    "action": "desktop_runtime_already_active",
                    "mode": "direct",
                }
            )
            return

        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="resume_requested",
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)
        client.resume_thread(thread_id)
        for _ in range(2):
            if target.resume_grace_seconds > 0:
                self._sleep(target.resume_grace_seconds)
            wake_thread = client.read_thread(thread_id, include_turns=True)
            wake_goal = client.get_goal(thread_id)
            if (
                not isinstance(wake_goal, dict)
                or wake_goal.get("status") != "active"
            ):
                mark_desktop_direct_recovery(
                    state,
                    target.name,
                    thread_id,
                    turn_id=evidence_turn_id,
                    action="goal_changed",
                    app_server_url=target.app_server_url,
                    now=now,
                )
                store.save(state)
                report["actions"].append(
                    {
                        "thread_id": thread_id,
                        "action": "goal_changed_before_turn_start",
                        "mode": "direct",
                    }
                )
                return
            if _thread_or_turn_active(wake_thread):
                recovery_turn_id = _latest_turn_id(wake_thread)
                mark_desktop_direct_recovery(
                    state,
                    target.name,
                    thread_id,
                    turn_id=evidence_turn_id,
                    action="runtime_active",
                    recovery_turn_id=recovery_turn_id,
                    app_server_url=target.app_server_url,
                    now=now,
                )
                store.save(state)
                report["actions"].append(
                    {
                        "thread_id": thread_id,
                        "action": "desktop_runtime_became_active",
                        "turn_id": recovery_turn_id,
                        "mode": "direct",
                    }
                )
                _, goal_after_turn = self._wait_until_settled(
                    client,
                    thread_id,
                    target,
                )
                mark_desktop_direct_recovery(
                    state,
                    target.name,
                    thread_id,
                    turn_id=evidence_turn_id,
                    action="turn_settled",
                    recovery_turn_id=recovery_turn_id,
                    app_server_url=target.app_server_url,
                    now=now,
                )
                store.save(state)
                report["actions"].append(
                    {
                        "thread_id": thread_id,
                        "action": "desktop_turn_settled",
                        "turn_id": recovery_turn_id,
                        "goal_status": (
                            goal_after_turn.get("status")
                            if isinstance(goal_after_turn, dict)
                            else None
                        ),
                        "mode": "direct",
                    }
                )
                return

        message_id = _desktop_recovery_message_id(
            target.name,
            evidence_turn_id,
            thread_id,
        )
        turn = client.start_turn(
            thread_id,
            prompt=recovery_prompt,
            client_user_message_id=message_id,
        )
        recovery_turn_id = turn.get("id")
        normalized_recovery_turn_id = (
            str(recovery_turn_id)
            if recovery_turn_id is not None
            else None
        )
        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="turn_started",
            recovery_turn_id=normalized_recovery_turn_id,
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)
        report["actions"].append(
            {
                "thread_id": thread_id,
                "action": "desktop_turn_started",
                "turn_id": recovery_turn_id,
                "client_user_message_id": message_id,
                "mode": "direct",
            }
        )

        _, goal_after_turn = self._wait_until_settled(
            client,
            thread_id,
            target,
        )
        mark_desktop_direct_recovery(
            state,
            target.name,
            thread_id,
            turn_id=evidence_turn_id,
            action="turn_settled",
            recovery_turn_id=normalized_recovery_turn_id,
            app_server_url=target.app_server_url,
            now=now,
        )
        store.save(state)
        report["actions"].append(
            {
                "thread_id": thread_id,
                "action": "desktop_turn_settled",
                "turn_id": recovery_turn_id,
                "goal_status": (
                    goal_after_turn.get("status")
                    if isinstance(goal_after_turn, dict)
                    else None
                ),
                "mode": "direct",
            }
        )

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
            if target.recovery_mode != "cli_turn":
                report["errors"].append(
                    {
                        "thread_id": None,
                        "error": "desktop recovery requires an explicit request",
                    }
                )
                return False
            if self._process_probe(target):
                report["skipped"].append(
                    {"thread_id": None, "reason": "cli_process_running"}
                )
                return False
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
                        record.get("action") in _STAGED_RECOVERY_ACTIONS
                        and thread_id not in known_ids
                    ):
                        summaries.append({"id": thread_id})

                had_error = False
                recovery_waiting = False
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
                    if record_action in _STAGED_RECOVERY_ACTIONS:
                        terminal = not target.start_recovery_turn

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
                            if record_action in _STAGED_RECOVERY_ACTIONS and (
                                reason.startswith("goal_")
                                or reason == "thread_stale"
                            ):
                                action = (
                                    "thread_resumed_stale"
                                    if reason == "thread_stale"
                                    else "thread_resumed_goal_changed"
                                )
                                if not dry_run:
                                    mark_recovered(
                                        state,
                                        target.name,
                                        outage_generation,
                                        thread_id,
                                        action=action,
                                        turn_id=None,
                                        now=now,
                                    )
                                    store.save(state)
                                report["actions"].append(
                                    {
                                        "thread_id": thread_id,
                                        "action": action,
                                    }
                                )
                            elif (
                                record_action in _STAGED_RECOVERY_ACTIONS
                                and _is_safety_rejection(reason)
                            ):
                                if not dry_run:
                                    mark_recovered(
                                        state,
                                        target.name,
                                        outage_generation,
                                        thread_id,
                                        action="recovery_skipped_safety",
                                        turn_id=None,
                                        now=now,
                                    )
                                    store.save(state)
                                report["actions"].append(
                                    {
                                        "thread_id": thread_id,
                                        "action": "recovery_skipped_safety",
                                        "reason": reason,
                                    }
                                )
                            elif (
                                target.start_recovery_turn
                                and not terminal
                                and reason in _WAITING_RECOVERY_REASONS
                            ):
                                recovery_waiting = True
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
                            after_resume, goal_after_resume = (
                                self._wait_until_settled(
                                    client,
                                    thread_id,
                                    target,
                                )
                            )
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
                                        "action": (
                                            "thread_resumed_goal_changed"
                                        ),
                                    }
                                )
                                continue
                            mark_recovered(
                                state,
                                target.name,
                                outage_generation,
                                thread_id,
                                action="thread_resumed_turn_completed",
                                turn_id=None,
                                now=now,
                            )
                            store.save(state)
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "thread_resumed_turn_completed",
                                }
                            )
                            continue

                        if not target.start_recovery_turn:
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
                        _, goal_after_turn = self._wait_until_settled(
                            client,
                            thread_id,
                            target,
                        )
                        final_action = (
                            "turn_completed"
                            if (
                                isinstance(goal_after_turn, dict)
                                and goal_after_turn.get("status") == "active"
                            )
                            else "thread_resumed_goal_changed"
                        )
                        mark_recovered(
                            state,
                            target.name,
                            outage_generation,
                            thread_id,
                            action=final_action,
                            turn_id=(
                                str(turn_id)
                                if turn_id is not None
                                else None
                            ),
                            now=now,
                        )
                        store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": final_action,
                                "turn_id": turn_id,
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
                return not had_error and not recovery_waiting
        except Exception as error:
            report["errors"].append(
                {"thread_id": None, "error": _safe_exception(error)}
            )
            return False

    @staticmethod
    def _default_client(target: TargetConfig) -> AppServerClient:
        command = target.command
        if target.app_server_url is None:
            command = command + ("app-server", "--listen", "stdio://")
        return AppServerClient(
            command=command,
            codex_home=target.codex_home,
            timeout_seconds=(
                120
                if target.recovery_mode == "desktop_goal_state"
                else 15
            ),
            websocket_url=target.app_server_url,
        )

    def _wait_until_settled(
        self,
        client: Any,
        thread_id: str,
        target: TargetConfig,
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        idle_observations = 0
        thread: dict[str, Any] = {}
        goal: Optional[dict[str, Any]] = None
        while idle_observations < 2:
            if target.resume_grace_seconds > 0:
                self._sleep(target.resume_grace_seconds)
            thread = client.read_thread(thread_id, include_turns=True)
            goal = client.get_goal(thread_id)
            if _thread_or_turn_active(thread):
                idle_observations = 0
            else:
                idle_observations += 1
        return thread, goal


def _thread_status(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict):
        return str(status.get("type", ""))
    return str(status or "")


def _thread_source(thread: dict[str, Any]) -> str:
    source = thread.get("source")
    if isinstance(source, str):
        return source.strip().lower()
    if isinstance(source, dict):
        for key in ("type", "kind"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


def _latest_recovery_candidate_turn(
    thread: dict[str, Any],
) -> dict[str, Any] | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if isinstance(turn, dict) and not _is_guardian_heartbeat_turn(turn):
            return turn
    return None


def _model_capacity_failure_turn(
    thread: dict[str, Any], target: TargetConfig
) -> dict[str, Any] | None:
    candidate = _latest_recovery_candidate_turn(thread)
    if not isinstance(candidate, dict):
        return None
    if str(candidate.get("status", "")) not in {
        "completed",
        "failed",
        "interrupted",
    }:
        return None
    error = _desktop_turn_error(thread, candidate, target)
    if not isinstance(error, dict):
        return None
    if not _error_looks_like_model_capacity_failure(error):
        return None
    return candidate


def _new_model_capacity_record(
    failure_turn_id: str,
    target: TargetConfig,
    *,
    now: int,
) -> dict[str, Any]:
    failure_count = 1
    return {
        "failure_turn_id": failure_turn_id,
        "recovery_turn_id": None,
        "action": "retry_scheduled",
        "model_index": 0,
        "model": None,
        "attempts_in_model": 0,
        "total_attempts": 0,
        "capacity_failure_count": failure_count,
        "next_retry_at": now + _model_capacity_backoff_seconds(
            target, failure_count
        ),
    }


def _schedule_observed_capacity_failure(
    record: dict[str, Any],
    failure_turn_id: str,
    target: TargetConfig,
    *,
    now: int,
) -> dict[str, Any]:
    updated = dict(record)
    failure_count = int(updated.get("capacity_failure_count", 0)) + 1
    updated.update(
        {
            "failure_turn_id": failure_turn_id,
            "recovery_turn_id": None,
            "action": "retry_scheduled",
            "capacity_failure_count": failure_count,
            "next_retry_at": now
            + _model_capacity_backoff_seconds(target, failure_count),
        }
    )
    return updated


def _model_capacity_backoff_seconds(
    target: TargetConfig, failure_count: int
) -> int:
    exponent = max(0, min(failure_count - 1, 30))
    delay = target.model_capacity_backoff_initial_seconds * (2**exponent)
    return min(delay, target.model_capacity_backoff_max_seconds)


def _capacity_model(target: TargetConfig, model_index: int) -> str | None:
    if model_index == 0:
        return None
    return target.model_capacity_fallback_models[model_index - 1]


def _capacity_model_label(record: dict[str, Any]) -> str:
    value = record.get("model")
    return str(value) if isinstance(value, str) and value else "thread_default"


def _model_capacity_message_id(
    target_name: str,
    thread_id: str,
    failure_turn_id: str,
    model_index: int,
    attempt_number: int,
) -> str:
    value = (
        "codex-goal-guardian:model-capacity:"
        f"{target_name}:{thread_id}:{failure_turn_id}:"
        f"{model_index}:{attempt_number}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _desktop_recovery_candidate_turn(
    thread: dict[str, Any],
    *,
    pending_evidence_turn_id: str | None,
) -> dict[str, Any] | None:
    latest = _latest_recovery_candidate_turn(thread)
    if (
        not pending_evidence_turn_id
        or not isinstance(latest, dict)
        or latest.get("id") == pending_evidence_turn_id
    ):
        return latest

    turns = thread.get("turns")
    if not isinstance(turns, list):
        return latest
    evidence_index = next(
        (
            index
            for index, turn in enumerate(turns)
            if (
                isinstance(turn, dict)
                and turn.get("id") == pending_evidence_turn_id
            )
        ),
        None,
    )
    if evidence_index is None:
        return latest
    evidence = turns[evidence_index]
    for later in turns[evidence_index + 1 :]:
        if not isinstance(later, dict) or _is_guardian_heartbeat_turn(later):
            continue
        if not _is_empty_interrupted_turn(later):
            return latest
    return evidence if isinstance(evidence, dict) else latest


def _is_empty_interrupted_turn(turn: dict[str, Any]) -> bool:
    items = turn.get("items")
    return (
        turn.get("status") == "interrupted"
        and isinstance(items, list)
        and not items
    )


def _is_guardian_heartbeat_turn(turn: dict[str, Any]) -> bool:
    items = turn.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if (
                isinstance(text, str)
                and "<heartbeat>" in text
                and "request-desktop-recovery" in text
            ):
                return True
    return False


def _desktop_turn_error(
    thread: dict[str, Any],
    turn: dict[str, Any],
    target: TargetConfig,
) -> dict[str, Any] | None:
    inline_error = turn.get("error")
    if isinstance(inline_error, dict) and any(
        str(inline_error.get(key, "")).strip()
        for key in ("message", "additionalDetails", "codexErrorInfo")
    ):
        return inline_error

    turn_id = turn.get("id")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
        return None
    return _session_task_complete_error(
        thread,
        turn_id,
        codex_home=target.codex_home,
    )


def _session_task_complete_error(
    thread: dict[str, Any],
    turn_id: str,
    *,
    codex_home: str,
) -> dict[str, Any] | None:
    path_value = thread.get("path")
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        session_path = _normalized_local_path(path_value).resolve(strict=True)
        sessions_root = (
            _normalized_local_path(codex_home).resolve(strict=True)
            / "sessions"
        )
        session_path.relative_to(sessions_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if session_path.suffix.lower() != ".jsonl":
        return None

    needle = turn_id.encode("utf-8")
    try:
        with session_path.open("rb") as handle:
            if session_path.stat().st_size == 0:
                return None
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                position = data.rfind(needle)
                for _ in range(64):
                    if position < 0:
                        break
                    line_start = data.rfind(b"\n", 0, position) + 1
                    line_end = data.find(b"\n", position)
                    if line_end < 0:
                        line_end = len(data)
                    if line_end - line_start <= 1_048_576:
                        try:
                            event = json.loads(data[line_start:line_end])
                        except (UnicodeError, json.JSONDecodeError):
                            event = None
                        if isinstance(event, dict):
                            payload = event.get("payload")
                            if (
                                event.get("type") == "event_msg"
                                and isinstance(payload, dict)
                                and payload.get("type") == "task_complete"
                                and payload.get("turn_id") == turn_id
                            ):
                                error = payload.get("error")
                                return error if isinstance(error, dict) else None
                    position = data.rfind(needle, 0, line_start)
    except (OSError, ValueError):
        return None
    return None


def _normalized_local_path(value: str) -> Path:
    normalized = value
    if os.name == "nt":
        if normalized.startswith("\\\\?\\UNC\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
    return Path(normalized).expanduser()


def _desktop_network_failure_turn_id(
    thread: dict[str, Any],
    target: TargetConfig,
    *,
    pending_evidence_turn_id: str | None = None,
) -> str | None:
    if _thread_status(thread) not in {"idle", "systemError", "notLoaded"}:
        return None
    if _has_in_progress_turn(thread):
        return None
    if _thread_source(thread) not in target.allowed_sources:
        return None

    candidate = _desktop_recovery_candidate_turn(
        thread,
        pending_evidence_turn_id=pending_evidence_turn_id,
    )
    if not isinstance(candidate, dict):
        return None
    turn_id = candidate.get("id")
    if (
        not isinstance(turn_id, str)
        or not turn_id
        or len(turn_id) > 128
    ):
        return None
    error = _desktop_turn_error(thread, candidate, target)
    if not isinstance(error, dict):
        return None
    if not _error_looks_like_network_failure(error):
        return None
    return turn_id


def _desktop_continuity_turn_id(
    thread: dict[str, Any],
    target: TargetConfig,
    *,
    pending_evidence_turn_id: str | None = None,
) -> str | None:
    if not target.delegated_continuity_enabled:
        return _desktop_network_failure_turn_id(
            thread,
            target,
            pending_evidence_turn_id=pending_evidence_turn_id,
        )
    if _thread_status(thread) not in {"idle", "systemError", "notLoaded"}:
        return None
    if _has_in_progress_turn(thread):
        return None
    if _thread_source(thread) not in target.allowed_sources:
        return None
    candidate = _desktop_recovery_candidate_turn(
        thread,
        pending_evidence_turn_id=pending_evidence_turn_id,
    )
    if not isinstance(candidate, dict):
        return None
    if str(candidate.get("status", "")) not in {
        "completed",
        "failed",
        "interrupted",
    }:
        return None
    turn_id = candidate.get("id")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
        return None
    error = _desktop_turn_error(thread, candidate, target)
    if isinstance(error, dict) and _error_looks_like_prompt_policy_rejection(
        error
    ):
        return None
    return turn_id


def _has_in_progress_turn(thread: dict[str, Any]) -> bool:
    turns = thread.get("turns")
    return bool(
        isinstance(turns, list)
        and any(
            isinstance(turn, dict) and turn.get("status") == "inProgress"
            for turn in turns
        )
    )


def _latest_turn_id(thread: dict[str, Any]) -> str | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _latest_activity_timestamp(
    thread: dict[str, Any], goal: Optional[dict[str, Any]]
) -> int | None:
    timestamps = [
        _timestamp_seconds(thread.get("updatedAt")),
        _timestamp_seconds(goal.get("updatedAt"))
        if isinstance(goal, dict)
        else None,
    ]
    turns = thread.get("turns")
    if isinstance(turns, list):
        for turn in turns[-12:]:
            if not isinstance(turn, dict):
                continue
            timestamps.extend(
                _timestamp_seconds(turn.get(field))
                for field in (
                    "updatedAt",
                    "startedAt",
                    "completedAt",
                    "createdAt",
                )
            )
    present = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(present) if present else None


def _verify_goal_reactivation(
    previous: dict[str, Any], current: dict[str, Any]
) -> None:
    if current.get("status") != "active":
        raise AppServerError(
            "thread/goal/set did not return an active goal"
        )
    for field in (
        "threadId",
        "objective",
        "tokenBudget",
        "tokensUsed",
        "timeUsedSeconds",
        "createdAt",
    ):
        if current.get(field) != previous.get(field):
            raise AppServerError(
                f"thread/goal/set changed preserved field {field}"
            )


def _is_safety_rejection(reason: str) -> bool:
    return reason.startswith("source_") or reason in {
        "prompt_policy_rejection",
        "turn_completed",
        "turn_not_network_failure",
    }


def _has_staged_recovery(
    state: dict[str, Any],
    target_name: str,
    outage_generation: int,
) -> bool:
    return any(
        record.get("action") in _STAGED_RECOVERY_ACTIONS
        for record in recovery_records(
            state,
            target_name,
            outage_generation,
        ).values()
    )


def _thread_or_turn_active(thread: dict[str, Any]) -> bool:
    if _thread_status(thread) == "active":
        return True
    return _has_in_progress_turn(thread)


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


def _desktop_recovery_message_id(
    target_name: str,
    evidence_turn_id: str,
    thread_id: str,
) -> str:
    seed = (
        "codex-goal-guardian:desktop:"
        f"{target_name}:{evidence_turn_id}:{thread_id}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _safe_exception(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:1000]
