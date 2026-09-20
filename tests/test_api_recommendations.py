"""The recommendation request, projection and policy contract (issue #28).

Three groups of evidence live here:

- tests/fixtures/api/recommendation-requests.json replays every pure request
  rule against backend.api.recommendations.parse_recommendation_request;
- a real run over a socket (its library built by the flow test's helpers) proves
  that the projected block carries exactly the contract's fields, that the one
  documented deviation is the dropped local_path, and that the run block
  satisfies its arithmetic;
- source and policy assertions prove that this module holds no second filter, no
  forbidden literal, no credential and no outbound client of its own, and that
  the route inherits #27's Host, Origin, media-type, length and loopback rules.

Every id, endpoint and key here is synthetic.
"""

from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import fields
import json
from pathlib import Path
import socket

import pytest

from backend.api import errors, recommendations, service, schemas
from backend.api.errors import ApiError
from backend.contracts import (AudioFeatures, AudioMetadata, DimensionScore, JevJudgment,
                               LabelProbability, Measurement, MusicalKey, PaletteContext,
                               RankedCandidate, RecommendationBatch, Sample, SongContext)
from tests.api_client import start_service
from tests.test_api_recommendation_flow import (ScriptedTransport, build_library,
                                                collaborators_for, _request_body, _rule_case)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "api"
MODULE_FILE = ROOT / "backend" / "api" / "recommendations.py"

REQUEST_CASES = json.loads(
    (FIXTURES / "recommendation-requests.json").read_text(encoding="utf-8"))["cases"]
RUN_CASES = json.loads(
    (FIXTURES / "recommendation-runs.json").read_text(encoding="utf-8"))["cases"]

#: The literals, statements and client modules this module must never hold: a
#: weight, a label map, a measure name, a reliability threshold, a statement or
#: an outbound client of its own.
FORBIDDEN_LITERALS = ("0.30", "0.20", "0.10", "very-poor", "excellent", "band_sub", "0.80",
                      "0.60", "SELECT", "ORDER BY")

#: The outbound clients this module must not import of its own; the transport
#: #14 owns does the sending.
FORBIDDEN_MODULES = ("socket", "urllib.request", "http.client", "requests", "httpx",
                     "subprocess")

#: The contract records of one projected batch and where each one sits.
PROJECTION_TYPES = (
    ("recommendation", RecommendationBatch),
    ("recommendation.palette", PaletteContext),
    ("recommendation.palette.song", SongContext),
    ("recommendation.palette.song.tempo", Measurement),
    ("recommendation.palette.song.key", MusicalKey),
    ("recommendation.samples[]", Sample),
    ("recommendation.samples[].audio", AudioMetadata),
    ("recommendation.samples[].features", AudioFeatures),
    ("recommendation.samples[].features.measurements[]", Measurement),
    ("recommendation.samples[].features.key", MusicalKey),
    ("recommendation.results[]", RankedCandidate),
    ("recommendation.results[].dsp_dimensions[]", DimensionScore),
    ("recommendation.results[].jev_judgments[]", JevJudgment),
    ("recommendation.results[].jev_judgments[].probabilities[]", LabelProbability),
)

#: The four non-path fields of one projected AudioMetadata: the one documented
#: deviation from the contract, exactly as #27's feature route projects it.
AUDIO_FIELDS = ("sample_rate_hz", "channels", "frame_count", "duration_ms")


# ---------------------------------------------------------------------------
# the pure request cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", REQUEST_CASES, ids=[case["name"] for case in REQUEST_CASES])
def test_the_request_cases_hold(case):
    if "code" in case:
        with pytest.raises(ApiError) as caught:
            recommendations.parse_recommendation_request(case["input"])
        assert caught.value.code == case["code"]
        assert caught.value.status == errors.STATUS_BY_CODE[case["code"]]
        assert caught.value.details == case.get("details", {})
    else:
        assert recommendations.parse_recommendation_request(case["input"]).as_value() \
            == case["value"]


