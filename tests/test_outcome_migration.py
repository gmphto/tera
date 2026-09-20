"""The outcome-history migration (issue #29).

Migration 5 is the next entry in #21's single chain, so these tests cover the
fresh database, the upgrade of a database that already holds #21's, #22's,
#23's, #24's and #26's rows, the closed vocabularies the table checks, the
foreign keys and cascades, and the promise that no landed table gained a column.
Every path, id, hash and timestamp here is synthetic; no audio file is opened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.library.schema import (MIGRATIONS, SCHEMA_VERSION, migrate, open_database, verify)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "_docs" / "outcome-storage.md"

# The one table this migration adds, with every column in the order
# `_docs/outcome-storage.md` lists them.
OUTCOME_COLUMNS = ("event_id", "client_event_id", "event_type", "project_id", "palette_id",
                   "palette_revision", "run_id", "candidate_id", "ranking_version", "mode",
                   "candidate_analysis_version", "removes_event_id", "recorded_at")
OUTCOME_INDEXES = ("ux_outcomes_removes", "idx_outcomes_project",
                   "idx_outcomes_palette_candidate", "idx_outcomes_run")

# The thirteen user tables a version-4 database holds.
V3_TABLES = ("analysis_versions", "sample_features", "sample_keys", "sample_packs", "sample_tags",
             "samples", "job_items", "job_runs", "palette_items", "palettes", "projects")
V4_TABLES = tuple(sorted(V3_TABLES + ("decision_cache", "decision_model_versions")))
V5_TABLES = tuple(sorted(V4_TABLES + ("recommendation_outcomes",)))

ANALYSIS_VERSION = "0" * 64
CREATED_AT = "2024-01-01T00:00:00Z"
SYNTHETIC_PATH = "C:/tera-fixtures/outcome-upgrade-001.wav"


def user_tables(connection):
    return tuple(sorted(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()))


def columns_of(connection, table):
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall())


def ordered_dump(connection, tables):
    return tuple(tuple(sorted(tuple(row) for row in connection.execute("SELECT * FROM " + table)))
                 for table in tables)


def seed_v3(connection):
    """One row in each version-3 table, with synthetic values."""

    connection.execute("INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                       "VALUES (?, ?, ?)", (ANALYSIS_VERSION, "{}", CREATED_AT))
    connection.execute("INSERT INTO sample_packs (pack_id, name, vendor, created_at) "
                       "VALUES ('pack-001', 'Synthetic Pack', NULL, ?)", (CREATED_AT,))
    connection.execute(
        "INSERT INTO samples (sample_id, schema_version, content_sha256, role, original_path, "
        "path_key, filename, pack_id, file_status, sample_rate_hz, channels, frame_count, "
        "duration_ms, imported_at, updated_at) "
        "VALUES ('sample-001', '1.0', ?, 'kick', ?, ?, 'outcome-upgrade-001.wav', 'pack-001', "
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


def version_four_database(path):
    """A committed version-4 database holding one row in each version-3 table."""

    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert migrate(connection, MIGRATIONS[:4]) == 4
        seed_v3(connection)
    finally:
        connection.close()
    return path


def outcome_row(connection, project_id="project-001", palette_id="palette-001", **overrides):
    """Insert one valid outcome row directly, with any field overridden."""

    values = {"client_event_id": "event-direct-1", "event_type": "auditioned",
              "project_id": project_id, "palette_id": palette_id, "palette_revision": 0,
              "run_id": "run-001", "candidate_id": "sample-001", "ranking_version": "ranking-v1",
              "mode": "dsp-only", "candidate_analysis_version": ANALYSIS_VERSION,
              "removes_event_id": None, "recorded_at": CREATED_AT}
    values.update(overrides)
    names = tuple(name for name in OUTCOME_COLUMNS if name != "event_id")
    connection.execute(
        "INSERT INTO recommendation_outcomes (" + ", ".join(names) + ") VALUES ("
        + ", ".join("?" for _ in names) + ")", tuple(values[name] for name in names))


def test_the_outcome_table_carries_every_documented_column_and_index(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        assert SCHEMA_VERSION == 5 and MIGRATIONS[-1][0] == SCHEMA_VERSION
        assert user_tables(connection) == V5_TABLES
        assert columns_of(connection, "recommendation_outcomes") == OUTCOME_COLUMNS
        for column in connection.execute("PRAGMA table_info(recommendation_outcomes)"):
            assert (column[2] or "").upper() != "BLOB", column[1]
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert set(OUTCOME_INDEXES) <= indexes
        assert verify(connection) == ()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_the_client_event_identity_is_unique_and_the_removal_index_is_partial(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        listed = {row[1]: row for row in connection.execute(
            "PRAGMA index_list(recommendation_outcomes)").fetchall()}
        # `client_event_id` is a table-level UNIQUE, so SQLite materialises it as an
        # automatic index rather than a named one; the uniqueness is what matters.
        unique_indexes = [name for name, row in listed.items() if row[2] == 1]
        assert sorted(
            tuple(column[2] for column in connection.execute(f"PRAGMA index_info({name})"))
            for name in unique_indexes) == [("client_event_id",), ("removes_event_id",)]
        assert listed["ux_outcomes_removes"][4] == 1
    finally:
        connection.close()


def test_the_table_references_projects_palettes_and_itself_but_never_a_sample(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        referenced = {row[2] for row in connection.execute(
            "PRAGMA foreign_key_list(recommendation_outcomes)").fetchall()}
        assert referenced == {"projects", "palettes", "recommendation_outcomes"}
        assert "samples" not in referenced
    finally:
        connection.close()


def test_upgrading_a_version_four_database_keeps_every_row(tmp_path):
    path = version_four_database(tmp_path / "library.sqlite3")
    before = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert user_tables(before) == V4_TABLES
        assert before.execute("PRAGMA user_version").fetchone()[0] == 4
        rows_before = ordered_dump(before, V3_TABLES)
        assert any(len(table) > 0 for table in rows_before)
    finally:
        before.close()
    connection = open_database(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert user_tables(connection) == V5_TABLES
        assert ordered_dump(connection, V3_TABLES) == rows_before
        assert connection.execute(
            "SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 0
        assert verify(connection) == ()
    finally:
        connection.close()


def test_migration_five_adds_no_column_to_a_landed_table(tmp_path):
    path = version_four_database(tmp_path / "library.sqlite3")
    before = sqlite3.connect(str(path), isolation_level=None)
    try:
        landed = {table: columns_of(before, table) for table in V4_TABLES}
    finally:
        before.close()
    connection = open_database(path)
    try:
        assert {table: columns_of(connection, table) for table in V4_TABLES} == landed
    finally:
        connection.close()


def test_the_closed_vocabularies_are_enforced(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        connection.execute("INSERT INTO projects (project_id, name, created_at, updated_at) "
                           "VALUES ('project-001', 'Track A', ?, ?)", (CREATED_AT, CREATED_AT))
        connection.execute(
            "INSERT INTO palettes (palette_id, project_id, name, revision, created_at, updated_at)"
            " VALUES ('palette-001', 'project-001', 'Main', 0, ?, ?)", (CREATED_AT, CREATED_AT))
        for overrides in ({"event_type": "ignored"},
                          {"mode": "guesswork"},
                          {"palette_revision": -1},
                          {"event_type": "removed", "removes_event_id": None},
                          {"client_event_id": "  "}):
            with pytest.raises(sqlite3.IntegrityError):
                outcome_row(connection, **overrides)
        outcome_row(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            outcome_row(connection, client_event_id="event-direct-1")
    finally:
        connection.close()


def test_deleting_a_project_cascades_and_keeps_another_projects_rows(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    try:
        connection.execute("INSERT INTO projects (project_id, name, created_at, updated_at) "
                           "VALUES ('project-001', 'Track A', ?, ?)", (CREATED_AT, CREATED_AT))
        connection.execute("INSERT INTO projects (project_id, name, created_at, updated_at) "
                           "VALUES ('project-002', 'Track B', ?, ?)", (CREATED_AT, CREATED_AT))
        for suffix in ("001", "002"):
            connection.execute(
                "INSERT INTO palettes (palette_id, project_id, name, revision, created_at, "
                "updated_at) VALUES (?, ?, 'Main', 0, ?, ?)",
                (f"palette-{suffix}", f"project-{suffix}", CREATED_AT, CREATED_AT))
        outcome_row(connection, project_id="project-001", palette_id="palette-001")
        outcome_row(connection, client_event_id="event-direct-2",
                    project_id="project-002", palette_id="palette-002")
        connection.execute("DELETE FROM projects WHERE project_id = 'project-001'")
        remaining = connection.execute(
            "SELECT project_id, palette_id FROM recommendation_outcomes").fetchall()
        assert [tuple(row) for row in remaining] == [("project-002", "palette-002")]
    finally:
        connection.close()


def test_the_storage_document_names_the_migration_its_table_and_its_codes():
    text = DOCUMENT.read_text(encoding="utf-8")
    assert "Migration 5" in text and "recommendation_outcomes" in text
    for code in ("invalid_outcome", "unknown_project", "unknown_palette", "cross_project_reference",
                 "unknown_palette_revision", "selection_not_in_palette", "unknown_selection",
                 "outcome_conflict", "removal_not_reflected", "idempotency_conflict",
                 "write_failed", "database_locked"):
        assert code in text, code
    for column in OUTCOME_COLUMNS:
        assert f"`{column}`" in text, column
