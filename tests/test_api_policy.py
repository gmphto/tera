"""The access, output and availability policy of the local service (issue #27).

What this file proves, one property per test: the package reaches nothing but
#21/#22/#23 and hands no SQL of its own to the database; no response, header or
stdout line carries a path, a drive letter or an audio byte; no handler opens an
outbound socket; the listen socket is loopback only; a refused `Host` or
`Origin` is refused before a database read; a burst of concurrent requests is
bounded and never answers 500; and a connection that stops mid-request is closed
at the socket timeout with its slot released.

Every root, name and id here is synthetic.
"""

from __future__ import annotations

import ast
from contextlib import closing
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from backend.api import errors, schemas, service
from backend.api.errors import ApiError
from tests.api_client import build_library, connection, start_service, tone


PACKAGE = Path(__file__).resolve().parents[1] / "backend" / "api"

# The tables this package must never name in a statement of its own.
LIBRARY_TABLES = ("samples", "sample_features", "sample_keys", "sample_tags", "sample_packs",
                  "analysis_versions", "job_items", "job_runs")
SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", " FROM ")


def _module_sources():
    return sorted(PACKAGE.glob("*.py"))


def _code_literals(tree):
    """Every string constant that is not a docstring."""

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_the_package_reaches_nothing_outside_the_library_modules():
    for path in _module_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("backend.intelligence"), (path, name)
                assert not name.startswith("backend.evaluation"), (path, name)
                assert name not in ("subprocess", "urllib.request", "http.client",
                                    "requests"), (path, name)
        assert "TERA_JEV" not in path.read_text(encoding="utf-8")


def test_no_module_hands_the_database_a_statement_of_its_own():
    for path in _module_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for literal in _code_literals(tree):
            upper = literal.upper()
            if any(table.upper() in upper for table in LIBRARY_TABLES):
                assert not any(verb in upper for verb in SQL_VERBS), (path, literal)


def _wait_for_terminal(client, run_id, timeout=120.0):
    """Poll one run's status until it is terminal; return the status document."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document = client.get(f"/imports/{run_id}").body["import"]
        if document["phase"] not in ("scanning", "analyzing", "running"):
            return document
        time.sleep(0.02)
    raise AssertionError("the import did not finish")


def test_no_response_header_or_stdout_line_carries_a_path(tmp_path, capfd):
    token = "synth-library-7f3a91"
    root = build_library(tmp_path, {token: {token + "-kick-one.wav": tone(55)}})
    database = tmp_path / "library.sqlite3"
    running = start_service(str(database))
    try:
        reply = running.client.post("/imports", {"root": str(root / token), "role": "kick"})
        assert reply.status == 202
        run_id = reply.body["import"]["run_id"]
        _wait_for_terminal(running.client, run_id)
        status = running.client.get(f"/imports/{run_id}")
        samples = running.client.get("/library/samples")
        assert samples.body["page"]["count"] == 1
        sample_id = samples.body["items"][0]["sample_id"]
        detail = running.client.get(f"/library/samples/{sample_id}")
        failed = running.client.get("/library/samples?limit=1.5")
    finally:
        running.stop()
    captured = capfd.readouterr()
    bodies = [reply, status, samples, detail, failed]
    headers = [f"{name}: {value}" for reply in bodies for name, value in reply.headers.items()]
    raw = [reply.raw for reply in bodies]
    drive = os.path.splitdrive(str(tmp_path))[0]
    everything = ([reply.raw.decode("utf-8") for reply in bodies] + headers
                  + [captured.out, captured.err])
    for text in everything:
        assert str(root) not in text
        assert str(tmp_path) not in text
        assert "\\" not in text
        assert drive not in text
    assert "\\" not in b"".join(raw).decode("utf-8")
    # The two things a client may see do appear: the file name and the label.
    assert token + "-kick-one.wav" in samples.body["items"][0]["file_name"]
    assert status.body["import"]["root_label"] == token
    assert token + "-kick-one.wav" in detail.body["sample"]["file_name"]


def test_the_api_opens_no_outbound_socket(tmp_path, monkeypatch):
    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55)}})
    database = tmp_path / "library.sqlite3"
    running = start_service(str(database))
    attempts = []
    try:
        reply = running.client.post("/imports", {"root": str(root), "role": "kick"})
        run_id = reply.body["import"]["run_id"]
        _wait_for_terminal(running.client, run_id)
        sample_id = running.client.get("/library/samples").body["items"][0]["sample_id"]
        # The service is already listening: the patch only forbids outgoing ones.
        monkeypatch.setattr(socket, "create_connection",
                            lambda *a, **k: attempts.append("create_connection"))
        monkeypatch.setattr(socket.socket, "connect",
                            lambda *a, **k: attempts.append("connect"))
        outcomes = [_in_process(running.app, template, params=params, body=body)
                    for template, params, body in (
                        ("/health", {}, None),
                        ("/library/samples", {}, None),
                        ("/library/samples/{sample_id}", {"sample_id": sample_id}, None),
                        ("/imports/{run_id}", {"run_id": run_id}, None),
                        ("/imports/{run_id}/cancel", {"run_id": run_id}, None),
                        ("/imports/{run_id}/retry", {"run_id": run_id}, None),
                        ("/imports", {}, {"root": str(root), "role": "kick"}),
                    )]
    finally:
        running.stop()
    assert attempts == []
    for outcome in outcomes:
        assert outcome.status in (200, 202) or 400 <= outcome.status < 500
        assert outcome.status != 500


def _in_process(app, template, *, params=None, query=(), body=None):
    """Call one route's handler directly, exactly as the transport would."""

    route = next(route for route in service.ROUTES if route.path == template)
    try:
        return route.handler(schemas.RequestContext(
            app=app, method=route.method, template=route.path, params=dict(params or {}),
            query=tuple(query), body=body))
    except ApiError as error:
        return schemas.Response(error.status, error.document(app.api_schema))


