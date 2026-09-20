"""The request and transport contracts of the local service (issue #27).

Two fixtures drive this file: `tests/fixtures/api/request-cases.json` replays
every pure validation case against `backend.api.schemas`, and
`tests/fixtures/api/http-cases.json` drives the same rules over a real socket
against a service created by `create_server` on port 0. Together they cover
every code in `backend.api.errors.ERROR_CODES` except `server_busy` (which
needs a saturated service and is covered by `test_api_policy.py`) and
`internal_error`, which no documented case may produce.

Every root, file name and id here is synthetic.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import socket

import pytest

from backend.api import errors, schemas
from backend.api.errors import ApiError
from backend.library import indexer, queue
from backend.library.repository import transaction
from tests.api_client import Reply, build_library, connection, start_service, tone


FIXTURES = Path(__file__).parent / "fixtures" / "api"

REQUEST_CASES = json.loads((FIXTURES / "request-cases.json").read_text(encoding="utf-8"))["cases"]
HTTP_CASES = json.loads((FIXTURES / "http-cases.json").read_text(encoding="utf-8"))["cases"]

VALIDATORS = {
    "import_request": schemas.import_request,
    "sample_query": schemas.sample_query,
    "require_run_id": schemas.require_run_id,
    "require_sample_id": schemas.require_sample_id,
}


def _value(result):
    """The comparable value of a validator's result."""

    if hasattr(result, "as_value"):
        return result.as_value()
    if is_dataclass(result):
        return asdict(result)
    return result


# ---------------------------------------------------------------------------
# the pure request cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", REQUEST_CASES, ids=[case["name"] for case in REQUEST_CASES])
def test_the_request_cases_hold(case):
    validator = VALIDATORS[case["case"]]
    if "code" in case:
        with pytest.raises(ApiError) as caught:
            validator(case["input"])
        assert caught.value.code == case["code"]
        assert caught.value.status == errors.STATUS_BY_CODE[case["code"]]
        assert caught.value.details == case.get("details", {})
    else:
        assert _value(validator(case["input"])) == case["value"]


def test_every_validation_code_has_at_least_one_request_case():
    covered = {case["code"] for case in REQUEST_CASES if "code" in case}
    # The transport-only codes are exercised by the HTTP cases instead.
    transport_only = {"invalid_json", "invalid_root", "unknown_route", "method_not_allowed",
                      "host_not_allowed", "origin_not_allowed", "unknown_import",
                      "unknown_sample", "import_already_running", "import_not_live",
                      "length_required", "request_too_large", "unsupported_media_type",
                      "database_unavailable", "server_busy", "internal_error"}
    assert {code for code, _status in errors.ERROR_CODES} - transport_only <= covered


# ---------------------------------------------------------------------------
# the transport cases
# ---------------------------------------------------------------------------


@contextmanager
def _prepared(tmp_path, setup=None):
    """A running service prepared for one case, plus the case's placeholders."""

    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55)}})
    database = tmp_path / "library.sqlite3"
    if setup == "degraded":
        # A file that is not a SQLite database: the service starts degraded.
        database.write_bytes(b"this file is not a SQLite database, and never was.")
    else:
        with closing(connection(database)):
            pass
    run_id = None
    if setup in ("live_run", "terminal_run"):
        with closing(connection(database)) as opened:
            run_id = queue.open_run(opened, indexer.current_analysis_version())
            if setup == "terminal_run":
                with transaction(opened):
                    queue.finish_run(opened, run_id, queue.RUN_COMPLETE)
    running = start_service(str(database))
    try:
        yield running, {"{root}": str(root), "{absent}": str(tmp_path / "absent-root"),
                        "{port}": str(running.port), "{run_id}": run_id or ""}
    finally:
        running.stop()


def _substitute(value, placeholders, *, escaped=()):
    """Replace the case's placeholders; a body escapes the ones that are paths."""

    for token, replacement in placeholders.items():
        if token in escaped:
            replacement = json.dumps(replacement)[1:-1]
        value = value.replace(token, replacement)
    return value


