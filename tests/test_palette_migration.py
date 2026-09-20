"""Migration 3: the project, palette and palette-item tables (issue #24).

The palette migration is the next entry in #21's `MIGRATIONS` chain, so these
tests check the fresh database, the upgrade from a version-2 database that
already holds library and queue rows, idempotent reopen, the three refusals with
bytes and rows unchanged, a failed palette-column migration that rolls back, and
a further upgrade step of the same chain after a palette exists.

Every database is a temporary file; the referenced samples are the synthetic
contract fixtures and every content hash is the SHA-256 of a synthetic label.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import RecommendationBatch, SongContext
from backend.library import queue
from backend.library.errors import (
    DatabaseCorrupt,
    MigrationFailed,
    NotADatabase,
    SchemaVersionNewer,
)
from backend.library.repository import LibraryRepository
from backend.library.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    migrate,
    open_database,
    utc_now,
    verify,
)
from backend.palette.model import PaletteContextState, palette_hash


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"
CASES_PATH = Path(__file__).parent / "fixtures" / "palette" / "palette-cases.json"

USER_TABLE_QUERY = ("SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")
USER_TABLES = ("analysis_versions", "decision_cache", "decision_model_versions", "job_items",
               "job_runs", "palette_items", "palettes", "projects", "recommendation_outcomes",
               "sample_features", "sample_keys", "sample_packs", "sample_tags", "samples")
# The version-1 and version-2 tables, in dump order.
LIBRARY_TABLES = ("analysis_versions", "job_runs", "job_items", "sample_packs", "samples",
                  "sample_features", "sample_keys", "sample_tags")
PALETTE_COLUMNS = {
    "projects": ("project_id", "name", "created_at", "updated_at"),
    "palettes": ("palette_id", "project_id", "name", "revision", "tempo_bpm", "tempo_confidence",
                 "tempo_unavailable_reason", "key_tonic", "key_mode", "key_confidence",
                 "key_unavailable_reason", "genre", "genre_unavailable_reason", "created_at",
                 "updated_at"),
    "palette_items": ("item_id", "palette_id", "slot", "sample_id", "role", "added_revision",
                      "added_at", "removed_revision", "removed_at"),
}
UPGRADE = MIGRATIONS + ((SCHEMA_VERSION + 1, "ALTER TABLE projects ADD COLUMN notes TEXT"),)
CONTEXT = SongContext.from_dict({
    "tempo": {"name": "tempo", "value": 128.0, "unit": "BPM", "confidence": 0.9},
    "key": {"tonic": "F#", "mode": "minor", "confidence": 0.72},
    "genre": "techno"})


def content_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def seed_library(repository: LibraryRepository) -> str:
    """Two analysed samples plus a second kick, all synthetic."""

    version = repository.register_analysis_version(batch.analysis_descriptor())
    kick, bass = RecommendationBatch.from_json(
        (FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples
    for sample_id, source, role in (("kick-001", kick, "kick"), ("kick-002", kick, "kick"),
                                    ("bass-001", bass, "bass")):
        sample = replace(source, sample_id=sample_id, role=role, analysis_version=version,
                         audio=replace(source.audio,
                                       local_path=f"C:/tera-fixtures/{sample_id}.wav"))
        repository.import_sample(sample, content_sha256=content_hash(sample_id),
                                 file_status="present")
    return version


def build_palette(repository: LibraryRepository):
    """A palette with an active kick, a selected bass, a known context and a removed item."""

    project = repository.create_project("Track A")
    palette_id = project.palette_id
    repository.set_palette_item(palette_id, "kick", "kick-001", expected_revision=0)
    repository.set_palette_item(palette_id, "bass", "bass-001", expected_revision=1)
    repository.set_palette_item(palette_id, "kick", "kick-002", expected_revision=2)
    repository.set_palette_context(palette_id, CONTEXT, expected_revision=3)
    return project


def rows(connection, table):
    return tuple(sorted(tuple(row) for row in connection.execute(f"SELECT * FROM {table}")))


def library_rows(connection):
    return tuple(rows(connection, table) for table in LIBRARY_TABLES)


def palette_rows(connection):
    return tuple(rows(connection, table) for table in PALETTE_COLUMNS)


def user_tables(connection):
    return tuple(row[0] for row in connection.execute(USER_TABLE_QUERY))


def columns(connection, table):
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))


def snapshot(connection):
    return (connection.execute("PRAGMA user_version").fetchone()[0], user_tables(connection),
            library_rows(connection), palette_rows(connection))


def inspect(path):
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return snapshot(connection)
    finally:
        connection.close()


def stored_palette_rows(path):
    """The palette tables as stored, read without touching a damaged page."""

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return tuple(rows(connection, table) for table in PALETTE_COLUMNS)
    finally:
        connection.close()


def close_and_checkpoint(connection):
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()


def journal_mode(path):
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


def set_journal_mode(path, mode):
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return connection.execute(f"PRAGMA journal_mode = {mode}").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the fresh database
# ---------------------------------------------------------------------------


def test_a_fresh_database_reaches_the_current_version_with_the_palette_tables(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        assert SCHEMA_VERSION == 5
        assert [version for version, _script in MIGRATIONS] == [1, 2, 3, 4, 5]
        assert MIGRATIONS[-1][0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == USER_TABLES
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert verify(connection) == ()
        for table, expected in PALETTE_COLUMNS.items():
            assert columns(connection, table) == expected
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"ux_palette_items_active_slot", "idx_palette_items_sample"} <= indexes
        for table in PALETTE_COLUMNS:
            for column in connection.execute(f"PRAGMA table_info({table})"):
                assert (column[2] or "").upper() != "BLOB", (table, column[1])
    finally:
        connection.close()


def test_palette_items_reference_palettes_but_never_samples(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        # foreign_key_list columns are (id, seq, table, from, to, on_update, on_delete, match).
        references = {(row[2], row[3], row[6]) for row in
                      connection.execute("PRAGMA foreign_key_list(palette_items)")}
        assert references == {("palettes", "palette_id", "CASCADE")}
        project_references = {(row[2], row[3], row[6]) for row in
                              connection.execute("PRAGMA foreign_key_list(palettes)")}
        assert project_references == {("projects", "project_id", "CASCADE")}
    finally:
        connection.close()


def test_the_palette_checks_reject_what_the_specification_forbids(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    try:
        project = repository.create_project("Track A")
        palette_id = project.palette_id
        now = utc_now()
        insert = (
            "INSERT INTO palettes (palette_id, project_id, name, revision, tempo_bpm, "
            "tempo_confidence, tempo_unavailable_reason, key_tonic, key_mode, key_confidence, "
            "key_unavailable_reason, genre, genre_unavailable_reason, created_at, updated_at) "
            "VALUES (?, ?, 'Main', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        blank = {"revision": 0, "tempo_bpm": None, "tempo_confidence": None,
                 "tempo_unavailable_reason": None, "key_tonic": None, "key_mode": None,
                 "key_confidence": None, "key_unavailable_reason": None, "genre": None,
                 "genre_unavailable_reason": None}
        # A negative revision, a non-positive tempo, a tempo value without its
        # confidence, a blank reason, an unknown tonic, an incomplete key, an
        # out-of-range confidence and a genre that also carries a reason are all
        # refused by the table itself.
        probes = (
            {"revision": -1},
            {"tempo_bpm": 0.0, "tempo_confidence": 0.5},
            {"tempo_bpm": 120.0},
            {"tempo_unavailable_reason": "   "},
            {"key_tonic": "H", "key_mode": "minor", "key_confidence": 0.5},
            {"key_tonic": "C", "key_confidence": 0.5},
            {"key_confidence": 1.5},
            {"genre": "techno", "genre_unavailable_reason": "also_a_reason"},
        )
        for index, overrides in enumerate(probes):
            fields = {**blank, **overrides}
            # The probe palette needs a project of its own, because one project
            # owns exactly one palette.
            connection.execute(
                "INSERT INTO projects (project_id, name, created_at, updated_at) "
                "VALUES (?, 'Probe', ?, ?)", (f"project-probe-{index}", now, now))
            parameters = (f"palette-probe-{index}", f"project-probe-{index}", fields["revision"],
                          fields["tempo_bpm"], fields["tempo_confidence"],
                          fields["tempo_unavailable_reason"], fields["key_tonic"],
                          fields["key_mode"], fields["key_confidence"],
                          fields["key_unavailable_reason"], fields["genre"],
                          fields["genre_unavailable_reason"], now, now)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, parameters)
        assert repository.load_palette(palette_id).revision == 0
        item = ("INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, "
                "added_revision, added_at, removed_revision, removed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        for index, overrides in enumerate((
                {"slot": "snare"}, {"role": "snare"}, {"sample_id": "   "},
                {"added_revision": 2, "removed_revision": 1, "removed_at": now},
                {"removed_revision": 1})):
            fields = {"slot": "kick", "sample_id": "kick-001", "role": "kick",
                      "added_revision": 0, "removed_revision": None, "removed_at": None}
            fields.update(overrides)
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(item, (f"item-probe-{index}", palette_id, fields["slot"],
                                          fields["sample_id"], fields["role"],
                                          fields["added_revision"], now,
                                          fields["removed_revision"], fields["removed_at"]))
        assert connection.execute("SELECT COUNT(*) FROM palette_items").fetchone()[0] == 0
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the upgrade
# ---------------------------------------------------------------------------


def test_an_upgrade_adds_the_palette_tables_without_touching_a_library_row(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert migrate(connection, MIGRATIONS[:2]) == 2
        repository = LibraryRepository(connection)
        version = seed_library(repository)
        # A path row with no analysis is #22's pending work; #23's queue records
        # it as an item so the upgrade has a queue row to preserve too.
        repository.insert_path_record(
            "C:/tera-fixtures/pending-001.wav", role="bass",
            content_sha256=content_hash("pending-001"), sample_rate_hz=48000, channels=1,
            frame_count=48000, duration_ms=1000.0, file_status="present",
            sample_id="pending-001")
        assert queue.enqueue(connection, version) == 1
        queue.open_run(connection, version, 1, 3)
        before = (connection.execute("PRAGMA user_version").fetchone()[0], library_rows(connection))
        assert before[0] == 2
        assert any(row for row in before[1])
    finally:
        connection.close()

    connection = open_database(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (2, library_rows(connection)) == before
        assert palette_rows(connection) == ((), (), ())
        assert connection.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 1
        assert verify(connection) == ()
    finally:
        connection.close()


def test_open_database_twice_changes_neither_the_version_nor_a_row(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    seed_library(LibraryRepository(connection))
    project = build_palette(LibraryRepository(connection))
    before = snapshot(connection)
    close_and_checkpoint(connection)

    connection = open_database(path)
    try:
        assert snapshot(connection) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert LibraryRepository(connection).load_palette(project.palette_id).revision == 4
    finally:
        connection.close()
    connection = open_database(path)
    try:
        assert snapshot(connection) == before
    finally:
        connection.close()


def test_a_palette_survives_a_further_upgrade_step_of_the_same_chain(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = build_palette(repository)
    palette_id = project.palette_id
    before = repository.load_palette(palette_id)
    before_hash = palette_hash(before)
    assert before.revision == 4
    assert [item.sample_id for item in before.active_items] == ["bass-001", "kick-002"]
    assert [item.sample_id for item in before.removed_items] == ["kick-001"]
    assert migrate(connection, UPGRADE) == SCHEMA_VERSION + 1
    after = repository.load_palette(palette_id)
    connection.close()

    assert after == before
    assert after.context_state == PaletteContextState("known", "known", "known")
    assert after.song.to_dict() == CONTEXT.to_dict()
    assert [item.removed_revision for item in after.removed_items] == [3]
    assert palette_hash(after) == before_hash

    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        assert migrate(connection, UPGRADE) == SCHEMA_VERSION + 1
        again = LibraryRepository(connection).load_palette(palette_id)
        assert again == after
        assert palette_hash(again) == before_hash
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------


def test_a_non_database_file_is_refused_with_its_bytes_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    payload = b"this is not a database"
    path.write_bytes(payload)
    with pytest.raises(NotADatabase) as raised:
        open_database(path)
    assert raised.value.code == "not_a_database"
    assert path.read_bytes() == payload


def test_a_corrupt_database_is_refused_with_its_palette_rows_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = build_palette(repository)
    connection.executemany("INSERT INTO projects (project_id, name, created_at, updated_at) "
                           "VALUES (?, 'Filler', ?, ?)",
                           [(f"project-filler-{index}", utc_now(), utc_now())
                            for index in range(40)])
    before = snapshot(connection)
    close_and_checkpoint(connection)

    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    assert page_size > 0 and len(raw) > page_size * 2
    raw[page_size] = 0xFF  # Break the type byte of the second b-tree page.
    path.write_bytes(raw)

    with pytest.raises(DatabaseCorrupt) as raised:
        open_database(path)
    assert raised.value.code == "database_corrupt"
    assert path.read_bytes() == bytes(raw)
    # The damaged page belongs to another table, so the palette rows are still
    # exactly what the migration and the repository wrote.
    assert stored_palette_rows(path) == before[3]


def test_a_newer_schema_version_is_refused_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = build_palette(repository)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = snapshot(connection)
    close_and_checkpoint(connection)
    raw = path.read_bytes()

    with pytest.raises(SchemaVersionNewer) as raised:
        open_database(path)
    assert raised.value.code == "schema_version_newer"
    assert inspect(path) == before
    assert path.read_bytes() == raw  # Already in WAL: the refusal writes nothing.

    # A database that is not in WAL is refused before the pragmas switch the
    # journal mode, so its bytes are untouched as well.
    assert set_journal_mode(path, "delete") == "delete"
    raw = path.read_bytes()
    with pytest.raises(SchemaVersionNewer) as raised:
        open_database(path)
    assert raised.value.code == "schema_version_newer"
    assert journal_mode(path) == "delete"
    assert path.read_bytes() == raw
    assert inspect(path) == before


# ---------------------------------------------------------------------------
# the failed migration
# ---------------------------------------------------------------------------


def test_a_failing_palette_column_migration_rolls_back(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = build_palette(repository)
    try:
        before = snapshot(connection)
        broken = MIGRATIONS + ((SCHEMA_VERSION + 1,
                                "ALTER TABLE palettes ADD COLUMN notes TEXT; NOT VALID SQL;"),)
        with pytest.raises(MigrationFailed) as raised:
            migrate(connection, broken)
        assert raised.value.code == "migration_failed"
        assert isinstance(raised.value.__cause__, sqlite3.Error)
        assert snapshot(connection) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "notes" not in columns(connection, "palettes")
        assert repository.load_palette(project.palette_id).revision == 4
    finally:
        connection.close()


def test_a_failing_palette_table_migration_leaves_no_table_behind(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        before = snapshot(connection)
        broken = MIGRATIONS + ((SCHEMA_VERSION + 1,
                                "CREATE TABLE palette_notes (note TEXT); NOT VALID SQL;"),)
        with pytest.raises(MigrationFailed) as raised:
            migrate(connection, broken)
        assert raised.value.code == "migration_failed"
        assert "palette_notes" not in user_tables(connection)
        assert snapshot(connection) == before
    finally:
        connection.close()


def test_the_palette_migration_is_not_applied_twice(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    seed_library(repository)
    project = build_palette(repository)
    try:
        before = snapshot(connection)
        assert migrate(connection) == SCHEMA_VERSION
        assert migrate(connection) == SCHEMA_VERSION
        assert snapshot(connection) == before
        assert repository.load_palette(project.palette_id).revision == 4
    finally:
        connection.close()