def test_the_listen_socket_is_loopback_only(tmp_path, capfd):
    database = str(tmp_path / "library.sqlite3")
    assert service.ALLOWED_BIND_HOSTS == ("127.0.0.1",)
    for host in ("0.0.0.0", "::", "localhost", "example.invalid", "127.0.0.2"):
        with pytest.raises(service.ServiceConfigError) as caught:
            service.create_server(database, host=host, port=0)
        assert caught.value.code == "invalid_bind_host"
    assert not Path(database).exists()
    assert service.main(["--database", database, "--host", "0.0.0.0"]) == 2
    assert '"code":"invalid_bind_host"' in capfd.readouterr().err
    assert not Path(database).exists()


def test_a_case_refused_by_policy_is_refused_before_any_database_read(tmp_path):
    # The database is not a database: every route that reads it answers 503.
    (tmp_path / "library.sqlite3").write_bytes(b"not a SQLite database at all")
    running = start_service(str(tmp_path / "library.sqlite3"))
    try:
        assert running.app.health()["state"] == "degraded"
        assert running.app.health()["database"]["code"] == "not_a_database"
        assert running.client.get("/health").status == 200
        assert running.client.get("/library/samples").code() == "database_unavailable"
        bad_host = running.client.get("/library/samples", host="example.invalid")
        assert (bad_host.status, bad_host.code()) == (403, "host_not_allowed")
        bad_origin = running.client.get("/library/samples",
                                        headers={"Origin": "http://example.invalid"})
        assert (bad_origin.status, bad_origin.code()) == (403, "origin_not_allowed")
    finally:
        running.stop()


def test_no_response_sets_a_credential_or_a_permissive_cors_header(tmp_path):
    running = start_service(str(tmp_path / "library.sqlite3"))
    try:
        for reply in (running.client.get("/health"),
                      running.client.get("/health", headers={"Origin": "tauri://localhost"}),
                      running.client.get("/library/audio")):
            assert "set-cookie" not in reply.headers
            assert "access-control-allow-credentials" not in reply.headers
            assert reply.headers.get("access-control-allow-origin") != "*"
        allowed = running.client.get("/health", headers={"Origin": "tauri://localhost"})
        assert allowed.headers["access-control-allow-origin"] == "tauri://localhost"
        assert allowed.headers["vary"] == "Origin"
    finally:
        running.stop()


