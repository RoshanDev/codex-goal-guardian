import json
import sys
import tempfile
import unittest
from pathlib import Path

from codex_goal_guardian.app_server import (
    AppServerClient,
    AppServerError,
    _app_server_creation_flags,
)
from tests.fixtures.fake_websocket_app_server import (
    FakeWebSocketAppServer,
)


class AppServerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.trace_path = Path(self.temporary.name) / "trace.jsonl"
        server = Path(__file__).parent / "fixtures" / "fake_app_server.py"
        self.client = AppServerClient(
            command=(sys.executable, str(server)),
            codex_home=self.temporary.name,
            timeout_seconds=2,
            extra_env={"FAKE_APP_SERVER_TRACE": str(self.trace_path)},
        )
        self.addCleanup(self.client.close)

    def test_high_level_calls_use_documented_methods(self) -> None:
        with self.client:
            threads = self.client.list_threads(limit=7)
            goal = self.client.get_goal("thread-1")
            reactivated = self.client.reactivate_goal("thread-1")
            loaded = self.client.read_thread("thread-1", include_turns=True)
            resumed = self.client.resume_thread("thread-1")
            started = self.client.start_turn(
                "thread-1",
                prompt="reconcile and continue",
                client_user_message_id="message-1",
            )

        self.assertEqual(threads[0]["id"], "thread-1")
        self.assertEqual(goal["status"], "active")
        self.assertEqual(reactivated["status"], "active")
        self.assertEqual(loaded["turns"][-1]["status"], "failed")
        self.assertEqual(resumed["id"], "thread-1")
        self.assertEqual(started["id"], "turn-recovery")

        messages = [
            json.loads(line)
            for line in self.trace_path.read_text(encoding="utf-8").splitlines()
        ]
        methods = [message["method"] for message in messages]
        self.assertEqual(
            methods,
            [
                "initialize",
                "initialized",
                "thread/list",
                "thread/goal/get",
                "thread/goal/set",
                "thread/read",
                "thread/resume",
                "turn/start",
            ],
        )
        self.assertEqual(
            messages[4]["params"],
            {"threadId": "thread-1", "status": "active"},
        )
        turn_params = messages[-1]["params"]
        self.assertEqual(
            turn_params["input"],
            [{"type": "text", "text": "reconcile and continue"}],
        )
        self.assertEqual(turn_params["clientUserMessageId"], "message-1")

    def test_request_timeout_is_bounded(self) -> None:
        with self.client:
            with self.assertRaises(AppServerError) as context:
                self.client.request("hang", {})

        self.assertIn("timed out", str(context.exception))

    def test_protocol_error_includes_method(self) -> None:
        with self.client:
            with self.assertRaises(AppServerError) as context:
                self.client.request("missing/method", {})

        self.assertIn("missing/method", str(context.exception))

    def test_windows_app_server_uses_create_no_window(self) -> None:
        self.assertEqual(_app_server_creation_flags("nt"), 0x08000000)
        self.assertEqual(_app_server_creation_flags("posix"), 0)

    def test_shared_websocket_clients_observe_the_same_goal_state(self) -> None:
        with FakeWebSocketAppServer() as server:
            first = AppServerClient(
                command=("unused",),
                codex_home=self.temporary.name,
                websocket_url=server.url,
                timeout_seconds=2,
            )
            second = AppServerClient(
                command=("unused",),
                codex_home=self.temporary.name,
                websocket_url=server.url,
                timeout_seconds=2,
            )
            self.addCleanup(first.close)
            self.addCleanup(second.close)

            with first, second:
                first.reactivate_goal("thread-1")
                observed = second.get_goal("thread-1")

        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(observed["status"], "active")
        self.assertEqual(observed["tokensUsed"], 100)

    def test_shared_websocket_rejects_non_loopback_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            AppServerClient(
                command=("unused",),
                codex_home=self.temporary.name,
                websocket_url="ws://example.com:47831/rpc",
            )


if __name__ == "__main__":
    unittest.main()
