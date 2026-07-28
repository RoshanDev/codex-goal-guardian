from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import HealthConfig


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    reason: str
    status_code: int | None
    elapsed_ms: int


def probe_health(
    config: HealthConfig,
    *,
    open_url: Optional[Callable[..., Any]] = None,
    create_connection: Callable[..., Any] = socket.create_connection,
) -> HealthResult:
    """Probe transport reachability without requiring Codex credentials."""

    started = time.monotonic()
    if (config.tcp_host is None) != (config.tcp_port is None):
        return _result(started, False, "TCP precheck is incompletely configured")

    if config.tcp_host is not None and config.tcp_port is not None:
        try:
            connection = create_connection(
                (config.tcp_host, config.tcp_port),
                timeout=config.timeout_seconds,
            )
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        except (OSError, TimeoutError) as error:
            return _result(
                started,
                False,
                f"TCP precheck failed: {_safe_error(error, config)}",
            )

    if open_url is None:
        handlers: list[Any] = []
        if config.proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": config.proxy_url, "https": config.proxy_url}
                )
            )
        open_url = urllib.request.build_opener(*handlers).open

    request = urllib.request.Request(
        config.url,
        headers={"User-Agent": "codex-goal-guardian/0.1"},
        method="HEAD",
    )
    try:
        with open_url(request, timeout=config.timeout_seconds) as response:
            status_value = getattr(response, "status", None)
            if status_value is None:
                status_value = response.getcode()
            status = int(status_value)
            healthy = 200 <= status < 500
            return _result(
                started,
                healthy,
                f"HTTP {status} reachable" if healthy else f"HTTP {status} server error",
                status,
            )
    except urllib.error.HTTPError as error:
        status = int(error.code)
        healthy = 200 <= status < 500
        return _result(
            started,
            healthy,
            f"HTTP {status} reachable" if healthy else f"HTTP {status} server error",
            status,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return _result(
            started,
            False,
            f"HTTP transport failed: {_safe_error(error, config)}",
        )


def _result(
    started: float,
    healthy: bool,
    reason: str,
    status_code: int | None = None,
) -> HealthResult:
    return HealthResult(
        healthy=healthy,
        reason=reason,
        status_code=status_code,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _safe_error(error: BaseException, config: HealthConfig) -> str:
    message = str(error)
    if config.proxy_url:
        message = message.replace(config.proxy_url, "<proxy>")
    return message[:500]