def test_the_published_bounds_are_the_documented_ones():
    assert (service.SERVICE_NAME, service.SERVICE_VERSION, service.API_SCHEMA_VERSION) == (
        "tera-local-service", "0.1.0", "1.0")
    assert (service.DEFAULT_HOST, service.DEFAULT_PORT, service.PORT_MIN,
            service.PORT_MAX) == ("127.0.0.1", 7391, 1024, 65535)
    assert service.PORT_ENV == "TERA_SERVICE_PORT"
    assert service.MAX_CONCURRENT_REQUESTS == 8
    assert service.REQUEST_QUEUE_TIMEOUT_SECONDS == 5
    assert service.LISTEN_BACKLOG == 32
    assert service.REQUEST_TIMEOUT_SECONDS == 30
    assert service.SHUTDOWN_GRACE_SECONDS == 5
    assert service.MAX_REQUEST_BYTES == 65536
    assert schemas.DEFAULT_PAGE_SIZE == 50
    assert (schemas.PAGE_SIZE_MIN, schemas.PAGE_SIZE_MAX) == (1, 200)
    assert schemas.MAX_QUERY_LENGTH == 200
    assert schemas.CURSOR_VERSION == 1
    assert errors.MAX_MESSAGE_LENGTH == 200
    assert (service.EXIT_OK, service.EXIT_INVALID, service.EXIT_STOPPED) == (0, 2, 130)
    assert service.CONFIG_CODES == ("invalid_bind_host", "invalid_port", "port_out_of_range",
                                    "database_not_found", "invalid_port_file",
                                    "invalid_dev_origin", "port_in_use")
    assert service.ALLOWED_ORIGINS == ("tauri://localhost", "http://tauri.localhost",
                                       "https://tauri.localhost")


def _burst(running, paths, workers=40):
    """Issue one request per worker across the given paths."""

    results, start = [None] * workers, threading.Barrier(workers)

    def one(index):
        start.wait()
        try:
            results[index] = running.client.get(paths[index % len(paths)])
        except Exception as error:  # a refused connection is a failure, not a status
            results[index] = error

    threads = [threading.Thread(target=one, args=(index,), daemon=True)
               for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def test_a_burst_of_requests_is_bounded_and_never_answers_500(tmp_path):
    paths = ["/health", "/imports/run-00000000000000000000000000000000", "/library/samples",
             "/library/samples?limit=1", "/library/samples/sha256:" + "0" * 64,
             "/imports/not@a@run", "/imports/run-00000000000000000000000000000000/retry",
             "/imports/run-00000000000000000000000000000000/cancel", "/library/audio", "/health"]
    running = start_service(str(tmp_path / "library.sqlite3"))
    try:
        results = _burst(running, paths)
        deadline = time.monotonic() + 5
        while running.app.concurrency.in_flight and time.monotonic() < deadline:
            time.sleep(0.01)
        assert running.app.concurrency.in_flight == 0
        for reply in results:
            assert not isinstance(reply, Exception), reply
            assert reply.status in (200, 202) or 400 <= reply.status < 600
            assert reply.status != 500
            assert int(reply.headers["content-length"]) == len(reply.raw)
            assert reply.headers["content-type"] == "application/json; charset=utf-8"
        assert running.client.get("/health").status == 200
    finally:
        running.stop()


def test_a_request_that_cannot_get_a_slot_is_told_to_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "MAX_CONCURRENT_REQUESTS", 1)
    monkeypatch.setattr(service, "REQUEST_QUEUE_TIMEOUT_SECONDS", 0.05)
    running = start_service(str(tmp_path / "library.sqlite3"))
    held = running.app.concurrency.acquire()
    try:
        assert held is True
        reply = running.client.get("/health")
        assert (reply.status, reply.code()) == (503, "server_busy")
        assert reply.headers["retry-after"] == "1"
    finally:
        running.app.concurrency.release()
        running.stop()


def test_an_abandoned_connection_is_closed_at_the_timeout_and_frees_its_slot(
        tmp_path, monkeypatch):
    monkeypatch.setattr(service, "REQUEST_TIMEOUT_SECONDS", 1)
    running = start_service(str(tmp_path / "library.sqlite3"))
    timeout = running.app.request_timeout_seconds
    try:
        assert timeout == 1
        # A partial request line: the connection is not a request yet.
        abandoned = socket.create_connection(("127.0.0.1", running.port), timeout=10)
        abandoned.settimeout(10)
        abandoned.sendall(b"GET /health HTTP/1.1\r\n")
        started = time.monotonic()
        assert abandoned.recv(1024) == b""
        abandoned.close()
        assert time.monotonic() - started < 10
        # A request whose body never arrives holds a slot until the timeout.
        stalled = socket.create_connection(("127.0.0.1", running.port), timeout=10)
        stalled.settimeout(10)
        stalled.sendall(b"POST /imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                        b"Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n"
                        % running.port)
        deadline = time.monotonic() + 5
        while running.app.concurrency.in_flight == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert running.app.concurrency.in_flight == 1
        assert stalled.recv(1024) == b""
        stalled.close()
        # The slot is back and the service still answers.
        deadline = time.monotonic() + 5
        while running.app.concurrency.in_flight and time.monotonic() < deadline:
            time.sleep(0.01)
        assert running.app.concurrency.in_flight == 0
        assert running.client.get("/health").status == 200
    finally:
        running.stop()
