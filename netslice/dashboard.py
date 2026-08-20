"""dashboard.py - HTTP API and single-page UI served from the controller.

Open http://127.0.0.1:8080 with the controller running.

It runs **inside the controller process**, so it reads the live `NetworkState`
directly rather than through a copy that could go stale.

Three things forced the shape of this file:

* os-ken's published wheel omits `os_ken/app/wsgi` (the same stripping that
  removed `os_ken/cmd`), so its `ControllerBase`/`route` helpers do not exist
  here.
* **os-ken 4.2.1 runs on native OS threads, not greenlets** (`hub.HUB_TYPE ==
  "native"`: `hub.spawn` is a `threading.Thread` and `hub.listen` returns an
  ordinary blocking socket). So eventlet's WSGI server is the wrong tool, with
  no eventlet hub in the thread it accepts connections and then never answers
  them. stdlib `wsgiref` on a threading server is the right one, and it drops a
  dependency rather than adding one.
* The demo has to work with no network. So: a hand-written WSGI app of about
  a dozen routes, and one self-contained HTML file with no CDN, no framework
  and no build step.

Because the threads are real, dashboard requests genuinely run *concurrently*
with OpenFlow event handlers. Every controller method this module calls takes
the controller's lock, including the read-only ones, which would otherwise
iterate the flow table while a `PORT_STATUS` handler is rewriting it.

API:

    GET    /                    the page
    GET    /api/topology        the graph; fixed, fetched once
    GET    /api/state           flows + link utilisation + switch liveness
    GET    /api/events?since=N  event log tail
    POST   /api/flows           allocate; body is the request JSON
    DELETE /api/flows/<id>      release one flow
    POST   /api/clear           release everything
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Callable, List, Optional, Tuple
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

STATIC = Path(__file__).resolve().parent / "static"



FLOW_FIELDS = {
    "src": str,
    "dst": str,
    "bandwidth_mbps": float,
    "priority": int,
    "idle_timeout": int,
    "hard_timeout": int,
    "proto": str,
    "policy": str,
    "tie_break": str,
    "allow_preemption": bool,
    "admission_control": bool,
}


class Router:
    """Enough routing for a dozen endpoints, and no more."""

    def __init__(self):
        """Initialise an empty route table.

        Args:
            None.
        """
        self.routes: List[Tuple[str, re.Pattern, Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        """Register a new route.

        Args:
            method: HTTP method (e.g. ``"GET"``, ``"POST"``).
            pattern: Path regex (anchored by ``^`` and ``$``).
            handler: Callable ``handler(environ, start_response, *groups)``.
        """
        self.routes.append((method, re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str):
        """Match an HTTP request to a registered handler.

        Args:
            method: HTTP method.
            path: Request path.

        Returns:
            tuple[Optional[Callable], tuple]: ``(handler, capture_groups)``, or
            ``(None, ("405",))`` for a method mismatch, or ``(None, ("404",))``
            for no match.
        """
        allowed = False
        for route_method, pattern, handler in self.routes:
            found = pattern.match(path)
            if not found:
                continue
            if route_method != method:
                allowed = True
                continue
            return handler, found.groups()
        return (None, ("405",)) if allowed else (None, ("404",))


def _json_response(start_response, payload, status="200 OK"):
    """Serialise a payload as a JSON HTTP response.

    Args:
        start_response: The WSGI ``start_response`` callable.
        payload: Object to JSON-serialise.
        status: HTTP status line. Defaults to ``"200 OK"``.

    Returns:
        list[bytes]: The response body as a single JSON byte string.
    """
    body = json.dumps(payload, default=str).encode()
    start_response(status, [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),



        ("Cache-Control", "no-store"),
    ])
    return [body]


def _read_body(environ) -> dict:
    """Read and parse the JSON body of a request.

    Args:
        environ: The WSGI environment dict.

    Returns:
        dict: Parsed JSON body, or ``{}`` if empty/malformed.
    """
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return {}
    if length <= 0:
        return {}
    raw = environ["wsgi.input"].read(length)
    try:
        parsed = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_flow_request(body: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Coerce and validate a flow request before it reaches the controller.

    The controller expects the right types; a browser sends everything as
    strings, so this converts fields per the ``FLOW_FIELDS`` spec and rejects
    unknown fields.

    Args:
        body: Raw JSON body of the request.

    Returns:
        tuple[Optional[dict], Optional[str]]: ``(cleaned_request, error)``.
        On error, *request* is ``None`` and *error* is a message.
    """
    request = {}
    for key, value in body.items():
        if key not in FLOW_FIELDS:
            return None, f"unexpected field {key!r}"
        if value is None or value == "":
            continue
        try:
            request[key] = bool(value) if FLOW_FIELDS[key] is bool else FLOW_FIELDS[key](value)
        except (TypeError, ValueError):
            return None, f"{key}: expected {FLOW_FIELDS[key].__name__}, got {value!r}"

    missing = [k for k in ("src", "dst", "bandwidth_mbps") if k not in request]
    if missing:
        return None, f"missing required field(s): {', '.join(missing)}"
    return request, None


