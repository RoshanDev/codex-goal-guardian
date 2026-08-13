import json
import tempfile
import unittest
from pathlib import Path

from codex_goal_guardian.state import (
    StateCorruptionError,
    StateStore,
    default_state,
    delegated_cli_recovery_record,
    desktop_active_observation,
    enqueue_desktop_recovery_request,
    finish_desktop_recovery_request,
    mark_recovered,
    model_capacity_recovery_record,
    pending_desktop_recovery_requests,
    save_model_capacity_recovery,
    save_delegated_cli_recovery,
    save_desktop_active_observation,
    transition_health,
    was_recovered,
)


class HealthTransitionTests(unittest.TestCase):
    def test_unknown_to_healthy_does_not_trigger_recovery(self) -> None:
        state = default_state()

        transition = transition_health(
            state,
            target_name="wsl",
            healthy=True,
            required_successes=2,
            now=100,
        )

        self.assertFalse(transition.recover_now)
        self.assertEqual(transition.outage_generation, 0)
        self.assertEqual(state["targets"]["wsl"]["health"], "up")

    def test_down_then_two_healthy_checks_triggers_once(self) -> None:
        state = default_state()

        candidate = transition_health(
            state,
            target_name="windows",
            healthy=False,
            required_successes=2,
            now=90,
        )
        down = transition_health(
            state,
            target_name="windows",
            healthy=False,
            required_successes=2,
            now=100,
        )
        first = transition_health(
            state,
            target_name="windows",
            healthy=True,
            required_successes=2,
            now=110,
        )
        second = transition_health(
            state,
            target_name="windows",
            healthy=True,
            required_successes=2,
            now=120,
        )
        third = transition_health(
            state,
            target_name="windows",
            healthy=True,
            required_successes=2,
            now=130,
        )

        self.assertEqual(candidate.outage_generation, 0)
        self.assertEqual(down.outage_generation, 1)
        self.assertFalse(first.recover_now)
        self.assertTrue(second.recover_now)
        self.assertFalse(third.recover_now)
        self.assertEqual(state["targets"]["windows"]["health"], "up")

    def test_repeated_down_checks_keep_same_generation(self) -> None:
        state = default_state()

        first = transition_health(state, "wsl", False, 2, now=100)
        second = transition_health(state, "wsl", False, 2, now=110)
        third = transition_health(state, "wsl", False, 2, now=120)

        self.assertEqual(first.outage_generation, 0)
        self.assertEqual(second.outage_generation, 1)
        self.assertEqual(third.outage_generation, 1)
        self.assertEqual(state["targets"]["wsl"]["outage_started_at"], 100)

    def test_single_failed_probe_does_not_create_outage(self) -> None:
        state = default_state()
        transition_health(state, "windows", True, 2, now=90)

        failed = transition_health(
            state, "windows", False, 2, now=100
        )
        recovered = transition_health(
            state, "windows", True, 2, now=110
        )

        self.assertEqual(failed.outage_generation, 0)
        self.assertEqual(failed.health, "up")
        self.assertEqual(failed.consecutive_unhealthy, 1)
        self.assertFalse(recovered.recover_now)
        self.assertEqual(recovered.outage_generation, 0)

    def test_recovery_marker_is_scoped_to_target_generation_and_thread(self) -> None:
        state = default_state()

        mark_recovered(
            state,
            target_name="windows",
            outage_generation=3,
            thread_id="thread-a",
            action="turn_started",
            turn_id="turn-1",
            now=200,
        )

        self.assertTrue(was_recovered(state, "windows", 3, "thread-a"))
        self.assertFalse(was_recovered(state, "windows", 4, "thread-a"))
        self.assertFalse(was_recovered(state, "wsl", 3, "thread-a"))

    def test_model_capacity_retry_record_is_persisted_per_thread(self) -> None:
        state = default_state()
        save_model_capacity_recovery(
            state,
            "wsl",
            "thread-a",
            {
                "failure_turn_id": "turn-a",
                "attempts_in_model": 3,
                "next_retry_at": 600,
            },
            now=200,
        )

        self.assertEqual(
            model_capacity_recovery_record(state, "wsl", "thread-a"),
            {
                "failure_turn_id": "turn-a",
                "attempts_in_model": 3,
                "next_retry_at": 600,
                "recorded_at": 200,
            },
        )

    def test_delegated_cli_record_is_persisted_per_evidence_turn(self) -> None:
        state = default_state()
        save_delegated_cli_recovery(
            state,
            "wsl",
            "thread-a",
            {
                "evidence_turn_id": "turn-a",
                "recovery_turn_id": "turn-b",
                "action": "continuation_started",
            },
            now=200,
        )

        self.assertEqual(
            delegated_cli_recovery_record(state, "wsl", "thread-a"),
            {
                "evidence_turn_id": "turn-a",
                "recovery_turn_id": "turn-b",
                "action": "continuation_started",
                "recorded_at": 200,
            },
        )

    def test_desktop_recovery_request_coalesces_until_finished(self) -> None:
        state = default_state()

        first = enqueue_desktop_recovery_request(
            state,
            "desktop",
            "thread-a",
            now=100,
        )
        duplicate = enqueue_desktop_recovery_request(
            state,
            "desktop",
            "thread-a",
            now=110,
        )

        self.assertFalse(first["coalesced"])
        self.assertTrue(duplicate["coalesced"])
        self.assertEqual(first["generation"], duplicate["generation"])
        self.assertEqual(
            list(pending_desktop_recovery_requests(state, "desktop")),
            ["thread-a"],
        )

        self.assertTrue(
            finish_desktop_recovery_request(
                state,
                "desktop",
                "thread-a",
                expected_generation=first["generation"],
                action="goal_state_reactivated",
                now=120,
            )
        )
        self.assertEqual(
            pending_desktop_recovery_requests(state, "desktop"),
            {},
        )

        next_request = enqueue_desktop_recovery_request(
            state,
            "desktop",
            "thread-a",
            now=130,
        )
        self.assertGreater(next_request["generation"], first["generation"])
        self.assertFalse(next_request["coalesced"])

    def test_desktop_active_observation_is_persisted_per_thread(self) -> None:
        state = default_state()

        save_desktop_active_observation(
            state,
            "desktop",
            "thread-a",
            {
                "fingerprint": "abc",
                "last_progress_at": 190,
                "unchanged_observations": 2,
            },
            now=200,
        )

        self.assertEqual(
            desktop_active_observation(state, "desktop", "thread-a"),
            {
                "fingerprint": "abc",
                "last_progress_at": 190,
                "unchanged_observations": 2,
                "recorded_at": 200,
            },
        )

    def test_desktop_recovery_finish_requires_matching_generation(self) -> None:
        state = default_state()
        request = enqueue_desktop_recovery_request(
            state,
            "desktop",
            "thread-a",
            now=100,
        )

        self.assertFalse(
            finish_desktop_recovery_request(
                state,
                "desktop",
                "thread-a",
                expected_generation=request["generation"] + 1,
                action="goal_state_reactivated",
                now=110,
            )
        )
        self.assertIn(
            "thread-a",
            pending_desktop_recovery_requests(state, "desktop"),
        )


class StateStoreTests(unittest.TestCase):
    def test_round_trip_uses_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            state = default_state()
            state["targets"]["wsl"] = {"health": "up"}

            with store.locked():
                store.save(state)

            loaded = store.load()
            self.assertEqual(loaded["schema_version"], 1)
            self.assertEqual(loaded["targets"]["wsl"]["health"], "up")

    def test_corrupt_state_fails_closed_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            store = StateStore(path)

            with self.assertRaises(StateCorruptionError):
                store.load()

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
            self.assertEqual(json.loads(default_state_json())["schema_version"], 1)


def default_state_json() -> str:
    return json.dumps(default_state())


if __name__ == "__main__":
    unittest.main()
