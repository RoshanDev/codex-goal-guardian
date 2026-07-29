from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Callable, Optional

from .app_server import AppServerClient, AppServerError
from .config import GuardianConfig, TargetConfig
from .health import HealthResult, probe_health
from .ownership import cli_process_is_running
from .state import (
    StateStore,
    finish_desktop_recovery_request,
    is_recovery_pending,
    mark_recovered,
    pending_desktop_recovery_requests,
    recovery_record,
    recovery_records,
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
    turn = _recent_failed_turn(thread)
    if turn is None:
        return False
    return _turn_looks_like_network_failure(turn)


def _turn_looks_like_network_failure(turn: dict[str, Any]) -> bool:
    error = turn.get("error")
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
    if not _turn_looks_like_network_failure(last_turn):
        return False, "turn_not_network_failure"
    return True, "eligible"


def desktop_goal_reactivation_eligibility(
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
    if goal_status != "blocked":
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
    failed_turn = _recent_failed_turn(thread)
    if failed_turn is None:
        turn_status = str(turns[-1].get("status", "missing"))
        return False, f"turn_{turn_status}"
    if not looks_like_network_failure(thread):
        return False, "turn_not_network_failure"
    return True, "eligible"


class RecoveryEngine:
    def __init__(
        self,
        *,
        probe: Callable[[Any], HealthResult] = probe_health,
        client_factory: Optional[Callable[[TargetConfig], Any]] = None,
        process_probe: Callable[[TargetConfig], bool] = cli_process_is_running,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probe = probe
        self._client_factory = client_factory or self._default_client
        self._process_probe = process_probe
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

                if target.recovery_mode == "desktop_goal_state":
                    requests = pending_desktop_recovery_requests(
                        state, target.name
                    )
                    if not requests:
                        continue
                    complete = self._recover_desktop_requests(
                        target,
                        requests,
                        state,
                        store,
                        target_report,
                        timestamp,
                        dry_run=dry_run,
                    )
                    if dry_run:
                        target_report["status"] = "dry_run"
                    elif complete:
                        target_report["status"] = "recovered"
                    else:
                        target_report["status"] = "recovery_pending"
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

    def _recover_desktop_requests(
        self,
        target: TargetConfig,
        requests: dict[str, dict[str, Any]],
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
                for thread_id, request in requests.items():
                    generation = int(request.get("generation", -1))
                    try:
                        thread = client.read_thread(
                            thread_id, include_turns=True
                        )
                        goal = client.get_goal(thread_id)

                        if (
                            isinstance(goal, dict)
                            and goal.get("status") == "active"
                        ):
                            if not dry_run:
                                finish_desktop_recovery_request(
                                    state,
                                    target.name,
                                    thread_id,
                                    expected_generation=generation,
                                    action="goal_already_active",
                                    now=now,
                                )
                                store.save(state)
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "goal_already_active",
                                    "same_runtime_wake_required": True,
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
                            )
                        )
                        if not eligible:
                            if reason in _WAITING_RECOVERY_REASONS:
                                recovery_waiting = True
                            elif not dry_run:
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

                        if dry_run:
                            report["actions"].append(
                                {
                                    "thread_id": thread_id,
                                    "action": "would_reactivate_goal",
                                    "network_failure": True,
                                    "same_runtime_wake_required": True,
                                }
                            )
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
                            )
                        )
                        if not fresh_eligible:
                            if fresh_reason in _WAITING_RECOVERY_REASONS:
                                recovery_waiting = True
                            else:
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
                        finish_desktop_recovery_request(
                            state,
                            target.name,
                            thread_id,
                            expected_generation=generation,
                            action="goal_state_reactivated",
                            now=now,
                        )
                        store.save(state)
                        report["actions"].append(
                            {
                                "thread_id": thread_id,
                                "action": "goal_state_reactivated",
                                "tokens_used": confirmed_goal.get("tokensUsed"),
                                "time_used_seconds": confirmed_goal.get(
                                    "timeUsedSeconds"
                                ),
                                "same_runtime_wake_required": True,
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
        except Exception as error:
            report["errors"].append(
                {"thread_id": None, "error": _safe_exception(error)}
            )
            return False
        return not had_error and not recovery_waiting

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
        return AppServerClient(
            command=target.command + ("app-server", "--listen", "stdio://"),
            codex_home=target.codex_home,
            timeout_seconds=15,
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


def _recent_failed_turn(thread: dict[str, Any]) -> dict[str, Any] | None:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns[-12:]):
        if (
            isinstance(turn, dict)
            and turn.get("status") in {"failed", "interrupted"}
        ):
            return turn
    return None


def _has_in_progress_turn(thread: dict[str, Any]) -> bool:
    turns = thread.get("turns")
    return bool(
        isinstance(turns, list)
        and any(
            isinstance(turn, dict) and turn.get("status") == "inProgress"
            for turn in turns
        )
    )


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


def _safe_exception(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:1000]