def test_every_request_rule_code_has_at_least_one_case():
    covered = {case["code"] for case in REQUEST_CASES if "code" in case}
    assert covered == {"invalid_body", "missing_field", "unknown_field", "invalid_field_type",
                       "invalid_palette_id", "invalid_revision", "invalid_limit"}


def test_the_documented_bounds_are_the_documented_ones():
    assert (recommendations.RECOMMENDATION_METHOD, recommendations.RECOMMENDATION_PATH) \
        == ("POST", "/recommendations")
    assert (recommendations.RESULT_LIMIT_MIN, recommendations.RESULT_LIMIT_MAX,
            recommendations.DEFAULT_RESULT_LIMIT) == (5, 20, 10)
    assert recommendations.RECOMMENDATION_BUDGET_SECONDS == 25.0
    assert recommendations.RECOMMENDATION_BUDGET_SECONDS < service.REQUEST_TIMEOUT_SECONDS
    assert recommendations.FILTER_KEYS == tuple(
        field.name for field in fields(recommendations.FilterPolicy))
    assert recommendations.parse_recommendation_request(
        {"palette_id": "palette-00000000000000000000000000000001", "revision": 0}
    ).filters == recommendations.FilterPolicy()


def test_the_request_cases_and_the_run_cases_never_expect_a_500():
    statuses = set()
    for case in REQUEST_CASES:
        statuses.update([errors.STATUS_BY_CODE[case["code"]]] if "code" in case else [200])
    for case in RUN_CASES:
        statuses.update(expect["status"] for expect in case["expect"])
        for expect in case["expect"]:
            if expect["status"] != 200:
                assert expect["code"] in errors.STATUS_BY_CODE
                assert errors.STATUS_BY_CODE[expect["code"]] == expect["status"]
    assert 500 not in statuses
    assert {200, 400, 409} <= statuses
    # The 404s of this route -- unknown_palette and an unknown path -- are
    # covered by the shared transport cases of tests/fixtures/api/http-cases.json.
    shared = json.loads((FIXTURES / "http-cases.json").read_text(encoding="utf-8"))["cases"]
    assert any(case["code"] == "unknown_palette" for case in shared)


# ---------------------------------------------------------------------------
# the module's own source
# ---------------------------------------------------------------------------


def test_the_module_holds_no_forbidden_literal_or_outbound_client():
    source = MODULE_FILE.read_text(encoding="utf-8")
    for literal in FORBIDDEN_LITERALS:
        assert literal not in source, literal
    tree = ast.parse(source, filename=str(MODULE_FILE))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not set(FORBIDDEN_MODULES) & set(names), sorted(set(FORBIDDEN_MODULES) & set(names))
    assert "_docs/recommendation-api.md" in source


# ---------------------------------------------------------------------------
# one real run: the projection, the deviation and the policy
# ---------------------------------------------------------------------------


def _run(tmp_path, case_name, *, collaborators=None, environ=None, transport_spec=None):
    """One service with one synthetic library, for the projection and policy tests."""

    case = _rule_case(case_name)
    built = build_library(tmp_path, case["library"])
    if collaborators is None:
        collaborators, transport = collaborators_for(dict(case, environ=environ or {}),
                                                     built=built)
    else:
        transport = collaborators.transport
    running = start_service(built.database, recommendations=collaborators)
    return running, built, case, transport


def test_the_projected_block_carries_exactly_the_contract_fields(tmp_path):
    running, built, case, transport = _run(tmp_path, "a fully judged 12-candidate run")
    try:
        reply = running.client.post("/recommendations",
                                    _request_body(built, case["requests"][0]))
        assert reply.status == 200, reply.raw
        block = reply.body["recommendation"]
        for path, contract in PROJECTION_TYPES:
            for record in _records(block, path):
                expected = {field.name for field in fields(contract)}
                if contract is AudioMetadata:
                    expected = set(AUDIO_FIELDS)
                assert set(record) == expected, (path, sorted(set(record) ^ expected))
        # The one documented deviation is the dropped path and nothing else.
        assert "local_path" not in json.dumps(reply.body["recommendation"])
        assert "original_path" not in json.dumps(reply.body["recommendation"])
    finally:
        running.stop()


