"""The decision-cache migration (issue #26).

The cache tables are the next entry in #21's single migration chain, so these
tests cover the fresh database, the upgrade of a database that already holds
#21's, #22's, #23's and #24's rows, the refusals, a failing cache-column
migration and the JSON functions the payload CHECK depends on. Every path,
sample id, content hash and timestamp here is synthetic; no audio file is
opened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.library.errors import (DatabaseCorrupt, InvalidDatabasePath, MigrationFailed,
                                    NotADatabase, SchemaVersionNewer)
from backend.library.schema import (MIGRATIONS, SCHEMA_VERSION, migrate, open_database, verify)


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "_docs" / "decision-cache.md"

# The two tables this migration adds, with every column, transcribed by hand.
CACHE_COLUMNS = {
    "decision_cache": ("cache_key", "cache_key_version", "decision_kind", "source", "interface_name",
                       "adapter_version", "prompt_version", "model_version", "palette_hash",
                       "palette_hash_version", "candidate_id", "candidate_content_fingerprint",
                       "candidate_analysis_version", "dimension", "question_id", "kick_id",
                       "kick_content_fingerprint", "kick_analysis_version", "questions_digest",
                       "ranking_version", "weight_table_id", "baseline_ranking_version",
                       "baseline_weight_table_id", "payload_json", "created_at"),
    "decision_model_versions": ("interface_name", "source", "adapter_version", "prompt_version",
                                "model_version", "first_observed_at", "observed_at",
                                "observation_count"),
}
CACHE_INDEXES = ("idx_decision_cache_created", "idx_decision_cache_candidate")

# The eleven user tables a version-3 database holds, in the order the landed
# storage document lists them.
V3_TABLES = ("analysis_versions", "sample_features", "sample_keys", "sample_packs", "sample_tags",
             "samples", "job_items", "job_runs", "palette_items", "palettes", "projects")
V4_TABLES = tuple(sorted(V3_TABLES + ("decision_cache", "decision_model_versions")))

ANALYSIS_VERSION = "0" * 64
CREATED_AT = "2024-01-01T00:00:00Z"
SYNTHETIC_PATH = "C:/tera-fixtures/cache-upgrade-001.wav"


def user_tables(connection):
    return tuple(sorted(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()))


def ordered_dump(connection, tables):
    return tuple(tuple(sorted(tuple(row) for row in connection.execute("SELECT * FROM " + table)))
                 for table in tables)


def seed_v3(connection):
    """One row in each of the eleven version-3 tables, with synthetic values."""

    connection.execute("INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                       "VALUES (?, ?, ?)", (ANALYSIS_VERSION, "{}", CREATED_AT))
    connection.execute("INSERT INTO sample_packs (pack_id, name, vendor, created_at) "
                       "VALUES ('pack-001', 'Synthetic Pack', NULL, ?)", (CREATED_AT,))
    connection.execute(
        "INSERT INTO samples (sample_id, schema_version, content_sha256, role, original_path, "
        "path_key, filename, pack_id, file_status, sample_rate_hz, channels, frame_count, "
        "duration_ms, imported_at, updated_at) "
        "VALUES ('sample-001', '1.0', ?, 'kick', ?, ?, 'cache-upgrade-001.wav', 'pack-001', "
        "'unknown', 48000, 1, 24000, 500.0, ?, ?)",
        ("b" * 64, SYNTHETIC_PATH, SYNTHETIC_PATH.lower(), CREATED_AT, CREATED_AT))
    connection.execute(
        "INSERT INTO sample_features (sample_id, analysis_version, measurement, unit, value, "
        "unavailable_reason, confidence) "
        "VALUES ('sample-001', ?, 'rms', 'linear', 0.5, NULL, NULL)", (ANALYSIS_VERSION,))
    connection.execute(
        "INSERT INTO sample_keys (sample_id, analysis_version, tonic, mode, confidence, "
        "unavailable_reason) VALUES ('sample-001', ?, 'C', 'major', 0.9, NULL)",
        (ANALYSIS_VERSION,))
    connection.execute("INSERT INTO sample_tags (sample_id, tag, added_at) "
                       "VALUES ('sample-001', 'kick', ?)", (CREATED_AT,))
    connection.execute(
        "INSERT INTO job_runs (run_id, state, analysis_version, workers, max_attempts, "
        "owner_token, heartbeat_at, started_at, finished_at, cancel_requested) "
        "VALUES ('run-001', 'complete', ?, 1, 1, 'owner-001', ?, ?, ?, 0)",
        (ANALYSIS_VERSION, CREATED_AT, CREATED_AT, CREATED_AT))
    connection.execute(
        "INSERT INTO job_items (item_id, sample_id, analysis_version, path, role, state, "
        "disposition, attempts, run_id, claimed_at, finished_at, error_stage, error_code, "
        "error_message, created_at) "
        "VALUES (1, ?, ?, ?, 'kick', 'complete', 'analyzed', 1, 'run-001', ?, ?, NULL, NULL, "
        "NULL, ?)",
        ("sha256:" + "b" * 64, ANALYSIS_VERSION, SYNTHETIC_PATH, CREATED_AT, CREATED_AT,
         CREATED_AT))
    connection.execute("INSERT INTO projects (project_id, name, created_at, updated_at) "
                       "VALUES ('project-001', 'Track A', ?, ?)", (CREATED_AT, CREATED_AT))
    connection.execute(
        "INSERT INTO palettes (palette_id, project_id, name, revision, created_at, updated_at) "
        "VALUES ('palette-001', 'project-001', 'Main', 0, ?, ?)", (CREATED_AT, CREATED_AT))
    connection.execute(
        "INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, added_revision, "
        "added_at, removed_revision, removed_at) "
        "VALUES ('item-001', 'palette-001', 'kick', 'sample-001', 'kick', 1, ?, NULL, NULL)",
        (CREATED_AT,))


def version_three_database(path):
    """A committed version-3 database holding one row in each of its tables."""

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert migrate(connection, MIGRATIONS[:3]) == 3
        seed_v3(connection)
    finally:
        connection.close()
    return path


def test_the_chain_has_one_entry_per_migration_and_the_cache_is_the_last(tmp_path):
    assert [version for version, _script in MIGRATIONS] == [1, 2, 3, 4]
    assert SCHEMA_VERSION == 4
    assert MIGRATIONS[-1][0] == SCHEMA_VERSION
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == V4_TABLES
        assert len(V4_TABLES) == 13
    finally:
        connection.close()


def test_the_two_cache_tables_carry_every_documented_column_and_index(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        for table, expected in CACHE_COLUMNS.items():
            names = tuple(row[1] for row in connection.execute("PRAGMA table_info(%s)" % table))
            assert names == expected, table
            for column in connection.execute("PRAGMA table_info(%s)" % table):
                assert (column[2] or "").upper() != "BLOB", (table, column[1])
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert set(CACHE_INDEXES) <= indexes
        assert verify(connection) == ()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_list(decision_cache)").fetchall() == []
        assert connection.execute("PRAGMA foreign_key_list(decision_model_versions)").fetchall() \
            == []
        runs = connection.execute("PRAGMA index_info(idx_decision_cache_created)").fetchall()
        assert [row[2] for row in runs] == ["created_at", "cache_key"]
        candidate = connection.execute("PRAGMA index_info(idx_decision_cache_candidate)").fetchall()
        assert [row[2] for row in candidate] == ["candidate_id"]
    finally:
        connection.close()


def test_the_json_functions_the_payload_check_depends_on_are_present(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        valid, kind = connection.execute("SELECT json_valid('{}'), json_type('{}')").fetchone()
        assert (valid, kind) == (1, "object")
        # The bundled SQLite is recorded here: the payload CHECK needs both functions.
        assert sqlite3.sqlite_version_info >= (3, 38), sqlite3.sqlite_version
        assert tuple(int(part) for part in sqlite3.sqlite_version.split(".")) >= (3, 38)
    finally:
        connection.close()


def test_the_payload_check_rejects_a_non_object(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        values = {"cache_key": "0" * 64, "cache_key_version": "decision-cache-v1",
                  "decision_kind": "judgment", "source": "double",
                  "interface_name": "jev-contract-double", "adapter_version": "jev-adapter-v1",
                  "prompt_version": "jev-questions-v1", "model_version": "synthetic-model-1",
                  "palette_hash": "a" * 64, "palette_hash_version": "palette-hash-v1",
                  "candidate_id": "bass-001", "candidate_content_fingerprint": "b" * 64,
                  "candidate_analysis_version": "dsp-fixture-1", "dimension": "frequency",
                  "question_id": "q-" + "0" * 64, "kick_id": None,
                  "kick_content_fingerprint": None, "kick_analysis_version": None,
                  "questions_digest": None, "ranking_version": None, "weight_table_id": None,
                  "baseline_ranking_version": None, "baseline_weight_table_id": None,
                  "created_at": CREATED_AT}
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        for payload in ("not json at all", "[1, 2]", "null", "7"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO decision_cache (%s, payload_json) VALUES (%s, ?)"
                    % (names, marks), (*values.values(), payload))
        connection.execute("INSERT INTO decision_cache (%s, payload_json) VALUES (%s, '{}')"
                           % (names, marks), tuple(values.values()))
        assert connection.execute("SELECT COUNT(*) FROM decision_cache").fetchone()[0] == 1
    finally:
        connection.close()


def test_upgrading_a_version_three_database_keeps_every_row(tmp_path):
    path = version_three_database(tmp_path / "library.sqlite3")
    before = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert user_tables(before) == tuple(sorted(V3_TABLES))
        assert before.execute("PRAGMA user_version").fetchone()[0] == 3
        rows_before = ordered_dump(before, V3_TABLES)
        assert any(len(table) > 0 for table in rows_before)
    finally:
        before.close()
    connection = open_database(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == V4_TABLES
        assert ordered_dump(connection, V3_TABLES) == rows_before
        assert ordered_dump(connection, tuple(CACHE_COLUMNS)) == ((), ())
        assert verify(connection) == ()
    finally:
        connection.close()


def test_opening_twice_changes_neither_the_version_nor_a_row(tmp_path):
    path = version_three_database(tmp_path / "library.sqlite3")
    first = open_database(path)
    try:
        snapshot = (first.execute("PRAGMA user_version").fetchone()[0],
                    user_tables(first), ordered_dump(first, V4_TABLES))
    finally:
        first.close()
    second = open_database(path)
    try:
        assert (second.execute("PRAGMA user_version").fetchone()[0],
                user_tables(second), ordered_dump(second, V4_TABLES)) == snapshot
        assert migrate(second) == SCHEMA_VERSION
    finally:
        second.close()


def test_a_non_database_file_is_refused_with_its_bytes_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    path.write_bytes(b"not a sqlite database at all")
    original = path.read_bytes()
    with pytest.raises(NotADatabase) as caught:
        open_database(path)
    assert caught.value.code == "not_a_database"
    assert path.read_bytes() == original


def test_a_corrupt_database_is_refused_with_its_bytes_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    path.write_bytes(b"SQLite format 3\x00" + b"synthetic corruption" * 64)
    original = path.read_bytes()
    with pytest.raises(DatabaseCorrupt) as caught:
        open_database(path)
    assert caught.value.code == "database_corrupt"
    assert path.read_bytes() == original


def test_a_newer_schema_is_refused_with_its_bytes_unchanged(tmp_path):
    path = version_three_database(tmp_path / "library.sqlite3")
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 1))
    finally:
        connection.close()
    original = path.read_bytes()
    with pytest.raises(SchemaVersionNewer) as caught:
        open_database(path)
    assert caught.value.code == "schema_version_newer"
    assert path.read_bytes() == original
    check = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert user_tables(check) == tuple(sorted(V3_TABLES))
    finally:
        check.close()


def test_a_failing_cache_column_migration_rolls_back_completely(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_database(path)
    try:
        before = ordered_dump(connection, V4_TABLES)
        script = ("ALTER TABLE decision_cache ADD COLUMN notes TEXT;\n"
                  "SELECT no_such_function();")
        with pytest.raises(MigrationFailed) as caught:
            migrate(connection, MIGRATIONS + ((SCHEMA_VERSION + 1, script),))
        assert caught.value.code == "migration_failed"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == V4_TABLES
        assert ordered_dump(connection, V4_TABLES) == before
        columns = {row[1] for row in connection.execute("PRAGMA table_info(decision_cache)")}
        assert "notes" not in columns
        assert verify(connection) == ()
    finally:
        connection.close()


def test_the_cache_migration_adds_no_column_to_a_landed_table(tmp_path):
    path = version_three_database(tmp_path / "library.sqlite3")
    columns_before = {}
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        for table in V3_TABLES:
            columns_before[table] = tuple(row[1] for row in
                                          connection.execute("PRAGMA table_info(%s)" % table))
    finally:
        connection.close()
    upgraded = open_database(path)
    try:
        for table, expected in columns_before.items():
            actual = tuple(row[1] for row in upgraded.execute("PRAGMA table_info(%s)" % table))
            assert actual == expected, table
    finally:
        upgraded.close()


def test_the_document_names_the_migration_and_the_table_set():
    text = DOCUMENT.read_text(encoding="utf-8")
    assert "migration 4" in text or "Migration 4" in text
    assert "SCHEMA_VERSION" in text and str(SCHEMA_VERSION) in text
    for table in CACHE_COLUMNS:
        assert table in text
    for index in CACHE_INDEXES:
        assert index in text
    assert "no foreign key" in text
