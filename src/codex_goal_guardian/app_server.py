from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import __version__


CREATE_NO_WINDOW = 0x08000000


def _app_server_creation_flags(platform_name: str | None = None) -> int:
    current_platform = os.name if platform_name is None else platform_name
    return CREATE_NO_WINDOW if current_platform == "nt" else 0


class AppServerError(RuntimeError):
    """Raised when the Codex app-server cannot complete a request."""


class _ReaderStopped:
    def __init__(self, reason: str) -> None:
        self.reason = reason


class AppServerClient:
    """Small newline-delimited JSON-RPC client for ``codex app-server``."""

    def __init__(
        self,
        command: Sequence[str],
        codex_home: str,
        *,
        timeout_seconds: float = 10,
        extra_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.command = tuple(str(part) for part in command)
        self.codex_home = str(Path(codex_home).expanduser())
        self.timeout_seconds = float(timeout_seconds)
        self.extra_env = dict(extra_env or {})

        self._process: Optional[subprocess.Popen[str]] = None
        self._messages: "queue.Queue[object]" = queue.Queue()
        self._stderr: "deque[str]" = deque(maxlen=100)
        self._notifications: "deque[dict[str, Any]]" = deque(maxlen=100)
        self._request_id = 0
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def __enter__(self) -> "AppServerClient":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        Path(self.codex_home).mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["CODEX_HOME"] = self.codex_home
        environment.update(self.extra_env)

        self._messages = queue.Queue()
        self._stderr.clear()
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=_app_server_creation_flags(),
            )
        except OSError as error:
            raise AppServerError(
                f"failed to start app-server command {self.command!r}: {error}"
            ) from error

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        threading.Thread(
            target=self._read_stdout,
            args=(self._process.stdout,),
            name="codex-goal-guardian-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(self._process.stderr,),
            name="codex-goal-guardian-stderr",
            daemon=True,
        ).start()

        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_goal_guardian",
                        "title": "Codex Goal Guardian",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def request(
        self, method: str, params: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        self.start()
        return self._request(method, params or {})

    def notify(
        self, method: str, params: Optional[Mapping[str, Any]] = None
    ) -> None:
        self.start()
        self._send({"method": method, "params": dict(params or {})})

    def list_threads(self, *, limit: int = 50) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {
                "limit": limit,
                "archived": False,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            },
        )
        data = result.get("data", [])
        if not isinstance(data, list):
            raise AppServerError("thread/list returned a non-list data field")
        return data

    def get_goal(self, thread_id: str) -> Optional[dict[str, Any]]:
        result = self.request("thread/goal/get", {"threadId": thread_id})
        goal = result.get("goal")
        if goal is not None and not isinstance(goal, dict):
            raise AppServerError("thread/goal/get returned an invalid goal")
        return goal

    def reactivate_goal(self, thread_id: str) -> dict[str, Any]:
        result = self.request(
            "thread/goal/set",
            {"threadId": thread_id, "status": "active"},
        )
        return self._required_object(result, "goal", "thread/goal/set")

    def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        result = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        return self._required_object(result, "thread", "thread/read")

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        result = self.request("thread/resume", {"threadId": thread_id})
        return self._required_object(result, "thread", "thread/resume")

    def start_turn(
        self,
        thread_id: str,
        *,
        prompt: str,
        client_user_message_id: str,
    ) -> dict[str, Any]:
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "clientUserMessageId": client_user_message_id,
            },
        )
        return self._required_object(result, "turn", "turn/start")

    def _request(
        self, method: str, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._request_lock:
            self._request_id += 1
            request_id = self._request_id
            self._send(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )

            deadline = time.monotonic() + self.timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._failure(
                        f"{method} timed out after {self.timeout_seconds:g}s"
                    )
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise self._failure(
                        f"{method} timed out after {self.timeout_seconds:g}s"
                    ) from error

                if isinstance(message, _ReaderStopped):
                    raise self._failure(
                        f"{method} failed because app-server stopped: {message.reason}"
                    )
                if not isinstance(message, dict):
                    continue
                if message.get("id") != request_id:
                    self._notifications.append(message)
                    continue
                if "error" in message:
                    error_payload = message.get("error")
                    raise self._failure(
                        f"{method} failed with app-server error: {error_payload!r}"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise self._failure(f"{method} returned an invalid result")
                return result

    def _send(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise self._failure("app-server is not running")
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise self._failure(f"failed to write to app-server: {error}") from error

    def _read_stdout(self, stream: Iterable[str]) -> None:
        try:
            for raw_line in stream:
                if not raw_line.strip():
                    continue
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError as error:
                    self._messages.put(
                        _ReaderStopped(f"invalid JSON response: {error}")
                    )
                    return
                self._messages.put(message)
        except (OSError, ValueError) as error:
            self._messages.put(_ReaderStopped(str(error)))
            return
        self._messages.put(_ReaderStopped("stdout reached EOF"))

    def _read_stderr(self, stream: Iterable[str]) -> None:
        try:
            for raw_line in stream:
                self._stderr.append(raw_line.rstrip())
        except (OSError, ValueError):
            return

    def _failure(self, message: str) -> AppServerError:
        stderr = "\n".join(line for line in self._stderr if line)
        if stderr:
            message = f"{message}; stderr: {stderr}"
        return AppServerError(message)

    @staticmethod
    def _required_object(
        result: Mapping[str, Any], key: str, method: str
    ) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise AppServerError(f"{method} returned an invalid {key}")
        return value
