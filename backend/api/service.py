"""The loopback HTTP service the desktop client drives (issue #27).

    uv run python -m backend.api.service --database DB.sqlite3 [--host 127.0.0.1]
                                         [--port 7391] [--port-file PATH]
                                         [--dev-origin ORIGIN]...

One process serves one local library over HTTP/1.1 on the IPv4 loopback
interface, with JSON request and response bodies and nothing but the standard
library: `http.server.ThreadingHTTPServer` accepts the connections, `json`
writes the bodies and `os.write` writes every stdout and stderr line as one
unbuffered line. No web framework, no dependency, no outbound socket, no
credential and no audio byte.

The service is a thin transport over #21's repository, #22's scanner and #23's
queue: `backend.api.library` and `backend.api.imports` own the operations,
`backend.api.schemas` validates a request and `backend.api.errors` holds the
closed code table. `ROUTES` below is the one route table; #28, #30 and the
client panels read it instead of keeping their own.

Three properties a client depends on:

- The listen socket binds `ALLOWED_BIND_HOSTS`, and every request must carry a
  loopback `Host` and an allowed `Origin`, so a browser page cannot reach an
  operation even if it resolves its own name to 127.0.0.1.
- No response carries a path, a root or an audio byte. A stored path becomes
  its file name; the import root is echoed nowhere.
- Every line on stdout is JSON: the listening line, one access-log line per
  request, and #23's own per-item progress lines from the runner thread.

See `_docs/local-service.md` for the route table, the code tables, the numeric
bounds, the policies and the reproduction commands.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from backend.analysis.batch import BatchError, ROLES, canonical, local_path
from backend.api import imports, library, recommendations, schemas
from backend.api.errors import ApiError
from backend.api.schemas import RequestContext
from backend.library import indexer, queue
from backend.library.errors import InvalidDatabasePath, LibraryError
from backend.library.repository import LibraryRepository
from backend.library.schema import SCHEMA_VERSION, open_database, utc_now


SERVICE_NAME = "tera-local-service"
SERVICE_VERSION = "0.1.0"
API_SCHEMA_VERSION = "1.0"

DEFAULT_HOST = "127.0.0.1"
ALLOWED_BIND_HOSTS = ("127.0.0.1",)
DEFAULT_PORT = 7391
PORT_MIN = 1024
PORT_MAX = 65535
PORT_ENV = "TERA_SERVICE_PORT"

#: The origins the desktop shell uses. A browser page at any other origin is
#: refused before any database read; `--dev-origin` adds one development origin.
ALLOWED_ORIGINS = ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost")

MAX_CONCURRENT_REQUESTS = 8
REQUEST_QUEUE_TIMEOUT_SECONDS = 5
LISTEN_BACKLOG = 32
REQUEST_TIMEOUT_SECONDS = 30
SHUTDOWN_GRACE_SECONDS = 5

#: The largest request body: refused from its Content-Length alone, unread.
MAX_REQUEST_BYTES = 65536

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_STOPPED = 130

#: The closed set of configuration codes. They are exit-2 stderr codes, not HTTP
#: responses, so they live beside the command and not in `errors.ERROR_CODES`.
CONFIG_CODES = ("invalid_bind_host", "invalid_port", "port_out_of_range",
                "database_not_found", "invalid_port_file", "invalid_dev_origin",
                "port_in_use")

#: How long one accept poll waits before the loop checks the stop flag again.
POLL_SECONDS = 0.25

_METHOD_PATTERN = re.compile(r"[A-Za-z]{1,16}\Z")
_DEV_ORIGIN_PATTERN = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d{1,5})\Z")
_PRINT_LOCK = threading.Lock()


class ServiceConfigError(Exception):
    """One refused configuration value, with the exit-2 code `main` reports."""

    def __init__(self, code: str, *, host=None, port=None):
        if code not in CONFIG_CODES:
            raise ValueError(f"Unknown configuration code: {code!r}")
        self.code = code
        self.host = host
        self.port = port
        super().__init__(code)


def _print_line(document, stream=None) -> None:
    """One JSON line, written with one `os.write` so threads cannot interleave.

    `print` buffers, so two threads writing short lines can merge into one
    buffer flush. Every line this service writes is a whole JSON document and
    reaches the descriptor in a single write call instead.
    """

    data = (canonical(document) + "\n").encode("utf-8")
    descriptor = 2 if stream is sys.stderr else 1
    with _PRINT_LOCK:
        os.write(descriptor, data)


# ---------------------------------------------------------------------------
# the handler-side database and its connection
# ---------------------------------------------------------------------------


class _Database:
    """The connection every request handler reads and writes through.

    A SQLite connection belongs to the thread that opened it, so the service
    owns exactly one handler-side connection and runs every statement on the one
    thread that opened it. One thread is enough for a local single-operator
    service: the runner thread holds its own connection and its own write lock,
    and #21 configures WAL, so a reader is never blocked by the import that is
    writing. Opening the connection once, instead of once per request, is what
    keeps a page's cost at the statements it actually runs.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.code = None
        self.schema_version = None
        self.journal_mode = None
        self.connection = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tera-database")

    def start(self) -> None:
        """Open the connection on its own thread and record the database's state.

        A path that cannot be a database file is a configuration failure and
        raises `ServiceConfigError`, so the process exits 2 having written
        nothing. A file that exists but is not a database, a corrupt database or
        one whose stored schema is newer is the database's own state: the
        service starts degraded, reports #21's code and keeps serving `/health`.
        """

        try:
            self._executor.submit(self._open).result()
        except InvalidDatabasePath:
            raise ServiceConfigError("database_not_found") from None
        except sqlite3.Error:
            raise ServiceConfigError("database_not_found") from None
        except LibraryError as error:
            self.code = error.code

    def run(self, function):
        """Run `function(connection)` on the database thread and return its value."""

        if self.code is not None:
            raise ApiError("database_unavailable")
        return self._executor.submit(function, self.connection).result()

    def close(self) -> None:
        """Close the connection on its own thread and stop the thread."""

        try:
            self._executor.submit(self._close).result()
        except Exception:  # closing twice, or a thread that never started
            pass
        self._executor.shutdown(wait=False)

    def _open(self) -> None:
        connection = open_database(self.path)
        self.connection = connection
        self.schema_version = SCHEMA_VERSION
        self.journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    def _close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class _Concurrency:
    """How many requests are handled at once, and who waits for a slot.

    A request that cannot get a slot inside `timeout` is answered 503
    `server_busy` instead of queueing behind the burst. `in_flight` is what the
    shutdown sequence waits for and what a test reads to see a slot released.
    """

    def __init__(self, size: int, timeout: float):
        self.size = size
        self.timeout = timeout
        self._slots = threading.Semaphore(size)
        self._lock = threading.Lock()
        self._in_flight = 0

    def acquire(self) -> bool:
        if not self._slots.acquire(timeout=self.timeout):
            return False
        with self._lock:
            self._in_flight += 1
        return True

    def release(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
        self._slots.release()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


# ---------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------


class ServiceApp:
    """One service process: the database, the run registry and the bounds.

    The transport asks this object for everything. A handler reaches the
    database through `database.run` and the runs this process drives through
    `runs`; the runner thread is started by `spawn_runner` and joined by
    `stop`.
    """

    def __init__(self, path, *, host: str, port: int, dev_origins=()):
        self.path = Path(path)
        self.host = host
        self.port = port
        self.api_schema = API_SCHEMA_VERSION
        self.started_at = utc_now()
        self.allowed_origins = tuple(ALLOWED_ORIGINS) + tuple(dev_origins)
        self.request_timeout_seconds = REQUEST_TIMEOUT_SECONDS
        self.concurrency = _Concurrency(MAX_CONCURRENT_REQUESTS, REQUEST_QUEUE_TIMEOUT_SECONDS)
        self.runs = imports.RunRegistry()
        self.database = _Database(self.path)
        self._runners = {}
        self._runner_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.database.start()

    def spawn_runner(self, run_id: str, *, root=None, role=None) -> threading.Thread:
        """Start the thread that scans and drains one open run.

        The thread is the service's own: the request that opened the run returns
        as soon as this call returns, so no handler ever scans, decodes or
        extracts.
        """

        thread = threading.Thread(target=imports.runner, args=(self, run_id),
                                  kwargs={"root": root, "role": role},
                                  name=f"tera-import-{run_id}", daemon=True)
        with self._runner_lock:
            self._runners[run_id] = thread
        thread.start()
        return thread

    def runner_finished(self, run_id: str) -> None:
        with self._runner_lock:
            self._runners.pop(run_id, None)

    def stop(self, timeout: float) -> None:
        """Cancel a live run and wait up to `timeout` for the runner threads.

        The cancellation flag is #23's, so a run this process is driving stops at
        its next item boundary with everything committed still readable. The
        wait is bounded: the process leaves even if an extraction is longer than
        the grace, and the run's lease is what makes it recoverable.
        """

        if self.database.code is None:
            try:
                self.database.run(queue.request_cancel)
            except (ApiError, LibraryError, sqlite3.Error, queue.QueueError):
                pass
        deadline = time.monotonic() + timeout
        with self._runner_lock:
            threads = list(self._runners.values())
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def close(self) -> None:
        self.database.close()

    # -- what the routes read ----------------------------------------------

    def phase_of(self, run_id: str, state) -> str:
        """What a run is doing: the scan, the drain, or its #23 state.

        A run this process drives reports `scanning` while #22 reconciles the
        folder and `analyzing` while #23 drains. A run another process drives
        has no observable phase, so it is reported with its own state
        (`running`) rather than guessed at, and a run that reached a terminal
        state always reports that state.
        """

        if state != queue.RUN_RUNNING:
            return state
        record = self.runs.get(run_id)
        return state if record is None else record.phase

    def health(self) -> dict:
        """The whole status document; the one route that answers when degraded."""

        degraded = self.database.code is not None
        document = {
            "service": SERVICE_NAME,
            "api_schema": API_SCHEMA_VERSION,
            "service_version": SERVICE_VERSION,
            "state": "degraded" if degraded else "ok",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "database": {"state": "degraded" if degraded else "ok",
                         "schema_version": self.database.schema_version,
                         "journal_mode": self.database.journal_mode,
                         "code": self.database.code},
            "library": {"samples": None, "by_role": None, "roots": None,
                        "pending_analysis": None},
            "import": {"run_id": None, "state": None, "phase": None, "started_at": None},
            "roles": list(ROLES),
            "analysis_version": indexer.current_analysis_version(),
            "limits": {"max_page_size": schemas.PAGE_SIZE_MAX,
                       "max_concurrent_requests": MAX_CONCURRENT_REQUESTS,
                       "max_request_bytes": MAX_REQUEST_BYTES},
        }
        if degraded:
            # No run row and no count can be read, so neither is invented.
            return document
        counts, pending, live = self.database.run(_health_reads)
        document["library"] = {"samples": counts["samples"], "by_role": counts["by_role"],
                               "roots": counts["roots"], "pending_analysis": pending}
        if live is None:
            document["import"] = {"run_id": None, "state": "idle", "phase": None,
                                  "started_at": None}
        else:
            document["import"] = {"run_id": live["run_id"], "state": live["state"],
                                  "phase": self.phase_of(live["run_id"], live["state"]),
                                  "started_at": live["started_at"]}
        return document

    def log_request(self, method: str, route: str, status: int, code, started: float) -> None:
        """One access-log line per request, with no query text, id or path."""

        _print_line({"event": "request", "method": method, "route": route, "status": status,
                     "code": code,
                     "duration_ms": int(round((time.monotonic() - started) * 1000))})


def _health_reads(connection):
    """The three reads `/health` needs, on the database thread."""

    counts = LibraryRepository(connection).sample_counts()
    pending = len(indexer.pending_analysis(connection))
    return counts, pending, queue.live_run(connection)


# ---------------------------------------------------------------------------
# the route table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """One operation: the method it answers, its path template and its handler.

    `body` is True for the one operation that has a request body. Every other
    operation still has to carry the media type and the length of a request (so a
    cross-site form post cannot reach it), but a body it does not define is
    refused as an unknown field rather than read as data.
    """

    method: str
    path: str
    handler: object
    body: bool = False


def health(context: RequestContext) -> schemas.Response:
    """`GET /health`: the whole status document, degraded or not."""

    return schemas.Response(200, context.app.health())


#: The one route table. Its order is the order of the `Allow` header a 405
#: carries, and nothing else in the service matches a method and a path.
ROUTES = (
    Route("GET", "/health", health),
    Route("POST", "/imports", imports.start, body=True),
    Route("GET", "/imports/{run_id}", imports.status),
    Route("POST", "/imports/{run_id}/cancel", imports.cancel),
    Route("POST", "/imports/{run_id}/retry", imports.retry),
    Route("GET", "/library/samples", library.list_samples),
    Route("GET", "/library/samples/{sample_id}", library.sample),
    Route("POST", "/recommendations", recommendations.handle_recommendation, body=True),
)

_PREFLIGHT_HEADERS = (("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                      ("Access-Control-Allow-Headers", "Content-Type"),
                      ("Access-Control-Max-Age", "600"))


def _params(template: str, path: str):
    """The template's parameters for a request path, or None when it differs.

    A path is matched literally: no percent-decoding, and a template segment in
    braces matches exactly one non-empty segment. An unknown path therefore
    stays unknown however it is spelled.
    """

    if template == path:
        return {}
    expected, actual = template.split("/"), path.split("/")
    if len(expected) != len(actual):
        return None
    params = {}
    for piece, value in zip(expected, actual):
        if piece.startswith("{") and piece.endswith("}"):
            if not value:
                return None
            params[piece[1:-1]] = value
        elif piece != value:
            return None
    return params


def _match_path(path: str):
    """Every route whose path matches, and the parameters of that path."""

    matches, params = [], None
    for route in ROUTES:
        candidate = _params(route.path, path)
        if candidate is None:
            continue
        if params is None:
            params = candidate
        matches.append(route)
    return None if not matches else (matches, params)


def _template_of(path: str):
    match = _match_path(path)
    return None if match is None else match[0][0].path


def _require_empty(document):
    """`None` for an operation with no request body, or the refusal it earns.

    An empty body and `{}` are both "no fields", which is what an operation
    without a request body accepts; anything else names a field it does not have.
    """

    if document is None:
        return None
    if type(document) is not dict:
        raise ApiError("invalid_body")
    if document:
        raise ApiError("unknown_field", details={"field": sorted(document)[0]})
    return None


def _host_allowed(value, port: int) -> bool:
    """True for `127.0.0.1` or `localhost`, with the bound port or without it.

    A `Host` header the service did not bind is refused, so a name that
    resolves to the loopback address from a browser page cannot reach an
    operation (DNS rebinding).
    """

    if not isinstance(value, str):
        return False
    candidate = value.strip().lower()
    if candidate in ("127.0.0.1", "localhost"):
        return True
    return candidate in (f"127.0.0.1:{port}", f"localhost:{port}")


# ---------------------------------------------------------------------------
# the HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """One connection: policy first, then the one route table, then the log.

    The request is refused before any database read when its `Host` or
    `Origin` is not allowed, and a non-GET request must carry
    `Content-Type: application/json` and a valid integer `Content-Length`
    before a field is parsed, so a cross-site form post cannot reach an
    operation.
    """

    protocol_version = "HTTP/1.1"
    server_version = f"{SERVICE_NAME}/{SERVICE_VERSION}"
    sys_version = ""

    def __getattr__(self, name):
        # Every method is dispatched, so a method this service does not implement
        # is a 405 with an Allow header rather than the standard library's 501.
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def log_message(self, format, *args):  # noqa: A002 - the signature is stdlib's
        """Silence the standard library's stderr line; this service logs JSON."""

    # -- connection lifecycle ----------------------------------------------

    def setup(self) -> None:
        super().setup()
        self._slotted = False
        # True once the declared body has been read, or its framing refused.
        self._body_settled = False
        self.connection.settimeout(self.server.app.request_timeout_seconds)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            self._release_slot()

    def _release_slot(self) -> None:
        if self._slotted:
            self._slotted = False
            self.server.app.concurrency.release()

    # -- the one entry point -----------------------------------------------

    def _dispatch(self) -> None:
        app = self.server.app
        started = time.monotonic()
        method = self.command
        logged_method = method if _METHOD_PATTERN.fullmatch(method) else "<unknown>"
        route_template = "<unknown>"
        status, code, document, headers, allow, origin = (
            500, "internal_error", None, [], None, None)
        try:
            target = urlsplit(self.path)
            if not _host_allowed(self.headers.get("Host"), app.port):
                raise ApiError("host_not_allowed")
            origin = self._allowed_origin(app)
            if method == "OPTIONS":
                route_template = _template_of(target.path) or "<unknown>"
                if origin is None:
                    raise ApiError("origin_not_allowed")
                status, code, document, headers = 204, None, {}, list(_PREFLIGHT_HEADERS)
            else:
                match = _match_path(target.path)
                if match is None:
                    raise ApiError("unknown_route")
                allowed, params = match
                # The path matched, so the log names its template even when the
                # method is the part that is refused.
                route_template = f"{logged_method} {allowed[0].path}"
                route = None
                for candidate in allowed:
                    if candidate.method == method:
                        route = candidate
                        break
                if route is None:
                    allow = ", ".join(candidate.method for candidate in allowed)
                    raise ApiError("method_not_allowed")
                if not app.concurrency.acquire():
                    raise ApiError("server_busy")
                self._slotted = True
                if route.handler is not health and app.database.code is not None:
                    raise ApiError("database_unavailable")
                body = self._document()
                if not route.body:
                    body = _require_empty(body)
                response = route.handler(RequestContext(
                    app=app, method=method, template=route.path, params=params,
                    query=tuple(parse_qsl(target.query, keep_blank_values=True)),
                    body=body))
                status, code, document, headers = (
                    response.status, None, response.body, list(response.headers))
        except ApiError as error:
            status, code = error.status, error.code
            document, headers = error.document(app.api_schema), []
        except (TimeoutError, ConnectionError) as error:
            # The client stopped mid-request: the socket timeout expired or
            # the connection was reset. There is no channel left to answer
            # on, so the connection is closed (the slot is released by
            # `finish`) and one path-free line records the type.
            self._abandoned(error)
            return
        except Exception as error:  # never leaks a message or a traceback
            _print_line({"event": "internal_error", "error": type(error).__name__},
                        stream=sys.stderr)
            status, code = 500, "internal_error"
            document, headers = ApiError("internal_error").document(app.api_schema), []
        try:
            # Whatever the request was refused for, the connection is left at
            # the start of the next request before the answer is written.
            self._reframe_body()
            self._respond(status, headers, document, allow=allow, origin=origin, code=code)
        except (TimeoutError, ConnectionError) as error:
            # The client left while the refused body was being drained or
            # the answer was being written.
            self._abandoned(error)
        finally:
            app.log_request(logged_method, route_template, status, code, started)

    def _allowed_origin(self, app):
        """The request's one allowed origin, or None when it carries none."""

        value = self.headers.get("Origin")
        if value is None:
            return None
        candidate = value.strip()
        if candidate in app.allowed_origins:
            return candidate
        raise ApiError("origin_not_allowed")

    def _document(self):
        """The parsed JSON body of one non-GET request.

        The media type and the length are checked before a byte is read, and a
        body larger than `MAX_REQUEST_BYTES` is refused from its header alone:
        the service never reads a body it will not use. A body it refuses
        before reading is still taken off the connection by `_reframe_body`
        before the answer, or the connection is closed when its framing
        cannot be trusted.
        """

        if self.command in ("GET", "HEAD", "OPTIONS"):
            # Nothing is read here; `_reframe_body` settles the connection.
            return None
        media_type = self.headers.get("Content-Type", "")
        if media_type.split(";")[0].strip().lower() != "application/json":
            raise ApiError("unsupported_media_type")
        declared = self.headers.get("Content-Length")
        if declared is None or not declared.strip().isdigit():
            # Without a length the body's framing is unknown, so the connection
            # is closed rather than reused.
            self._body_settled = True
            self.close_connection = True
            raise ApiError("length_required")
        length = int(declared, 10)
        if length > MAX_REQUEST_BYTES:
            # Refused from the header alone, so there is nothing to reframe.
            self._body_settled = True
            self.close_connection = True
            raise ApiError("request_too_large")
        raw = self.rfile.read(length)
        self._body_settled = True
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("invalid_json") from None

    def _reframe_body(self) -> None:
        """Leave the connection at the start of the next request, or close it.

        A request refused before its body was read (415, and every policy
        refusal) leaves the declared bytes on a kept-alive connection. The
        standard library would read them as the next request line and answer
        with its own HTML 400, so the body is drained before this service's
        envelope is written. A body whose framing cannot be trusted — no
        valid integer `Content-Length`, a `Transfer-Encoding` this service
        does not read, or more than `MAX_REQUEST_BYTES` — is not drained and
        the connection is closed instead, which is what 411 and 413 already
        ask for from their own headers.
        """

        if self._body_settled:
            return
        self._body_settled = True
        declared = self.headers.get("Content-Length")
        if declared is None:
            # Nothing was declared: a GET-shaped request has no body to take
            # off the connection, and a chunked request has a framing this
            # service does not read.
            if self.headers.get("Transfer-Encoding") is not None:
                self.close_connection = True
            return
        if not declared.strip().isdigit():
            self.close_connection = True
            return
        length = int(declared, 10)
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            return
        self.rfile.read(length)

    def _abandoned(self, error) -> None:
        """One path-free line for a request the client stopped mid-flight."""

        self.close_connection = True
        _print_line({"event": "request_aborted", "error": type(error).__name__},
                    stream=sys.stderr)

    def _respond(self, status, headers, document, *, allow=None, origin=None, code=None) -> None:
        payload = b"" if status == 204 else canonical(document).encode("utf-8")
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if allow is not None:
            self.send_header("Allow", allow)
        if code == "server_busy":
            self.send_header("Retry-After", "1")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if payload and self.command != "HEAD":
            self.wfile.write(payload)


class _Service(ThreadingHTTPServer):
    """The listen socket, its bound port and the application behind it."""

    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second socket steal an address that is
    # already listening, which would make "the address is already in use" a
    # success. The descriptor is bound exclusively there instead.
    allow_reuse_address = os.name != "nt"
    request_queue_size = LISTEN_BACKLOG
    timeout = POLL_SECONDS

    def server_bind(self) -> None:
        # The standard library's server_bind resolves its own name with a
        # reverse lookup; the bound host is what this service reports instead.
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]

    def handle_error(self, request, client_address) -> None:
        """One path-free line for a connection that failed outside a request.

        The standard library prints the whole traceback here, which carries
        absolute interpreter paths into the service's stderr. A defect inside
        a request is reported by the handler as `internal_error`; what reaches
        this method is a socket, so the exception's type is the whole report.
        """

        error = sys.exc_info()[1]
        _print_line({"event": "connection_aborted",
                     "error": type(error).__name__ if error is not None else "unknown"},
                    stream=sys.stderr)

    def serve_until(self, stop: threading.Event) -> None:
        """Accept and answer requests until `stop` is set."""

        while not stop.is_set():
            self.handle_request()