def make_app(controller):
    """Build the WSGI application around a live controller instance.

    Args:
        controller: The running ``NetSliceController``.

    Returns:
        Callable: A WSGI application.
    """
    router = Router()

    def page(environ, start_response, *_):
        """Serve the dashboard HTML page."""
        return _static(environ, start_response, "dashboard.html")

    def _static(environ, start_response, name):
        """Serve a static file, restricted to the static directory."""
        target = (STATIC / name).resolve()

        if not str(target).startswith(str(STATIC)) or not target.is_file():
            return _json_response(start_response, {"ok": False, "reason": "not found"},
                                  "404 Not Found")
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start_response("200 OK", [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    def static(environ, start_response, name):
        """Serve a static file by name."""
        return _static(environ, start_response, name)

    def topology(environ, start_response, *_):
        """GET /api/topology, return the static topology graph."""
        return _json_response(start_response, controller.topology_view())

    def state(environ, start_response, *_):
        """GET /api/state, return a full live snapshot."""
        return _json_response(start_response, controller.snapshot())

    def events(environ, start_response, *_):
        """GET /api/events, return buffered events since a sequence number."""
        query = environ.get("QUERY_STRING", "")
        since = 0
        for part in query.split("&"):
            if part.startswith("since="):
                try:
                    since = int(part[6:])
                except ValueError:
                    since = 0
        return _json_response(
            start_response, {"ok": True, "events": controller.events_since(since)}
        )

    def add_flow(environ, start_response, *_):
        """POST /api/flows, clean, validate, and admit a flow request."""
        request, error = _clean_flow_request(_read_body(environ))
        if error:
            return _json_response(start_response, {"ok": False, "reason": error},
                                  "400 Bad Request")
        reply = controller.request_flow(**request)


        return _json_response(start_response, reply)

    def remove_flow(environ, start_response, flow_id):
        """DELETE /api/flows/<id> — release one flow."""
        reply = controller.remove_flow(flow_id)
        return _json_response(start_response, reply,
                              "200 OK" if reply.get("ok") else "404 Not Found")

    def clear(environ, start_response, *_):
        """POST /api/clear — release every flow."""
        return _json_response(start_response, controller.clear_flows())

    router.add("GET", "/", page)
    router.add("GET", "/index.html", page)
    router.add("GET", "/static/([A-Za-z0-9._-]+)", static)
    router.add("GET", "/api/topology", topology)
    router.add("GET", "/api/state", state)
    router.add("GET", "/api/events", events)
    router.add("POST", "/api/flows", add_flow)
    router.add("DELETE", "/api/flows/([A-Za-z0-9_-]+)", remove_flow)
    router.add("POST", "/api/clear", clear)

    def application(environ, start_response):
        """WSGI entry point: route the request to a handler.
        """
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        handler, groups = router.match(method, path)
        if handler is None:
            status = "405 Method Not Allowed" if groups[0] == "405" else "404 Not Found"
            return _json_response(start_response, {"ok": False, "reason": status}, status)
        try:
            return handler(environ, start_response, *groups)
        except Exception as exc:
            controller.logger.exception("dashboard request failed: %s %s", method, path)
            return _json_response(
                start_response,
                {"ok": False, "reason": f"{type(exc).__name__}: {exc}"},
                "500 Internal Server Error",
            )

    return application


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per request, and none of them keeping the process alive.

    The browser polls once a second and holds connections open; without
    `daemon_threads` a Ctrl-C would wait on every one of them.
    """

    daemon_threads = True
    allow_reuse_address = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        """Suppress per-request access log lines.

        A line per poll, per second, forever would bury the OpenFlow events
        that actually matter during a demo.

        Args:
            *args: Ignored log-format arguments.
        """
        pass


def serve(controller, addr) -> None:
    """Serve the dashboard until the process is stopped.

    Blocks forever in its own daemon thread.

    Args:
        controller: The running ``NetSliceController``.
        addr: ``(host, port)`` tuple to bind.
    """
    server = make_server(
        addr[0], addr[1], make_app(controller),
        server_class=_ThreadingWSGIServer, handler_class=_QuietHandler,
    )
    controller.logger.info("dashboard on http://%s:%d", *addr)
    server.serve_forever()
