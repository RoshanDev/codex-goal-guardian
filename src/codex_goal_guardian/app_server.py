from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from . import __version__


CREATE_NO_WINDOW = 0x08000000
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_WEBSOCKET_MESSAGE_BYTES = 256 * 1024 * 1024


def _app_server_creation_flags(platform_name: str | None = None) -> int:
    current_platform = os.name if platform_name is None else platform_name
    return CREATE_NO_WINDOW if current_platform == "nt" else 0


class AppServerError(RuntimeError):
    """Raised when the Codex app-server cannot complete a request."""


class _ReaderStopped:
    def __init__(self, reason: str) -> None:
        self.reason = reason


class AppServerClient:
    """Small JSON-RPC client for stdio or a shared WebSocket app-server."""

    def __init__(
        self,
        command: Sequence[str],
        codex_home: str,
        *,
        timeout_seconds: float = 10,
        extra_env: Optional[Mapping[str, str]] = None,
        websocket_url: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.command = tuple(str(part) for part in command)
        self.codex_home = str(Path(codex_home).expanduser())
        self.timeout_seconds = float(timeout_seconds)
        self.extra_env = dict(extra_env or {})
        self.websocket_url = (
            _validated_loopback_websocket_url(websocket_url)
            if websocket_url
            else None
        )

        self._process: Optional[subprocess.Popen[str]] = None
        self._socket: socket.socket | None = None
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
        if self.websocket_url is not None:
            if self._socket is not None:
                return
            self._start_websocket()
            return
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

    def _start_websocket(self) -> None:
        assert self.websocket_url is not None
        self._messages = queue.Queue()
        self._stderr.clear()
        try:
            connection = _connect_websocket(
                self.websocket_url,
                timeout_seconds=self.timeout_seconds,
            )
        except (OSError, ValueError) as error:
            raise AppServerError(
                f"failed to connect to shared app-server "
                f"{self.websocket_url!r}: {error}"
            ) from error
        self._socket = connection
        threading.Thread(
            target=self._read_websocket,
            args=(connection,),
            name="codex-goal-guardian-websocket",
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
        connection = self._socket
        self._socket = None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

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
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.start()
        return self._request(
            method,
            params or {},
            timeout_seconds=timeout_seconds,
        )

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

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            timeout_seconds=30,
        )

    def start_turn(
        self,
        thread_id: str,
        *,
        prompt: str,
        client_user_message_id: str,
        model: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "clientUserMessageId": client_user_message_id,
        }
        if model is not None:
            params["model"] = model
        result = self.request(
            "turn/start",
            params,
        )
        return self._required_object(result, "turn", "turn/start")

    def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
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

            request_timeout = (
                self.timeout_seconds
                if timeout_seconds is None
                else float(timeout_seconds)
            )
            if request_timeout <= 0:
                raise ValueError("request timeout_seconds must be positive")
            deadline = time.monotonic() + request_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._failure(
                        f"{method} timed out after {request_timeout:g}s"
                    )
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as error:
                    raise self._failure(
                        f"{method} timed out after {request_timeout:g}s"
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
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        connection = self._socket
        if connection is not None:
            with self._write_lock:
                try:
                    _send_websocket_frame(
                        connection,
                        opcode=0x1,
                        payload=encoded.encode("utf-8"),
                    )
                except OSError as error:
                    raise self._failure(
                        f"failed to write to shared app-server: {error}"
                    ) from error
            return

        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise self._failure("app-server is not running")
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

    def _read_websocket(self, connection: socket.socket) -> None:
        try:
            while True:
                raw_message = self._receive_websocket_message(connection)
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError as error:
                    self._messages.put(
                        _ReaderStopped(f"invalid JSON response: {error}")
                    )
                    return
                self._messages.put(message)
        except (EOFError, OSError, UnicodeDecodeError, ValueError) as error:
            self._messages.put(_ReaderStopped(str(error)))

    def _receive_websocket_message(self, connection: socket.socket) -> str:
        chunks: list[bytes] = []
        message_opcode: int | None = None
        total_size = 0
        while True:
            final, opcode, payload = _read_websocket_frame(connection)
            if opcode == 0x8:
                raise EOFError("shared app-server closed the WebSocket")
            if opcode == 0x9:
                with self._write_lock:
                    _send_websocket_frame(
                        connection,
                        opcode=0xA,
                        payload=payload,
                    )
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if message_opcode is not None:
                    raise ValueError("received a new WebSocket message mid-frame")
                message_opcode = opcode
            elif opcode == 0x0:
                if message_opcode is None:
                    raise ValueError("received an unexpected continuation frame")
            else:
                raise ValueError(f"unsupported WebSocket opcode: {opcode}")

            chunks.append(payload)
            total_size += len(payload)
            if total_size > _MAX_WEBSOCKET_MESSAGE_BYTES:
                raise ValueError("shared app-server WebSocket message is too large")
            if final:
                return b"".join(chunks).decode("utf-8")

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


def _validated_loopback_websocket_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "ws":
        raise ValueError("shared app-server URL must use ws://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("shared app-server URL must not contain credentials")
    if parsed.fragment:
        raise ValueError("shared app-server URL must not contain a fragment")
    host = parsed.hostname
    if not host:
        raise ValueError("shared app-server URL must include a host")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback:
        raise ValueError("shared app-server URL must use a loopback host")
    if parsed.port is None:
        raise ValueError("shared app-server URL must include a port")
    return value


def _connect_websocket(value: str, *, timeout_seconds: float) -> socket.socket:
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    assert parsed.port is not None
    connection = socket.create_connection(
        (parsed.hostname, parsed.port),
        timeout=timeout_seconds,
    )
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host_header = parsed.hostname
        if ":" in host_header:
            host_header = f"[{host_header}]"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        connection.sendall(request.encode("ascii"))
        response = _read_http_headers(connection)
        lines = response.decode("iso-8859-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise ValueError(
                f"WebSocket upgrade failed: {lines[0] if lines else 'empty response'}"
            )
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, header_value = line.split(":", 1)
            headers[name.strip().lower()] = header_value.strip()
        expected_accept = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected_accept:
            raise ValueError("WebSocket upgrade returned an invalid accept key")
        if headers.get("upgrade", "").lower() != "websocket":
            raise ValueError("WebSocket upgrade header is missing")
        connection.settimeout(None)
        return connection
    except Exception:
        connection.close()
        raise


def _read_http_headers(connection: socket.socket) -> bytes:
    response = bytearray()
    while not response.endswith(b"\r\n\r\n"):
        chunk = connection.recv(1)
        if not chunk:
            raise EOFError("shared app-server closed during WebSocket upgrade")
        response.extend(chunk)
        if len(response) > 64 * 1024:
            raise ValueError("WebSocket upgrade headers are too large")
    return bytes(response)


def _send_websocket_frame(
    connection: socket.socket, *, opcode: int, payload: bytes
) -> None:
    first = 0x80 | opcode
    length = len(payload)
    if length < 126:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((first, 0x80 | 127)) + struct.pack("!Q", length)
    mask = secrets.token_bytes(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    connection.sendall(header + mask + masked)


def _read_websocket_frame(
    connection: socket.socket,
) -> tuple[bool, int, bytes]:
    header = _read_exact(connection, 2)
    first, second = header
    if first & 0x70:
        raise ValueError("compressed WebSocket frames are not supported")
    final = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(connection, 8))[0]
    if length > _MAX_WEBSOCKET_MESSAGE_BYTES:
        raise ValueError("shared app-server WebSocket frame is too large")
    if opcode >= 0x8 and (not final or length > 125):
        raise ValueError("invalid WebSocket control frame")
    mask = _read_exact(connection, 4) if masked else b""
    payload = _read_exact(connection, length)
    if masked:
        payload = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
    return final, opcode, payload


def _read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise EOFError("shared app-server WebSocket reached EOF")
        data.extend(chunk)
    return bytes(data)
