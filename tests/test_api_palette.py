"""The palette and project routes (issue #32).

Four routes over a real socket against a temporary database built with #21's
schema and #24's operations: the first-run sequence, a kick select that is a
no-op the second time, a replace, every context state written and read back
exactly, a two-connection conflict, each documented refusal, the #27 policies
the new routes inherit, and the statement budget of the read.

Every id here is synthetic; no test names a real library path or audio byte.
"""

from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from backend.api import palette
from backend.api.service import MAX_REQUEST_BYTES
from backend.library.repository import LibraryRepository
from backend.palette.model import SONG_CONTEXT_ABSENT_REASON
from tests.api_client import connection, insert_samples, start_service

KICK = "sha256:" + "a" * 64
OTHER_KICK = "sha256:" + "b" * 64
BASS = "sha256:" + "c" * 64


@pytest.fixture
def service(tmp_path):
    """A running service over a migrated database with two kicks and a bass."""

    database = tmp_path / "library.sqlite3"
    with closing(connection(database)):
        pass
    insert_samples(database, 1, directory=tmp_path, role="kick")
    insert_samples(database, 1, directory=tmp_path, role="kick", start=2)
    insert_samples(database, 1, directory=tmp_path, role="bass", start=3)
    with closing(connection(database)) as opened:
        rows = opened.execute("SELECT sample_id, role FROM samples ORDER BY sample_id").fetchall()
    kicks = [row["sample_id"] for row in rows if row["role"] == "kick"]
    bass = [row["sample_id"] for row in rows if row["role"] == "bass"][0]
    running = start_service(str(database))
    try:
        yield running, database, kicks, bass
    finally:
        running.stop()


def create(running, name="Track A"):
    return running.client.call("POST", "/projects", body={"name": name})


def set_item(running, slot, sample_id, revision, palette_id):
    return running.client.call("PUT", f"/palette/items/{slot}", body={
        "palette_id": palette_id, "sample_id": sample_id, "expected_revision": revision})


def set_context(running, palette_id, revision, tempo, key, genre):
    return running.client.call("PUT", "/palette/context", body={
        "palette_id": palette_id, "expected_revision": revision,
        "tempo": tempo, "key": key, "genre": genre})


def test_the_first_run_sequence(service):
    running, _database, _kicks, _bass = service
    empty = running.client.get("/palette")
    assert empty.status == 404 and empty.code() == "unknown_project"
    assert empty.body["error"]["details"] == {"project_id": None}

    created = create(running)
    assert created.status == 201
    document = created.body["palette"]
    assert document["revision"] == 0
    assert document["items"] == {"kick": None, "bass": None}
    assert document["context"] == {"tempo": {"state": "unset"}, "key": {"state": "unset"},
                                   "genre": {"state": "unset"}}
    assert set(document) == {"palette_id", "project", "name", "revision", "context", "items"}
    assert set(document["project"]) == {"project_id", "name"}

    again = running.client.get("/palette")
    assert again.status == 200 and again.body["palette"]["revision"] == 0

    repeat = create(running)
    assert repeat.status == 409 and repeat.code() == "project_exists"
    assert set(repeat.body["error"]["details"]) == {"project_id"}


def test_a_kick_is_selected_then_the_same_selection_is_a_no_op(service):
    running, database, kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]

    first = set_item(running, "kick", kicks[0], 0, palette_id)
    assert first.status == 200 and first.body["changed"] is True
    assert first.body["palette"]["revision"] == 1
    assert first.body["palette"]["items"]["kick"]["sample_id"] == kicks[0]
    assert first.body["palette"]["items"]["kick"]["sample_state"] == "unknown"
    assert first.body["palette"]["items"]["kick"]["slot_role_mismatch"] is False

    second = set_item(running, "kick", kicks[0], 1, palette_id)
    assert second.status == 200 and second.body["changed"] is False
    assert second.body["palette"]["revision"] == 1

    with closing(connection(database)) as opened:
        active = opened.execute(
            "SELECT COUNT(*) FROM palette_items WHERE palette_id = ? AND slot = 'kick' "
            "AND removed_at IS NULL", (palette_id,)).fetchone()[0]
    assert active == 1


def test_a_replacement_advances_the_revision_and_keeps_one_active_kick(service):
    running, database, kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]
    set_item(running, "kick", kicks[0], 0, palette_id)

    replaced = set_item(running, "kick", kicks[1], 1, palette_id)
    assert replaced.status == 200 and replaced.body["changed"] is True
    assert replaced.body["palette"]["revision"] == 2
    assert replaced.body["palette"]["items"]["kick"]["sample_id"] == kicks[1]
    with closing(connection(database)) as opened:
        active = opened.execute(
            "SELECT COUNT(*) FROM palette_items WHERE palette_id = ? AND slot = 'kick' "
            "AND removed_at IS NULL", (palette_id,)).fetchone()[0]
    assert active == 1


def test_a_sample_of_the_wrong_role_is_refused_with_both_roles(service):
    running, _database, _kicks, bass = service
    palette_id = create(running).body["palette"]["palette_id"]
    refused = set_item(running, "kick", bass, 0, palette_id)
    assert refused.status == 409 and refused.code() == "role_mismatch"
    details = refused.body["error"]["details"]
    assert details == {"slot": "kick", "accepted_roles": ["kick"], "stored_role": "bass",
                       "sample_id": bass}


