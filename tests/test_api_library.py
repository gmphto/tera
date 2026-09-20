"""The library read routes: paging, cursors and stored measurements (issue #27).

Every database, folder and file here is temporary and synthetic: the library
builder writes WAV bytes under `tmp_path`, the bulk rows carry `f"{index:064x}"`
content hashes, and the one real extraction is a 55 Hz tone whose measured
fundamental is asserted against the extractor's own output.

`test_a_page_costs_at_most_the_statement_budget` reads
`sqlite3.Connection.set_trace_callback` on the service's own connection, so the
number it asserts is the number of statements the page actually ran.
"""

from __future__ import annotations

from contextlib import closing
import threading
import time
from urllib.parse import quote

import pytest

from backend.analysis import batch
from backend.api import library
from backend.contracts import MEASURES
from backend.library import indexer, queue
from backend.library.repository import LibraryRepository, transaction
from tests.api_client import (
    build_library,
    connection,
    insert_samples,
    scan,
    start_service,
    store_analysis,
    store_extra_version,
    tone,
)


def _prepared(tmp_path, *, name="library"):
    """An empty migrated database and a synthetic root, ready for rows."""

    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55)}})
    database = tmp_path / (name + ".sqlite3")
    with closing(connection(database)):
        pass
    return root, database


def _cursor_path(cursor, **parameters):
    parts = [f"cursor={quote(cursor)}"] + [f"{name}={quote(str(value))}"
                                           for name, value in parameters.items()]
    return "/library/samples?" + "&".join(parts)


def _traced(running, path):
    """One request with the service connection's statements recorded.

    The callback is installed from the connection's own thread, because a
    SQLite connection may only be used by the thread that opened it; the
    database thread runs one job at a time, so nothing else can slip in.
    """

    statements = []
    running.app.database.run(lambda connection: connection.set_trace_callback(
        statements.append))
    try:
        reply = running.client.get(path)
    finally:
        running.app.database.run(lambda connection: connection.set_trace_callback(None))
    return reply, statements


def _content_ids(database):
    """The API identity of every stored row, in content order."""

    with closing(connection(database)) as opened:
        return ["sha256:" + row["content_sha256"] for row in
                opened.execute("SELECT content_sha256 FROM samples ORDER BY content_sha256")]


def _row_id(database, sample_id):
    """The #21 row id behind one API content identity."""

    with closing(connection(database)) as opened:
        row = opened.execute("SELECT sample_id FROM samples WHERE content_sha256 = ?",
                             (sample_id.split(":", 1)[1],)).fetchone()
    return row["sample_id"]


