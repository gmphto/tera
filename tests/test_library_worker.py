"""The analysis worker: the command, one item's life and the run's guarantees.

Every tree and database here is synthetic and temporary, and every audio byte is
produced by the `wav(...)` helper under `tmp_path`. See
`tests/test_library_jobs_recovery.py` for the restart, cancellation and
concurrency cases.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import tracemalloc

import numpy as np
import pytest

from backend.analysis import batch, loudness
from backend.contracts import Sample
from backend.library import indexer, queue, worker
from backend.library.repository import LibraryRepository, transaction
from tests.test_audio import wav
from tests.test_library_queue import (
    CURRENT,
    build,
    connect,
    features,
    index_files,
    items,
    library_with_two_files,
    path_row,
    query,
    run_scan,
    store_analysis_for,
    tone,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# A second process that patches the extractor to be slow, then runs the real CLI.
# The patch is applied to the module object, which is what the worker calls.
BOOTSTRAP = """import os
import sys
import threading
import time

sys.path.insert(0, {root!r})

from backend.analysis import batch

_real = batch.extract


def slow_extract(data, path, role, fingerprint, version):
    destination = os.environ.get("TERA_TEST_PID_FILE")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            stream.write("%d %d\\n" % (os.getpid(), threading.get_ident()))
    time.sleep(float(os.environ.get("TERA_TEST_EXTRACT_DELAY", "0.25")))
    return _real(data, path, role, fingerprint, version)


batch.extract = slow_extract

from backend.library import queue

if os.environ.get("TERA_TEST_LEASE_SECONDS"):
    queue.LEASE_SECONDS = float(os.environ["TERA_TEST_LEASE_SECONDS"])
if os.environ.get("TERA_TEST_HEARTBEAT_SECONDS"):
    queue.HEARTBEAT_SECONDS = float(os.environ["TERA_TEST_HEARTBEAT_SECONDS"])

from backend.library import worker