def test_the_documented_refusals(service):
    running, _database, _kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]

    unknown_sample = set_item(running, "kick", "sha256:" + "d" * 64, 0, palette_id)
    assert unknown_sample.status == 404 and unknown_sample.code() == "unknown_sample"
    assert set(unknown_sample.body["error"]["details"]) == {"sample_id"}

    unknown_slot = set_item(running, "snare", KICK, 0, palette_id)
    assert unknown_slot.status == 400 and unknown_slot.code() == "unknown_slot"
    assert unknown_slot.body["error"]["details"] == {"slot": "snare"}

    unknown_palette = set_item(running, "kick", KICK, 0, "palette-00000000000000000000000000000000")
    assert unknown_palette.status == 404 and unknown_palette.code() == "unknown_palette"

    unknown_project = running.client.get("/palette?project_id=project-00000000000000000000000000000000")
    assert unknown_project.status == 404 and unknown_project.code() == "unknown_project"
    assert unknown_project.body["error"]["details"]["project_id"] != None  # noqa: E711


def test_every_context_state_is_written_and_read_back_exactly(service):
    running, database, _kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]

    written = set_context(
        running, palette_id, 0,
        {"state": "known", "bpm": 140},
        {"state": "known", "tonic": "C", "mode": "major"},
        {"state": "unknown", "reason": "producer_marked_unknown"})
    assert written.status == 200, written.raw
    context = written.body["palette"]["context"]
    assert context["tempo"] == {"state": "known", "bpm": 140.0}
    assert context["key"] == {"state": "known", "tonic": "C", "mode": "major"}
    assert context["genre"] == {"state": "unknown", "reason": "producer_marked_unknown"}

    read = running.client.get("/palette").body["palette"]["context"]
    assert read == context

    with closing(connection(database)) as opened:
        record = LibraryRepository(opened).load_palette(palette_id)
    assert record.context_state.tempo == "known"
    assert record.song.tempo.confidence == palette.MANUAL_CONTEXT_CONFIDENCE
    assert record.song.key.confidence == palette.MANUAL_CONTEXT_CONFIDENCE
    assert record.context_state.genre == "unknown"
    assert record.song.genre_unavailable_reason == "producer_marked_unknown"

    cleared = set_context(running, palette_id, 1,
                          {"state": "unset"}, {"state": "unset"}, {"state": "unset"})
    assert cleared.body["palette"]["context"] == {"tempo": {"state": "unset"},
                                                 "key": {"state": "unset"},
                                                 "genre": {"state": "unset"}}
    with closing(connection(database)) as opened:
        record = LibraryRepository(opened).load_palette(palette_id)
    assert record.song.tempo.unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert record.song.key.unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert record.song.genre_unavailable_reason == SONG_CONTEXT_ABSENT_REASON
    assert record.context_state.tempo == "unset"


def test_an_explicitly_unknown_field_and_an_unset_one_differ_on_the_wire(service):
    running, _database, _kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]
    unknown = set_context(running, palette_id, 0, {"state": "unknown", "reason": "no_idea"},
                          {"state": "unset"}, {"state": "unset"}).body["palette"]["context"]
    unset = set_context(running, palette_id, 1, {"state": "unset"}, {"state": "unset"},
                        {"state": "unset"}).body["palette"]["context"]
    assert unknown["tempo"] == {"state": "unknown", "reason": "no_idea"}
    assert unset["tempo"] == {"state": "unset"}
    assert unknown != unset


def test_a_stale_revision_conflicts_and_writes_nothing(service):
    running, database, kicks, _bass = service
    palette_id = create(running).body["palette"]["palette_id"]
    set_item(running, "kick", kicks[0], 0, palette_id)

    # A second writer advances the palette behind this client's back.
    with closing(connection(database)) as opened:
        LibraryRepository(opened).set_palette_item(
            palette_id, "kick", kicks[1], expected_revision=1)

    conflicted = set_item(running, "kick", kicks[0], 1, palette_id)
    assert conflicted.status == 409 and conflicted.code() == "revision_conflict"
    assert conflicted.body["error"]["details"] == {"expected_revision": 1,
                                                   "current_revision": 2}


def test_the_policies_the_new_routes_inherit(service):
    running, _database, _kicks, _bass = service
    assert running.client.call("POST", "/palette", body={}).status == 405
    assert running.client.call("POST", "/palette", body={}).headers["allow"] == "GET"
    assert running.client.get("/palettes").status == 404
    assert running.client.call("PUT", "/palette/items/kick", body={}).status == 400
    huge = running.client.call("PUT", "/palette/context", body={}, headers={
        "Content-Length": str(MAX_REQUEST_BYTES + 1)})
    assert huge.status in (411, 413)


def test_the_read_issues_no_write_and_grows_only_by_the_item_enrichment(service):
    running, database, kicks, bass = service
    palette_id = create(running).body["palette"]["palette_id"]
    set_item(running, "kick", kicks[0], 0, palette_id)

    def statements():
        traced = []
        with closing(connection(database)) as opened:
            opened.set_trace_callback(traced.append)
            palette._read(opened, None)
            opened.set_trace_callback(None)
        return traced

    one = statements()
    # The route reads, and only reads.
    assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                   for statement in one)
    set_item(running, "bass", bass, 1, palette_id)
    two = statements()
    # The route's own statements are constant; each item costs exactly two more
    # because #24's `_item_record` enriches one item at a time with its sample's
    # state. Issue #172 owns batching that; this test pins today's behaviour so a
    # change there is visible here.
    assert len(two) == len(one) + 2


def test_no_palette_response_carries_a_path(service):
    running, _database, kicks, _bass = service
    document = create(running).body["palette"]
    set_item(running, "kick", kicks[0], 0, document["palette_id"])
    body = running.client.get("/palette").raw.decode("utf-8")
    for forbidden in ("C:", "\\\\", "/Users/", "local_path", ".wav"):
        assert forbidden not in body