def _terminal(running, run_id, timeout=180.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document = running.client.get(f"/imports/{run_id}").body["import"]
        if document["phase"] not in ("scanning", "analyzing", "running"):
            return document
        time.sleep(0.02)
    raise AssertionError("the import did not finish")


def _hold_write(database, seconds):
    """Hold one write transaction open for a while, the way another writer would."""

    with closing(connection(database)) as opened:
        opened.execute("BEGIN IMMEDIATE")
        try:
            time.sleep(seconds)
        finally:
            opened.execute("ROLLBACK")

# ---------------------------------------------------------------------------
# paging
# ---------------------------------------------------------------------------


def test_one_page_of_two_hundred_against_two_hundred_and_sixty(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 260, directory=root / "synth", status="present")
    running = start_service(str(database))
    try:
        first = running.client.get("/library/samples?limit=200")
        assert first.status == 200
        assert len(first.body["items"]) == 200
        assert first.body["page"]["limit"] == 200
        assert first.body["page"]["count"] == 200
        assert first.body["page"]["has_more"] is True
        assert first.body["page"]["next_cursor"] is not None
        assert [item["sample_id"] for item in first.body["items"]] == sorted(ids)[:200]
        for item in first.body["items"]:
            assert item["role"] == "bass"
            assert item["file_status"] == "present"
            assert item["analysis"] == {"state": "absent", "analysis_version": None}
            assert item["audio"] == {"sample_rate_hz": 48000, "channels": 1,
                                     "frame_count": 480, "duration_ms": 10.0}
        second = running.client.get(_cursor_path(first.body["page"]["next_cursor"], limit=200))
        assert len(second.body["items"]) == 60
        assert second.body["page"]["has_more"] is False
        assert second.body["page"]["next_cursor"] is None
        assert [item["sample_id"] for item in second.body["items"]] == sorted(ids)[200:]
    finally:
        running.stop()


def test_paging_the_whole_library_with_cursors_yields_every_sample_exactly_once(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 25, directory=root / "synth")
    running = start_service(str(database))
    try:
        seen, cursor, pages = [], None, 0
        while True:
            path = "/library/samples?limit=7" if cursor is None else _cursor_path(cursor, limit=7)
            reply = running.client.get(path)
            assert reply.status == 200
            seen.extend(item["sample_id"] for item in reply.body["items"])
            pages += 1
            if not reply.body["page"]["has_more"]:
                assert reply.body["page"]["next_cursor"] is None
                break
            cursor = reply.body["page"]["next_cursor"]
        assert pages == 4
        assert seen == sorted(ids)
        assert len(seen) == len(set(seen)) == 25
    finally:
        running.stop()


def test_a_page_costs_at_most_the_statement_budget(tmp_path):
    root, database = _prepared(tmp_path)
    insert_samples(database, 200, directory=root / "synth")
    running = start_service(str(database))
    try:
        for path, expected in (("/library/samples?limit=5", 5),
                               ("/library/samples?limit=200", 200)):
            reply, statements = _traced(running, path)
            assert reply.status == 200
            assert len(reply.body["items"]) == expected
            assert len(statements) <= library.READ_STATEMENT_BUDGET, statements
    finally:
        running.stop()


def test_a_page_of_current_samples_stays_inside_the_budget(tmp_path):
    root, database = _prepared(tmp_path)
    scan(root / "kicks", database, "kick")
    store_analysis(database, _row_id(database, _content_ids(database)[0]))
    running = start_service(str(database))
    try:
        reply, statements = _traced(running, "/library/samples?limit=1")
        assert reply.body["items"][0]["analysis"]["state"] == "current"
        assert len(statements) <= library.READ_STATEMENT_BUDGET, statements
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_roles_are_or_ed_and_the_search_text_matches_the_file_name_only(tmp_path):
    root = build_library(tmp_path, {
        "kickdown": {"Kick-One.wav": tone(55)},
        "bassdown": {"bass-two.wav": tone(110)},
        "subdown": {"sub-three.wav": tone(40)},
    })
    database = tmp_path / "library.sqlite3"
    folders = {"kick": "kickdown", "bass": "bassdown", "sub-bass": "subdown"}
    running = start_service(str(database))
    try:
        for role, folder in folders.items():
            started = running.client.post("/imports", {"root": str(root / folder), "role": role})
            assert started.status == 202
            assert _terminal(running, started.body["import"]["run_id"])["state"] == "complete"
        both = running.client.get("/library/samples?role=kick&role=bass")
        assert sorted(item["role"] for item in both.body["items"]) == ["bass", "kick"]
        assert both.body["query"] == {"roles": ["kick", "bass"], "text": None}
        text = running.client.get("/library/samples?q=KICK")
        assert [item["file_name"] for item in text.body["items"]] == ["Kick-One.wav"]
        assert text.body["query"] == {"roles": [], "text": "KICK"}
        # A folder name is not searchable: the match is on the stored file name.
        assert running.client.get("/library/samples?q=kickdown").body["items"] == []
        two = running.client.get("/library/samples?q=two")
        assert [item["file_name"] for item in two.body["items"]] == ["bass-two.wav"]
        every = running.client.get("/library/samples")
        assert sorted(item["role"] for item in every.body["items"]) == ["bass", "kick", "sub-bass"]
        assert every.body["page"]["count"] == 3
        assert every.body["page"]["has_more"] is False
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# cursors across a changing library
# ---------------------------------------------------------------------------


def test_inserting_between_pages_neither_duplicates_nor_skips(tmp_path):
    root, database = _prepared(tmp_path)
    first = insert_samples(database, 10, directory=root / "synth", start=1)
    running = start_service(str(database))
    try:
        page = running.client.get("/library/samples?limit=4")
        assert [item["sample_id"] for item in page.body["items"]] == first[:4]
        insert_samples(database, 4, directory=root / "synth", start=100)
        seen = list(first[:4])
        cursor = page.body["page"]["next_cursor"]
        while cursor is not None:
            reply = running.client.get(_cursor_path(cursor, limit=4))
            seen.extend(item["sample_id"] for item in reply.body["items"])
            cursor = reply.body["page"]["next_cursor"] if reply.body["page"]["has_more"] else None
        assert sorted(seen) == sorted(set(seen))
        # Every sample that existed for the whole sequence is returned once.
        assert set(first) <= set(seen)
        assert len([value for value in seen if value in first]) == len(first)
    finally:
        running.stop()


def test_pruning_between_pages_simply_disappears(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 10, directory=root / "synth", start=1)
    running = start_service(str(database))
    try:
        page = running.client.get("/library/samples?limit=3")
        assert [item["sample_id"] for item in page.body["items"]] == ids[:3]
        with closing(connection(database)) as opened:
            LibraryRepository(opened).delete_sample(ids[5])
        seen = list(ids[:3])
        cursor = page.body["page"]["next_cursor"]
        while cursor is not None:
            reply = running.client.get(_cursor_path(cursor, limit=3))
            seen.extend(item["sample_id"] for item in reply.body["items"])
            cursor = reply.body["page"]["next_cursor"] if reply.body["page"]["has_more"] else None
        assert ids[5] not in seen
        assert sorted(seen) == [value for value in sorted(ids) if value != ids[5]]
    finally:
        running.stop()


def test_a_cursor_naming_a_deleted_sample_is_still_a_position(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 5, directory=root / "synth", start=1)
    running = start_service(str(database))
    try:
        page = running.client.get("/library/samples?limit=2")
        cursor = page.body["page"]["next_cursor"]
        with closing(connection(database)) as opened:
            LibraryRepository(opened).delete_sample(ids[2])
        reply = running.client.get(_cursor_path(cursor, limit=10))
        assert reply.status == 200
        assert [item["sample_id"] for item in reply.body["items"]] == ids[3:]
    finally:
        running.stop()


def test_a_cursor_cannot_readmit_a_row_outside_the_filters(tmp_path):
    root, database = _prepared(tmp_path)
    kick = insert_samples(database, 3, directory=root / "synth", start=1, role="kick")
    bass = insert_samples(database, 3, directory=root / "synth", start=10, role="bass")
    running = start_service(str(database))
    try:
        page = running.client.get("/library/samples?role=kick&limit=2")
        cursor = page.body["page"]["next_cursor"]
        rest = running.client.get(_cursor_path(cursor, role="kick", limit=10))
        returned = [item["sample_id"] for item in rest.body["items"]]
        assert returned == kick[2:]
        assert not [value for value in bass if value in returned]
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# the analysis state vocabulary
# ---------------------------------------------------------------------------


def test_the_analysis_state_vocabulary_is_reported_per_sample(tmp_path):
    root = build_library(tmp_path, {"kicks": {"kick-one.wav": tone(55),
                                              "kick-two.wav": tone(110)}})
    database = tmp_path / "library.sqlite3"
    scan(root / "kicks", database, "kick")
    current_id, stale_id = _content_ids(database)
    failed = insert_samples(database, 1, directory=root / "synth", start=1,
                            status="present")[0]
    with closing(connection(database)) as opened:
        queue.enqueue(opened, indexer.current_analysis_version())
        item = opened.execute("SELECT item_id FROM job_items WHERE sample_id = ?",
                              (failed,)).fetchone()
        with transaction(opened):
            queue.finalize(opened, item["item_id"], queue.ITEM_FAILED,
                           error=queue.error_record(queue.STAGE_EXTRACT,
                                                    queue.CODE_EXTRACTOR_FAILURE, "synthetic"))
    store_analysis(database, _row_id(database, current_id))
    store_extra_version(database, _row_id(database, stale_id), {"synthetic": "an-older-version"})
    queued_id = insert_samples(database, 1, directory=root / "synth", start=90)[0]
    with closing(connection(database)) as opened:
        queue.enqueue(opened, indexer.current_analysis_version())
    # Inserted after the last enqueue, so nothing queued it: this is the row
    # that has no analysis and no recorded work at all.
    absent = insert_samples(database, 1, directory=root / "synth", start=200)[0]
    running = start_service(str(database))
    try:
        state = {item["sample_id"]: item["analysis"] for item in running.client.get(
            "/library/samples?limit=20").body["items"]}
        # What is stored beats what is queued: the current row keeps its own
        # pending item and still reads current, and the row whose only stored
        # version is another reads stale rather than pending. A failure with
        # nothing stored reads failed; a queued row reads pending; and a row
        # with neither reads absent.
        assert state[current_id] == {"state": "current",
                                     "analysis_version": indexer.current_analysis_version()}
        assert state[stale_id] == {"state": "stale", "analysis_version": None}
        assert state[failed] == {"state": "failed", "analysis_version": None}
        assert state[queued_id] == {"state": "pending", "analysis_version": None}
        assert state[absent] == {"state": "absent", "analysis_version": None}
    finally:
        running.stop()


def test_a_sample_whose_only_stored_version_is_another_reads_stale(tmp_path):
    root, database = _prepared(tmp_path)
    scan(root / "kicks", database, "kick")
    sample_id = _content_ids(database)[0]
    version = store_extra_version(database, _row_id(database, sample_id),
                                  {"synthetic": "an-older-version"})
    running = start_service(str(database))
    try:
        document = running.client.get(f"/library/samples/{sample_id}").body["sample"]
        assert document["analysis"]["state"] == "stale"
        assert document["analysis"]["analysis_version"] is None
        assert document["analysis"]["stored_versions"] == [version]
        assert document["features"] is None
        listed = running.client.get("/library/samples").body["items"][0]
        assert listed["analysis"] == {"state": "stale", "analysis_version": None}
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# feature detail
# ---------------------------------------------------------------------------


def test_the_detail_route_serves_the_stored_measurements_of_a_known_signal(tmp_path):
    root = build_library(tmp_path, {"kicks": {"kick-55.wav": tone(55, frames=24000)}})
    database = tmp_path / "library.sqlite3"
    running = start_service(str(database))
    try:
        started = running.client.post("/imports", {"root": str(root / "kicks"), "role": "kick"})
        run = _terminal(running, started.body["import"]["run_id"])
        assert run["state"] == "complete"
        sample_id = running.client.get("/library/samples").body["items"][0]["sample_id"]
        reply = running.client.get(f"/library/samples/{sample_id}")
        assert reply.status == 200
        sample = reply.body["sample"]
        assert set(sample) == {"sample_id", "role", "file_name", "file_status", "analysis",
                               "audio", "features"}
        assert sample["sample_id"] == sample_id
        assert sample["role"] == "kick"
        assert sample["file_name"] == "kick-55.wav"
        assert sample["file_status"] == "present"
        assert sample["analysis"]["state"] == "current"
        assert sample["analysis"]["attempts"] == 1
        assert sample["analysis"]["error_code"] is None
        assert sample["analysis"]["stored_versions"] == [sample["analysis"]["analysis_version"]]
        assert sample["analysis"]["stored_versions_truncated"] is False
        assert sample["audio"] == {"sample_rate_hz": 48000, "channels": 1,
                                   "frame_count": 24000, "duration_ms": 500.0}
        features = sample["features"]
        assert features["schema_version"] == "1.0"
        assert features["analysis_version"] == sample["analysis"]["analysis_version"]
        names = [item["name"] for item in features["measurements"]]
        assert names == list(MEASURES)
        assert len(names) == len(set(names))
        by_name = {item["name"]: item for item in features["measurements"]}
        for name, (unit, _low, _high) in MEASURES.items():
            assert by_name[name]["unit"] == unit
        fundamental = by_name["fundamental"]
        assert fundamental["unit"] == "Hz"
        # The extractor measured a 55 Hz fundamental well inside 1 per cent.
        assert abs(fundamental["value"] - 55.0) <= 0.55
        assert 0.0 <= fundamental["confidence"] <= 1.0
        unknown = [item for item in features["measurements"] if item["value"] is None]
        assert unknown
        for item in unknown:
            assert item["unavailable_reason"]
            assert "confidence" not in item
        assert set(features["key"]) == {"tonic", "mode", "confidence", "unavailable_reason"}
        # Nothing derived, ranked or scored is added, and no path is present.
        text = reply.raw.decode("utf-8")
        for forbidden in ("similarity", "compatibility", "warning", "rank", "score",
                          "local_path", str(root), "\\"):
            assert forbidden not in text
    finally:
        running.stop()


def test_the_detail_route_reports_a_sample_without_a_current_analysis(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 2, directory=root / "synth", start=1)
    running = start_service(str(database))
    try:
        document = running.client.get(f"/library/samples/{ids[0]}").body["sample"]
        assert document["analysis"] == {"state": "absent", "analysis_version": None,
                                        "analyzed_at": None, "attempts": None,
                                        "error_code": None, "stored_versions": [],
                                        "stored_versions_truncated": False}
        assert document["features"] is None
        assert document["audio"] == {"sample_rate_hz": 48000, "channels": 1,
                                     "frame_count": 480, "duration_ms": 10.0}
        assert document["file_name"] == "synth-0001.wav"
    finally:
        running.stop()


def test_a_sample_with_a_failed_item_reports_its_code_and_attempts(tmp_path):
    root, database = _prepared(tmp_path)
    ids = insert_samples(database, 1, directory=root / "synth", start=1)
    with closing(connection(database)) as opened:
        queue.enqueue(opened, indexer.current_analysis_version())
        item = opened.execute("SELECT item_id FROM job_items WHERE sample_id = ?",
                              (ids[0],)).fetchone()
        with transaction(opened):
            queue.finalize(opened, item["item_id"], queue.ITEM_FAILED,
                           error=queue.error_record(queue.STAGE_DECODE,
                                                    "unsupported_channels", "synthetic"))
    running = start_service(str(database))
    try:
        analysis = running.client.get(f"/library/samples/{ids[0]}").body["sample"]["analysis"]
        assert analysis["state"] == "failed"
        assert analysis["error_code"] == "unsupported_channels"
        assert analysis["attempts"] == 0
        assert analysis["analyzed_at"] is None
    finally:
        running.stop()


def test_stored_versions_are_truncated_after_eight(tmp_path):
    root, database = _prepared(tmp_path)
    scan(root / "kicks", database, "kick")
    sample_id = _content_ids(database)[0]
    row_id = _row_id(database, sample_id)
    store_analysis(database, row_id)
    versions = [store_extra_version(database, row_id, {"synthetic": index})
                for index in range(9)]
    running = start_service(str(database))
    try:
        document = running.client.get(f"/library/samples/{sample_id}").body["sample"]
        every = sorted(versions + [indexer.current_analysis_version()])
        assert len(every) > library.MAX_STORED_VERSIONS
        assert document["analysis"]["stored_versions"] == every[:library.MAX_STORED_VERSIONS]
        assert document["analysis"]["stored_versions_truncated"] is True
        assert document["analysis"]["state"] == "current"
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# reads during writes
# ---------------------------------------------------------------------------


def test_reads_answer_while_an_import_writes_and_a_transaction_is_held(tmp_path, monkeypatch):
    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55, frames=24000)},
                                    "bass": {"bass-01.wav": tone(110, frames=24000)}})
    database = tmp_path / "library.sqlite3"
    real_extract = batch.extract
    started = threading.Event()
    release = threading.Event()

    def blocked(*args, **kwargs):
        started.set()
        release.wait(30)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(batch, "extract", blocked)
    running = start_service(str(database))
    try:
        reply = running.client.post("/imports", {"root": str(root / "kicks"), "role": "kick"})
        assert reply.status == 202
        assert started.wait(30), "the runner never reached the extractor"
        held = threading.Thread(target=_hold_write, args=(database, 2.0), daemon=True)
        held.start()
        time.sleep(0.2)
        for path in ("/library/samples", "/health"):
            before = time.monotonic()
            answer = running.client.get(path)
            elapsed = time.monotonic() - before
            assert answer.status == 200, answer.raw
            assert b"database is locked" not in answer.raw
            assert elapsed < 1.0, (path, elapsed)
        held.join(10)
        assert held.is_alive() is False
    finally:
        release.set()
        running.stop()
