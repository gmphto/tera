"""Queue recovery: killed owners, leases, cancellation and lost samples.

Every tree and database here is synthetic and temporary. The cases follow the
criteria of issue #23 that are about what happens when a run does not simply
finish: a killed owner, a slow item that must not look dead, cancellation from a
second connection, a sample row that disappears under the queue, and a storage
failure that stops the run without losing committed work.
"""

import json
import threading
import time

from backend.analysis import batch
from backend.library import queue, worker
from backend.library.errors import WriteFailed
from backend.library.repository import LibraryRepository
from tests.test_library_queue import (
    CURRENT,
    build,
    connect,
    features,
    items,
    path_row,
    query,
    run_scan,
    tone,
)
from tests.test_library_worker import ChildCli, named, run_worker, summary_of


def age_the_lease(database, run_id):
    """Expire one run's lease without waiting `LEASE_SECONDS` for real."""

    connection = connect(database)
    try:
        connection.execute("UPDATE job_runs SET heartbeat_at = ? WHERE run_id = ?",
                           ("2000-01-01T00:00:00Z", run_id))
    finally:
        connection.close()


def identity_of(database, relative):
    return "sha256:" + path_row(database, relative)["content_sha256"]


def row_identity_for(database, sample_id):
    """The stored `samples` row identity behind one item's content identity."""

    for row in query(database, "SELECT * FROM samples ORDER BY path_key"):
        if "sha256:" + row["content_sha256"] == sample_id:
            return row["sample_id"]
    raise AssertionError(f"no stored row for {sample_id}")


# ---------------------------------------------------------------------------
# a killed owner
# ---------------------------------------------------------------------------