raise SystemExit(worker.main(sys.argv[1:]))
"""


class ChildCli:
    """One worker process with its stdout on a file (the sandbox forbids pipes)."""

    def __init__(self, tmp_path, name, arguments, environment=None):
        script = tmp_path / (name + "-bootstrap.py")
        script.write_text(BOOTSTRAP.format(root=str(REPOSITORY_ROOT)), encoding="utf-8")
        self.stdout_path = tmp_path / (name + "-stdout.jsonl")
        self._handle = open(self.stdout_path, "w", encoding="utf-8")
        environment_values = dict(os.environ)
        environment_values["PYTHONPATH"] = str(REPOSITORY_ROOT) + os.pathsep \
            + environment_values.get("PYTHONPATH", "")
        environment_values.update(environment or {})
        self.process = subprocess.Popen([sys.executable, str(script), *arguments],
                                        stdout=self._handle, stderr=subprocess.STDOUT,
                                        cwd=str(REPOSITORY_ROOT), env=environment_values)

    def wait(self, timeout=180):
        code = self.process.wait(timeout=timeout)
        self._handle.close()
        return code

    def lines(self):
        if not self._handle.closed:
            self._handle.close()
        return [line for line in self.stdout_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


def run_worker(capsys, database, *arguments):
    """Run the CLI in this process and return (exit code, printed JSON lines)."""

    code = worker.main(["--database", str(database), *arguments])
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, printed


def summary_of(lines):
    return json.loads(lines[-1])


def shift(audio_bytes, offset=44):
    """The same byte length with different content, so only the hash changes."""

    changed = bytearray(audio_bytes)
    changed[offset] = (changed[offset] + 1) % 256
    return bytes(changed)


def truncated_wav():
    """A data chunk whose RIFF size was repaired: the reader still refuses it."""

    original = wav([[0.5], [0.25]])
    cut = bytearray(original[:-2])
    struct.pack_into("<I", cut, 4, len(cut) - 8)
    return bytes(cut)


def named(database):
    """Every item with the basename of its stored path, for readable assertions."""

    return {Path(item["path"]).name: item for item in items(database)}


def tree_state(root):
    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            for path in sorted(Path(root).iterdir())}


def completion_line(line):
    """One per-item progress line, keyed the way #9 keys a manifest line."""

    return set(json.loads(line)) == {"state", "analysis_version", "completed", "failed",
                                     "remaining", "analyzed", "reused", "pending", "running",
                                     "cancelled", "orphaned", "superseded"}


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def test_the_command_drains_a_scanned_library_and_prints_the_summary(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert len(lines) == 3  # one line per item plus the summary
    assert all(completion_line(line) for line in lines[:-1])
    payload = summary_of(lines)
    assert payload["state"] == "complete"
    assert payload["counts"] == {"pending": 0, "running": 0, "complete": 2, "failed": 0,
                                 "cancelled": 0, "orphaned": 0, "superseded": 0, "analyzed": 2,
                                 "reused": 0, "remaining": 0}
    assert payload["failures"] == []
    assert payload["analysis_version"] == CURRENT
    stored = features(database)
    assert len(stored) == 2 * 19
    assert {row["analysis_version"] for row in stored} == {CURRENT}
    assert all(item["state"] == "complete" and item["disposition"] == "analyzed"
               and item["attempts"] == 1 for item in items(database))
    connection = connect(database)
    try:
        for row in query(database, "SELECT * FROM samples"):
            stored_sample = LibraryRepository(connection).get_sample(row["sample_id"], CURRENT)
            assert len(stored_sample.sample.features.measurements) == 19
    finally:
        connection.close()


def test_the_summary_is_also_written_atomically_to_the_named_file(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    destination = tmp_path / "run-summary.json"
    code, lines = run_worker(capsys, database, "--summary", str(destination))
    assert code == 0
    assert json.loads(destination.read_text(encoding="utf-8")) == summary_of(lines)
    assert destination.read_text(encoding="utf-8").endswith("\n")
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith("run-summary.json.")] \
        == []


def test_the_command_refuses_a_bad_command_database_or_parameter(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    assert run_worker(capsys, database, "--workers", "0")[0] == 2
    assert run_worker(capsys, database, "--workers", "5")[0] == 2
    assert run_worker(capsys, database, "--max-attempts", "0")[0] == 2
    assert run_worker(capsys, database, "--max-attempts", "11")[0] == 2
    assert run_worker(capsys, database, "--run-id", "run-does-not-exist")[0] == 2
    assert run_worker(capsys, database, "--summary", str(tmp_path / "gone" / "s.json"))[0] == 2
    assert run_worker(capsys, tmp_path / "not-a-database.sqlite3",
                      "--workers", "2")[0] == 0  # A new database is created and migrated.
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"this is not a database")
    assert run_worker(capsys, broken)[0] == 2
    assert items(database) == []  # Nothing was claimed by any refused command.


def test_an_unsupported_schema_is_refused_with_zero_writes(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        connection.execute("PRAGMA user_version = 99")
        assert connection.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 0
    finally:
        connection.close()
    code, lines = run_worker(capsys, database)
    assert code == 2 and lines == []
    # Reopen by hand: open_database would refuse the row too.
    raw = sqlite3.connect(str(database), isolation_level=None)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 99
        assert raw.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 0
    finally:
        raw.close()


def test_cancel_only_requests_cancellation_and_exits_zero(tmp_path, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        run_id = queue.open_run(connection, CURRENT, 1, 3)
    finally:
        connection.close()
    code, lines = run_worker(capsys, database, "--cancel")
    assert code == 0
    payload = json.loads(lines[-1])
    assert (payload["state"], payload["runs"]) == ("cancel_requested", 1)
    assert queue.cancel_requested(connect(database), run_id) is True
    assert items(database) == [] and features(database) == []


# ---------------------------------------------------------------------------
# reuse: a stored analysis is never measured twice
# ---------------------------------------------------------------------------


def test_an_item_whose_analysis_is_stored_is_reused_without_extraction(tmp_path, monkeypatch,
                                                                       capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
    finally:
        connection.close()
    for item in items(database):
        row = [row for row in query(database, "SELECT * FROM samples ORDER BY path_key")
               if "sha256:" + row["content_sha256"] == item["sample_id"]][0]
        store_analysis_for(database, row, descriptor=batch.analysis_descriptor())
    before = features(database)

    def refuse(*arguments, **keywords):
        raise AssertionError("a stored analysis must never be measured again")

    monkeypatch.setattr(batch, "snapshot", refuse)
    monkeypatch.setattr(batch, "extract", refuse)
    code, lines = run_worker(capsys, database)
    assert code == 0
    payload = summary_of(lines)
    assert payload["counts"]["reused"] == 2 and payload["counts"]["analyzed"] == 0
    assert all(item["disposition"] == "reused" for item in items(database))
    assert features(database) == before


def test_a_commit_that_loses_the_race_keeps_the_stored_analysis(tmp_path, monkeypatch, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    real_extract = batch.extract
    stored = {}

    def racing_extract(data, path, role, fingerprint, version):
        payload = real_extract(data, path, role, fingerprint, version)
        changed = json.loads(json.dumps(payload))
        changed["features"]["measurements"][0]["value"] = 12345.0
        row = [row for row in query(database, "SELECT * FROM samples ORDER BY path_key")
               if row["content_sha256"] == fingerprint][0]
        # Another writer stores the real analysis between this item's claim and
        # its commit; the commit must keep it and record the item as reused.
        connection = connect(database)
        try:
            with transaction(connection):
                LibraryRepository(connection).store_analysis(
                    Sample.from_dict(payload), content_sha256=fingerprint,
                    sample_id=row["sample_id"], descriptor=batch.analysis_descriptor())
        finally:
            connection.close()
        stored[fingerprint] = payload
        return changed

    monkeypatch.setattr(batch, "extract", racing_extract)
    code, lines = run_worker(capsys, database)
    assert code == 0
    payload = summary_of(lines)
    assert payload["counts"]["reused"] == 2 and payload["counts"]["analyzed"] == 0
    assert len(stored) == 2
    assert 12345.0 not in {row["value"] for row in features(database)}
    for fingerprint, original in stored.items():
        row = [row for row in query(database, "SELECT * FROM samples ORDER BY path_key")
               if row["content_sha256"] == fingerprint][0]
        actual = [row for row in features(database, "sample_id = ?", (row["sample_id"],))]
        assert len(actual) == 19
        expected = {measurement["name"]: measurement["value"]
                    for measurement in original["features"]["measurements"]}
        assert {row["measurement"]: row["value"] for row in actual} == expected


# ---------------------------------------------------------------------------
# failures, retries and the closed stage/code tables
# ---------------------------------------------------------------------------


def corrupt_library(tmp_path, capsys, monkeypatch):
    """A library where one good file, four bad files and one denied file are queued."""

    root = build(tmp_path, {"good.wav": tone(110), "empty.wav": wav(np.zeros((0, 1))),
                            "nan.wav": wav([[float("nan")]], subtype="FLOAT"),
                            "truncated.wav": truncated_wav(),
                            "wide.wav": wav(np.zeros((16, 3))), "denied.wav": tone(330)})
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database)
    assert code == 1  # The scan refuses the two layouts the schema cannot store.
    assert summary["counts"]["queued_analysis"] == 4
    # The frozen schema stores one or two channels and a readable header, so the
    # wide and truncated files are indexed here for their content identity the way
    # a path row would be; the queue then reports the reader's own refusal. #167
    # owns per-path rows and wider channel layouts.
    index_files(database, [batch.local_path(root / "wide.wav"),
                           batch.local_path(root / "truncated.wav")])
    real_snapshot = batch.snapshot
    denied = batch.local_path(root / "denied.wav")

    def guarded_snapshot(path):
        if Path(path) == denied:
            raise PermissionError(13, "synthetic denial")
        return real_snapshot(path)

    monkeypatch.setattr(batch, "snapshot", guarded_snapshot)
    return root, database


def test_a_corrupt_or_unreadable_sample_fails_alone(tmp_path, monkeypatch, capsys):
    root, database = corrupt_library(tmp_path, capsys, monkeypatch)
    code, lines = run_worker(capsys, database)
    assert code == 1
    payload = summary_of(lines)
    assert payload["state"] == "complete"
    failures = {Path(record["path"]).name: record for record in payload["failures"]}
    assert set(failures) == {"denied.wav", "empty.wav", "nan.wav", "truncated.wav", "wide.wav"}
    assert (failures["empty.wav"]["stage"], failures["empty.wav"]["code"]) \
        == ("decode", "empty_audio")
    assert (failures["nan.wav"]["stage"], failures["nan.wav"]["code"]) == ("decode", "invalid_audio")
    assert (failures["truncated.wav"]["stage"], failures["truncated.wav"]["code"]) \
        == ("decode", "invalid_audio")
    assert (failures["wide.wav"]["stage"], failures["wide.wav"]["code"]) \
        == ("decode", "unsupported_channels")
    assert (failures["denied.wav"]["stage"], failures["denied.wav"]["code"]) \
        == ("read", "access_denied")
    assert failures["denied.wav"]["attempts"] == queue.MAX_ATTEMPTS
    assert all(failures[name]["attempts"] == 1 for name in
               ("empty.wav", "nan.wav", "truncated.wav", "wide.wav"))
    assert all(failures[name]["message"] for name in failures)
    assert failures["denied.wav"]["sample_id"].startswith("sha256:")
    # The good file completed, nothing fake was stored, and the run continued.
    assert payload["counts"]["complete"] == 1 and payload["counts"]["analyzed"] == 1
    assert len(features(database)) == 19
    assert named(database)["good.wav"]["state"] == "complete"
    assert all(named(database)[name]["state"] == "failed" for name in failures)
    # The database stays usable: a following run finds nothing left to do.
    assert run_worker(capsys, database)[0] == 0


def test_retry_failed_gives_a_retryable_failure_fresh_attempts(tmp_path, monkeypatch, capsys):
    root, database = corrupt_library(tmp_path, capsys, monkeypatch)
    assert run_worker(capsys, database)[0] == 1
    complete = named(database)["good.wav"]
    good_row = path_row(database, "good.wav")["sample_id"]
    before = features(database, "sample_id = ?", (good_row,))
    assert len(before) == 19
    code, lines = run_worker(capsys, database, "--retry-failed")
    assert code == 1
    payload = summary_of(lines)
    reset = {Path(record["path"]).name: record for record in payload["failures"]}
    assert reset["denied.wav"]["attempts"] == queue.MAX_ATTEMPTS
    assert reset["empty.wav"]["attempts"] == 1  # Terminal on the first attempt, every run.
    assert named(database)["denied.wav"]["attempts"] == queue.MAX_ATTEMPTS
    assert named(database)["empty.wav"]["attempts"] == 1
    assert named(database)["good.wav"] == complete
    assert features(database, "sample_id = ?", (good_row,)) == before


# ---------------------------------------------------------------------------
# content and version changes while queued
# ---------------------------------------------------------------------------


def test_a_queued_file_whose_bytes_changed_is_superseded_and_repaired_by_a_scan(tmp_path,
                                                                               monkeypatch,
                                                                               capsys):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    original = (root / "a.wav").read_bytes()
    (root / "a.wav").write_bytes(shift(original))
    assert len(shift(original)) == len(original)
    extracted = []
    real_extract = batch.extract

    def counting_extract(data, path, role, fingerprint, version):
        extracted.append(fingerprint)
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", counting_extract)
    code, lines = run_worker(capsys, database)
    assert code == 1
    payload = summary_of(lines)
    assert payload["counts"] == {"pending": 0, "running": 0, "complete": 1, "failed": 0,
                                 "cancelled": 0, "orphaned": 0, "superseded": 1, "analyzed": 1,
                                 "reused": 0, "remaining": 0}
    superseded = named(database)["a.wav"]
    assert (superseded["state"], superseded["error_stage"], superseded["error_code"]) \
        == ("superseded", "queue", "content_changed")
    changed_fingerprint = hashlib.sha256(shift(original)).hexdigest()
    assert changed_fingerprint not in extracted
    assert superseded["sample_id"] != "sha256:" + changed_fingerprint
    assert {row["sample_id"] for row in features(database)} \
        == {path_row(database, "b.wav")["sample_id"]}

    # The next scan repairs the row's content identity and queues the new one,
    # which a later run completes.
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["counts"]["modified"] == 1
    assert summary["counts"]["queued_analysis"] == 1
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert summary_of(lines)["counts"]["analyzed"] == 1
    repaired = named(database)["a.wav"]
    assert repaired["state"] == "complete"
    assert repaired["sample_id"] == "sha256:" + changed_fingerprint


def test_a_version_change_supersedes_leftover_work_and_keeps_the_old_features(tmp_path,
                                                                             monkeypatch,
                                                                             capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    assert run_worker(capsys, database)[0] == 0
    version_a = CURRENT
    old_features = features(database, "analysis_version = ?", (version_a,))
    assert len(old_features) == 2 * 19
    # An interrupted run at A can leave a pending item behind.
    connection = connect(database)
    try:
        connection.execute("UPDATE job_items SET state = 'pending', disposition = NULL, "
                           "run_id = NULL, attempts = 0 WHERE item_id = 1")
    finally:
        connection.close()
    monkeypatch.setattr(loudness, "ANALYSIS_VERSION", "loudness-version-b-synthetic")
    version_b = indexer.current_analysis_version()
    assert version_b != version_a
    extracted = []
    real_extract = batch.extract

    def counting_extract(data, path, role, fingerprint, version):
        extracted.append((fingerprint, version))
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", counting_extract)
    code, lines = run_worker(capsys, database)
    # An item this run superseded is one of its failure records, so the run is not
    # a clean one: 1, with the two B items analysed.
    assert code == 1
    payload = summary_of(lines)
    assert payload["analysis_version"] == version_b
    assert payload["counts"]["analyzed"] == 2 and payload["counts"]["superseded"] == 1
    older = items(database, "analysis_version = ?", (version_a,))
    assert [item["state"] for item in older] == ["superseded", "complete"]
    assert older[0]["error_code"] == "analysis_version_changed"
    assert older[0]["attempts"] == 0  # Never executed, only superseded.
    assert set(extracted) == {(row["content_sha256"], version_b)
                              for row in query(database, "SELECT * FROM samples")}
    assert len(features(database, "analysis_version = ?", (version_a,))) == 2 * 19
    assert features(database, "analysis_version = ?", (version_a,)) == old_features
    assert len(features(database, "analysis_version = ?", (version_b,))) == 2 * 19
    connection = connect(database)
    try:
        for row in query(database, "SELECT * FROM samples"):
            assert LibraryRepository(connection).get_sample(row["sample_id"], version_a)
            assert LibraryRepository(connection).get_sample(row["sample_id"], version_b)
    finally:
        connection.close()
    # A third run at B inserts nothing and extracts nothing.
    extracted.clear()
    code, lines = run_worker(capsys, database)
    assert code == 0 and extracted == []
    assert summary_of(lines)["counts"]["remaining"] == 0


# ---------------------------------------------------------------------------
# progress, reads and transactions
# ---------------------------------------------------------------------------


def test_progress_is_observable_from_another_connection_while_the_cli_runs(tmp_path, capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(6)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    child = ChildCli(tmp_path, "progress",
                     ["--database", str(database), "--workers", "1"],
                     {"TERA_TEST_EXTRACT_DELAY": "0.25"})
    observed = []
    connection = connect(database)
    try:
        deadline = time.monotonic() + 120
        while child.process.poll() is None and time.monotonic() < deadline:
            started = time.monotonic()
            status = queue.status(connection)
            assert time.monotonic() - started < 1.0
            observed.append(status)
            if status["state"] == "running" and status["current"] is not None:
                break
            time.sleep(0.05)
    finally:
        connection.close()
    code = child.wait()
    assert code == 0
    running = [status for status in observed if status["state"] == "running"]
    assert running and any(status["current"] is not None for status in running)
    in_flight = [status["current"] for status in running if status["current"] is not None][0]
    assert set(in_flight) == {"sample_id", "path", "attempts"} and in_flight["attempts"] == 1
    lines = child.lines()
    assert json.loads(lines[-1])["counts"]["complete"] == 6
    assert all(completion_line(line) for line in lines[:-1])
    # A run opened by another process is reported by its own id and state.
    assert running[0]["run_id"] is not None


def test_reads_and_pending_analysis_never_wait_for_a_blocked_extraction(tmp_path, monkeypatch,
                                                                       capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    release, started = threading.Event(), threading.Event()
    real_extract = batch.extract

    def blocked_extract(data, path, role, fingerprint, version):
        started.set()
        assert release.wait(20), "the test never released the extractor"
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", blocked_extract)
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.update(code=worker.main(["--database", str(database)])))
    thread.start()
    assert started.wait(30)
    reading = connect(database)
    try:
        for _ in range(20):
            assert reading.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
            assert reading.execute("SELECT COUNT(*) FROM sample_features").fetchone()[0] == 0
            assert reading.execute("SELECT COUNT(*) FROM analysis_versions").fetchone()[0] == 0
            assert len(indexer.pending_analysis(reading)) == 2
            # A short write by a second connection also succeeds while the worker
            # is decoding: no long-lived write transaction is held.
            with transaction(reading):
                reading.execute("UPDATE job_items SET error_message = error_message "
                                "WHERE item_id = -1")
    finally:
        reading.close()
    release.set()
    thread.join(60)
    capsys.readouterr()
    assert outcome["code"] == 0


def test_no_sqlite_transaction_is_held_while_a_snapshot_or_extraction_runs(tmp_path, monkeypatch,
                                                                          capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    captured = {}
    real_claim, real_snapshot, real_extract = queue.claim, batch.snapshot, batch.extract

    def claiming(connection, run_id, horizon=None):
        captured["connection"] = connection
        return real_claim(connection, run_id, horizon)

    def check(inner):
        def wrapper(*arguments, **keywords):
            path = arguments[0] if arguments else keywords.get("path")
            if not isinstance(path, bytes):
                assert captured["connection"].in_transaction is False
            return inner(*arguments, **keywords)
        return wrapper

    monkeypatch.setattr(queue, "claim", claiming)
    monkeypatch.setattr(batch, "snapshot", check(real_snapshot))
    monkeypatch.setattr(batch, "extract", check(real_extract))
    code, lines = run_worker(capsys, database)
    assert code == 0 and summary_of(lines)["counts"]["complete"] == 2
    assert "connection" in captured


# ---------------------------------------------------------------------------
# bounded workers, bounded memory and the UI-thread rule
# ---------------------------------------------------------------------------


def test_at_most_workers_snapshots_or_extractions_are_in_flight(tmp_path, monkeypatch, capsys):
    root = build(tmp_path, {f"file-{number:02d}.wav": tone(110 + number) for number in range(12)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    counter = {"now": 0, "max": 0}
    lock = threading.Lock()

    def tracked(inner):
        def wrapper(*arguments, **keywords):
            with lock:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            try:
                time.sleep(0.03)
                return inner(*arguments, **keywords)
            finally:
                with lock:
                    counter["now"] -= 1
        return wrapper

    monkeypatch.setattr(batch, "snapshot", tracked(batch.snapshot))
    monkeypatch.setattr(batch, "extract", tracked(batch.extract))
    code, lines = run_worker(capsys, database, "--workers", "4")
    assert code == 0 and summary_of(lines)["counts"]["complete"] == 12
    assert counter["max"] == 4 <= queue.MAX_WORKERS


def test_peak_memory_does_not_grow_with_the_queue_length(tmp_path, capsys):
    """A claimed item is bounded work: the queue length never enters memory.

    Every file holds distinct bytes, so the 200-file library really queues 200
    content identities: an earlier version of this test built the 200 files from
    40 tones, the scan stored those as 40 rows with 160 `duplicate` entries, and
    the run it measured was a 40-item one. The scan counts are asserted so the
    measurement cannot quietly degrade again.

    The 20-item run is always drawn first and an untraced warm-up runs before
    both, so the one-time allocations of the first extraction and the first
    printed line are not mistaken for queue growth.
    """

    def drain(count, label, trace):
        root = build(tmp_path, {f"{label}-{number:03d}.wav": tone(110 + number)
                                for number in range(count)}, label)
        database = tmp_path / (label + ".sqlite3")
        code, summary = run_scan(capsys, root, database)
        assert code == 0, summary
        assert summary["counts"]["added"] == count, summary
        assert summary["counts"]["duplicate"] == 0, summary
        if not trace:
            assert worker.main(["--database", str(database), "--workers", "2"]) == 0
            capsys.readouterr()
            return None
        tracemalloc.start()
        try:
            code = worker.main(["--database", str(database), "--workers", "4"])
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert code == 0
        assert json.loads(printed[-1])["counts"]["complete"] == count
        return peak

    assert drain(2, "warmup", False) is None
    small = drain(20, "small", True)
    large = drain(200, "large", True)
    assert large < 2 * small, (small, large)


def test_a_progress_line_reads_counts_not_the_summary_records(tmp_path, monkeypatch, capsys):
    """One item's line costs the item, not the run's remaining queue.

    `queue.summary` is the only call that builds the `failures` array — one
    record per unfinished item, so its size is the queue's — and the per-item
    line uses `queue.progress` instead. A twelve-item run must therefore reach
    the summary exactly once, for its final line, however many lines it prints.
    """

    root = build(tmp_path, {f"file-{number:03d}.wav": tone(110 + number)
                            for number in range(12)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    calls = []
    real = queue.summary

    def counted(connection, run_id):
        calls.append(run_id)
        return real(connection, run_id)

    monkeypatch.setattr(queue, "summary", counted)
    code, lines = run_worker(capsys, database, "--workers", "2")
    assert code == 0
    assert len(calls) == 1
    assert len(lines) == 13
    assert json.loads(lines[-1])["counts"]["complete"] == 12


def test_a_stdout_with_no_reader_left_does_not_fail_the_run(tmp_path, monkeypatch, capsys):
    """The desktop host stops reading the service's stdout once it is healthy.

    A per-item line reports an item that is already committed, so a refused
    write cannot change the run's outcome. Measured on this machine before the
    fix: the first completed item marked the whole run failed and left every
    other item pending, which is the only reason a 3345-file library imported
    one file per run.
    """

    root = build(tmp_path, {f"file-{number:03d}.wav": tone(110 + number)
                            for number in range(4)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0

    class Unreadable:
        """A stream whose descriptor has no reader: every write refuses."""

        def write(self, text):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(worker, "_PRINT_BROKEN", False)
    monkeypatch.setattr(sys, "stdout", Unreadable())
    code, printed = run_worker(capsys, database, "--workers", "1")
    assert code == 0
    assert printed == [], "the summary is retired with the channel"
    states = {item["state"] for item in items(database)}
    assert states == {queue.ITEM_COMPLETE}, states


def test_the_drain_reclaims_its_unreachable_cycles_on_the_documented_interval(
        tmp_path, monkeypatch, capsys):
    """The bounded-memory claim rests on a collection schedule, so pin it.

    One item's contract validation leaves reference cycles that only a
    generation-2 collection returns; `worker.COLLECT_INTERVAL` is how many
    finalized items may accumulate before the drain completes one. A twelve-item
    drain therefore collects exactly once, and never once per item.
    """

    import gc

    root = build(tmp_path, {f"file-{number:03d}.wav": tone(110 + number)
                            for number in range(12)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    collections = []
    real = gc.collect

    def counted(generation=2):
        collections.append(generation)
        return real(generation)

    monkeypatch.setattr(gc, "collect", counted)
    code, _lines = run_worker(capsys, database, "--workers", "2")
    assert code == 0
    assert worker.COLLECT_INTERVAL >= 1
    assert len(collections) == 12 // worker.COLLECT_INTERVAL
    assert all(generation == 2 for generation in collections)


def test_the_caller_keeps_the_run_id_while_extraction_runs_elsewhere(tmp_path, capsys):
    """`open_run` returns before any byte is read; the drain runs in another process.

    #27 owns the service integration: nothing here runs `run_queue` inline on a
    calling thread. The CLI adopts the caller-opened run with `--run-id`.
    """

    root, database = library_with_two_files(tmp_path, capsys)
    extractors = tmp_path / "extractors.txt"
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 2
        run_id = queue.open_run(connection, CURRENT, 1, queue.MAX_ATTEMPTS)
        # The caller already holds the run id and nothing has been read yet.
        assert features(database) == [] and items(database)
        assert queue.status(connection, run_id)["counts"]["pending"] == 2
        child = ChildCli(tmp_path, "adopt",
                         ["--database", str(database), "--run-id", run_id],
                         {"TERA_TEST_EXTRACT_DELAY": "0.4",
                          "TERA_TEST_PID_FILE": str(extractors)})
        blocked = False
        status = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and child.process.poll() is None:
            status = queue.status(connection, run_id)
            if status["current"] is not None:
                blocked = True
                break
            time.sleep(0.05)
        assert blocked, "the run never reported an in-flight item"
        assert status["state"] == "running"
        code = child.wait()
    finally:
        connection.close()
    assert code == 0
    written = [line.split() for line in extractors.read_text(encoding="utf-8").splitlines()]
    assert written
    assert all(int(pid) != os.getpid() for pid, _ident in written)
    payload = json.loads(child.lines()[-1])
    assert payload["run_id"] == run_id and payload["counts"]["complete"] == 2


# ---------------------------------------------------------------------------
# no Jev, no network, local audio
# ---------------------------------------------------------------------------


def test_a_run_imports_no_jev_module_makes_no_network_call_and_leaves_the_tree_alone(
        tmp_path, monkeypatch, capsys):
    root, database = library_with_two_files(tmp_path, capsys)
    before = tree_state(root)
    monkeypatch.delitem(sys.modules, "backend.intelligence", raising=False)

    def refuse(*arguments, **keywords):
        raise AssertionError("the worker must not use the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    summary_file = tmp_path / "summary.json"
    code, lines = run_worker(capsys, database, "--summary", str(summary_file))
    assert code == 0 and summary_of(lines)["counts"]["complete"] == 2
    assert tree_state(root) == before
    assert set(tree_state(root)) == {"a.wav", "b.wav"}
    assert "backend.intelligence" not in sys.modules
    written = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert written <= {"summary.json", "library.sqlite3", "library.sqlite3-wal",
                       "library.sqlite3-shm"}
    assert {path.name for path in tmp_path.iterdir() if path.is_dir()} == {"library"}


def test_the_worker_modules_import_only_the_standard_library_and_backend():
    for name in ("queue.py", "worker.py"):
        source = (REPOSITORY_ROOT / "backend" / "library" / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [module for module in imported if module.startswith("backend.intelligence")]
        assert not [module for module in imported if module.split(".")[0]
                    in {"socket", "urllib", "http", "requests"}]
        outside = {module.split(".")[0] for module in imported} \
            - set(sys.stdlib_module_names) - {"backend"}
        assert outside == set()
