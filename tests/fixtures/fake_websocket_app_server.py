from __future__ import annotations

import base64
import hashlib
import json
import socketserver
import struct
import threading
from typing import Any


_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeWebSocketAppServer:
    def __init__(
        self,
        *,
        source: str = "vscode",
        goal_status: str = "blocked",
    ) -> None:
        self.source = source
        self.goal = {
            "threadId": "thread-1",
            "objective": "finish safely",
            "status": goal_status,
            "tokenBudget": 40_000,
            "tokensUsed": 100,
            "timeUsedSeconds": 20,
            "createdAt": 90,
            "updatedAt": 110,
        }
        self.messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server = _ThreadingServer(("127.0.0.1", 0), _Handler)
        self._server.fixture = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-websocket-app-server",
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"ws://{host}:{port}/rpc"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> "FakeWebSocketAppServer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def respond(self, message: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self.messages.append(message)
            method = message.get("method")
            request_id = message.get("id")
            if method == "initialized":
                return None
            if method == "initialize":
                return {
                    "id": request_id,
                    "result": {
                        "userAgent": "fake-websocket-app-server",
                        "platformFamily": "windows",
                        "platformOs": "windows",
                    },
                }
            if method == "thread/list":
                return {
                    "id": request_id,
                    "result": {
                        "data": [self._thread_payload()],
                        "nextCursor": None,
                    },
                }
            if method == "thread/goal/get":
                return {
                    "id": request_id,
                    "result": {"goal": dict(self.goal)},
                }
            if method == "thread/goal/set":
                params = message.get("params", {})
                if params.get("threadId") != "thread-1":
                    return self._error(request_id, "unknown thread")
                self.goal["status"] = params.get("status", self.goal["status"])
                self.goal["updatedAt"] += 1
                return {
                    "id": request_id,
                    "result": {"goal": dict(self.goal)},
                }
            if method == "thread/read":
                include_turns = bool(
                    message.get("params", {}).get("includeTurns")
                )
                return {
                    "id": request_id,
                    "result": {
                        "thread": self._thread_payload(
                            include_turns=include_turns
                        )
                    },
                }
            if method == "thread/resume":
                return {
                    "id": request_id,
                    "result": {"thread": self._thread_payload()},
                }
            if method == "turn/interrupt":
                return {"id": request_id, "result": {}}
            if method == "turn/start":
                return {
                    "id": request_id,
                    "result": {
                        "turn": {
                            "id": "turn-recovery",
                            "status": "inProgress",
                            "items": [],
                        }
                    },
                }
            return self._error(request_id, f"method not found: {method}")

    def _thread_payload(self, *, include_turns: bool = False) -> dict[str, Any]:
        turns: list[dict[str, Any]] = []
        if include_turns:
            turns.append(
                {
                    "id": "turn-failed",
                    "status": "failed",
                    "items": [],
                    "error": {
                        "message": "stream disconnected before completion",
                        "additionalDetails": "Reconnecting 5/5",
                        "codexErrorInfo": None,
                    },
                    "startedAt": 100,
                    "completedAt": 110,
                }
            )
        return {
            "id": "thread-1",
            "sessionId": "thread-1",
            "preview": "test",
            "modelProvider": "openai",
            "createdAt": 100,
            "updatedAt": 110,
            "status": {"type": "idle"},
            "cwd": "/workspace",
            "cliVersion": "0.145.0",
            "source": self.source,
            "ephemeral": False,
            "turns": turns,
        }

    @staticmethod
    def _error(request_id: Any, message: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "error": {"code": -32601, "message": message},
        }


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    fixture: FakeWebSocketAppServer


class _Handler(socketserver.BaseRequestHandler):
    server: _ThreadingServer

    def handle(self) -> None:
        headers = self._read_headers()
        key = headers.get("sec-websocket-key")
        if not key:
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()
        ).decode("ascii")
        self.request.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        while True:
            try:
                opcode, payload = self._read_frame()
            except EOFError:
                return
            if opcode == 0x8:
                return
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode != 0x1:
                return
            message = json.loads(payload.decode("utf-8"))
            response = self.server.fixture.respond(message)
            if response is not None:
                self._send_frame(
                    0x1,
                    json.dumps(response, separators=(",", ":")).encode("utf-8"),
                )

    def _read_headers(self) -> dict[str, str]:
        raw = bytearray()
        while not raw.endswith(b"\r\n\r\n"):
            chunk = self.request.recv(1)
            if not chunk:
                raise EOFError
            raw.extend(chunk)
        lines = raw.decode("iso-8859-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        return headers

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if second & 0x80 else b""
        payload = self._read_exact(length)
        if mask:
            payload = bytes(
                byte ^ mask[index % 4]
                for index, byte in enumerate(payload)
            )
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        if len(payload) < 126:
            header = bytes((first, len(payload)))
        elif len(payload) <= 0xFFFF:
            header = bytes((first, 126)) + struct.pack("!H", len(payload))
        else:
            header = bytes((first, 127)) + struct.pack("!Q", len(payload))
        self.request.sendall(header + payload)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return bytes(data)
