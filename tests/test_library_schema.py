"""Schema, migration and connection policy for the local sample library (#21).

Every database lives in `tmp_path`; nothing here writes inside the repository
tree and no path in this file names a real library.
"""

import ast
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from backend.library.errors import (
    DatabaseCorrupt,
    InvalidDatabasePath,
    MigrationFailed,
    NotADatabase,
    SchemaVersionNewer,
)
from backend.library.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    default_database_path,
    migrate,
    open_database,
    utc_now,
    verify,
)


USER_TABLE_QUERY = (
    "SELECT name FROM sqlite_master WHERE type = 'table' "
    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
)

USER_TABLES = (
    "analysis_versions",
    "decision_cache",
    "decision_model_versions",
    "job_items",
    "job_runs",
    "palette_items",
    "palettes",
    "projects",
    "sample_features",
    "sample_keys",
    "sample_packs",
    "sample_tags",
    "samples",
)

COLUMNS = {
    "analysis_versions": ("analysis_version", "descriptor", "created_at"),
    "decision_cache": ("cache_key", "cache_key_version", "decision_kind", "source",
                       "interface_name", "adapter_version", "prompt_version", "model_version",
                       "palette_hash", "palette_hash_version", "candidate_id",
                       "candidate_content_fingerprint", "candidate_analysis_version", "dimension",
                       "question_id", "kick_id", "kick_content_fingerprint",
                       "kick_analysis_version", "questions_digest", "ranking_version",
                       "weight_table_id", "baseline_ranking_version",
                       "baseline_weight_table_id", "payload_json", "created_at"),
    "decision_model_versions": ("interface_name", "source", "adapter_version", "prompt_version",
                                "model_version", "first_observed_at", "observed_at",
                                "observation_count"),
    "job_runs": ("run_id", "state", "analysis_version", "workers", "max_attempts", "owner_token",
                 "heartbeat_at", "started_at", "finished_at", "cancel_requested"),
    "job_items": ("item_id", "sample_id", "analysis_version", "path", "role", "state",
                  "disposition", "attempts", "run_id", "claimed_at", "finished_at", "error_stage",
                  "error_code", "error_message", "created_at"),
    "projects": ("project_id", "name", "created_at", "updated_at"),
    "palettes": ("palette_id", "project_id", "name", "revision", "tempo_bpm", "tempo_confidence",
                 "tempo_unavailable_reason", "key_tonic", "key_mode", "key_confidence",
                 "key_unavailable_reason", "genre", "genre_unavailable_reason", "created_at",
                 "updated_at"),
    "palette_items": ("item_id", "palette_id", "slot", "sample_id", "role", "added_revision",
                      "added_at", "removed_revision", "removed_at"),
    "sample_packs": ("pack_id", "name", "vendor", "created_at"),
    "samples": ("sample_id", "schema_version", "content_sha256", "role", "original_path",
                "path_key", "filename", "pack_id", "file_status", "sample_rate_hz", "channels",
                "frame_count", "duration_ms", "imported_at", "updated_at"),
    "sample_features": ("sample_id", "analysis_version", "measurement", "unit", "value",
                        "unavailable_reason", "confidence"),
    "sample_keys": ("sample_id", "analysis_version", "tonic", "mode", "confidence",
                    "unavailable_reason"),
    "sample_tags": ("sample_id", "tag", "added_at"),
}

ANALYSIS_VERSION = "0" * 64
CREATED_AT = "2024-01-01T00:00:00Z"
SYNTHETIC_PATH = "C:/tera-fixtures/sample-001.wav"
UPGRADE = MIGRATIONS + ((SCHEMA_VERSION + 1, "ALTER TABLE samples ADD COLUMN notes TEXT"),)


def open_library(path):
    return open_database(path)


def user_tables(connection):
    return tuple(row[0] for row in connection.execute(USER_TABLE_QUERY).fetchall())


def dump(connection, table):
    names = ", ".join(COLUMNS[table])
    rows = connection.execute(f"SELECT {names} FROM {table}").fetchall()
    return sorted(tuple(row) for row in rows)


def rows(connection):
    return tuple(dump(connection, table) for table in USER_TABLES)


def snapshot(connection):
    return (connection.execute("PRAGMA user_version").fetchone()[0],
            user_tables(connection), rows(connection))


def inspect(path):
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        return snapshot(connection)
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


def seed(connection):
    connection.execute(
        "INSERT INTO sample_packs (pack_id, name, vendor, created_at) VALUES (?, ?, ?, ?)",
        ("pack-001", "Test Pack One", None, CREATED_AT))
    connection.execute(
        "INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
        "VALUES (?, ?, ?)", (ANALYSIS_VERSION, "{}", CREATED_AT))