def _records(block, path):
    """Every projected record at one dotted path, expanding [] over a list."""

    current = [block]
    for step in path.split(".")[1:]:
        collected = []
        expand = step.endswith("[]")
        name = step[:-2] if expand else step
        for item in current:
            value = item[name]
            collected.extend(value if expand else [value])
        current = collected
    return current


def test_the_run_block_is_closed_and_its_arithmetic_holds(tmp_path):
    running, built, case, transport = _run(tmp_path, "a fully judged 12-candidate run")
    try:
        reply = running.client.post("/recommendations",
                                    _request_body(built, case["requests"][0]))
        assert reply.status == 200, reply.raw
        run = reply.body["run"]
        assert set(run) == {"run_id", "palette_hash", "analysis_version", "filters", "retrieval",
                            "ranking", "jev", "counts", "exclusions", "evidence"}
        assert set(run["retrieval"]) == {"policy_version", "representation_version",
                                         "normalization_id", "shortlist_size_requested",
                                         "shortlist_size_returned", "limit_reason",
                                         "duplicate_candidates", "skipped_stale_analysis",
                                         "skipped_absent_analysis"}
        assert set(run["ranking"]) == {"ranking_version", "weight_table_id", "jev_status"}
        assert set(run["jev"]) == {"prompt_version", "adapter_version", "model_versions"}
        assert set(run["counts"]) == {"eligible", "excluded", "shortlisted", "scored", "unscored",
                                      "requested", "returned"}
        assert set(run["evidence"]) == {"interface", "double", "unavailable", "cache",
                                        "cache_errors"}
        assert set(run["filters"]) == {"policy_version", "tempo_lock", "exact_key_lock"}
        assert len(run["run_id"]) == 64
        assert all(character in "0123456789abcdef" for character in run["run_id"])
        assert "sha256:" not in run["run_id"]
        counts = run["counts"]
        assert counts["returned"] == min(counts["requested"], counts["scored"])
        assert counts["returned"] == len(reply.body["recommendation"]["results"])
        assert run["jev"]["model_versions"] == sorted(set(run["jev"]["model_versions"]))
        used = {judgment["model_version"] for record in reply.body["recommendation"]["results"]
                for judgment in record["jev_judgments"]}
        assert used <= set(run["jev"]["model_versions"])
        assert run["retrieval"]["shortlist_size_requested"] == 100
    finally:
        running.stop()


def test_no_response_carries_a_credential_endpoint_path_or_audio(tmp_path, capfd):
    endpoint = "https://jev.invalid.example/synthetic"
    api_key = "synthetic-key-0000000000000000"
    case = _rule_case("a fully judged 12-candidate run")
    built = build_library(tmp_path, case["library"])
    transport = ScriptedTransport(dict(case["transport"], source="interface",
                                       interface_name="typesafe-jev-http"))
    collaborators = recommendations.RecommendationCollaborators(
        transport=transport, environ={recommendations_key(0): endpoint,
                                      recommendations_key(1): api_key})
    running = start_service(built.database, recommendations=collaborators)
    try:
        body = _request_body(built, case["requests"][0])
        replies = [running.client.post("/recommendations", body)]
        # Every documented refusal of the request schema, plus the unknown
        # palette, the unsupported method and the unknown route: each response
        # body, header and stdout line is grepped for the credential below.
        replies.extend(running.client.post("/recommendations", case["input"])
                       for case in REQUEST_CASES)
        replies.extend([running.client.post("/recommendations", {"palette_id": "palette-000",
                                                                 "revision": 0}),
                        running.client.post("/recommendations", dict(body, limit=4)),
                        running.client.get("/recommendations"),
                        running.client.get("/recommendations/0123456789abcdef")])
        # The synthetic sample ids do reach the client, so the grep is not vacuous.
        assert "kick-001" in replies[0].raw.decode("utf-8")
        assert replies[0].status == 200
        assert replies[0].body["run"]["evidence"] == {"interface": 60, "double": 0,
                                                      "unavailable": 0, "cache": 0,
                                                      "cache_errors": 0}
        for reply in replies:
            text = reply.raw.decode("utf-8")
            assert endpoint not in text and api_key not in text
            assert str(tmp_path) not in text and "\\" not in text
            assert "C:/tera-fixtures" not in text
            for value in reply.headers.values():
                assert endpoint not in value and api_key not in value
        second = running.client.post("/recommendations", body)
        assert second.status == 200
        assert second.body["run"]["evidence"]["cache"] == 60
        captured = capfd.readouterr()
        assert endpoint not in captured.out and api_key not in captured.out
        assert endpoint not in captured.err and api_key not in captured.err
    finally:
        running.stop()


