#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


TRACE_PATH = os.environ.get("FAKE_APP_SERVER_TRACE")


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def trace(message: dict) -> None:
    if not TRACE_PATH:
        return
    with Path(TRACE_PATH).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, separators=(",", ":")) + "\n")


def thread(status: str = "idle", *, include_turns: bool = False) -> dict:
    turns = []
    if include_turns:
        turns = [
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
        ]
    return {
        "id": "thread-1",
        "sessionId": "thread-1",
        "preview": "test",
        "modelProvider": "openai",
        "createdAt": 100,
        "updatedAt": 110,
        "status": {"type": status},
        "cwd": "/workspace",
        "cliVersion": "0.145.0",
        "source": "cli",
        "ephemeral": False,
        "turns": turns,
    }


for raw_line in sys.stdin:
    if not raw_line.strip():
        continue
    message = json.loads(raw_line)
    trace(message)
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialized":
        continue
    if method == "initialize":
        emit(
            {
                "id": request_id,
                "result": {
                    "userAgent": "fake-app-server",
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            }
        )
    elif method == "thread/list":
        emit({"method": "thread/status/changed", "params": {"threadId": "thread-1"}})
        emit({"id": request_id, "result": {"data": [thread()], "nextCursor": None}})
    elif method == "thread/goal/get":
        emit(
            {
                "id": request_id,
                "result": {
                    "goal": {
                        "threadId": "thread-1",
                        "objective": "finish safely",
                        "status": "active",
                        "tokensUsed": 100,
                        "timeUsedSeconds": 20,
                        "createdAt": 90,
                        "updatedAt": 110,
                    }
                },
            }
        )
    elif method == "thread/read":
        emit(
            {
                "id": request_id,
                "result": {
                    "thread": thread(
                        include_turns=bool(message.get("params", {}).get("includeTurns"))
                    )
                },
            }
        )
    elif method == "thread/resume":
        emit({"id": request_id, "result": {"thread": thread(), "cwd": "/workspace"}})
    elif method == "turn/start":
        emit(
            {
                "id": request_id,
                "result": {
                    "turn": {
                        "id": "turn-recovery",
                        "status": "inProgress",
                        "items": [],
                    }
                },
            }
        )
    elif method == "hang":
        continue
    else:
        emit(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method}",
                },
            }
        )
