from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.local_server import create_server


class LocalServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def json_request(self, path: str, *, method: str = "GET"):
        request = Request(
            self.base_url + path,
            method=method,
            headers={"Accept": "application/json", "Origin": self.base_url},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_status_exposes_api_but_disables_update_without_blpapi(self) -> None:
        with patch("app.local_server.bloomberg_client.is_bloomberg_available", return_value=False):
            request = Request(self.base_url + "/api/update/status", headers={"Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                status = response.status
                payload = json.loads(response.read())
                cache_control = response.headers["Cache-Control"]
        self.assertEqual(status, 200)
        self.assertEqual(cache_control, "no-store")
        self.assertTrue(payload["update_api"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["config"], "security_roots.xlsx")
        self.assertIn("blpapi", payload["message"])

    def test_static_dashboard_is_served_from_fixed_root(self) -> None:
        with urlopen(self.base_url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            cache_control = response.headers["Cache-Control"]
        self.assertIn("Pricing Dashboard", html)
        self.assertIn('id="data-update"', html)
        self.assertEqual(cache_control, "private, no-cache")
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base_url + "/../../README.md", timeout=5)
        self.assertEqual(raised.exception.code, 404)

    def test_post_runs_fixed_update_and_returns_summary(self) -> None:
        result = {"ok": True, "success": True, "message": "Update complete", "rows": 42}
        finished = threading.Event()

        def complete_update(*, paths):
            finished.set()
            return result

        with (
            patch("app.local_server.bloomberg_client.is_bloomberg_available", return_value=True),
            patch("app.local_server.update_pipeline.run_bloomberg_update", side_effect=complete_update) as update,
        ):
            status, payload = self.json_request("/api/update", method="POST")
            self.assertTrue(finished.wait(timeout=2))
            _status, completed = self.json_request("/api/update/status")
        self.assertEqual(status, 202)
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["config"], "security_roots.xlsx")
        self.assertEqual(completed["last_result"]["rows"], 42)
        update.assert_called_once_with(paths=self.server.update_paths)

    def test_concurrent_post_returns_409(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_update(*, paths):
            started.set()
            release.wait(timeout=5)
            return {"ok": True, "success": True, "message": "Update complete"}

        with (
            patch("app.local_server.bloomberg_client.is_bloomberg_available", return_value=True),
            patch("app.local_server.update_pipeline.run_bloomberg_update", side_effect=blocking_update),
        ):
            first_status, first_payload = self.json_request("/api/update", method="POST")
            self.assertTrue(started.wait(timeout=2))
            request = Request(
                self.base_url + "/api/update",
                method="POST",
                headers={"Accept": "application/json", "Origin": self.base_url},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 409)
            release.set()
            for _attempt in range(100):
                _status, completed = self.json_request("/api/update/status")
                if completed["last_success"] is True:
                    break
                threading.Event().wait(0.01)
        self.assertEqual(first_status, 202)
        self.assertTrue(first_payload["accepted"])
        self.assertTrue(completed["last_success"])

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
