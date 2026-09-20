"""The outcome recording and history contract (issue #29).

Two fixtures drive this module over a real socket:
`tests/fixtures/outcomes/outcome-cases.json` posts one closed body to
`POST /outcomes` (and, where a case needs it, a second body after it) and
asserts the status, the code and the created flag; `history-cases.json` reads
`GET /outcomes` and asserts the page arithmetic. The scenario each case names is
built here from #21's repository and #22's synthetic rows, so no real library
path, sample name or audio byte takes part.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, fields
import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from backend.contracts import RecommendationBatch
from backend.library.outcomes import OutcomeEvent, OutcomeSubmission
from backend.library.repository import LibraryRepository
from tests.api_client import connection, insert_samples, start_service

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "outcomes"

OUTCOME_DOCUMENT = json.loads((FIXTURES / "outcome-cases.json").read_text(encoding="utf-8"))
HISTORY_DOCUMENT = json.loads((FIXTURES / "history-cases.json").read_text(encoding="utf-8"))
OUTCOME_CASES = OUTCOME_DOCUMENT["cases"]
HISTORY_CASES = HISTORY_DOCUMENT["cases"]
RECORDED_EVENTS = HISTORY_DOCUMENT["recorded_events"]

#: The contract's own first mode literal, so a fixture cannot assert a mode the
#: product does not have.
MODE = get_args(get_type_hints(RecommendationBatch)["mode"])[0]
RUN_ID = "run-00000000000000000000000000000001"
COLUMNS = {field.name for field in fields(OutcomeEvent)}


@dataclass(frozen=True)
class Scenario:
    """The ids the fixture bodies name once the scenario has been built."""

    project_id: str = ""
    palette_id: str = ""
    candidate_id: str = ""
    other_project_id: str = ""
    other_palette_id: str = ""

    def tokens(self) -> dict:
        return ({f"{{{name}}}": value for name, value in self.__dict__.items()}
                | {"{mode}": MODE})


def _submission(client_event_id, event_type, scenario, **overrides) -> OutcomeSubmission:
    supplied = dict(project_id=scenario.project_id, palette_id=scenario.palette_id,
                    candidate_id=scenario.candidate_id, run_id=RUN_ID, palette_revision=1,
                    ranking_version="ranking-v1", mode=MODE,
                    candidate_analysis_version="analysis-v1")
    supplied.update(overrides)
    return OutcomeSubmission(client_event_id=client_event_id, event_type=event_type, **supplied)


def _scenario(database, name, directory) -> Scenario:
    """Build the rows the named scenario needs, then return the ids it exposes."""

    if name == "empty":
        return Scenario()
    candidate_id = insert_samples(database, 1, directory=directory, role="bass")[0]
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        project = repository.create_project("Track A")
        other = repository.create_project("Track B") if name == "two_projects" else None
        if name in ("selection", "live_selection", "recorded"):
            repository.set_palette_item(project.palette_id, "bass", candidate_id,
                                        expected_revision=0)
        scenario = Scenario(project_id=project.project_id, palette_id=project.palette_id,
                            candidate_id=candidate_id,
                            other_project_id="" if other is None else other.project_id,
                            other_palette_id="" if other is None else other.palette_id)
        if name == "live_selection":
            written = repository.record_outcome(
                _submission("event-selected-seed", "selected", scenario))
            assert written.created is True and written.event.event_id == 1
    return scenario


def _body(document, scenario):
    """The fixture body with the scenario's ids and the contract's mode filled in."""

    text = json.dumps(document)
    for token, value in scenario.tokens().items():
        text = text.replace(token, str(value))
    return json.loads(text)


def _service(tmp_path, scenario_name):
    database = tmp_path / "library.sqlite3"
    with closing(connection(database)):
        pass
    scenario = _scenario(database, scenario_name, tmp_path)
    return start_service(str(database)), scenario


@pytest.mark.parametrize("case", OUTCOME_CASES, ids=[case["name"] for case in OUTCOME_CASES])
def test_the_outcome_cases_hold(tmp_path, case):
    running, scenario = _service(tmp_path, case["scenario"])
    try:
        if "second" in case:
            first = running.client.post("/outcomes", _body(case["body"], scenario))
            assert first.status in (200, 201), first.raw
            reply = running.client.post("/outcomes", _body(case["second"], scenario))
        else:
            reply = running.client.post("/outcomes", _body(case["body"], scenario))
    finally:
        running.stop()
    assert reply.status == case["status"], reply.raw
    assert reply.code() == case["code"]
    if case["code"] is None:
        assert reply.body["created"] is case["created"]
        assert set(reply.body["outcome"]) == COLUMNS


@pytest.mark.parametrize("case", HISTORY_CASES, ids=[case["name"] for case in HISTORY_CASES])
def test_the_history_cases_hold(tmp_path, case):
    running, scenario = _service(tmp_path, case["scenario"])
    try:
        if case["scenario"] == "recorded":
            for body in RECORDED_EVENTS:
                written = running.client.post("/outcomes", _body(body, scenario))
                assert written.status in (200, 201), written.raw
        reply = running.client.get(case["path"])
    finally:
        running.stop()
    assert reply.status == case["status"], reply.raw
    assert reply.code() == case["code"]
    if case["code"] is not None:
        return
    assert len(reply.body["items"]) == case["count"]
    assert reply.body["page"]["has_more"] is case["has_more"]
    assert (reply.body["page"]["next_cursor"] is not None) is case["has_more"]
    if "event_types" in case:
        assert [item["event_type"] for item in reply.body["items"]] == case["event_types"]
    if "event_ids" in case:
        assert [item["event_id"] for item in reply.body["items"]] == case["event_ids"]


def test_every_outcome_refusal_code_has_a_case():
    """The ten codes `POST /outcomes` can refuse with are all exercised above."""

    covered = {case["code"] for case in OUTCOME_CASES + HISTORY_CASES if case["code"]}
    assert {"invalid_outcome", "unknown_project", "unknown_palette", "unknown_selection",
            "unknown_palette_revision", "cross_project_reference", "selection_not_in_palette",
            "removal_not_reflected", "outcome_conflict", "idempotency_conflict"} <= covered


def test_a_recorded_event_never_carries_a_path_or_an_audio_field(tmp_path):
    running, scenario = _service(tmp_path, "selection")
    try:
        reply = running.client.post("/outcomes", _body(RECORDED_EVENTS[0], scenario))
    finally:
        running.stop()
    assert reply.status == 201, reply.raw
    assert set(reply.body["outcome"]) == COLUMNS
    assert "path" not in json.dumps(reply.body) and str(tmp_path) not in json.dumps(reply.body)
