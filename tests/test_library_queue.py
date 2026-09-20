"""The persisted analysis queue: migration, claims, runs, retries and schemas.

Every database and tree here is synthetic and temporary. Nothing in this file
names a real library path, a real sample or a byte of real audio; fingerprints
are computed from synthetic WAV helpers under `tmp_path`.
"""

from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pytest

from backend.analysis import batch
from backend.analysis.batch import ROLES
from backend.contracts import Sample
from backend.library import indexer, queue, scanner
from backend.library.errors import InvalidContentIdentity, UnknownSample, WriteFailed
from backend.library.repository import LibraryRepository, transaction
from backend.library.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    migrate,
    open_database,
    utc_now,
    verify,
)
from tests.test_audio import wav


CURRENT = indexer.current_analysis_version()

ITEM_COLUMNS = ("item_id", "sample_id", "analysis_version", "path", "role", "state",
                "disposition", "attempts", "run_id", "claimed_at", "finished_at", "error_stage",
                "error_code", "error_message", "created_at")
RUN_COLUMNS = ("run_id", "state", "analysis_version", "workers", "max_attempts", "owner_token",
               "heartbeat_at", "started_at", "finished_at", "cancel_requested")
LIBRARY_TABLES = ("analysis_versions", "sample_features", "sample_keys", "sample_packs",
                  "sample_tags", "samples")


def tone(frequency=110, frames=480, rate=48000, amplitude=0.3):
    """A deterministic synthetic tone; only ever written under tmp_path."""

    phase = 2 * np.pi * frequency * np.arange(frames) / rate
    return wav((amplitude * np.sin(phase))[:, None], rate)


def build(tmp_path, files, name="library"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def connect(database):
    return open_database(str(database))


def query(database, statement, parameters=()):
    connection = connect(database)
    try:
        return [dict(row) for row in connection.execute(statement, parameters)]
    finally:
        connection.close()


def items(database, where="1", parameters=()):
    return query(database, "SELECT * FROM job_items WHERE " + where + " ORDER BY item_id",
                 parameters)


def runs(database):
    return query(database, "SELECT * FROM job_runs ORDER BY started_at, run_id")


def features(database, where="1", parameters=()):
    return query(database, "SELECT * FROM sample_features WHERE " + where
                 + " ORDER BY sample_id, analysis_version, measurement", parameters)


def run_scan(capsys, root, database, role="bass"):
    code = scanner.main([str(root), "--role", role, "--database", str(database)])
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, json.loads(printed[-1])


def library_with_two_files(tmp_path, capsys, name="library"):
    """A scanned synthetic library with two files and two queued content identities."""

    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)}, name)
    database = tmp_path / (name + ".sqlite3")
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["counts"]["queued_analysis"] == 2
    return root, database


def path_row(database, relative="a.wav"):
    stored = query(database, "SELECT * FROM samples ORDER BY path_key")
    for row in stored:
        if row["original_path"].endswith(relative):
            return row
    raise AssertionError(f"no stored row for {relative}")


def index_files(database, files, role="bass"):
    """Insert one path row per synthetic file, exactly as #22's scan does."""

    connection = connect(database)
    try:
        repository = LibraryRepository(connection)
        for path in files:
            data = batch.snapshot(path)
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
            repository.insert_path_record(
                str(path), role=role, content_sha256=hashlib.sha256(data).hexdigest(),
                sample_rate_hz=48000, channels=1, frame_count=480, duration_ms=10.0,
                sample_id="library:" + batch.digest({"library_path": key}))
    finally:
        connection.close()


def store_analysis_for(database, row, version=None, descriptor=None):
    """Store one complete analysis for a path row, the way the worker does."""

    version = version or CURRENT
    path = Path(row["path"] if "path" in row else row["original_path"])
    data = batch.snapshot(path)
    fingerprint = hashlib.sha256(data).hexdigest()
    payload = batch.extract(data, path, row["role"], fingerprint, version)
    sample = Sample.from_dict(payload)
    connection = connect(database)
    try:
        repository = LibraryRepository(connection)
        with transaction(connection):
            repository.store_analysis(sample, content_sha256=fingerprint,
                                      sample_id=row["sample_id"], descriptor=descriptor)
    finally:
        connection.close()
    return fingerprint


# ---------------------------------------------------------------------------
# the migration and its closed vocabularies
# ---------------------------------------------------------------------------


