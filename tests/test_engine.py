from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_goal_guardian.app_server import AppServerError
from codex_goal_guardian.config import (
    GuardianConfig,
    HealthConfig,
    TargetConfig,
)
from codex_goal_guardian.engine import (
    RecoveryEngine,
    desktop_goal_reactivation_eligibility,
    looks_like_network_failure,
    thread_eligibility,
)
from codex_goal_guardian.health import HealthResult
from codex_goal_guardian.state import (
    StateStore,
    default_state,
    enqueue_desktop_recovery_request,
    is_recovery_pending,
    mark_recovered,
    pending_desktop_recovery_requests,
    recovery_record,
    transition_health,
    was_recovered,
)


def make_thread(
    *,
    thread_status: str = "idle",
    turn_status: str = "failed",
    updated_at: int = 110,
    error_message: str = "stream disconnected before completion",
    source: str = "cli",
) -> dict:
    return {
        "id": "thread-1",
        "source": source,
        "status": {"type": thread_status},
        "updatedAt": updated_at,
        "turns": [
            {
                "id": "turn-1",
                "status": turn_status,
                "error": {"message": error_message},
            }
        ],
    }


class EligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = TargetConfig(
            name="wsl",
            command=("codex",),
            codex_home="/tmp/codex",
            max_thread_age_seconds=100,
        )
        self.goal = {"status": "active"}

    def test_network_failed_active_goal_is_eligible(self) -> None:
        thread = make_thread()
        eligible, reason = thread_eligibility(
            thread,
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertTrue(eligible, reason)
        self.assertTrue(looks_like_network_failure(thread))

    def test_non_network_failure_is_not_misclassified(self) -> None:
        thread = make_thread(error_message="tool command returned exit code 2")

        self.assertFalse(looks_like_network_failure(thread))

    def test_idle_completed_turn_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(turn_status="completed", error_message=""),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "turn_completed")

    def test_non_network_failed_turn_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(error_message="tool command returned exit code 2"),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "turn_not_network_failure")

    def test_desktop_vscode_thread_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(source="vscode"),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "source_vscode")

    def test_running_turn_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(thread_status="active", turn_status="inProgress"),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "thread_active")

    def test_inactive_goal_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(),
            {"status": "paused"},
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "goal_paused")

    def test_stale_thread_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(updated_at=10),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "thread_stale")

    def test_completed_recovery_is_rejected(self) -> None:
        eligible, reason = thread_eligibility(
            make_thread(),
            self.goal,
            self.target,
            now=150,
            already_recovered=True,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "already_recovered")


class DesktopGoalReactivationEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = TargetConfig(
            name="windows-desktop-goal-state",
            command=("codex",),
            codex_home="/tmp/codex",
            recovery_mode="desktop_goal_state",
            allowed_sources=("vscode",),
            max_thread_age_seconds=100,
            start_recovery_turn=False,
        )
        self.goal = {"status": "blocked"}

    def test_blocked_desktop_goal_after_network_failure_is_eligible(self) -> None:
        eligible, reason = desktop_goal_reactivation_eligibility(
            make_thread(source="vscode"),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertTrue(eligible, reason)

    def test_paused_goal_is_never_reactivated(self) -> None:
        eligible, reason = desktop_goal_reactivation_eligibility(
            make_thread(source="vscode"),
            {"status": "paused"},
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "goal_paused")

    def test_active_turn_is_never_reactivated(self) -> None:
        eligible, reason = desktop_goal_reactivation_eligibility(
            make_thread(
                source="vscode",
                thread_status="active",
                turn_status="inProgress",
            ),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "thread_active")

    def test_non_network_failure_is_never_reactivated(self) -> None:
        eligible, reason = desktop_goal_reactivation_eligibility(
            make_thread(
                source="vscode",
                error_message="tool command returned exit code 2",
            ),
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "turn_not_network_failure")

    def test_completed_heartbeat_after_network_failure_remains_eligible(
        self,
    ) -> None:
        thread = make_thread(source="vscode")
        thread["turns"].append(
            {
                "id": "heartbeat-turn",
                "status": "completed",
                "completedAt": 145,
            }
        )

        eligible, reason = desktop_goal_reactivation_eligibility(
            thread,
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertTrue(eligible, reason)
        self.assertTrue(looks_like_network_failure(thread))

    def test_recent_goal_activity_keeps_long_lived_thread_eligible(self) -> None:
        thread = make_thread(source="vscode", updated_at=1)
        goal = {"status": "blocked", "updatedAt": 145}

        eligible, reason = desktop_goal_reactivation_eligibility(
            thread,
            goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertTrue(eligible, reason)

    def test_any_in_progress_turn_blocks_desktop_reactivation(self) -> None:
        thread = make_thread(source="vscode")
        thread["turns"].insert(
            0,
            {"id": "running-turn", "status": "inProgress"},
        )

        eligible, reason = desktop_goal_reactivation_eligibility(
            thread,
            self.goal,
            self.target,
            now=150,
            already_recovered=False,
        )

        self.assertFalse(eligible)
        self.assertEqual(reason, "turn_in_progress")


class _FakeClient:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        read_sequence: list[dict] | None = None,
        listed_thread: dict | None = None,
        goal: dict | None = None,
        reactivated_goal: dict | None = None,
    ) -> None:
        self.fail_start = fail_start
        self.read_sequence = list(read_sequence or [])
        self.listed_thread = listed_thread or make_thread()
        self.goal = goal or {
            "threadId": "thread-1",
            "objective": "finish safely",
            "status": "active",
            "tokenBudget": 40_000,
            "tokensUsed": 100,
            "timeUsedSeconds": 20,
            "createdAt": 90,
            "updatedAt": 110,
        }
        self.reactivated_goal = reactivated_goal
        self.resume_calls = 0
        self.start_calls = 0
        self.reactivate_calls = 0
        self.read_calls = 0
        self.list_calls = 0

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_threads(self, *, limit: int) -> list[dict]:
        self.list_calls += 1
        return [self.listed_thread]

    def get_goal(self, thread_id: str) -> dict:
        return dict(self.goal)

    def reactivate_goal(self, thread_id: str) -> dict:
        self.reactivate_calls += 1
        if self.reactivated_goal is not None:
            self.goal = dict(self.reactivated_goal)
        else:
            self.goal = dict(self.goal)
            self.goal["status"] = "active"
            self.goal["updatedAt"] = int(self.goal.get("updatedAt", 0)) + 1
        return dict(self.goal)

    def read_thread(self, thread_id: str, *, include_turns: bool) -> dict:
        self.read_calls += 1
        if self.read_sequence:
            return self.read_sequence.pop(0)
        return self.listed_thread

    def resume_thread(self, thread_id: str) -> dict:
        self.resume_calls += 1
        return make_thread()

    def start_turn(
        self,
        thread_id: str,
        *,
        prompt: str,
        client_user_message_id: str,
    ) -> dict:
        self.start_calls += 1
        if self.fail_start:
            raise AppServerError("simulated turn/start failure")
        return {"id": "turn-recovery", "status": "inProgress"}


class RecoveryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = StateStore(root / "state.json")
        state = default_state()
        transition_health(state, "wsl", False, 2, now=100)
        transition_health(state, "wsl", False, 2, now=105)
        transition_health(state, "wsl", True, 2, now=110)
        self.store.save(state)
        self.target = TargetConfig(
            name="wsl",
            command=("codex",),
            codex_home=str(root / "codex-home"),
            max_thread_age_seconds=100,
            resume_grace_seconds=0,
        )
        self.config = GuardianConfig(
            state_path=str(self.store.path),
            log_path=str(root / "guardian.jsonl"),
            health=HealthConfig(required_consecutive_successes=2),
            targets=(self.target,),
            recovery_prompt="reconcile and continue",
        )

    @staticmethod
    def healthy(_: HealthConfig) -> HealthResult:
        return HealthResult(True, "HTTP 401 reachable", 401, 1)

    def test_confirmed_recovery_resumes_and_starts_once(self) -> None:
        client = _FakeClient()
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        first = engine.run_once(self.config)
        second = engine.run_once(self.config)

        self.assertEqual(client.resume_calls, 1)
        self.assertEqual(client.start_calls, 1)
        self.assertEqual(first["targets"][0]["actions"][0]["action"], "turn_started")
        self.assertEqual(second["targets"][0]["status"], "healthy")
        state = self.store.load()
        self.assertTrue(was_recovered(state, "wsl", 1, "thread-1"))

    def test_failed_turn_start_retries_after_fresh_resume(self) -> None:
        first_client = _FakeClient(fail_start=True)
        second_client = _FakeClient()
        clients = iter((first_client, second_client))
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: next(clients),
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        first = engine.run_once(self.config)
        second = engine.run_once(self.config)

        self.assertEqual(first_client.resume_calls, 1)
        self.assertEqual(first_client.start_calls, 1)
        self.assertEqual(second_client.resume_calls, 1)
        self.assertEqual(second_client.start_calls, 1)
        self.assertTrue(first["targets"][0]["errors"])
        self.assertEqual(second["targets"][0]["actions"][0]["action"], "turn_started")

    def test_active_turn_after_resume_is_followed_without_second_turn(self) -> None:
        client = _FakeClient(
            read_sequence=[
                make_thread(),
                make_thread(
                    thread_status="active",
                    turn_status="inProgress",
                ),
                make_thread(turn_status="completed"),
                make_thread(turn_status="completed"),
            ]
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(self.config)
        state = self.store.load()

        self.assertEqual(report["targets"][0]["status"], "recovered")
        self.assertEqual(
            report["targets"][0]["actions"][0]["action"],
            "thread_resumed_active",
        )
        self.assertFalse(is_recovery_pending(state, "wsl"))
        self.assertEqual(
            recovery_record(
                state,
                "wsl",
                1,
                "thread-1",
            )["action"],
            "thread_resumed_turn_completed",
        )
        self.assertEqual(client.resume_calls, 1)
        self.assertEqual(client.start_calls, 0)
        self.assertEqual(
            report["targets"][0]["actions"][1]["action"],
            "thread_resumed_turn_completed",
        )

    def test_initial_active_turn_stays_pending_until_idle(self) -> None:
        active = make_thread(
            thread_status="active",
            turn_status="inProgress",
        )
        first_client = _FakeClient(
            listed_thread=active,
            read_sequence=[active],
        )
        second_client = _FakeClient(
            read_sequence=[
                make_thread(),
                make_thread(),
                make_thread(turn_status="completed"),
                make_thread(turn_status="completed"),
            ]
        )
        clients = iter((first_client, second_client))
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: next(clients),
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        first = engine.run_once(self.config)
        second = engine.run_once(self.config)

        self.assertEqual(first["targets"][0]["status"], "recovery_pending")
        self.assertEqual(
            first["targets"][0]["skipped"][0]["reason"],
            "thread_active",
        )
        self.assertEqual(first_client.resume_calls, 0)
        self.assertEqual(first_client.start_calls, 0)
        self.assertEqual(second_client.resume_calls, 1)
        self.assertEqual(second_client.start_calls, 1)
        self.assertEqual(
            second["targets"][0]["actions"][0]["action"],
            "turn_started",
        )

    def test_legacy_active_record_is_rearmed_after_upgrade(self) -> None:
        state = self.store.load()
        transition_health(state, "wsl", True, 2, now=115)
        mark_recovered(
            state,
            "wsl",
            1,
            "thread-1",
            action="thread_resumed_active",
            turn_id=None,
            now=115,
        )
        self.store.save(state)
        client = _FakeClient(
            read_sequence=[
                make_thread(turn_status="interrupted"),
                make_thread(turn_status="interrupted"),
            ]
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(self.config)

        self.assertEqual(report["targets"][0]["status"], "recovered")
        self.assertEqual(client.resume_calls, 1)
        self.assertEqual(client.start_calls, 1)
        self.assertEqual(
            report["targets"][0]["actions"][0]["action"],
            "turn_started",
        )

    def test_staged_desktop_recovery_is_closed_without_mutation(self) -> None:
        state = self.store.load()
        mark_recovered(
            state,
            "wsl",
            1,
            "thread-1",
            action="thread_resumed_active",
            turn_id=None,
            now=115,
        )
        self.store.save(state)
        desktop = make_thread(source="vscode")
        client = _FakeClient(
            listed_thread=desktop,
            read_sequence=[desktop],
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(self.config)

        self.assertEqual(report["targets"][0]["status"], "recovered")
        self.assertEqual(client.resume_calls, 0)
        self.assertEqual(client.start_calls, 0)
        self.assertEqual(
            report["targets"][0]["actions"][0]["action"],
            "recovery_skipped_safety",
        )
        self.assertEqual(
            recovery_record(
                self.store.load(),
                "wsl",
                1,
                "thread-1",
            )["action"],
            "recovery_skipped_safety",
        )

    def test_running_native_cli_process_defers_recovery(self) -> None:
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: self.fail("client should not start"),
            process_probe=lambda _: True,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(self.config)

        self.assertEqual(
            report["targets"][0]["status"],
            "recovery_pending",
        )
        self.assertEqual(
            report["targets"][0]["skipped"][0]["reason"],
            "cli_process_running",
        )

    def test_dry_run_reports_candidate_without_mutating_thread(self) -> None:
        client = _FakeClient()
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(self.config, dry_run=True)

        self.assertEqual(client.resume_calls, 0)
        self.assertEqual(client.start_calls, 0)
        self.assertEqual(report["targets"][0]["actions"][0]["action"], "would_resume")

    def test_desktop_mode_reactivates_goal_without_thread_takeover(self) -> None:
        desktop_target = TargetConfig(
            name="windows-desktop-goal-state",
            command=("codex",),
            codex_home=self.target.codex_home,
            recovery_mode="desktop_goal_state",
            allowed_sources=("vscode",),
            max_thread_age_seconds=100,
            start_recovery_turn=False,
        )
        state = self.store.load()
        transition_health(
            state,
            desktop_target.name,
            False,
            2,
            now=100,
        )
        transition_health(
            state,
            desktop_target.name,
            False,
            2,
            now=105,
        )
        transition_health(
            state,
            desktop_target.name,
            True,
            2,
            now=110,
        )
        enqueue_desktop_recovery_request(
            state,
            desktop_target.name,
            "thread-1",
            now=111,
        )
        self.store.save(state)
        goal = {
            "threadId": "thread-1",
            "objective": "finish safely",
            "status": "blocked",
            "tokenBudget": 40_000,
            "tokensUsed": 1234,
            "timeUsedSeconds": 5678,
            "createdAt": 90,
            "updatedAt": 110,
        }
        desktop = make_thread(source="vscode")
        client = _FakeClient(
            listed_thread=desktop,
            read_sequence=[desktop],
            goal=goal,
        )
        config = GuardianConfig(
            state_path=str(self.store.path),
            log_path=self.config.log_path,
            health=self.config.health,
            targets=(desktop_target,),
            recovery_prompt=self.config.recovery_prompt,
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: True,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(config)

        self.assertEqual(client.reactivate_calls, 1)
        self.assertEqual(client.resume_calls, 0)
        self.assertEqual(client.start_calls, 0)
        self.assertEqual(
            report["targets"][0]["actions"],
            [
                {
                    "thread_id": "thread-1",
                    "action": "goal_state_reactivated",
                    "tokens_used": 1234,
                    "time_used_seconds": 5678,
                    "same_runtime_wake_required": True,
                }
            ],
        )
        self.assertEqual(client.goal["objective"], goal["objective"])
        self.assertEqual(client.goal["tokensUsed"], goal["tokensUsed"])
        self.assertEqual(
            client.goal["timeUsedSeconds"],
            goal["timeUsedSeconds"],
        )

    def test_desktop_mode_fails_closed_if_accounting_changes(self) -> None:
        desktop_target = TargetConfig(
            name="windows-desktop-goal-state",
            command=("codex",),
            codex_home=self.target.codex_home,
            recovery_mode="desktop_goal_state",
            allowed_sources=("vscode",),
            max_thread_age_seconds=100,
            start_recovery_turn=False,
        )
        state = self.store.load()
        transition_health(
            state,
            desktop_target.name,
            False,
            2,
            now=100,
        )
        transition_health(
            state,
            desktop_target.name,
            False,
            2,
            now=105,
        )
        transition_health(
            state,
            desktop_target.name,
            True,
            2,
            now=110,
        )
        enqueue_desktop_recovery_request(
            state,
            desktop_target.name,
            "thread-1",
            now=111,
        )
        self.store.save(state)
        goal = {
            "threadId": "thread-1",
            "objective": "finish safely",
            "status": "blocked",
            "tokenBudget": 40_000,
            "tokensUsed": 1234,
            "timeUsedSeconds": 5678,
            "createdAt": 90,
            "updatedAt": 110,
        }
        changed = dict(goal)
        changed.update(status="active", tokensUsed=0, updatedAt=111)
        desktop = make_thread(source="vscode")
        client = _FakeClient(
            listed_thread=desktop,
            read_sequence=[desktop],
            goal=goal,
            reactivated_goal=changed,
        )
        config = GuardianConfig(
            state_path=str(self.store.path),
            log_path=self.config.log_path,
            health=self.config.health,
            targets=(desktop_target,),
            recovery_prompt=self.config.recovery_prompt,
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(config)

        self.assertEqual(client.resume_calls, 0)
        self.assertEqual(client.start_calls, 0)
        self.assertIn("preserved field tokensUsed", report["targets"][0]["errors"][0]["error"])
        self.assertTrue(
            pending_desktop_recovery_requests(
                self.store.load(),
                desktop_target.name,
            )
        )

    def test_desktop_mode_closes_pre_mutation_activity_race(self) -> None:
        desktop_target = TargetConfig(
            name="windows-desktop-goal-state",
            command=("codex",),
            codex_home=self.target.codex_home,
            recovery_mode="desktop_goal_state",
            allowed_sources=("vscode",),
            max_thread_age_seconds=100,
            start_recovery_turn=False,
        )
        state = self.store.load()
        transition_health(
            state,
            desktop_target.name,
            True,
            2,
            now=110,
        )
        enqueue_desktop_recovery_request(
            state,
            desktop_target.name,
            "thread-1",
            now=111,
        )
        self.store.save(state)
        goal = {
            "threadId": "thread-1",
            "objective": "finish safely",
            "status": "blocked",
            "tokenBudget": 40_000,
            "tokensUsed": 1234,
            "timeUsedSeconds": 5678,
            "createdAt": 90,
            "updatedAt": 110,
        }
        desktop = make_thread(source="vscode")
        active = make_thread(
            source="vscode",
            thread_status="active",
            turn_status="inProgress",
        )
        client = _FakeClient(
            listed_thread=desktop,
            read_sequence=[desktop, active],
            goal=goal,
        )
        config = GuardianConfig(
            state_path=str(self.store.path),
            log_path=self.config.log_path,
            health=self.config.health,
            targets=(desktop_target,),
            recovery_prompt=self.config.recovery_prompt,
        )
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: client,
            process_probe=lambda _: False,
            now=lambda: 120,
            sleep=lambda _: None,
        )

        report = engine.run_once(config)

        self.assertEqual(client.reactivate_calls, 0)
        self.assertEqual(client.resume_calls, 0)
        self.assertEqual(client.start_calls, 0)
        self.assertEqual(
            report["targets"][0]["skipped"][0],
            {
                "thread_id": "thread-1",
                "reason": "thread_active",
                "stage": "pre_mutation",
            },
        )
        self.assertTrue(
            pending_desktop_recovery_requests(
                self.store.load(),
                desktop_target.name,
            )
        )

    def test_repeated_healthy_check_does_not_rewrite_state(self) -> None:
        state = self.store.load()
        transition_health(state, "wsl", True, 2, now=120)
        self.store.save(state)
        engine = RecoveryEngine(
            probe=self.healthy,
            client_factory=lambda _: self.fail("client should not start"),
            process_probe=lambda _: False,
            now=lambda: 130,
            sleep=lambda _: None,
        )

        with patch.object(StateStore, "save", autospec=True) as save:
            report = engine.run_once(self.config)

        save.assert_not_called()
        self.assertFalse(report["targets"][0]["state_changed"])


if __name__ == "__main__":
    unittest.main()