# ---------------------------------------------------------------------------
# creating the service
# ---------------------------------------------------------------------------


def _checked_port(port):
    if isinstance(port, bool) or not isinstance(port, int):
        raise ServiceConfigError("invalid_port")
    if port != 0 and not PORT_MIN <= port <= PORT_MAX:
        raise ServiceConfigError("port_out_of_range", port=port)
    return port


def _database_target(database) -> Path:
    """The configured database path, refused unless it can be a database file.

    Configuration means the path itself: a UNC path, a mapped network drive or a
    linked ancestor (the rules of #9's `local_path`), a missing parent directory
    and a directory are refused before anything is opened, so a mistyped
    `--database` never creates a file. What is inside the file — a non-SQLite
    file, a corrupt database, a newer schema — is the database's own state and
    starts the service degraded instead of refusing to run.
    """

    try:
        path = local_path(database)
    except (BatchError, OSError, TypeError, ValueError) as error:
        raise ServiceConfigError("database_not_found") from error
    if path.is_dir() or not path.parent.is_dir():
        raise ServiceConfigError("database_not_found")
    return path


def _recommendation_collaborators():
    """The recommendation collaborators built from the process environment.

    Called once at startup, so a service that never receives a recommendation
    request still builds one object and reads nothing else.
    """

    return recommendations.RecommendationCollaborators()