def recommendations_key(index):
    """The two credential variable names #14 owns, read from #14 itself."""

    from backend.intelligence import jev

    return (jev.ENV_ENDPOINT, jev.ENV_API_KEY)[index]


def test_a_credential_absent_cold_run_opens_no_socket(tmp_path, monkeypatch):
    case = _rule_case("a fully degraded run with credentials absent")
    built = build_library(tmp_path, case["library"])
    collaborators = recommendations.RecommendationCollaborators(transport=None, environ={})
    running = start_service(built.database, recommendations=collaborators)
    attempts = []
    try:
        # The route is called in process, exactly as #27's own no-outbound-socket
        # test calls its routes: the patch would otherwise also forbid the test
        # client's own connection to the service.
        monkeypatch.setattr(socket, "create_connection",
                            lambda *args, **kwargs: attempts.append("create_connection"))
        monkeypatch.setattr(socket.socket, "connect",
                            lambda *args, **kwargs: attempts.append("connect"))
        reply = _in_process(running.app, _request_body(built, case["requests"][0]))
        assert reply.status == 200
        assert reply.body["recommendation"]["mode"] == "dsp-only"
        assert reply.body["run"]["evidence"]["unavailable"] == 15
    finally:
        running.stop()
    assert attempts == []


def _in_process(app, body):
    """Call the recommendation handler directly, exactly as the transport would."""

    route = next(route for route in service.ROUTES if route.path == recommendations.RECOMMENDATION_PATH)
    try:
        return route.handler(schemas.RequestContext(app=app, method=route.method,
                                                    template=route.path, params={}, query=(),
                                                    body=body))
    except ApiError as error:
        return schemas.Response(error.status, error.document(app.api_schema))


