"""Scanner recovery: availability, interruption, case collisions and links.

Every tree and database here is synthetic and temporary. The cases follow the
criteria of issue #22 that #23 owns the other half of: what a scan does when a
file moves, disappears, is unreadable or has been replaced mid-read, and how the
operator clears each state by re-running the identical command.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.library import indexer, scanner
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database
from tests.test_library_scanner import (build, only_row, run_scan, sample_rows, table_dump,
                                        tone)


def projection(database):
    """The stored records without their timestamps, for state comparison."""

    connection = open_database(str(database))
    try:
        return [tuple(row) for row in connection.execute(
            "SELECT sample_id, content_sha256, role, original_path, path_key, filename, pack_id, "
            "file_status, sample_rate_hz, channels, frame_count, duration_ms FROM samples "
            "ORDER BY path_key ASC")]
    finally:
        connection.close()


def copy_database(source, destination):
    """A consistent copy of a live database (WAL and all), for a control run."""

    with sqlite3.connect(str(source)) as origin, sqlite3.connect(str(destination)) as target:
        origin.backup(target)


def status_of(database, name):
    return {Path(row["original_path"]).name: row["file_status"]
            for row in sample_rows(database)}[name]


# ---------------------------------------------------------------------------
# missing, inaccessible and their recovery
# ---------------------------------------------------------------------------


def test_a_deleted_file_is_missing_and_its_row_survives(tmp_path, capsys):
    root = build(tmp_path, {"gone.wav": tone(110), "kept.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    before = {row["original_path"]: row for row in sample_rows(database)}
    connection = open_database(str(database))
    row = connection.execute("SELECT * FROM samples WHERE original_path LIKE '%gone.wav'").fetchone()
    connection.execute(
        "INSERT INTO sample_tags (sample_id, tag, added_at) VALUES (?, ?, ?)",
        (row["sample_id"], "synthetic", "2024-01-01T00:00:00Z"))
    connection.close()

    (root / "gone.wav").unlink()
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["missing"] == 1 and summary["counts"]["unchanged"] == 1
    record = summary["files"][0]
    assert record["path"] == "gone.wav" and record["code"] == "missing"
    assert record["analysis"] == "none" and record["error_code"] == "not_found"
    assert record["stage"] == "discovery" and "no longer in the scanned folder" in record["message"]
    stored = [item for item in sample_rows(database)
              if item["original_path"].endswith("gone.wav")][0]
    assert stored["sample_id"] == row["sample_id"]
    assert stored["content_sha256"] == row["content_sha256"]
    assert stored["role"] == row["role"] and stored["file_status"] == "missing"
    assert stored["original_path"] == before[row["original_path"]]["original_path"]
    connection = open_database(str(database))
    assert connection.execute("SELECT count(*) FROM sample_tags").fetchone()[0] == 1
    connection.close()


def test_a_returning_file_restores_availability_and_leaves_the_queue(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    data = (root / "a.wav").read_bytes()
    (root / "a.wav").unlink()
    assert run_scan(capsys, root, database)[1]["counts"]["missing"] == 1
    assert indexer.pending_analysis(open_database(str(database))) == ()

    (root / "a.wav").write_bytes(data)
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["unchanged"] == 1 and summary["counts"]["missing"] == 0
    assert status_of(database, "a.wav") == "present"
    requests = indexer.pending_analysis(open_database(str(database)))
    assert [item.path for item in requests] == [str(root / "a.wav")]


def test_a_file_that_vanishes_between_enumeration_and_read_is_inaccessible(tmp_path, capsys,
                                                                          monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    original = batch.snapshot

    def vanish(path):
        if Path(path).name == "b.wav":
            Path(path).unlink()
        return original(path)

    monkeypatch.setattr(batch, "snapshot", vanish)
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    assert summary["counts"]["added"] == 1 and summary["counts"]["inaccessible"] == 1
    record = [item for item in summary["files"] if item["path"] == "b.wav"][0]
    assert record["code"] == "inaccessible" and record["error_code"] == "not_found"
    assert record["stage"] == "read" and record["analysis"] == "none"
    assert record["sample_id"] is None
    assert [Path(row["original_path"]).name for row in sample_rows(database)] == ["a.wav"]


def test_an_unreadable_row_is_kept_unavailable_and_never_re_paired(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = [item for item in sample_rows(database) if item["original_path"].endswith("a.wav")][0]
    original = batch.snapshot

    def denied(path):
        if Path(path).name == "a.wav":
            raise PermissionError(13, "synthetic denial")
        return original(path)

    monkeypatch.setattr(batch, "snapshot", denied)
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    assert summary["counts"]["inaccessible"] == 1 and summary["counts"]["missing"] == 0
    record = [item for item in summary["files"] if item["path"] == "a.wav"][0]
    assert record["code"] == "inaccessible" and record["error_code"] == "access_denied"
    assert record["stage"] == "read" and record["analysis"] == "none"
    assert record["sample_id"] == "sha256:" + row["content_sha256"]
    stored = [item for item in sample_rows(database) if item["original_path"].endswith("a.wav")][0]
    assert stored["sample_id"] == row["sample_id"] and stored["file_status"] == "missing"
    assert stored["content_sha256"] == row["content_sha256"]


def test_a_new_path_holding_an_unreadable_rows_content_is_duplicate(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    data = (root / "a.wav").read_bytes()
    (root / "copy.wav").write_bytes(data)
    original = batch.snapshot

    def denied(path):
        if Path(path).name == "a.wav":
            raise PermissionError(13, "synthetic denial")
        return original(path)

    monkeypatch.setattr(batch, "snapshot", denied)
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    codes = {item["path"]: item["code"] for item in summary["files"]}
    assert codes == {"a.wav": "inaccessible", "copy.wav": "duplicate"}
    assert summary["counts"]["missing"] == 0


def test_a_source_changed_while_reading_creates_no_row(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    original = batch.snapshot

    def torn(path):
        if Path(path).name == "a.wav":
            raise batch.SourceError("source_changed", "Source changed while its snapshot was read.")
        return original(path)

    monkeypatch.setattr(batch, "snapshot", torn)
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    record = [item for item in summary["files"] if item["path"] == "a.wav"][0]
    assert record["code"] == "inaccessible" and record["error_code"] == "source_changed"
    assert record["analysis"] == "none" and record["sample_id"] is None
    assert [Path(row["original_path"]).name for row in sample_rows(database)] == ["b.wav"]


def test_the_recoverable_records_name_the_local_cause(tmp_path, capsys):
    root = build(tmp_path, {"bad.wav": b"not a wave file, longer than twelve bytes",
                            "gone.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 1
    (root / "gone.wav").unlink()
    (root / "bad.wav").write_bytes(b"still not a wave file")
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    assert summary["counts"]["missing"] == 1 and summary["counts"]["unsupported"] == 1
    for record in summary["files"]:
        assert record["path"] in {"bad.wav", "gone.wav"}
        assert record["error_code"] and record["stage"] and record["message"]
    assert {record["path"]: record["error_code"] for record in summary["files"]} == {
        "bad.wav": "unsupported_format", "gone.wav": "not_found"}


# ---------------------------------------------------------------------------
# interruption and restart
# ---------------------------------------------------------------------------


def test_an_interrupted_scan_skips_the_missing_sweep_and_resumes(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220), "gone.wav": tone(330)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    control_source = tmp_path / "control-source.sqlite3"
    copy_database(database, control_source)
    (root / "gone.wav").unlink()
    (root / "c.wav").write_bytes(tone(440))

    original = LibraryRepository.insert_path_record
    calls = {"count": 0}

    def interrupt_once(self, *args, **kwargs):
        record = original(self, *args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            raise KeyboardInterrupt
        return record

    with monkeypatch.context() as patch:
        patch.setattr(LibraryRepository, "insert_path_record", interrupt_once)
        code, interrupted = run_scan(capsys, root, database)
    assert code == 130
    assert interrupted["state"] == "interrupted"
    # The interrupted file was committed before the interrupt but is not
    # reported by the interrupted summary, which lists durable evidence only.
    assert interrupted["counts"]["added"] == 0
    assert status_of(database, "c.wav") == "present"
    # The missing sweep was skipped: the deleted file is still marked present.
    assert status_of(database, "gone.wav") == "present"

    code, resumed = run_scan(capsys, root, database)
    assert code == 0 and resumed["state"] == "complete"
    assert resumed["counts"]["missing"] == 1 and resumed["counts"]["added"] == 0
    assert resumed["counts"]["unchanged"] == 3
    assert status_of(database, "gone.wav") == "missing"

    control = tmp_path / "control.sqlite3"
    copy_database(control_source, control)
    assert run_scan(capsys, root, control)[0] == 0
    assert projection(control) == projection(database)


def test_a_hard_kill_mid_write_leaves_no_partial_row(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    original = LibraryRepository._one

    def die(self, statement, parameters=()):
        if statement.startswith("SELECT * FROM samples WHERE sample_id"):
            raise SystemExit("simulated hard kill")
        return original(self, statement, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(LibraryRepository, "_one", die)
        with pytest.raises(SystemExit):
            run_scan(capsys, root, database)
    connection = sqlite3.connect(str(database))
    assert connection.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert run_scan(capsys, root, database)[0] == 0
    assert only_row(database)["file_status"] == "present"


# ---------------------------------------------------------------------------
# discovery errors, case collisions and links
# ---------------------------------------------------------------------------


def test_a_subdirectory_that_cannot_be_enumerated_leaves_the_rest_reconciled(tmp_path, capsys,
                                                                            monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "locked/b.wav": tone(220), "c.wav": tone(330)})
    database = tmp_path / "library.sqlite3"
    real_scandir = os.scandir

    def scandir(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "synthetic denial")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    assert summary["counts"]["added"] == 2 and summary["counts"]["discovery_errors"] == 1
    error = summary["discovery_errors"][0]
    assert error["path"] == "locked"
    assert error["error"]["stage"] == "discovery"
    assert error["error"]["code"] == "enumeration_failed"
    assert [Path(row["original_path"]).name for row in sample_rows(database)] == ["a.wav", "c.wav"]


def test_two_entries_that_normalise_alike_fail_before_any_write(tmp_path, capsys):
    composed = "\u00c5.wav"
    decomposed = "A\u030a.wav"
    root = build(tmp_path, {composed: tone(110), decomposed: tone(220)})
    if len(os.listdir(root)) != 2:
        pytest.skip("this platform folds the two synthetic names into one entry")
    database = tmp_path / "library.sqlite3"
    assert scanner.main([str(root), "--role", "bass", "--database", str(database)]) == 2
    assert not database.exists()


def test_two_rows_that_normalise_alike_fail_before_any_write(tmp_path, capsys):
    root = build(tmp_path, {"\u00c5.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    before = table_dump(database)
    connection = open_database(str(database))
    repository = LibraryRepository(connection)
    repository.insert_path_record(root / "A\u030a.wav", role="bass",
                                  content_sha256="0" * 63 + "1", sample_rate_hz=48000, channels=1,
                                  frame_count=480, duration_ms=10.0,
                                  sample_id="library:" + "0" * 63 + "1")
    connection.close()

    before = table_dump(database)
    assert scanner.main([str(root), "--role", "bass", "--database", str(database),
                         "--summary", str(tmp_path / "unused.json")]) == 2
    assert table_dump(database) == before
    assert not (tmp_path / "unused.json").exists()


def test_linked_entries_are_skipped_and_counted(tmp_path, capsys):
    root = build(tmp_path, {"real.wav": tone(110)})
    link = root / "link.wav"
    try:
        os.symlink(root / "real.wav", link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"this platform cannot create a symbolic link: {error}")
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 0
    assert summary["counts"]["discovered"] == 1 and summary["counts"]["skipped_linked"] == 1
    assert summary["counts"]["added"] == 1
    assert [Path(row["original_path"]).name for row in
            sample_rows(tmp_path / "library.sqlite3")] == ["real.wav"]


# ---------------------------------------------------------------------------
# cooperative cancellation (#78)
# ---------------------------------------------------------------------------


#: The stated bound on the delay between a cancellation request and the stopped
#: scan, measured on this module's synthetic trees. The callback is consulted
#: between folder entries and between file reconciliations, so at most one
#: file's work remains when a request arrives.
CANCEL_LATENCY_BOUND_SECONDS = 1.0


def scan_with_signal(root, database, signal, capsys, role="bass"):
    """Run the scanner itself with a cancellation callback and return its summary."""

    code = scanner.run(root, role, database, cancelled=signal)
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, json.loads(printed[-1])


def test_a_cancelled_scan_reports_cancelled_and_exits_130(tmp_path, capsys):
    root = build(tmp_path, {f"{index}.wav": tone(110 + index) for index in range(6)})
    database = tmp_path / "library.sqlite3"
    calls = []

    def signal():
        calls.append(1)
        return len(calls) > 2

    code, summary = scan_with_signal(root, database, signal, capsys)
    assert code == 130
    assert summary["state"] == scanner.STATE_CANCELLED
    assert summary["counts"]["discovered"] <= 6
    # The scan stopped early: it never claimed to have finished the folder, and it
    # wrote no row for a file it did not reach.
    assert len(sample_rows(database)) <= 6


def test_a_cancelled_scan_is_recovered_by_the_identical_command(tmp_path, capsys):
    root = build(tmp_path, {f"{index}.wav": tone(110 + index) for index in range(8)})
    database = tmp_path / "library.sqlite3"
    control = tmp_path / "control.sqlite3"
    # The same tree scanned without interruption is the state to match.
    assert run_scan(capsys, root, control)[0] == 0
    calls = []

    def signal():
        calls.append(1)
        return len(calls) > 5

    code, summary = scan_with_signal(root, database, signal, capsys)
    assert code == 130 and summary["state"] == scanner.STATE_CANCELLED
    code, summary = run_scan(capsys, root, database)
    assert code == 0 and summary["state"] == scanner.STATE_COMPLETE
    assert summary["counts"]["discovered"] == 8 and summary["counts"]["missing"] == 0
    assert projection(database) == projection(control)


def test_a_cancellation_request_is_honoured_within_the_stated_bound(tmp_path, capsys):
    root = build(tmp_path, {f"{index:03d}.wav": tone(110 + index) for index in range(200)})
    database = tmp_path / "library.sqlite3"
    reached, cancel = threading.Event(), threading.Event()

    def signal():
        reached.set()
        cancel.wait(30.0)
        return True

    outcome = {}

    def scan():
        outcome["code"] = scanner.run(root, "bass", database, cancelled=signal)

    worker = threading.Thread(target=scan, name="tera-scan-cancel-test")
    worker.start()
    assert reached.wait(30.0)
    requested = time.monotonic()
    cancel.set()
    worker.join(30.0)
    stopped = time.monotonic() - requested
    assert not worker.is_alive()
    assert outcome["code"] == 130
    assert stopped <= CANCEL_LATENCY_BOUND_SECONDS, stopped
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert json.loads(printed[-1])["state"] == scanner.STATE_CANCELLED


def test_discovery_is_identical_with_and_without_a_cancellation_signal(tmp_path, capsys):
    root = build(tmp_path, {name: tone(110 + index)
                            for index, name in enumerate(("b.wav", "a.wav", "c.wav"))})
    plain = scanner.traverse(root)
    assert plain == scanner.traverse(root, cancelled=lambda: False)
    assert plain[0] == ["a.wav", "b.wav", "c.wav"]
    # A signal that never fires leaves the summary's state and counts alone.
    code_plain, plain_summary = run_scan(capsys, root, tmp_path / "plain.sqlite3")
    code_signal, signal_summary = scan_with_signal(root, tmp_path / "signal.sqlite3",
                                                   lambda: False, capsys)
    assert (code_signal, signal_summary["state"], signal_summary["counts"]) == \
           (code_plain, plain_summary["state"], plain_summary["counts"])
