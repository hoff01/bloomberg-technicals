"""Loopback-only owner server for the Pricing Dashboard trade builder.

The portable dashboard remains a static artifact.  This small server is only
for the licensed owner workstation: it serves ``app/static`` and exposes one
fixed update action that rebuilds the shareable artifacts through
``app.update_pipeline``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import socket
import threading
from typing import Any, Sequence
from urllib.parse import urlsplit
import webbrowser

from app import bloomberg_client, update_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = (PROJECT_ROOT / "app" / "static").resolve()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATUS_PATH = "/api/update/status"
UPDATE_PATH = "/api/update"

_BLOOMBERG_MISSING_MESSAGE = (
    "Bloomberg update is unavailable on this machine. Run INSTALL_BLOOMBERG.bat "
    "to install blpapi on the licensed workstation, then open Bloomberg Terminal."
)
_BLOOMBERG_READY_MESSAGE = (
    "Bloomberg API support is installed. Bloomberg Terminal must be open and "
    "logged in on this computer before updating."
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _is_loopback_host(host: str) -> bool:
    value = str(host or "").strip()
    if value.lower() == "localhost":
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_host(host: str) -> str:
    value = str(host or "").strip()
    if not _is_loopback_host(value):
        raise ValueError(
            "The Pricing Dashboard owner server only accepts a loopback host "
            "such as 127.0.0.1 or localhost."
        )
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def _browser_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


@dataclass
class UpdateState:
    """Thread-safe status plus a nonblocking single-flight update lock."""

    update_lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    updating: bool = False
    message: str = ""
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_success: bool | None = None
    last_result: dict[str, Any] | None = None

    def try_begin(self) -> bool:
        if not self.update_lock.acquire(blocking=False):
            return False
        with self.state_lock:
            self.updating = True
            self.message = "Updating Bloomberg data and rebuilding the export..."
            self.last_started_at = _utc_now()
            self.last_finished_at = None
            self.last_success = None
            self.last_result = None
        return True

    def finish(self, *, success: bool, message: str, result: dict[str, Any] | None) -> None:
        try:
            with self.state_lock:
                self.updating = False
                self.message = str(message)
                self.last_finished_at = _utc_now()
                self.last_success = bool(success)
                self.last_result = dict(result) if result is not None else None
        finally:
            self.update_lock.release()

    def snapshot(self, *, available: bool) -> dict[str, Any]:
        with self.state_lock:
            updating = self.updating
            message = self.message
            payload: dict[str, Any] = {
                "update_api": True,
                "available": bool(available),
                "updating": updating,
                "running": updating,
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_success": self.last_success,
            }
            if self.last_result is not None:
                payload["last_result"] = dict(self.last_result)

        if not available and not updating:
            message = _BLOOMBERG_MISSING_MESSAGE
        elif not message:
            message = _BLOOMBERG_READY_MESSAGE
        payload["message"] = message
        return payload


class LocalDashboardServer(ThreadingHTTPServer):
    """HTTP server carrying the fixed static root and shared update state."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        static_root: Path = STATIC_ROOT,
        update_state: UpdateState | None = None,
        update_paths: update_pipeline.UpdatePaths | None = None,
    ) -> None:
        root = Path(static_root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Dashboard static directory not found: {root}")
        self.static_root = root
        self.update_state = update_state or UpdateState()
        self.update_paths = update_paths or update_pipeline.UpdatePaths()
        handler = partial(DashboardRequestHandler, directory=str(root))
        super().__init__(server_address, handler)

    def update_status(self, *, available: bool) -> dict[str, Any]:
        payload = self.update_state.snapshot(available=available)
        payload["config"] = self.update_paths.config.name
        return payload

    def start_update(self) -> None:
        thread = threading.Thread(
            target=self._run_update,
            name="pricing-dashboard-update",
            daemon=True,
        )
        thread.start()

    def _run_update(self) -> None:
        try:
            result = update_pipeline.run_bloomberg_update(paths=self.update_paths)
            response = dict(result) if isinstance(result, dict) else {}
            warnings = response.pop("warnings", ())
            response.setdefault("ok", True)
            response.setdefault("success", True)
            response.setdefault("message", "Bloomberg update complete. Reloading...")
            response["warning_count"] = len(warnings)
            response["config"] = self.update_paths.config.name
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self.update_state.finish(success=False, message=message, result=None)
            return

        self.update_state.finish(
            success=True,
            message=str(response["message"]),
            result=response,
        )


class IPv6LocalDashboardServer(LocalDashboardServer):
    address_family = socket.AF_INET6


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """Serve fixed dashboard assets and the two fixed update API routes."""

    server: LocalDashboardServer

    def end_headers(self) -> None:
        request_path = urlsplit(self.path).path
        cache_control = "no-store" if request_path.startswith("/api/") else "private, no-cache"
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        candidate = Path(super().translate_path(path)).resolve()
        try:
            candidate.relative_to(self.server.static_root)
        except ValueError:
            return str(self.server.static_root / ".not-found")
        return str(candidate)

    def list_directory(self, path: str) -> None:
        self.send_error(HTTPStatus.NOT_FOUND, "Directory listing is disabled")
        return None

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path == STATUS_PATH:
            self._send_json(
                HTTPStatus.OK,
                self.server.update_status(
                    available=bloomberg_client.is_bloomberg_available()
                ),
            )
            return
        if request_path.startswith("/api/"):
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "success": False, "error": "Unknown API endpoint."},
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path != UPDATE_PATH:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "success": False, "error": "Unknown API endpoint."},
            )
            return
        if not self._request_origin_is_local():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {
                    "ok": False,
                    "success": False,
                    "error": "Update requests are accepted only from this local dashboard.",
                },
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = -1
        if content_length != 0:
            self.close_connection = True
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "success": False,
                    "error": "The update endpoint does not accept a request body.",
                },
            )
            return

        available = bloomberg_client.is_bloomberg_available()
        if not available:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "success": False,
                    "update_api": True,
                    "available": False,
                    "updating": False,
                    "config": self.server.update_paths.config.name,
                    "message": _BLOOMBERG_MISSING_MESSAGE,
                    "error": _BLOOMBERG_MISSING_MESSAGE,
                },
            )
            return

        state = self.server.update_state
        if not state.try_begin():
            payload = self.server.update_status(available=True)
            payload.update(
                {
                    "ok": False,
                    "success": False,
                    "error": "A Bloomberg update is already in progress.",
                }
            )
            self._send_json(HTTPStatus.CONFLICT, payload)
            return

        self.server.start_update()
        response = self.server.update_status(available=True)
        response.update(
            {
                "ok": True,
                "success": True,
                "accepted": True,
                "update_api": True,
                "available": True,
                "updating": True,
            }
        )
        self._send_json(HTTPStatus.ACCEPTED, response)

    def _request_origin_is_local(self) -> bool:
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            return True
        parsed = urlsplit(source)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if not _is_loopback_host(parsed.hostname):
            return False
        expected_port = int(self.server.server_address[1])
        actual_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return actual_port == expected_port

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode(
            "utf-8"
        )
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    static_root: Path = STATIC_ROOT,
    update_state: UpdateState | None = None,
    update_paths: update_pipeline.UpdatePaths | None = None,
) -> LocalDashboardServer:
    """Create, but do not start, a validated loopback dashboard server."""

    loopback_host = _validate_host(host)
    port_number = int(port)
    if port_number < 0 or port_number > 65_535:
        raise ValueError("port must be between 0 and 65535")
    server_class = IPv6LocalDashboardServer if ":" in loopback_host else LocalDashboardServer
    return server_class(
        (loopback_host, port_number),
        static_root=static_root,
        update_state=update_state,
        update_paths=update_paths,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the local Pricing Dashboard owner UI and Bloomberg update API."
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Loopback host only (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Local HTTP port (default: {DEFAULT_PORT}; use 0 for an automatic port).",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open the dashboard in the default browser after the server starts.",
    )
    parser.add_argument(
        "--config",
        default=str(update_pipeline.DEFAULT_CONFIG),
        help="Live security-root workbook used by UPDATE DATA.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        update_paths = update_pipeline.UpdatePaths(
            config=Path(args.config).expanduser().resolve()
        )
        server = create_server(args.host, args.port, update_paths=update_paths)
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    bound_host, bound_port = server.server_address[:2]
    display_host = args.host if args.host.lower() == "localhost" else str(bound_host)
    url = f"http://{_browser_host(display_host)}:{bound_port}/"
    print(f"Pricing Dashboard owner server: {url}")
    print("Press Ctrl+C to stop.")
    if args.open_browser:
        browser_thread = threading.Thread(
            target=webbrowser.open,
            args=(url,),
            name="pricing-dashboard-browser",
            daemon=True,
        )
        browser_thread.start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Pricing Dashboard owner server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