def test_a_killed_owner_loses_at_most_its_in_flight_item(tmp_path, capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(5)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    child = ChildCli(tmp_path, "killed", ["--database", str(database), "--workers", "1"],
                     {"TERA_TEST_EXTRACT_DELAY": "0.5"})
    connection = connect(database)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            finished = items(database, "state = 'complete'")
            if finished:
                break
            assert child.process.poll() is None, "the run ended before the kill"
            time.sleep(0.05)
        assert finished
        child.process.kill()
        assert child.wait() is not None
        killed = [run for run in query(database, "SELECT * FROM job_runs")][0]
        stored = items(database)
        assert [item["state"] for item in stored].count("complete") \
            == len(query(database, "SELECT * FROM sample_features GROUP BY sample_id"))
        assert all(item["state"] in ("complete", "running", "pending") for item in stored)
        assert all(item["error_code"] is None for item in stored)
    finally:
        connection.close()

    completed_before = {item["sample_id"] for item in items(database, "state = 'complete'")}
    extract_calls = []
    real_extract = batch.extract
    original = batch.extract

    def counting_extract(data, path, role, fingerprint, version):
        extract_calls.append(fingerprint)
        return original(data, path, role, fingerprint, version)

    batch.extract = counting_extract
    try:
        age_the_lease(database, killed["run_id"])
        code, lines = run_worker(capsys, database, "--workers", "2")
    finally:
        batch.extract = real_extract
    assert code == 0
    payload = summary_of(lines)
    # The summary counts this run's items; the whole library is finished.
    assert payload["counts"]["remaining"] == 0
    assert {item["state"] for item in items(database)} == {"complete"}
    assert len(features(database)) == 5 * 19
    before = [run for run in query(database, "SELECT * FROM job_runs")
              if run["run_id"] == killed["run_id"]][0]
    assert before["state"] == "interrupted" and before["finished_at"] is not None
    assert not [fingerprint for fingerprint in extract_calls
                if "sha256:" + fingerprint in completed_before]
    # The item that was in flight when the owner died kept its attempt and was
    # finished by the second run.
    attempts = {item["sample_id"]: item["attempts"] for item in items(database)}
    assert attempts
    assert all(1 <= attempt <= 2 for attempt in attempts.values())


def test_a_slow_item_is_never_mistaken_for_a_dead_owner(tmp_path, monkeypatch, capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(4)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    child = ChildCli(tmp_path, "lease", ["--database", str(database), "--workers", "1"],
                     {"TERA_TEST_EXTRACT_DELAY": "1.0",
                      "TERA_TEST_LEASE_SECONDS": "3",
                      "TERA_TEST_HEARTBEAT_SECONDS": "1",
                      "TERA_TEST_PID_FILE": str(tmp_path / "heartbeats.txt")})
    connection = connect(database)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not query(database, "SELECT * FROM job_runs"):
            time.sleep(0.05)
            assert child.process.poll() is None, "the run ended before the check"
        assert query(database, "SELECT * FROM job_runs")
        live = query(database, "SELECT * FROM job_runs")[0]
        # The owner is still healthy: its heartbeat is refreshed while the slow
        # item runs, so a second process is refused and writes nothing.
        time.sleep(1.5)
        code, lines = run_worker(capsys, database)
        assert code == 2 and lines == []
        assert [run["run_id"] for run in query(database, "SELECT * FROM job_runs")] \
            == [live["run_id"]]
        code = child.wait()
    finally:
        connection.close()
    assert code == 0
    payload = json.loads(child.lines()[-1])
    assert payload["counts"]["complete"] == 4 and payload["state"] == "complete"
    assert all(item["attempts"] == 1 for item in items(database))

    # New work arrives, the owner is killed with two items left, its lease
    # expires, and the next process reconciles the abandoned run and finishes the
    # library.
    for number in range(3):
        (root / f"late-{number}.wav").write_bytes(tone(440 + number))
    assert run_scan(capsys, root, database)[0] == 0
    earlier = {run["run_id"] for run in query(database, "SELECT * FROM job_runs")}
    second = ChildCli(tmp_path, "takeover", ["--database", str(database), "--workers", "1"],
                      {"TERA_TEST_EXTRACT_DELAY": "1.0",
                       "TERA_TEST_LEASE_SECONDS": "3",
                       "TERA_TEST_HEARTBEAT_SECONDS": "1"})
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if len(items(database, "state = 'complete'")) > 4:
            break
        assert second.process.poll() is None, "the run ended before the kill"
        time.sleep(0.05)
    assert len(items(database, "state = 'complete'")) > 4
    second.process.kill()
    second.wait()
    abandoned = [run for run in query(database, "SELECT * FROM job_runs")
                 if run["run_id"] not in earlier]
    assert len(abandoned) == 1
    run_id = abandoned[0]["run_id"]
    age_the_lease(database, run_id)
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert summary_of(lines)["counts"]["remaining"] == 0
    assert len(features(database)) == 7 * 19
    interrupted = [run for run in query(database, "SELECT * FROM job_runs")
                   if run["run_id"] == run_id][0]
    assert interrupted["state"] == "interrupted" and interrupted["finished_at"] is not None
    assert all(item["state"] == "complete" for item in items(database))


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


def test_cancellation_mid_file_and_mid_queue_leaves_finished_work_readable(tmp_path, monkeypatch,
                                                                          capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(3)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    calls, release, blocked = [], threading.Event(), threading.Event()
    real_extract = batch.extract

    def instrumented_extract(data, path, role, fingerprint, version):
        calls.append(fingerprint)
        if len(calls) == 2:
            blocked.set()
            assert release.wait(20), "the test never released the extractor"
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", instrumented_extract)
    outcome = {}
    thread = threading.Thread(
        target=lambda: outcome.update(code=worker.main(["--database", str(database)])))
    thread.start()
    assert blocked.wait(60)
    connection = connect(database)
    try:
        finished = items(database, "state = 'complete'")
        assert len(finished) == 1
        first = finished[0]
        first_row = row_identity_for(database, first["sample_id"])
        stored = LibraryRepository(connection).get_sample(first_row, CURRENT)
        assert stored.sample.sample_id == first_row
        # A second process asks for cancellation while the second item is in
        # flight; nothing new starts after the flag is set.
        code, lines = run_worker(capsys, database, "--cancel")
        assert code == 0 and json.loads(lines[-1])["runs"] == 1
        release.set()
        thread.join(60)
    finally:
        connection.close()
    assert outcome["code"] == 130
    assert len(calls) == 2  # The third item never started.
    stored = items(database)
    assert [item["state"] for item in stored] == ["complete", "cancelled", "cancelled"]
    assert stored[1]["error_code"] is None and stored[2]["error_code"] is None
    assert stored[1]["attempts"] == 1 and stored[2]["attempts"] == 0
    run_id = stored[0]["run_id"]
    cancelled_run = [run for run in query(database, "SELECT * FROM job_runs")
                     if run["run_id"] == run_id][0]
    assert cancelled_run["state"] == "cancelled" and cancelled_run["finished_at"] is not None
    # The finished item is still readable and the cancelled identities are
    # revived by the next run, which finishes the library.
    assert {row["sample_id"] for row in features(database)} == {first_row}
    monkeypatch.undo()
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert summary_of(lines)["counts"]["remaining"] == 0
    assert all(item["state"] == "complete" for item in items(database))
    assert len(features(database)) == 3 * 19


# ---------------------------------------------------------------------------
# the sample row disappears
# ---------------------------------------------------------------------------


def test_a_sample_removed_before_the_claim_is_orphaned(tmp_path, capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(3)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    removed = identity_of(database, "file-1.wav")
    connection = connect(database)
    try:
        assert queue.enqueue(connection, CURRENT) == 3
        LibraryRepository(connection).delete_sample(path_row(database, "file-1.wav")["sample_id"])
    finally:
        connection.close()
    code, lines = run_worker(capsys, database)
    assert code == 1
    payload = summary_of(lines)
    assert payload["counts"] == {"pending": 0, "running": 0, "complete": 2, "failed": 0,
                                 "cancelled": 0, "orphaned": 1, "superseded": 0, "analyzed": 2,
                                 "reused": 0, "remaining": 0}
    failure = payload["failures"][0]
    assert (failure["stage"], failure["code"]) == ("queue", "sample_missing")
    assert failure["sample_id"] == removed
    assert len(query(database, "SELECT * FROM samples")) == 2  # No row was recreated.
    assert len(features(database)) == 2 * 19
    assert all(item["state"] != "orphaned" or item["disposition"] is None
               for item in items(database))


def test_a_sample_removed_between_the_claim_and_the_commit_is_orphaned(tmp_path, monkeypatch,
                                                                      capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(3)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    real_extract = batch.extract
    removed = {"sample_id": path_row(database, "file-2.wav")["sample_id"],
               "content": identity_of(database, "file-2.wav")}

    def removing_extract(data, path, role, fingerprint, version):
        payload = real_extract(data, path, role, fingerprint, version)
        row = [row for row in query(database, "SELECT * FROM samples ORDER BY path_key")
               if row["content_sha256"] == fingerprint]
        if row and row[0]["sample_id"] == removed["sample_id"]:
            connection = connect(database)
            try:
                LibraryRepository(connection).delete_sample(removed["sample_id"])
            finally:
                connection.close()
        return payload

    monkeypatch.setattr(batch, "extract", removing_extract)
    code, lines = run_worker(capsys, database)
    assert code == 1
    payload = summary_of(lines)
    assert payload["counts"]["orphaned"] == 1 and payload["counts"]["complete"] == 2
    assert payload["counts"]["analyzed"] == 2
    failure = payload["failures"][0]
    assert (failure["stage"], failure["code"]) == ("queue", "sample_missing")
    assert failure["sample_id"] == removed["content"]
    assert failure["path"].endswith("file-2.wav")
    assert len(query(database, "SELECT * FROM samples")) == 2
    assert {row["sample_id"] for row in features(database)} == {
        path_row(database, "file-0.wav")["sample_id"],
        path_row(database, "file-1.wav")["sample_id"]}


# ---------------------------------------------------------------------------
# a storage failure
# ---------------------------------------------------------------------------


def test_a_storage_failure_stops_the_run_and_keeps_committed_items_readable(tmp_path, monkeypatch,
                                                                           capsys):
    root = build(tmp_path, {f"file-{number}.wav": tone(110 + number) for number in range(3)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    real_store = LibraryRepository.store_analysis
    calls = {"count": 0}

    def failing_store(self, sample, *, content_sha256, sample_id=None, descriptor=None):
        calls["count"] += 1
        if calls["count"] == 2:
            raise WriteFailed("synthetic storage failure")
        return real_store(self, sample, content_sha256=content_sha256, sample_id=sample_id,
                          descriptor=descriptor)

    monkeypatch.setattr(LibraryRepository, "store_analysis", failing_store)
    code, lines = run_worker(capsys, database, "--workers", "1")
    assert code == 2
    payload = summary_of(lines)
    assert payload["state"] == "failed" and payload["finished_at"] is not None
    assert payload["counts"]["complete"] == 1
    assert payload["counts"]["pending"] + payload["counts"]["running"] == 2
    assert len(features(database)) == 19
    connection = connect(database)
    try:
        committed = [row for row in query(database, "SELECT * FROM samples ORDER BY path_key")
                     if row["sample_id"] in {item["sample_id"] for item in features(database)}]
        assert committed
        stored = LibraryRepository(connection).get_sample(committed[0]["sample_id"], CURRENT)
        assert len(stored.sample.features.measurements) == 19
    finally:
        connection.close()

    # The in-flight item comes back at the next reconciliation and the following
    # run finishes the library without re-measuring what was committed.
    monkeypatch.setattr(LibraryRepository, "store_analysis", real_store)
    extract_calls = []
    real_extract = batch.extract

    def counting_extract(data, path, role, fingerprint, version):
        extract_calls.append(fingerprint)
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", counting_extract)
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert summary_of(lines)["counts"]["complete"] == 2
    assert len(features(database)) == 3 * 19
    assert all(item["state"] == "complete" for item in items(database))


# ---------------------------------------------------------------------------
# a scan during a drain
# ---------------------------------------------------------------------------


def test_a_scan_mid_drain_adds_work_without_duplicating_it(tmp_path, monkeypatch, capsys):
    root = build(tmp_path, {"first.wav": tone(110), "second.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    calls, release, blocked = [], threading.Event(), threading.Event()
    real_extract = batch.extract

    def instrumented_extract(data, path, role, fingerprint, version):
        calls.append(fingerprint)
        if len(calls) == 1:
            blocked.set()
            assert release.wait(20), "the test never released the extractor"
        return real_extract(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", instrumented_extract)
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.update(
        code=worker.main(["--database", str(database), "--once"])))
    thread.start()
    assert blocked.wait(60)
    # A scan runs while the worker drains: the new content identity gets one item,
    # and the item already queued is not duplicated.
    (root / "third.wav").write_bytes(tone(330))
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["counts"]["added"] == 1
    # The derived queue counts what still lacks analysis; the scan writes no job
    # row, so the new identity waits for the next enqueue.
    assert summary["counts"]["queued_analysis"] == 3
    assert len(items(database)) == 2
    release.set()
    thread.join(60)
    capsys.readouterr()
    assert outcome["code"] == 0
    # --once processed only the work derived at start: the two identities it
    # derived were extracted, the scan's new identity was never enqueued and no
    # extraction touched it.
    assert "third.wav" not in named(database)
    assert len(calls) == 2
    assert identity_of(database, "third.wav") not in {"sha256:" + call for call in calls}
    code, lines = run_worker(capsys, database)
    assert code == 0
    assert summary_of(lines)["counts"]["complete"] == 1
    assert named(database)["third.wav"]["state"] == "complete"
    assert all(item["state"] == "complete" for item in items(database))