def insert_sample(connection, sample_id):
    """One sample with its pack link, a feature row, a key row and a tag."""

    connection.execute(
        "INSERT INTO samples (sample_id, schema_version, content_sha256, role, original_path, "
        "path_key, filename, pack_id, file_status, sample_rate_hz, channels, frame_count, "
        "duration_ms, imported_at, updated_at) "
        "VALUES (?, '1.0', ?, 'kick', ?, ?, 'sample-001.wav', 'pack-001', 'unknown', 48000, 1, "
        "24000, 500.0, ?, ?)",
        (sample_id, "b" * 64, SYNTHETIC_PATH,
         os.path.normcase(os.path.abspath(SYNTHETIC_PATH)), CREATED_AT, CREATED_AT))
    connection.execute(
        "INSERT INTO sample_features (sample_id, analysis_version, measurement, unit, value, "
        "unavailable_reason, confidence) VALUES (?, ?, 'rms', 'linear', 0.5, NULL, NULL)",
        (sample_id, ANALYSIS_VERSION))
    connection.execute(
        "INSERT INTO sample_keys (sample_id, analysis_version, tonic, mode, confidence, "
        "unavailable_reason) VALUES (?, ?, 'C', 'major', 0.9, NULL)",
        (sample_id, ANALYSIS_VERSION))
    connection.execute(
        "INSERT INTO sample_tags (sample_id, tag, added_at) VALUES (?, 'kick', ?)",
        (sample_id, CREATED_AT))


