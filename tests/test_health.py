import socket
import unittest
import urllib.error

from codex_goal_guardian.config import HealthConfig
from codex_goal_guardian.health import probe_health


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


class HealthProbeTests(unittest.TestCase):
    def test_reachable_authentication_error_counts_as_healthy(self) -> None:
        def open_url(*_: object, **__: object) -> object:
            raise urllib.error.HTTPError(
                "https://chatgpt.com/backend-api/codex",
                401,
                "Unauthorized",
                {},
                None,
            )

        result = probe_health(HealthConfig(), open_url=open_url)

        self.assertTrue(result.healthy)
        self.assertEqual(result.status_code, 401)

    def test_transport_error_is_unhealthy(self) -> None:
        def open_url(*_: object, **__: object) -> object:
            raise urllib.error.URLError("connection refused")

        result = probe_health(HealthConfig(), open_url=open_url)

        self.assertFalse(result.healthy)
        self.assertIn("connection refused", result.reason)

    def test_server_error_is_unhealthy(self) -> None:
        def open_url(*_: object, **__: object) -> _Response:
            return _Response(503)

        result = probe_health(HealthConfig(), open_url=open_url)

        self.assertFalse(result.healthy)
        self.assertEqual(result.status_code, 503)

    def test_failed_tcp_precheck_skips_http(self) -> None:
        http_called = False

        def connect(*_: object, **__: object) -> object:
            raise socket.timeout("proxy port timed out")

        def open_url(*_: object, **__: object) -> _Response:
            nonlocal http_called
            http_called = True
            return _Response(204)

        result = probe_health(
            HealthConfig(tcp_host="127.0.0.1", tcp_port=7890),
            open_url=open_url,
            create_connection=connect,
        )

        self.assertFalse(result.healthy)
        self.assertFalse(http_called)
        self.assertIn("TCP", result.reason)


if __name__ == "__main__":
    unittest.main()