def _raw(port, text):
    """One raw request written verbatim; the status line and headers come back."""

    with socket.create_connection(("127.0.0.1", port), timeout=10) as opened:
        opened.settimeout(10)
        opened.sendall(text)
        stream = opened.makefile("rb")
        status = int(stream.readline().decode("latin-1").split()[1])
        headers = {}
        while True:
            line = stream.readline()
            if line in (b"", b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        raw = stream.read(int(headers.get("content-length", "0")))
    return status, headers, raw


def test_the_route_inherits_the_transport_rules(tmp_path):
    root = Path(tmp_path)
    built = build_library(root, {"kick": {"key": "known"}, "basses": [{"id": "bass-001"}],
                                 "palette": {"kick": True, "bass": "bass-001"}})
    collaboration = recommendations.RecommendationCollaborators(transport=None, environ={})
    running = start_service(built.database, recommendations=collaboration)
    port = running.port
    body = "{\"palette_id\": \"%s\", \"revision\": 0}" % built.palette_id
    try:
        status, headers, raw = _raw(port, (
            "POST /recommendations HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
            "Content-Length: %d\r\n\r\n%s" % (port, len(body), body)).encode("ascii"))
        assert status == 415
        assert json.loads(raw)["error"]["code"] == "unsupported_media_type"
        chunked = ("POST /recommendations HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                   "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                   "0\r\n\r\n") % port
        status, headers, raw = _raw(port, chunked.encode("ascii"))
        assert status == 411
        assert json.loads(raw)["error"]["code"] == "length_required"
        large = ("POST /recommendations HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                 "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                 % (port, service.MAX_REQUEST_BYTES + 1))
        status, headers, raw = _raw(port, large.encode("ascii"))
        assert status == 413
        assert json.loads(raw)["error"]["code"] == "request_too_large"
        assert running.client.get("/recommendations", host="example.invalid").code() == \
            "host_not_allowed"
        refused = running.client.get("/recommendations",
                                     headers={"Origin": "http://example.invalid"})
        assert (refused.status, refused.code()) == (403, "origin_not_allowed")
        allowed = running.client.call("OPTIONS", "/recommendations",
                                      headers={"Origin": "tauri://localhost"})
        assert allowed.status == 204
        assert allowed.headers["access-control-allow-origin"] == "tauri://localhost"
        # `_PREFLIGHT_HEADERS` is one service-wide advertisement of every method
        # the route table carries, not a per-route one: the palette's PUT routes
        # put PUT in it for every preflight. An actual PUT to this route is still
        # refused with 405 and its own `Allow: POST`.
        assert allowed.headers["access-control-allow-methods"] == "GET, POST, PUT, OPTIONS"
        denied = running.client.call("OPTIONS", "/recommendations",
                                     headers={"Origin": "http://example.invalid"})
        assert (denied.status, denied.code()) == (403, "origin_not_allowed")
        method = running.client.get("/recommendations")
        assert (method.status, method.code(), method.headers["allow"]) == \
            (405, "method_not_allowed", "POST")
        assert running.client.get("/recommendations/0123456789abcdef").code() == "unknown_route"
    finally:
        running.stop()


def test_an_invalid_request_is_refused_before_any_palette_read(tmp_path):
    # A valid request against this library is 404 unknown_palette, so a malformed
    # one answered 400 proves the schema was validated before any palette read.
    built = build_library(tmp_path, {"kick": {"key": "known"}, "basses": [],
                                     "palette": {"kick": True, "bass": None}})
    running = start_service(built.database,
                            recommendations=recommendations.RecommendationCollaborators(
                                transport=None, environ={}))
    absent = {"palette_id": "palette-00000000000000000000000000000000", "revision": 0}
    try:
        assert running.client.post("/recommendations", absent).code() == "unknown_palette"
        assert running.client.post("/recommendations", dict(absent, limit=4)).code() == \
            "invalid_limit"
        assert running.client.post("/recommendations", dict(absent, revision=-1)).code() == \
            "invalid_revision"
        assert running.client.post("/recommendations", {"palette_id": "bad id",
                                                        "revision": 0}).code() == \
            "invalid_palette_id"
        assert running.client.post("/recommendations", dict(absent, filters={"x": True})).code() \
            == "unknown_field"
        # #27's policy is decided before any handler runs, and its degraded
        # database check before this route's schema or any palette read.
        assert running.client.post("/recommendations", absent,
                                   host="example.invalid").code() == "host_not_allowed"
        assert running.client.post("/recommendations", absent,
                                   headers={"Origin": "http://example.invalid"}).code() == \
            "origin_not_allowed"
        (tmp_path / "degraded.sqlite3").write_bytes(b"not a SQLite database at all")
        degraded = start_service(str(tmp_path / "degraded.sqlite3"))
        try:
            assert degraded.client.post("/recommendations", absent).code() == \
                "database_unavailable"
            assert degraded.client.post("/recommendations", absent,
                                        host="example.invalid").code() == "host_not_allowed"
        finally:
            degraded.stop()
    finally:
        running.stop()