def column_names(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def test_a_fresh_database_reaches_the_current_version(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        assert SCHEMA_VERSION == 4
        assert MIGRATIONS[-1][0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == USER_TABLES
        assert verify(connection) == ()
    finally:
        connection.close()


def test_a_zero_byte_file_is_initialised(tmp_path):
    path = tmp_path / "library.sqlite3"
    path.write_bytes(b"")
    connection = open_library(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert user_tables(connection) == USER_TABLES
    finally:
        connection.close()


def test_open_database_creates_the_parent_directory(tmp_path):
    path = tmp_path / "nested" / "library.sqlite3"
    assert not path.parent.exists()
    connection = open_library(path)
    connection.close()
    assert path.exists()


def test_migrating_again_changes_no_version_and_no_row(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        seed(connection)
        insert_sample(connection, "sample-001")
        before = snapshot(connection)
        assert migrate(connection) == SCHEMA_VERSION
        assert migrate(connection) == SCHEMA_VERSION
        assert snapshot(connection) == before
        assert verify(connection) == ()
    finally:
        connection.close()


def test_the_user_table_set_is_the_migrated_set_without_blob_columns(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        assert user_tables(connection) == USER_TABLES
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
        assert {"idx_samples_role", "idx_sample_features_analysis", "idx_job_items_claim",
                "idx_job_items_run"} <= indexes
        for table in USER_TABLES:
            for column in connection.execute(f"PRAGMA table_info({table})").fetchall():
                assert (column[2] or "").upper() != "BLOB", column[1]
    finally:
        connection.close()


def test_open_database_refuses_a_directory(tmp_path):
    with pytest.raises(InvalidDatabasePath) as raised:
        open_library(tmp_path)
    assert raised.value.code == "invalid_database_path"


def test_open_database_refuses_a_path_that_is_not_local(tmp_path):
    with pytest.raises(InvalidDatabasePath) as raised:
        open_library(r"\\server\share\library.sqlite3")
    assert raised.value.code == "invalid_database_path"


def test_a_non_database_file_is_refused_with_its_bytes_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    payload = b"this is not a database"
    path.write_bytes(payload)
    with pytest.raises(NotADatabase) as raised:
        open_library(path)
    assert raised.value.code == "not_a_database"
    assert path.read_bytes() == payload


def test_a_corrupt_database_is_refused(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_library(path)
    connection.execute("CREATE TABLE filler (payload TEXT)")
    connection.executemany("INSERT INTO filler VALUES (?)", [("x" * 200,) for _ in range(40)])
    close_and_checkpoint(connection)

    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    assert page_size > 0 and len(raw) > page_size * 2
    raw[page_size] = 0xFF  # Break the type byte of the second b-tree page.
    path.write_bytes(raw)

    with pytest.raises(DatabaseCorrupt) as raised:
        open_library(path)
    assert raised.value.code == "database_corrupt"


def test_a_newer_schema_version_is_refused_unchanged(tmp_path):
    path = tmp_path / "library.sqlite3"
    connection = open_library(path)
    seed(connection)
    insert_sample(connection, "sample-001")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = snapshot(connection)
    close_and_checkpoint(connection)
    raw = path.read_bytes()

    with pytest.raises(SchemaVersionNewer) as raised:
        open_library(path)
    assert raised.value.code == "schema_version_newer"
    assert inspect(path) == before
    assert path.read_bytes() == raw  # Already in WAL: the refusal writes nothing.

    # A database that is not in WAL is refused before the pragmas switch the
    # journal mode, so its bytes are untouched as well.
    assert set_journal_mode(path, "delete") == "delete"
    raw = path.read_bytes()
    with pytest.raises(SchemaVersionNewer) as raised:
        open_library(path)
    assert raised.value.code == "schema_version_newer"
    assert journal_mode(path) == "delete"
    assert path.read_bytes() == raw
    assert inspect(path) == before


def test_an_upgrade_keeps_every_existing_row(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        seed(connection)
        insert_sample(connection, "sample-001")
        before = rows(connection)
        assert migrate(connection, UPGRADE) == SCHEMA_VERSION + 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        assert "notes" in column_names(connection, "samples")
        assert rows(connection) == before
        assert migrate(connection, UPGRADE) == SCHEMA_VERSION + 1
        assert rows(connection) == before
    finally:
        connection.close()


def test_a_failing_migration_rolls_back_invalid_sql(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        seed(connection)
        insert_sample(connection, "sample-001")
        before = snapshot(connection)
        broken = MIGRATIONS + ((SCHEMA_VERSION + 1,
                                "ALTER TABLE samples ADD COLUMN notes TEXT; NOT VALID SQL;"),)
        with pytest.raises(MigrationFailed) as raised:
            migrate(connection, broken)
        assert raised.value.code == "migration_failed"
        assert isinstance(raised.value.__cause__, sqlite3.Error)
        assert snapshot(connection) == before
        assert "notes" not in column_names(connection, "samples")
    finally:
        connection.close()


def test_a_migration_aborted_by_a_trigger_rolls_back(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        seed(connection)
        insert_sample(connection, "sample-001")
        connection.execute(
            "CREATE TRIGGER refuse_versions BEFORE INSERT ON analysis_versions "
            "BEGIN SELECT RAISE(ABORT, 'no new versions'); END")
        before = snapshot(connection)
        script = ("ALTER TABLE samples ADD COLUMN notes TEXT; "
                  f"INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                  f"VALUES ('{'1' * 64}', '{{}}', '{CREATED_AT}');")
        with pytest.raises(MigrationFailed) as raised:
            migrate(connection, MIGRATIONS + ((SCHEMA_VERSION + 1, script),))
        assert raised.value.code == "migration_failed"
        assert snapshot(connection) == before
        assert "notes" not in column_names(connection, "samples")
    finally:
        connection.close()


def test_connection_pragmas_and_foreign_keys_are_set(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        assert connection.isolation_level is None
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        connection.close()


def test_verify_reports_a_foreign_key_violation(tmp_path):
    connection = open_library(tmp_path / "library.sqlite3")
    try:
        assert verify(connection) == ()
        seed(connection)
        insert_sample(connection, "sample-001")
        assert verify(connection) == ()
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM analysis_versions WHERE analysis_version = ?",
                           (ANALYSIS_VERSION,))
        problems = verify(connection)
        assert problems
        assert any(problem.startswith("foreign_key_check") for problem in problems)
    finally:
        connection.close()


def test_default_database_path_honours_the_override(tmp_path, monkeypatch):
    override = tmp_path / "custom" / "library.sqlite3"
    monkeypatch.setenv("TERA_LIBRARY_DB", str(override))
    assert default_database_path() == Path(os.path.abspath(str(override)))


def test_default_database_path_falls_back_to_a_local_data_directory(monkeypatch):
    monkeypatch.delenv("TERA_LIBRARY_DB", raising=False)
    path = default_database_path()
    assert path.is_absolute()
    assert path.name == "library.sqlite3"
    assert path.parent.name == "tera"


def test_utc_now_is_a_utc_timestamp():
    value = utc_now()
    assert len(value) == 20 and value.endswith("Z")
    assert datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").year >= 2024


def test_the_library_modules_import_no_third_party_package():
    imported = set()
    for path in sorted((Path(__file__).parent.parent / "backend" / "library").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= set(sys.stdlib_module_names) | {"backend"}