def create_server(database, *, host=DEFAULT_HOST, port=DEFAULT_PORT, dev_origins=(),
                  recommendations=None):
    """Bind the listen socket, open the database and return a ready server.

    The socket is bound before the database is touched, so an address that is
    already in use changes no file and opens no database. The returned server has
    not accepted a request yet: the caller runs `serve_until(stop)` or the
    standard library's `serve_forever()`. `server_address[1]` is the bound
    port, which is the ephemeral port when `port` is 0.

    `recommendations` is the one injectable seam of the recommendation route:
    None (the default) builds #28's collaborators from the process environment at
    startup, and a caller may supply its own to script a transport, a clock or
    #26's cache without touching this module.
    """

    if host not in ALLOWED_BIND_HOSTS:
        raise ServiceConfigError("invalid_bind_host", host=host)
    port = _checked_port(port)
    origins = tuple(_dev_origin(value) for value in dev_origins)
    path = _database_target(database)
    try:
        server = _Service((host, port), _Handler)
    except OSError as error:
        raise ServiceConfigError("port_in_use", host=host, port=port) from error
    app = ServiceApp(path, host=host, port=server.server_address[1], dev_origins=origins)
    try:
        app.start()
    except BaseException:
        server.server_close()
        raise
    app.recommendations = (recommendations if recommendations is not None
                           else _recommendation_collaborators())
    server.app = app
    return server


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Configuration:
    """One validated command line."""

    database: str
    host: str
    port: int
    port_file: Path | None
    dev_origins: tuple


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.api.service",
        description="Serve the local sample library over HTTP on the loopback interface.")
    parser.add_argument("--database", required=True,
                        help="the local library database (created by #21's migrations)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"the loopback address to bind ({', '.join(ALLOWED_BIND_HOSTS)})")
    parser.add_argument("--port",
                        help=f"the TCP port, {PORT_MIN} to {PORT_MAX}, or 0 for an "
                             f"ephemeral one (default {DEFAULT_PORT}, then the "
                             f"{PORT_ENV} environment variable)")
    parser.add_argument("--port-file",
                        help="a local file to write the listening object to, atomically")
    parser.add_argument("--dev-origin", action="append", default=[], metavar="ORIGIN",
                        help="one extra allowed development origin, "
                             "http://localhost:PORT or http://127.0.0.1:PORT")
    return parser


def _configured_port(raw, environ, host):
    """`--port`, then `TERA_SERVICE_PORT`, then `DEFAULT_PORT`.

    Zero is the one value outside `[PORT_MIN, PORT_MAX]` that is accepted: it
    asks the operating system for an ephemeral port, which the listening line and
    the port file then report.
    """

    if raw is None:
        raw = environ.get(PORT_ENV)
    if raw is None or raw == "":
        return DEFAULT_PORT
    try:
        number = int(raw, 10)
    except (TypeError, ValueError):
        raise ServiceConfigError("invalid_port", host=host) from None
    if number != 0 and not PORT_MIN <= number <= PORT_MAX:
        raise ServiceConfigError("port_out_of_range", host=host, port=number)
    return number


def _dev_origin(value) -> str:
    match = _DEV_ORIGIN_PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None or not 1 <= int(match.group(1)) <= PORT_MAX:
        raise ServiceConfigError("invalid_dev_origin")
    return value


def _port_file_target(value, host, port):
    if value is None:
        return None
    try:
        path = local_path(value)
    except (BatchError, OSError, TypeError, ValueError) as error:
        raise ServiceConfigError("invalid_port_file", host=host, port=port) from error
    if path.is_dir() or not path.parent.is_dir():
        raise ServiceConfigError("invalid_port_file", host=host, port=port)
    return path


def _configuration(arguments, environ) -> Configuration:
    host = arguments.host
    if host not in ALLOWED_BIND_HOSTS:
        raise ServiceConfigError("invalid_bind_host", host=host)
    port = _configured_port(arguments.port, environ, host)
    origins = tuple(_dev_origin(value) for value in arguments.dev_origin)
    port_file = _port_file_target(arguments.port_file, host, port)
    return Configuration(database=arguments.database, host=host, port=port,
                         port_file=port_file, dev_origins=origins)


def _write_port_file(destination: Path, document) -> None:
    """Replace the port file atomically with the listening object."""

    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".",
                                                 suffix=".tmp", dir=destination.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(document) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv=None) -> int:
    """Run the service until a stop signal; return the process exit code."""

    arguments = _parser().parse_args(argv)
    try:
        configuration = _configuration(arguments, os.environ)
        server = create_server(configuration.database, host=configuration.host,
                               port=configuration.port,
                               dev_origins=configuration.dev_origins)
    except ServiceConfigError as error:
        _report(error)
        return EXIT_INVALID
    app = server.app
    listening = {"event": "listening", "host": app.host, "port": app.port,
                 "pid": os.getpid(), "api_schema": API_SCHEMA_VERSION}
    if configuration.port_file is not None:
        try:
            _write_port_file(configuration.port_file, listening)
        except OSError:
            _report(ServiceConfigError("invalid_port_file", host=app.host, port=app.port))
            server.server_close()
            app.stop(SHUTDOWN_GRACE_SECONDS)
            app.close()
            return EXIT_INVALID
    _print_line(listening)
    stop = threading.Event()
    received = []

    def _handle(signum, frame):
        received.append(signum)
        if len(received) > 1:
            # A second stop signal during shutdown leaves at once.
            os._exit(EXIT_STOPPED)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _handle)
    try:
        server.serve_until(stop)
    except KeyboardInterrupt:
        stop.set()
    server.server_close()
    deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    while app.concurrency.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    app.stop(max(0.0, deadline - time.monotonic()))
    app.close()
    if configuration.port_file is not None:
        try:
            configuration.port_file.unlink()
        except OSError:
            pass
    return EXIT_OK


def _report(error: ServiceConfigError) -> None:
    """Exactly one JSON line on stderr for a refused configuration or address."""

    _print_line({"event": "bind_failed" if error.code == "port_in_use"
                 else "configuration_error",
                 "code": error.code, "host": error.host, "port": error.port},
                stream=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
