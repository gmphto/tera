"""Degraded palette reads: unavailable, unknown, pruned and role-changed samples (#24).

A palette item references a `sample_id` and nothing else, so a read has to
tolerate a row whose file is `missing` or `unknown`, a row that operator
pruning (#71) has removed, a sample whose stored role changed (#72) and an
analysis failure that #23's queue recorded. Every such read must return the
degraded record, write nothing and raise nothing; only a write validates the
reference.

Every database is a temporary file and every sample is a synthetic contract
fixture or a synthetic path row.
"""

from __future__ import annotations

import builtins
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import RecommendationBatch, SongContext
from backend.library import queue
from backend.library.errors import UnknownSample
from backend.library.repository import LibraryRepository, transaction
from backend.library.schema import open_database
from backend.palette.model import palette_hash


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def content_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def seed_library(repository: LibraryRepository, statuses=()):
    """The two contract samples, plus one row in each requested file state."""

    version = repository.register_analysis_version(batch.analysis_descriptor())
    kick, bass = RecommendationBatch.from_json(
        (FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples
    rows = {("kick-001", kick, "kick", "present"), ("bass-001", bass, "bass", "present")}
    rows.update((f"{role}-{status}-001", bass if role == "bass" else kick, role, status)
                for role, status in statuses)
    for sample_id, source, role, file_status in sorted(rows):
        sample = replace(source, sample_id=sample_id, role=role, analysis_version=version,
                         audio=replace(source.audio,
                                       local_path=f"C:/tera-fixtures/{sample_id}.wav"))
        repository.import_sample(sample, content_sha256=content_hash(sample_id),
                                 file_status=file_status)
    return version


@pytest.fixture
def library(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        yield repository
    finally:
        connection.close()


def palette_with(repository, rows, context=None):
    """A palette with one item per (slot, sample_id) pair; returns its id."""

    project = repository.create_project("Track A")
    palette_id = project.palette_id
    revision = 0
    for slot, sample_id in rows:
        repository.set_palette_item(palette_id, slot, sample_id, expected_revision=revision)
        revision += 1
    if context is not None:
        repository.set_palette_context(palette_id, context, expected_revision=revision)
    return palette_id


def palette_dump(connection, palette_id):
    return ([tuple(row) for row in connection.execute(
        "SELECT * FROM palettes WHERE palette_id = ?", (palette_id,))],
        [tuple(row) for row in connection.execute(
            "SELECT * FROM palette_items WHERE palette_id = ? ORDER BY item_id", (palette_id,))])


def item_for(record, slot):
    return next(item for item in record.active_items if item.slot == slot)


def read_any_way(record):
    """Every palette read a caller can make; none of them may raise."""

    return (palette_hash(record), record.to_context(), record.context_state, record.active_items)


# ---------------------------------------------------------------------------
# file availability
# ---------------------------------------------------------------------------


def test_a_missing_or_unknown_file_reads_its_state_and_keeps_the_item(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository, statuses=(("bass", "missing"), ("kick", "unknown")))
    try:
        palette_id = palette_with(repository, (("kick", "kick-unknown-001"),
                                               ("bass", "bass-missing-001")))
        record = repository.load_palette(palette_id)
        assert item_for(record, "kick").sample_state == "unknown"
        assert item_for(record, "bass").sample_state == "missing"
        assert [item.slot for item in record.active_items] == ["bass", "kick"]
        assert [item.sample_id for item in record.active_items] == ["bass-missing-001",
                                                                   "kick-unknown-001"]
        context = record.to_context()
        assert context.kick_id == "kick-unknown-001"
        assert context.selected_bass_id == "bass-missing-001"
        assert len(palette_hash(record)) == 64
        assert item_for(record, "kick").sample_error_code is None
    finally:
        connection.close()


def test_a_restored_file_reads_present_without_a_palette_write(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository, statuses=(("bass", "missing"),))
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"),
                                               ("bass", "bass-missing-001")))
        before_record = repository.load_palette(palette_id)
        before_rows = palette_dump(connection, palette_id)
        assert item_for(before_record, "bass").sample_state == "missing"
        # This is the call a later scan makes when the file returns.
        repository.mark_file_status("bass-missing-001", "present")
        after_record = repository.load_palette(palette_id)
        assert item_for(after_record, "bass").sample_state == "present"
        assert after_record.revision == before_record.revision
        assert palette_hash(after_record) == palette_hash(before_record)
        assert palette_dump(connection, palette_id) == before_rows
    finally:
        connection.close()


def test_setting_a_new_item_for_an_unavailable_file_succeeds(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository, statuses=(("bass", "missing"), ("kick", "unknown")))
    try:
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        mutation = repository.set_palette_item(palette_id, "kick", "kick-unknown-001",
                                               expected_revision=0)
        assert mutation.changed and mutation.active_item.sample_state == "unknown"
        mutation = repository.set_palette_item(palette_id, "bass", "bass-missing-001",
                                               expected_revision=1)
        assert mutation.changed and mutation.active_item.sample_state == "missing"
        record = repository.load_palette(palette_id)
        assert item_for(record, "bass").sample_state == "missing"
        assert read_any_way(record)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# operator pruning (#71)
# ---------------------------------------------------------------------------


def test_a_pruned_row_reads_removed_and_keeps_the_item(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"), ("bass", "bass-001")),
                                  context=SongContext.from_dict(
                                      {"tempo": {"name": "tempo", "value": 128.0, "unit": "BPM",
                                                 "confidence": 0.9},
                                       "key": {"tonic": "F#", "mode": "minor", "confidence": 0.72},
                                       "genre": "techno"}))
        before = repository.load_palette(palette_id)
        assert repository.delete_sample("bass-001") is True
        record = repository.load_palette(palette_id)
        assert item_for(record, "bass").sample_state == "removed"
        assert item_for(record, "bass").sample_id == "bass-001"
        assert item_for(record, "bass").slot == "bass"
        assert item_for(record, "bass").sample_error_code is None
        assert [item.slot for item in record.active_items] == ["bass", "kick"]
        assert record.revision == before.revision
        assert palette_hash(record) == palette_hash(before)
        assert record.to_context().selected_bass_id == "bass-001"
        assert [item.slot for item in repository.list_palettes(record.project_id)[0].active_items] \
            == ["bass", "kick"]
    finally:
        connection.close()


def test_a_pruned_row_does_not_cascade_the_palette_item_away(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"),))
        repository.delete_sample("kick-001")
        assert connection.execute("SELECT COUNT(*) FROM palette_items WHERE palette_id = ?",
                                  (palette_id,)).fetchone()[0] == 1
        record = repository.load_palette(palette_id)
        assert item_for(record, "kick").sample_state == "removed"
        assert record.to_context().kick_id == "kick-001"
    finally:
        connection.close()


def test_setting_a_pruned_sample_is_refused_and_writes_nothing(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"),))
        repository.delete_sample("bass-001")
        before = palette_dump(connection, palette_id)
        with pytest.raises(UnknownSample) as raised:
            repository.set_palette_item(palette_id, "bass", "bass-001", expected_revision=1)
        assert raised.value.code == "unknown_sample"
        assert palette_dump(connection, palette_id) == before
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# a stored role that changed after selection (#72)
# ---------------------------------------------------------------------------


def test_a_changed_role_is_flagged_and_never_rewritten(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"), ("bass", "bass-001")))
        before = repository.load_palette(palette_id)
        before_rows = palette_dump(connection, palette_id)
        assert item_for(before, "bass").slot_role_mismatch is False
        # Issue #72's edit, applied at the row: the palette stores the role the
        # item was selected with, so only the read can notice.
        connection.execute("UPDATE samples SET role = 'kick' WHERE sample_id = 'bass-001'")
        record = repository.load_palette(palette_id)
        item = item_for(record, "bass")
        assert item.slot_role_mismatch is True
        assert item.role == "kick"
        assert item.sample_id == "bass-001" and item.slot == "bass"
        assert record.revision == before.revision
        # The revision and the stored rows are untouched, but the hash follows
        # the role the record reports, so it now describes the changed role.
        assert palette_hash(record) != palette_hash(before)
        assert item_for(record, "kick").role == item_for(before, "kick").role == "kick"
        assert record.to_context().selected_bass_id == "bass-001"
        assert palette_dump(connection, palette_id) == before_rows
        assert connection.execute(
            "SELECT role FROM palette_items WHERE palette_id = ? AND slot = 'bass'",
            (palette_id,)).fetchone()[0] == "bass"
    finally:
        connection.close()


def test_a_role_that_still_matches_is_not_flagged(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    try:
        palette_id = palette_with(repository, (("bass", "bass-001"),))
        connection.execute("UPDATE samples SET role = 'sub-bass' WHERE sample_id = 'bass-001'")
        record = repository.load_palette(palette_id)
        item = item_for(record, "bass")
        assert item.slot_role_mismatch is False
        assert item.role == "sub-bass"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the analysis queue's stored error code
# ---------------------------------------------------------------------------


def test_a_stored_scan_error_code_is_reported(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    version = seed_library(repository)
    try:
        # #22 indexes a path before it is analysed and #23 queues the work, so
        # the failure is stored against the row's content identity.
        repository.insert_path_record(
            "C:/tera-fixtures/bass-recovery-001.wav", role="bass",
            content_sha256=content_hash("bass-recovery-001"), sample_rate_hz=48000, channels=1,
            frame_count=48000, duration_ms=1000.0, file_status="present",
            sample_id="bass-recovery-001")
        assert queue.enqueue(connection, version) == 1
        run_id = queue.open_run(connection, version, 1, 3)
        item = queue.claim(connection, run_id)
        with transaction(connection):
            queue.finalize(connection, item["item_id"], queue.ITEM_FAILED,
                           error=queue.error_record(queue.STAGE_READ, "not_found",
                                                    "synthetic probe failure"))
        palette_id = palette_with(repository, (("kick", "kick-001"),
                                               ("bass", "bass-recovery-001")))
        record = repository.load_palette(palette_id)
        assert item_for(record, "bass").sample_error_code == "not_found"
        assert item_for(record, "kick").sample_error_code is None
        assert item_for(record, "bass").sample_state == "present"
        assert len(palette_hash(record)) == 64
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# reads never write, and never open an audio file
# ---------------------------------------------------------------------------


def test_every_degraded_read_writes_nothing_and_raises_nothing(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository, statuses=(("bass", "missing"), ("kick", "unknown")))
    try:
        palette_id = palette_with(repository, (("bass", "bass-001"),
                                               ("kick", "kick-unknown-001")),
                                  context=SongContext.from_dict(
                                      {"tempo": {"name": "tempo", "value": None, "unit": "BPM",
                                                 "unavailable_reason": "no_tempo_estimate"},
                                       "key": {"tonic": None, "mode": None, "confidence": None,
                                               "unavailable_reason": "ambiguous_key"},
                                       "genre": None,
                                       "genre_unavailable_reason": "no_genre_metadata"}))
        repository.delete_sample("bass-001")
        # A role the kick slot does not accept: the read reports it and flags it.
        connection.execute("UPDATE samples SET role = 'bass' WHERE sample_id = 'kick-unknown-001'")
        before = palette_dump(connection, palette_id)
        record = repository.load_palette(palette_id)
        states = {item.sample_id: item.sample_state for item in record.active_items}
        assert states == {"bass-001": "removed", "kick-unknown-001": "unknown"}
        assert item_for(record, "kick").slot_role_mismatch is True
        reads = (lambda: repository.load_palette(palette_id),
                 lambda: repository.list_palettes(record.project_id),
                 lambda: record.to_context(),
                 lambda: palette_hash(record))
        for read in reads:
            result = read()
            assert result is not None
        assert palette_dump(connection, palette_id) == before
    finally:
        connection.close()


def test_a_palette_read_does_not_open_or_stat_a_file(tmp_path, monkeypatch):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository, statuses=(("bass", "missing"),))
    try:
        palette_id = palette_with(repository, (("kick", "kick-001"),
                                               ("bass", "bass-missing-001")))

        def refuse(*args, **kwargs):
            raise AssertionError("a palette read must not open or stat a file")

        monkeypatch.setattr(builtins, "open", refuse)
        monkeypatch.setattr("os.stat", refuse)
        monkeypatch.setattr("os.scandir", refuse)
        record = repository.load_palette(palette_id)
        assert record.active_items
        assert palette_hash(record)
        assert record.to_context()
        assert repository.list_palettes(record.project_id)
    finally:
        connection.close()