def test_the_queue_tables_ship_as_migration_two_with_the_named_columns(tmp_path):
    connection = connect(tmp_path / "library.sqlite3")
    try:
        assert SCHEMA_VERSION == 5
        assert [version for version, _script in MIGRATIONS] == [1, 2, 3, 4, 5]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"job_runs", "job_items"} <= tables
        assert set(LIBRARY_TABLES) <= tables
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"idx_job_items_claim", "idx_job_items_run"} <= indexes
        assert tuple(row[1] for row in connection.execute("PRAGMA table_info(job_items)")) \
            == ITEM_COLUMNS
        assert tuple(row[1] for row in connection.execute("PRAGMA table_info(job_runs)")) \
            == RUN_COLUMNS
        assert verify(connection) == ()
    finally:
        connection.close()


def test_the_migration_enforces_the_code_vocabularies(tmp_path):
    connection = connect(tmp_path / "library.sqlite3")
    try:
        item_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'job_items'").fetchone()[0]
        run_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'job_runs'").fetchone()[0]
        listed_roles = re.search(r"role IN \(([^)]*)\)", item_sql).group(1)
        assert {value.strip() for value in listed_roles.split(",")} \
            == {"'" + role + "'" for role in ROLES}
        listed_states = re.search(r"state IN \(([^)]*)\)", item_sql, re.S).group(1)
        assert {value.strip() for value in listed_states.split(",")} \
            == {"'" + state + "'" for state in queue.ITEM_STATES}
        listed_runs = re.search(r"state IN \(([^)]*)\)", run_sql, re.S).group(1)
        assert {value.strip() for value in listed_runs.split(",")} \
            == {"'" + state + "'" for state in queue.RUN_STATES}
        listed_stages = re.search(r"error_stage IN \(([^)]*)\)", item_sql, re.S).group(1)
        assert {value.strip() for value in listed_stages.split(",")} \
            == {"'" + stage + "'" for stage in queue.STAGES}
        listed_dispositions = re.search(r"disposition IN \(([^)]*)\)", item_sql).group(1)
        assert {value.strip() for value in listed_dispositions.split(",")} \
            == {"'" + disposition + "'" for disposition in queue.DISPOSITIONS}
        # The constraints are live, not decorative.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_items (sample_id, analysis_version, path, role, state, "
                "attempts, created_at) VALUES (?, ?, 'C:/synthetic/a.wav', 'snare', 'pending', "
                "0, ?)", ("sha256:" + "a" * 64, CURRENT, utc_now()))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_items (sample_id, analysis_version, path, role, state, "
                "attempts, created_at) VALUES (?, ?, 'C:/synthetic/a.wav', 'kick', 'sleeping', "
                "0, ?)", ("sha256:" + "a" * 64, CURRENT, utc_now()))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_runs (run_id, state, analysis_version, workers, max_attempts, "
                "owner_token, heartbeat_at, started_at, finished_at, cancel_requested) "
                "VALUES ('run-x', 'pending', ?, 1, 3, 'token', ?, ?, NULL, 0)",
                (CURRENT, utc_now(), utc_now()))
    finally:
        connection.close()