def _raw_exchange(port, request, timeout=30.0):
    """One request written to the socket verbatim; the response read by length."""

    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as opened:
        opened.settimeout(timeout)
        opened.sendall(request.encode("ascii"))
        stream = opened.makefile("rb")
        status = int(stream.readline().decode("latin-1").split()[1])
        headers = {}
        while True:
            line = stream.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        body = stream.read(int(headers.get("content-length", "0")))
    return status, headers, body


def _exchange(running, case, placeholders):
    """The response one transport case expects, from the service it needs."""

    if "raw" in case:
        status, headers, body = _raw_exchange(running.port,
                                              _substitute(case["raw"], placeholders))
        return Reply(status=status, headers=headers,
                     body=(json.loads(body) if body else None), raw=body)
    payload = None
    if "body" in case:
        payload = _substitute(case["body"], placeholders,
                              escaped=("{root}", "{absent}")).encode("utf-8")
    elif "body_size" in case:
        payload = b"x" * case["body_size"]
    headers = {name: _substitute(value, placeholders)
               for name, value in case.get("headers", {}).items()}
    return running.client.call(_substitute(case["method"], placeholders),
                               _substitute(case["path"], placeholders), body=payload,
                               headers=headers, host=case.get("host"))


@pytest.mark.parametrize("case", HTTP_CASES, ids=[case["name"] for case in HTTP_CASES])
def test_the_transport_cases_hold(tmp_path, case):
    with _prepared(tmp_path, case.get("setup")) as (running, placeholders):
        reply = _exchange(running, case, placeholders)
    assert reply.status == case["status"], reply.raw
    assert reply.code() == case["code"]
    assert case["code"] != "internal_error"
    if "allow" in case:
        assert reply.headers.get("allow") == case["allow"]
    else:
        assert "allow" not in reply.headers
    if "cors" in case:
        assert reply.headers.get("access-control-allow-origin") == case["cors"]
        assert reply.headers.get("vary") == "Origin"
    else:
        assert reply.headers.get("access-control-allow-origin") != "*"
    # Every response carries the same four headers and an exact length.
    assert reply.headers["content-type"] == "application/json; charset=utf-8"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert int(reply.headers["content-length"]) == len(reply.raw)
    if reply.status >= 300:
        assert set(reply.body) == {"api_schema", "error"}
        assert set(reply.body["error"]) == {"code", "message", "details"}
        assert reply.body["api_schema"] == "1.0"
        assert reply.body["error"]["code"] == case["code"]
        assert isinstance(reply.body["error"]["details"], dict)


def test_every_http_code_has_at_least_one_transport_case():
    covered = {case["code"] for case in HTTP_CASES if case["code"] is not None}
    covered |= {case["code"] for case in REQUEST_CASES if "code" in case}
    expected = {code for code, _status in errors.ERROR_CODES
                if code not in ("internal_error", "server_busy")}
    assert expected <= covered


def test_the_closed_code_table_is_well_formed():
    codes = [code for code, _status in errors.ERROR_CODES]
    assert len(codes) == len(set(codes))
    assert set(errors.MESSAGE_BY_CODE) == set(codes)
    assert all(isinstance(status, int) and 400 <= status <= 599 for _code, status in errors.ERROR_CODES)
    for code in codes:
        message = errors.MESSAGE_BY_CODE[code]
        assert 0 < len(message) <= errors.MAX_MESSAGE_LENGTH
        lowered = message.lower()
        assert "\\" not in message
        assert "select " not in lowered and " from " not in lowered
        assert "traceback" not in lowered and "sqlite" not in lowered
        assert ":\\" not in message and ":/" not in message


def test_the_route_table_is_closed_and_documented():
    from backend.api import service

    table = [(route.method, route.path) for route in service.ROUTES]
    assert table == [
        ("GET", "/health"),
        ("POST", "/imports"),
        ("GET", "/imports/{run_id}"),
        ("POST", "/imports/{run_id}/cancel"),
        ("POST", "/imports/{run_id}/retry"),
        ("GET", "/library/samples"),
        ("GET", "/library/samples/{sample_id}"),
    ]
    document = (Path(__file__).resolve().parents[1] / "_docs" / "local-service.md").read_text(
        encoding="utf-8")
    for method, path in table:
        assert f"`{method} {path}`" in document