def test_the_item_identity_must_be_the_content_identity(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
        for item in items(database):
            fingerprint = queue.fingerprint_of(item["sample_id"])
            assert fingerprint is not None and len(fingerprint) == 64
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO job_items (sample_id, analysis_version, path, role, state, "
                "attempts, created_at) VALUES ('library:not-a-fingerprint', ?, 'C:/x.wav', "
                "'kick', 'pending', 0, ?)", (CURRENT, utc_now()))
    finally:
        connection.close()


def test_the_error_vocabulary_is_closed_and_classified():
    assert set(queue.ERROR_CODES) == set(queue.RETRYABLE_CODES) | set(queue.TERMINAL_CODES)
    assert not set(queue.RETRYABLE_CODES) & set(queue.TERMINAL_CODES)
    assert queue.RETRYABLE_CODES == ("not_found", "access_denied", "io_error", "source_changed",
                                     "not_file")
    assert set(queue.TERMINAL_CODES) == {"unsupported_format", "unsupported_channels",
                                         "empty_audio", "invalid_audio", "extractor_failure",
                                         "sample_missing", "content_changed",
                                         "analysis_version_changed"}
    assert set(queue.CODE_STAGES) == set(queue.ERROR_CODES)
    assert set(queue.CODE_STAGES.values()) <= set(queue.STAGES)
    for code in queue.RETRYABLE_CODES:
        assert queue.is_retryable(code)
    for code in queue.TERMINAL_CODES:
        assert not queue.is_retryable(code) and queue.stage_for(code) == queue.CODE_STAGES[code]
    assert queue.stage_for("code-from-a-defect", queue.STAGE_READ) == queue.STAGE_READ
    assert queue.stage_for("code-from-a-defect") == queue.STAGE_EXTRACT
    assert set(queue.ITEM_STATES) == {"pending", "running", "complete", "failed", "cancelled",
                                      "orphaned", "superseded"}
    assert set(queue.RUN_STATES) == {"running", "complete", "cancelled", "interrupted", "failed"}
    assert queue.QUEUE_SCHEMA == "1.0" and queue.QUEUE_POLICY_VERSION == "library-jobs-v1"
    assert (queue.DEFAULT_WORKERS, queue.MAX_WORKERS, queue.MAX_ATTEMPTS,
            queue.LEASE_SECONDS, queue.HEARTBEAT_SECONDS) == (1, 4, 3, 60, 15)


def test_the_numeric_bounds_are_refused_outside_their_range():
    for workers in (0, 5, -1, True, "2"):
        with pytest.raises(queue.QueueError):
            queue.require_workers(workers)
    for attempts in (0, 11, -3, True, "3"):
        with pytest.raises(queue.QueueError):
            queue.require_attempts(attempts)
    assert queue.require_workers(4) == 4 and queue.require_attempts(10) == 10


# ---------------------------------------------------------------------------
# upgrading a database that already holds samples and features
# ---------------------------------------------------------------------------


def test_an_upgrade_adds_the_queue_without_touching_a_single_row(tmp_path):
    database = tmp_path / "library.sqlite3"
    audio = tone(110)
    path = tmp_path / "synthetic.wav"
    path.write_bytes(audio)
    path = batch.local_path(path)
    fingerprint = hashlib.sha256(audio).hexdigest()
    connection = sqlite3.connect(str(database), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert migrate(connection, MIGRATIONS[:1]) == 1
        repository = LibraryRepository(connection)
        repository.register_analysis_version(batch.analysis_descriptor())
        sample = Sample.from_dict(batch.extract(audio, path, "bass", fingerprint, CURRENT))
        repository.import_sample(sample, content_sha256=fingerprint, file_status="present")
        before = {name: [tuple(row) for row in connection.execute(
            "SELECT * FROM " + name + " ORDER BY 1, 2")] for name in LIBRARY_TABLES}
    finally:
        connection.close()

    connection = connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        after = {name: [tuple(row) for row in connection.execute(
            "SELECT * FROM " + name + " ORDER BY 1, 2")] for name in LIBRARY_TABLES}
        assert after == before
        assert connection.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 0
        assert verify(connection) == ()
    finally:
        connection.close()


def test_reapplying_the_migrations_after_a_run_leaves_the_queue_rows_identical(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
        run_id = queue.open_run(connection, CURRENT, 2, 3)
        item = queue.claim(connection, run_id)
        with transaction(connection):
            queue.finalize(connection, item["item_id"], queue.ITEM_COMPLETE,
                           queue.DISPOSITION_REUSED)
            queue.finish_run(connection, run_id, queue.RUN_COMPLETE)
        before = [items(database), runs(database)]
        assert migrate(connection) == SCHEMA_VERSION
        assert migrate(connection) == SCHEMA_VERSION
        assert [items(database), runs(database)] == before
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# enqueueing
# ---------------------------------------------------------------------------


def test_enqueue_is_idempotent_across_calls_and_connections(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    first, second = connect(database), connect(database)
    try:
        assert queue.enqueue(first, CURRENT) == 2
        assert queue.enqueue(first, CURRENT) == 0
        assert queue.enqueue(second, CURRENT) == 0
        stored = items(database)
        assert len(stored) == 2
        assert {item["state"] for item in stored} == {"pending"}
        assert {item["analysis_version"] for item in stored} == {CURRENT}
        assert len({item["sample_id"] for item in stored}) == 2
        # A scan executed while the queue holds the work adds no duplicate row.
        code, summary = run_scan(capsys, root, database)
        assert code == 0 and summary["counts"]["queued_analysis"] == 2
        assert queue.enqueue(first, CURRENT) == 0
        assert len(items(database)) == 2
    finally:
        first.close()
        second.close()


def test_enqueue_revives_a_cancelled_row_and_leaves_every_other_state(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
        connection.execute(
            "UPDATE job_items SET state = 'cancelled', attempts = 2, run_id = 'run-old', "
            "claimed_at = ?, finished_at = ?, error_stage = 'read', error_code = 'access_denied', "
            "error_message = 'synthetic' WHERE item_id = 1", (utc_now(), utc_now()))
        connection.execute(
            "UPDATE job_items SET state = 'complete', disposition = 'analyzed', attempts = 1 "
            "WHERE item_id = 2")
        assert queue.enqueue(connection, CURRENT) == 1
        revived = items(database, "item_id = 1")[0]
        assert (revived["state"], revived["attempts"], revived["run_id"], revived["claimed_at"],
                revived["finished_at"], revived["error_stage"], revived["error_code"],
                revived["error_message"], revived["disposition"]) \
            == ("pending", 0, None, None, None, None, None, None, None)
        kept = items(database, "item_id = 2")[0]
        assert (kept["state"], kept["disposition"]) == ("complete", "analyzed")
        assert queue.enqueue(connection, CURRENT) == 0
    finally:
        connection.close()


def test_enqueue_takes_only_the_run_version(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, "0" * 64) == 0
        assert items(database) == []
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------


def test_claims_are_deterministic_fifo_and_increment_attempts(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        queue.enqueue(connection, CURRENT)
        run_id = queue.open_run(connection, CURRENT, 2, 3)
        first = queue.claim(connection, run_id)
        second = queue.claim(connection, run_id)
        assert [first["item_id"], second["item_id"]] == [1, 2]
        assert [first["state"], first["attempts"], first["run_id"]] == ["running", 1, run_id]
        assert queue.claim(connection, run_id) is None
    finally:
        connection.close()


def test_two_claimers_never_receive_the_same_item(tmp_path, capsys):
    root = build(tmp_path, {f"file-{number:02d}.wav": tone(110 + number) for number in range(12)})
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["counts"]["queued_analysis"] == 12
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 12
        run_id = queue.open_run(connection, CURRENT, 4, 3)
    finally:
        connection.close()
    claimed, lock = [], threading.Lock()

    def claimer():
        with closing(connect(database)) as own:
            while True:
                item = queue.claim(own, run_id)
                if item is None:
                    return
                with lock:
                    claimed.append(item["item_id"])

    threads = [threading.Thread(target=claimer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == list(range(1, 13))
    assert len(claimed) == len(set(claimed))
    assert all(item["attempts"] == 1 for item in items(database))


def test_a_horizon_bounds_a_once_claim(tmp_path, capsys):
    root = build(tmp_path, {f"file-{number:02d}.wav": tone(110 + number) for number in range(3)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 3
        horizon = queue.claim_horizon(connection)
        assert horizon == 3
        # Work discovered after the horizon is not claimed by this run.
        (root / "late.wav").write_bytes(tone(330))
        code, summary = run_scan(capsys, root, database)
        assert code == 0 and summary["counts"]["queued_analysis"] == 4
        assert queue.enqueue(connection, CURRENT) == 1
        run_id = queue.open_run(connection, CURRENT, 1, 3)
        assert [queue.claim(connection, run_id, horizon)["item_id"] for _ in range(3)] == [1, 2, 3]
        assert queue.claim(connection, run_id, horizon) is None
        assert items(database, "item_id = 4")[0]["state"] == "pending"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# runs, leases and cancellation flags
# ---------------------------------------------------------------------------


def test_one_live_run_per_database(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    owner, other = connect(database), connect(database)
    try:
        assert queue.enqueue(owner, CURRENT) == 2
        run_id = queue.open_run(owner, CURRENT, 1, 3)
        with pytest.raises(queue.QueueError) as raised:
            queue.open_run(other, CURRENT, 1, 3)
        assert run_id in str(raised.value)
        assert [run["run_id"] for run in runs(database)] == [run_id]
        assert {item["state"] for item in items(database)} == {"pending"}
        # The first run's items all complete exactly once.
        assert queue.claim(owner, run_id) is not None
        assert queue.claim(other, run_id) is not None
        assert all(item["attempts"] == 1 for item in items(database))
        assert queue.claim(other, run_id) is None
    finally:
        owner.close()
        other.close()


def test_a_stale_lease_is_reconciled_before_the_next_run(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    owner = connect(database)
    try:
        queue.enqueue(owner, CURRENT)
        first = queue.open_run(owner, CURRENT, 1, 3)
        claimed = queue.claim(owner, first)
        assert claimed["attempts"] == 1
        owner.execute("UPDATE job_runs SET heartbeat_at = ? WHERE run_id = ?",
                      ("2000-01-01T00:00:00Z", first))
        assert queue.cancel_requested(owner, first) is False
        second = queue.open_run(owner, CURRENT, 1, 3)
        assert second != first
        old = [run for run in runs(database) if run["run_id"] == first][0]
        assert old["state"] == "interrupted" and old["finished_at"] is not None
        requeued = items(database, "item_id = ?", (claimed["item_id"],))[0]
        assert (requeued["state"], requeued["attempts"], requeued["run_id"]) == ("pending", 1, None)
        fresh = [run for run in runs(database) if run["run_id"] == second][0]
        assert fresh["state"] == "running" and fresh["finished_at"] is None
    finally:
        owner.close()


def test_a_live_heartbeat_is_renewed_and_a_lost_lease_is_refused(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        run_id = queue.open_run(connection, CURRENT, 1, 3)
        token = [run for run in runs(database) if run["run_id"] == run_id][0]["owner_token"]
        assert queue.heartbeat(connection, run_id, token) is True
        assert queue.heartbeat(connection, run_id, "not-the-owner") is False
        with transaction(connection):
            queue.finish_run(connection, run_id, queue.RUN_COMPLETE)
        assert queue.heartbeat(connection, run_id, token) is False
    finally:
        connection.close()


def test_reconcile_requeues_running_items_of_a_run_that_is_not_live(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        queue.enqueue(connection, CURRENT)
        run_id = queue.open_run(connection, CURRENT, 1, 3)
        claimed = queue.claim(connection, run_id)
        with transaction(connection):
            queue.finish_run(connection, run_id, queue.RUN_FAILED)
        assert queue.reconcile(connection) == {"runs": 0, "items": 1}
        requeued = items(database, "item_id = ?", (claimed["item_id"],))[0]
        assert (requeued["state"], requeued["attempts"], requeued["run_id"]) == ("pending", 1, None)
        assert queue.reconcile(connection) == {"runs": 0, "items": 0}
    finally:
        connection.close()


def test_request_cancel_flags_only_live_runs(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        live = queue.open_run(connection, CURRENT, 1, 3)
        with transaction(connection):
            queue.finish_run(connection, live, queue.RUN_COMPLETE)
        assert queue.request_cancel(connection) == 0
        live = queue.open_run(connection, CURRENT, 1, 3)
        assert queue.request_cancel(connection) == 1
        assert queue.cancel_requested(connection, live) is True
        assert queue.request_cancel(connection) == 1  # Idempotent: the row is still live.
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


def test_retry_failed_resets_only_failed_and_orphaned_items(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
        connection.execute(
            "UPDATE job_items SET state = 'failed', attempts = 3, error_stage = 'decode', "
            "error_code = 'empty_audio', error_message = 'synthetic' WHERE item_id = 1")
        connection.execute(
            "UPDATE job_items SET state = 'complete', disposition = 'analyzed', attempts = 1 "
            "WHERE item_id = 2")
        assert queue.retry_failed(connection, CURRENT) == 1
        assert queue.retry_failed(connection, CURRENT) == 0
        reset = items(database, "item_id = 1")[0]
        assert (reset["state"], reset["attempts"], reset["error_code"]) == ("pending", 0, None)
        assert items(database, "item_id = 2")[0]["state"] == "complete"
        assert queue.retry_failed(connection, "0" * 64) == 0
    finally:
        connection.close()


def test_an_item_at_the_attempt_limit_stays_failed(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        queue.enqueue(connection, CURRENT)
        run_id = queue.open_run(connection, CURRENT, 1, 3)
        item = queue.claim(connection, run_id)
        for attempt in range(1, 4):
            current = items(database, "item_id = ?", (item["item_id"],))[0]
            assert current["attempts"] == attempt
            if attempt < 3:
                with transaction(connection):
                    queue.requeue(connection, item["item_id"])
                assert queue.claim(connection, run_id)["item_id"] == item["item_id"]
            else:
                with transaction(connection):
                    queue.finalize(connection, item["item_id"], queue.ITEM_FAILED,
                                   error=queue.error_record(queue.STAGE_READ, "access_denied",
                                                            "synthetic"))
        assert items(database, "item_id = ?", (item["item_id"],))[0]["state"] == "failed"
        claimed_again = set()
        while True:
            following = queue.claim(connection, run_id)
            if following is None:
                break
            claimed_again.add(following["item_id"])
        assert item["item_id"] not in claimed_again
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# status and summary
# ---------------------------------------------------------------------------


def test_status_and_summary_have_the_documented_shapes(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        empty = queue.status(connection)
        assert set(empty) == {"run_id", "state", "analysis_version", "workers", "started_at",
                              "counts", "current"}
        assert (empty["run_id"], empty["state"], empty["current"]) == (None, None, None)
        assert set(empty["counts"]) == set(queue.ITEM_STATES) | {"remaining"}
        assert empty["counts"]["pending"] == 0

        assert queue.enqueue(connection, CURRENT) == 2
        run_id = queue.open_run(connection, CURRENT, 2, 5)
        status = queue.status(connection, run_id)
        assert set(status) == {"run_id", "state", "analysis_version", "workers", "started_at",
                               "counts", "current"}
        assert (status["run_id"], status["state"], status["analysis_version"], status["workers"]) \
            == (run_id, "running", CURRENT, 2)
        assert status["counts"]["pending"] == 2 and status["counts"]["remaining"] == 2
        assert status["current"] is None
        first = queue.claim(connection, run_id)
        second = queue.claim(connection, run_id)
        status = queue.status(connection, run_id)
        assert status["current"] == {"sample_id": first["sample_id"], "path": first["path"],
                                     "attempts": 1}
        assert status["counts"]["running"] == 2 and status["counts"]["remaining"] == 2
        with pytest.raises(queue.QueueError):
            queue.status(connection, "run-does-not-exist")
        with pytest.raises(queue.QueueError):
            queue.summary(connection, "run-does-not-exist")

        with transaction(connection):
            queue.finalize(connection, first["item_id"], queue.ITEM_COMPLETE,
                           queue.DISPOSITION_ANALYZED)
            queue.finalize(connection, second["item_id"], queue.ITEM_COMPLETE,
                           queue.DISPOSITION_REUSED)
            queue.finish_run(connection, run_id, queue.RUN_COMPLETE)
        payload = queue.summary(connection, run_id)
        assert set(payload) == {"queue_schema", "queue_policy_version", "run_id",
                                "analysis_version", "state", "workers", "max_attempts",
                                "started_at", "finished_at", "counts", "failures"}
        assert (payload["queue_schema"], payload["queue_policy_version"]) == ("1.0",
                                                                             "library-jobs-v1")
        assert set(payload["counts"]) == set(queue.ITEM_STATES) | {"analyzed", "reused",
                                                                   "remaining"}
        assert payload["counts"] == {"pending": 0, "running": 0, "complete": 2, "failed": 0,
                                     "cancelled": 0, "orphaned": 0, "superseded": 0,
                                     "analyzed": 1, "reused": 1, "remaining": 0}
        assert payload["failures"] == []
        assert payload["state"] == "complete" and payload["max_attempts"] == 5
        assert payload["finished_at"] is not None
    finally:
        connection.close()


def test_summary_counts_equal_the_persisted_rows_and_list_every_failure(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        queue.enqueue(connection, CURRENT)
        run_id = queue.open_run(connection, CURRENT, 1, 3)
        first = queue.claim(connection, run_id)
        with transaction(connection):
            queue.finalize(connection, first["item_id"], queue.ITEM_COMPLETE,
                           queue.DISPOSITION_REUSED)
        second = queue.claim(connection, run_id)
        with transaction(connection):
            queue.finalize(connection, second["item_id"], queue.ITEM_ORPHANED,
                           error=queue.error_record(queue.STAGE_QUEUE, "sample_missing",
                                                    "synthetic removal"))
            queue.finish_run(connection, run_id, queue.RUN_COMPLETE)
        payload = queue.summary(connection, run_id)
        persisted = items(database, "run_id = ?", (run_id,))
        for state in queue.ITEM_STATES:
            assert payload["counts"][state] == sum(item["state"] == state for item in persisted)
        assert payload["counts"]["remaining"] == 0
        assert len(payload["failures"]) == 1
        failure = payload["failures"][0]
        assert set(failure) == {"sample_id", "path", "role", "stage", "code", "message",
                                "attempts"}
        assert failure == {"sample_id": second["sample_id"], "path": second["path"],
                           "role": second["role"], "stage": "queue", "code": "sample_missing",
                           "message": "synthetic removal", "attempts": 1}
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# repository.store_analysis: the one operation #23 adds to #21
# ---------------------------------------------------------------------------


def test_store_analysis_requires_the_callers_transaction(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    row = path_row(database)
    audio = batch.snapshot(Path(row["original_path"]))
    fingerprint = hashlib.sha256(audio).hexdigest()
    sample = Sample.from_dict(
        batch.extract(audio, Path(row["original_path"]), row["role"], fingerprint, CURRENT))
    connection = connect(database)
    try:
        repository = LibraryRepository(connection)
        with pytest.raises(WriteFailed):
            repository.store_analysis(sample, content_sha256=fingerprint,
                                      sample_id=row["sample_id"])
        assert features(database) == []

        with pytest.raises(UnknownSample):
            with transaction(connection):
                repository.store_analysis(sample, content_sha256=fingerprint,
                                          sample_id="library:gone")
        with pytest.raises(InvalidContentIdentity):
            with transaction(connection):
                repository.store_analysis(sample, content_sha256="b" * 64,
                                          sample_id=row["sample_id"])
    finally:
        connection.close()


def test_store_analysis_writes_under_the_row_identity_and_touches_no_sample_row(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    row = path_row(database)
    before = query(database, "SELECT * FROM samples WHERE sample_id = ?", (row["sample_id"],))
    fingerprint = store_analysis_for(database, row, descriptor=batch.analysis_descriptor())
    after = query(database, "SELECT * FROM samples WHERE sample_id = ?", (row["sample_id"],))
    assert after == before
    stored = features(database, "sample_id = ?", (row["sample_id"],))
    assert len(stored) == 19
    connection = connect(database)
    try:
        stored_sample = LibraryRepository(connection).get_sample(row["sample_id"], CURRENT)
        assert stored_sample.sample.analysis_version == CURRENT
        assert stored_sample.sample.sample_id == row["sample_id"]
    finally:
        connection.close()
    assert query(database, "SELECT * FROM samples WHERE sample_id = ?", (row["sample_id"],)) \
        == before
    # A version with no registered descriptor cannot be stored anonymously.
    connection = connect(database)
    try:
        second = [item for item in query(database, "SELECT * FROM samples ORDER BY path_key")
                  if item["sample_id"] != row["sample_id"]][0]
        second_path = Path(second["original_path"])
        data = batch.snapshot(second_path)
        real = hashlib.sha256(data).hexdigest()
        sample = Sample.from_dict(batch.extract(data, second_path, second["role"], real, "d" * 64))
        with pytest.raises(Exception) as raised:
            with transaction(connection):
                LibraryRepository(connection).store_analysis(
                    sample, content_sha256=real, sample_id=second["sample_id"])
        assert getattr(raised.value, "code", None) == "unknown_analysis_version"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# pruning terminal history (#74)
# ---------------------------------------------------------------------------


#: One item per state, with the age its `finished_at` is given, the failure code
#: the schema requires for an error state, and whether the documented predicate
#: makes it prunable.
PRUNE_STATES = (
    ("complete", "aged", None, True),
    ("cancelled", "aged", None, True),
    ("superseded", "aged", "analysis_version_changed", True),
    ("complete", "recent", None, False),
    ("failed", "aged", "unsupported_format", False),
    ("orphaned", "aged", "sample_missing", False),
    ("pending", None, None, False),
    ("running", None, None, False),
)
#: Comfortably older than the thirty-day cutoff, in `schema.utc_now`'s format.
AGED_STAMP = "2024-01-01T00:00:00Z"


def library_rows(database):
    """Every library table's rows, as one comparable structure."""

    return {table: query(database, "SELECT * FROM " + table + " ORDER BY 1")
            for table in LIBRARY_TABLES}


def queue_in_every_state(tmp_path, capsys):
    """A scanned library queued with one item in each documented state."""

    files = {f"{letter}.wav": tone(110 + 10 * index)
             for index, letter in enumerate("abcdefgh")}
    root = build(tmp_path, files)
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["counts"]["queued_analysis"] == len(PRUNE_STATES)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == len(PRUNE_STATES)
        # A terminal run backs the one running item: the schema requires a run id
        # for that state, and the prune's deferral guard reads only live runs.
        run_id = queue.open_run(connection, CURRENT)
        queue.finish_run(connection, run_id, queue.RUN_COMPLETE)
        identifiers = [row["item_id"] for row in
                       connection.execute("SELECT item_id FROM job_items ORDER BY item_id")]
        assert len(identifiers) == len(PRUNE_STATES)
        for item_id, (state, age, code, _prunable) in zip(identifiers, PRUNE_STATES):
            finished = None if age is None else (AGED_STAMP if age == "aged" else utc_now())
            connection.execute(
                "UPDATE job_items SET state = ?, finished_at = ?, claimed_at = ?, "
                "error_code = ?, error_stage = ?, error_message = ?, run_id = ? "
                "WHERE item_id = ?",
                (state, finished, utc_now(), code,
                 None if code is None else queue.stage_for(code),
                 None if code is None else "Synthetic failure recorded by the prune test.",
                 run_id if state == "running" else None, item_id))
    finally:
        connection.close()
    # One analyzed sample, so the feature and analysis-version rows the prune
    # must never touch are actually present.
    store_analysis_for(database, query(database, "SELECT * FROM samples ORDER BY path_key")[0],
                       descriptor=batch.analysis_descriptor())
    return database


def test_pruning_removes_exactly_the_aged_terminal_items(tmp_path, capsys):
    assert queue.PRUNE_AFTER_DAYS == 30
    assert tuple(queue.PRUNABLE_ITEM_STATES) == ("complete", "cancelled", "superseded")
    database = queue_in_every_state(tmp_path, capsys)
    before = items(database)
    runs_before = runs(database)
    library_before = library_rows(database)
    assert library_before["sample_features"] and library_before["analysis_versions"]
    connection = connect(database)
    try:
        result = queue.prune_history(connection)
    finally:
        connection.close()
    assert result["older_than_days"] == 30 and result["deferred"] is False
    assert result["by_state"] == {"complete": 1, "cancelled": 1, "superseded": 1}
    assert result["total"] == 3
    kept = [row for row in before
            if row["state"] not in queue.PRUNABLE_ITEM_STATES or row["finished_at"] != AGED_STAMP]
    assert len(kept) == 5
    assert items(database) == kept
    assert {row["state"] for row in kept} == {"complete", "failed", "orphaned", "pending",
                                              "running"}
    # No library, feature, analysis-version or run row changed, and no content
    # identity moved: only the three aged terminal items are gone.
    assert library_rows(database) == library_before
    assert runs(database) == runs_before


def test_pruning_again_removes_nothing_and_reports_zero(tmp_path, capsys):
    database = queue_in_every_state(tmp_path, capsys)
    connection = connect(database)
    try:
        first = queue.prune_history(connection)
        second = queue.prune_history(connection)
    finally:
        connection.close()
    assert first["total"] == 3
    assert second == {"older_than_days": 30,
                      "by_state": {"complete": 0, "cancelled": 0, "superseded": 0},
                      "total": 0, "deferred": False}


def test_a_live_run_defers_the_prune_and_removes_nothing(tmp_path, capsys):
    database = queue_in_every_state(tmp_path, capsys)
    connection = connect(database)
    try:
        # Opening the live run claims items of its own, so the snapshot the prune
        # must leave alone is taken once the run exists.
        assert queue.open_run(connection, CURRENT)
        before = items(database)
        library_before = library_rows(database)
        result = queue.prune_history(connection)
    finally:
        connection.close()
    assert result == {"older_than_days": 30,
                      "by_state": {"complete": 0, "cancelled": 0, "superseded": 0},
                      "total": 0, "deferred": True}
    assert items(database) == before
    assert library_rows(database) == library_before


def test_the_prune_command_is_explicit_repeatable_and_coded(tmp_path, capsys):
    database = queue_in_every_state(tmp_path, capsys)
    assert queue.main(["--database", str(database), "--prune-history"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["total"] == 3 and printed["deferred"] is False
    assert queue.main(["--database", str(database), "--prune-history"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0
    # The command never runs without the explicit flag, and a refusal exits 2.
    with pytest.raises(SystemExit):
        queue.main(["--database", str(database)])
    broken = tmp_path / "not-a-database.sqlite3"
    broken.write_bytes(b"this file is not a SQLite database, and never was.")
    assert queue.main(["--database", str(broken), "--prune-history"]) == 2
    assert "code" in capsys.readouterr().err
