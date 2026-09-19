"""The folder scanner: traversal, format policy, reconciliation and queueing.

Every database is temporary and every tree is synthetic; no real library path,
sample name or audio byte is part of this file. See
`tests/test_library_scan_recovery.py` for the interruption, availability and
link cases.
"""

import hashlib
import json
import os
import sqlite3
import struct
from pathlib import Path

import numpy as np
import pytest

from backend import audio
from backend.analysis import batch, harmony, loudness, spectral, transient
from backend.audio import AudioErrorCode, AudioReadError, load_wav_bytes
from backend.contracts import MEASURES
from backend.library import indexer, scanner
from backend.library.errors import IncompleteFeatures, UnknownAnalysisVersion
from backend.library.repository import LibraryRepository, transaction
from backend.library.schema import open_database
from tests.test_audio import wav


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


def run_scan(capsys, root, database, role="bass", summary=None):
    """Run the command in process and return (exit code, printed summary)."""

    arguments = [str(root), "--role", role, "--database", str(database)]
    if summary is not None:
        arguments += ["--summary", str(summary)]
    code = scanner.main(arguments)
    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return code, json.loads(printed[-1])


def sample_rows(database):
    connection = open_database(str(database))
    try:
        return [dict(row) for row in
                connection.execute("SELECT * FROM samples ORDER BY path_key ASC")]
    finally:
        connection.close()


def table_dump(database):
    """Every library table, ordered, as one comparable structure."""

    connection = open_database(str(database))
    try:
        return {name: [tuple(row) for row in
                       connection.execute("SELECT * FROM " + name + " ORDER BY 1, 2")]
                for name in ("analysis_versions", "sample_features", "sample_keys",
                             "sample_tags", "samples")}
    finally:
        connection.close()


def store_analysis(database, sample_id, version=None, descriptor=None):
    """Store one complete analysis for a row, the way #23 will.

    The test writes the rows directly so a scanner-created path row can hold an
    analysis at the current version (analysis current) or at another one (stale
    analysis version).
    """

    version = version or indexer.current_analysis_version()
    stored = batch.canonical(descriptor or batch.analysis_descriptor())
    connection = open_database(str(database))
    try:
        with transaction(connection):
            connection.execute(
                "INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(analysis_version) DO NOTHING",
                (version, stored, "2024-01-01T00:00:00Z"))
            for name, (unit, _low, _high) in MEASURES.items():
                connection.execute(
                    "INSERT INTO sample_features (sample_id, analysis_version, measurement, unit, "
                    "value, unavailable_reason, confidence) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(sample_id, analysis_version, measurement) DO NOTHING",
                    (sample_id, version, name, unit, None, "synthetic scan-store test", None))
            connection.execute(
                "INSERT INTO sample_keys (sample_id, analysis_version, tonic, mode, confidence, "
                "unavailable_reason) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sample_id, analysis_version) DO NOTHING",
                (sample_id, version, None, None, None, "synthetic silence has no tonal centre"))
    finally:
        connection.close()


def only_row(database):
    rows = sample_rows(database)
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# entry point, summary and exit codes
# ---------------------------------------------------------------------------


def test_an_empty_folder_succeeds_with_zero_counts(tmp_path, capsys):
    root = build(tmp_path, {})
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 0
    assert summary["state"] == "complete"
    assert summary["files"] == [] and summary["discovery_errors"] == []
    assert summary["counts"] == {"discovered": 0, "skipped_linked": 0, "added": 0,
                                 "unchanged": 0, "modified": 0, "moved": 0, "duplicate": 0,
                                 "missing": 0, "inaccessible": 0, "unsupported": 0,
                                 "queued_analysis": 0, "discovery_errors": 0}


def test_the_summary_carries_the_schema_and_the_shared_identity(tmp_path, capsys):
    root = build(tmp_path, {"kick.wav": tone()})
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database, role="kick")
    assert code == 0
    assert summary["scan_schema"] == "1.0"
    assert summary["scan_policy_version"] == scanner.SCAN_POLICY_VERSION
    assert summary["root"] == str(root.resolve())
    assert summary["role"] == "kick"
    assert summary["database"] == str(database.resolve())
    assert summary["analysis_version"] == batch.digest(batch.analysis_descriptor())
    record = summary["files"][0]
    assert set(record) == {"path", "code", "analysis", "sample_id", "error_code", "stage",
                           "message"}
    assert record["path"] == "kick.wav" and record["code"] == "added"
    assert record["analysis"] == "queued"
    assert record["sample_id"] == "sha256:" + hashlib.sha256(tone()).hexdigest()
    assert (record["error_code"], record["stage"]) == (None, None)


def test_the_published_code_sets_are_closed_and_used(tmp_path, capsys):
    root = build(tmp_path, {"bad.wav": b"not a wave file, longer than twelve bytes",
                            "a.wav": tone()})
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 1
    assert scanner.RECONCILIATION_CODES == ("added", "unchanged", "modified", "moved",
                                            "duplicate", "missing", "inaccessible", "unsupported")
    assert set(summary["counts"]) == {"discovered", "skipped_linked", "queued_analysis",
                                      "discovery_errors"} | set(scanner.RECONCILIATION_CODES)
    for record in summary["files"]:
        assert record["code"] in scanner.RECONCILIATION_CODES
        assert record["analysis"] in scanner.ANALYSIS_STATES
        if record["code"] == "inaccessible":
            assert record["error_code"] in scanner.READ_ERROR_CODES
        if record["code"] == "unsupported":
            assert record["error_code"] in ("unsupported_format", "unsupported_channels")
    assert all(error["error"]["code"] in scanner.DISCOVERY_ERROR_CODES
               for error in summary["discovery_errors"])


def test_the_role_is_required_and_never_inferred(tmp_path, capsys):
    root = build(tmp_path, {"kick.wav": tone()})
    database = tmp_path / "library.sqlite3"
    with pytest.raises(SystemExit) as missing:
        scanner.main([str(root), "--database", str(database)])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as invalid:
        scanner.main([str(root), "--role", "snare", "--database", str(database)])
    assert invalid.value.code == 2
    assert not database.exists()


def test_an_invalid_root_or_database_refuses_before_any_write(tmp_path):
    database = tmp_path / "library.sqlite3"
    assert scanner.main([str(tmp_path / "absent"), "--role", "bass",
                         "--database", str(database)]) == 2
    assert not database.exists()
    file_path = tmp_path / "not-a-folder"
    file_path.write_bytes(b"synthetic")
    assert scanner.main([str(file_path), "--role", "bass", "--database", str(database)]) == 2
    assert not database.exists()
    assert scanner.main(["//server/share/tera", "--role", "bass",
                         "--database", str(database)]) == 2
    assert not database.exists()


def test_a_database_with_a_newer_schema_is_never_reset(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone()})
    database = tmp_path / "library.sqlite3"
    connection = open_database(str(database))
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    before = database.read_bytes()
    assert scanner.main([str(root), "--role", "bass", "--database", str(database)]) == 2
    assert database.read_bytes() == before
    connection = sqlite3.connect(str(database))
    assert connection.execute("SELECT count(*) FROM samples").fetchone()[0] == 0
    connection.close()


def test_a_summary_file_is_written_next_to_the_printed_one(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone()})
    database = tmp_path / "library.sqlite3"
    destination = tmp_path / "scan.json"
    code, summary = run_scan(capsys, root, database, summary=destination)
    assert code == 0
    assert json.loads(destination.read_text(encoding="utf-8")) == summary


# ---------------------------------------------------------------------------
# traversal, format policy and the no-decode boundary
# ---------------------------------------------------------------------------


def test_traversal_cannot_drift_from_the_batch_discovery_policy(tmp_path, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "B.WAV": tone(220), "sub/c.wav": tone(330),
                            "sub/notes.txt": b"synthetic", "skip.flac": b"synthetic",
                            "tone.wav.d": b"synthetic"})
    (root / "wav-directory.wav").mkdir()
    (root / "locked").mkdir()
    (root / "locked" / "hidden.wav").write_bytes(tone(440))
    real_scandir = os.scandir

    def scandir(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "synthetic denial")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    expected_paths, expected_errors = batch.discover(root)
    paths, discovery_errors, linked = scanner.traverse(root)
    assert paths == expected_paths == ["B.WAV", "a.wav", "sub/c.wav"]
    assert discovery_errors == expected_errors
    assert [record["path"] for record in discovery_errors] == ["locked"]
    assert discovery_errors[0]["error"]["code"] == "enumeration_failed"
    assert discovery_errors[0]["error"]["stage"] == "discovery"
    assert linked == []


def test_directories_and_non_wav_entries_are_ignored_not_failed(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(), "notes.txt": b"synthetic", "loop.aiff": b"synthetic",
                            "no-extension": b"synthetic"})
    (root / "folder.wav").mkdir()
    (root / "folder.wav" / "inner.wav").write_bytes(tone(220))
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 0
    assert summary["counts"]["discovered"] == 2
    assert summary["counts"]["added"] == 2
    assert sorted(record["path"] for record in summary["files"]) == ["a.wav", "folder.wav/inner.wav"]


@pytest.mark.parametrize("data", [b"", b"RIFF", b"short bytes", b"fLaC" + b"\0" * 40,
                                  b"RIFX" + b"\0" * 40, b"RF64" + b"\0" * 40])
def test_the_pre_filter_rejects_only_snapshots_the_reader_also_rejects(data):
    assert scanner.snapshot_format_code(data) == "unsupported_format"
    with pytest.raises(AudioReadError):
        load_wav_bytes(data)


@pytest.mark.parametrize("header", [b"fLaC", b"FORM", b"RIFX", b"RF64"])
def test_a_container_mismatch_is_the_readers_own_unsupported_format(header):
    data = header + b"\0" * 40
    assert scanner.snapshot_format_code(data) == "unsupported_format"
    with pytest.raises(AudioReadError) as caught:
        load_wav_bytes(data)
    assert caught.value.code is AudioErrorCode.UNSUPPORTED_FORMAT


def test_a_too_short_snapshot_is_rejected_by_the_reader_as_invalid_audio():
    """The pre-filter is narrower than the reader for a truncated header.

    The pre-filter reports `unsupported_format` for a snapshot under 12 bytes
    while the reader reports `invalid_audio`; both refuse it, so the pre-filter
    never accepts a file the reader rejects and never rejects one it accepts.
    """

    assert scanner.snapshot_format_code(b"RIFF") == "unsupported_format"
    with pytest.raises(AudioReadError) as caught:
        load_wav_bytes(b"RIFF")
    assert caught.value.code is AudioErrorCode.INVALID_AUDIO


def _riff_container(body):
    """A RIFF/WAVE container carrying exactly `body` after the 12-byte header."""

    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


@pytest.mark.parametrize("body", [
    b"",
    b"fmt " + struct.pack("<I", 4) + b"\0" * 4,
    b"fmt " + struct.pack("<I", 15) + b"\0" * 15,
])
def test_a_passing_container_without_a_storable_header_is_unsupported_format(body):
    """QA finding F3: the pre-filter accepts a container the header read refuses.

    The snapshot passes the 12-byte pre-filter, so `snapshot_format_code`
    accepts it, but it carries no `fmt `/`data` pair the schema could store:
    `sample_rate_hz`, `channels` and `frame_count` are NOT NULL with real
    constraints, so there is no honest row to write. The scan reports
    `unsupported`/`unsupported_format` while `load_wav_bytes` refuses the same
    bytes with the wider `invalid_audio`; `_docs/library-scan.md` records the
    mapping.
    """

    data = _riff_container(body)
    assert scanner.snapshot_format_code(data) is None
    assert scanner.read_wav_header(data) == (None, "unsupported_format")
    with pytest.raises(AudioReadError) as caught:
        load_wav_bytes(data)
    assert caught.value.code is AudioErrorCode.INVALID_AUDIO


def test_a_scan_never_decodes_audio_or_runs_an_extractor(tmp_path, capsys, monkeypatch):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220), "empty.wav": wav(np.zeros((0, 1)))})

    def refuse(*args, **kwargs):
        raise AssertionError("a scan must not decode or measure audio")

    monkeypatch.setattr(audio, "load_wav_bytes", refuse)
    monkeypatch.setattr(audio, "load_wav", refuse)
    for module in (loudness, spectral, transient, harmony):
        for name in dir(module):
            if name.startswith("measure_"):
                monkeypatch.setattr(module, name, refuse)
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 0
    assert summary["counts"]["added"] == 3


def test_an_unsupported_snapshot_creates_no_row_and_queues_nothing(tmp_path, capsys):
    root = build(tmp_path, {"bad.wav": b"not a wave file, but longer than twelve bytes",
                            "tiny.wav": b"RIFF"})
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database)
    assert code == 1
    assert summary["counts"]["unsupported"] == 2
    assert summary["counts"]["added"] == 0 and summary["counts"]["queued_analysis"] == 0
    assert [record["error_code"] for record in summary["files"]] == ["unsupported_format"] * 2
    assert [record["stage"] for record in summary["files"]] == ["read"] * 2
    assert all(record["analysis"] == "none" for record in summary["files"])
    assert sample_rows(database) == []
    assert indexer.pending_analysis(open_database(str(database))) == ()


def test_a_file_the_reader_rejects_deeper_is_indexed_and_queued(tmp_path, capsys):
    """empty_audio and invalid_audio pass the pre-filter, so the scan owns them."""

    empty = wav(np.zeros((0, 1)))
    nonfinite = wav([[float("nan")]], subtype="FLOAT")
    for data, reader_code in ((empty, AudioErrorCode.EMPTY_AUDIO),
                              (nonfinite, AudioErrorCode.INVALID_AUDIO)):
        with pytest.raises(AudioReadError) as caught:
            load_wav_bytes(data)
        assert caught.value.code is reader_code
    root = build(tmp_path, {"empty.wav": empty, "nan.wav": nonfinite})
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 0
    assert summary["counts"]["added"] == 2
    assert summary["counts"]["queued_analysis"] == 2


def test_a_layout_wider_than_two_channels_cannot_be_stored(tmp_path, capsys):
    """Documented deviation: the frozen schema stores only one or two channels.

    `samples.channels` has CHECK channels IN (1, 2), so a three-channel file that
    passes the pre-filter cannot be indexed without storing a false channel
    count. The scan reports it as `unsupported` with the reader's own
    `unsupported_channels` code instead of writing a row that lies.
    """

    data = wav(np.zeros((16, 3)))
    with pytest.raises(AudioReadError) as caught:
        load_wav_bytes(data)
    assert caught.value.code is AudioErrorCode.UNSUPPORTED_CHANNELS
    root = build(tmp_path, {"wide.wav": data})
    code, summary = run_scan(capsys, root, tmp_path / "library.sqlite3")
    assert code == 1
    assert summary["files"][0]["code"] == "unsupported"
    assert summary["files"][0]["error_code"] == "unsupported_channels"
    assert summary["files"][0]["analysis"] == "none"
    assert summary["counts"]["added"] == 0 and summary["counts"]["queued_analysis"] == 0


# ---------------------------------------------------------------------------
# reconciliation codes
# ---------------------------------------------------------------------------


def test_added_then_unchanged_and_the_queued_work_is_derived(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110), "sub/b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    code, first = run_scan(capsys, root, database)
    assert (code, first["counts"]["added"], first["counts"]["queued_analysis"]) == (0, 2, 2)
    rows = sample_rows(database)
    assert [Path(row["original_path"]).name for row in rows] == ["a.wav", "b.wav"]
    assert [row["role"] for row in rows] == ["bass", "bass"]
    assert all(row["file_status"] == "present" for row in rows)
    assert rows[0]["sample_rate_hz"] == 48000 and rows[0]["channels"] == 1
    assert rows[0]["frame_count"] == 480
    requests = indexer.pending_analysis(open_database(str(database)))
    assert {(item.fingerprint, item.role) for item in requests} == {
        (row["content_sha256"], row["role"]) for row in rows}
    assert [item.sample_id for item in requests] == sorted(item.sample_id for item in requests)

    code, second = run_scan(capsys, root, database)
    assert code == 0
    assert second["counts"]["unchanged"] == 2 and second["files"] == []
    assert second["counts"]["queued_analysis"] == first["counts"]["queued_analysis"] == 2


def test_a_repeat_scan_writes_no_row(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110), "sub/b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    before = table_dump(database)
    connection = open_database(str(database))
    changes = connection.total_changes
    connection.close()
    assert run_scan(capsys, root, database)[0] == 0
    assert table_dump(database) == before
    connection = open_database(str(database))
    assert connection.total_changes == changes
    connection.close()


def test_a_modification_keeps_the_record_and_its_previous_analysis(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    old_version = batch.digest({"batch": "synthetic-scan-previous"})
    store_analysis(database, row["sample_id"], version=old_version,
                   descriptor={"batch": "synthetic-scan-previous"})
    assert indexer.pending_analysis(open_database(str(database)))[0].fingerprint == \
        row["content_sha256"]

    (root / "a.wav").write_bytes(tone(330, frames=960))
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["modified"] == 1
    record = summary["files"][0]
    assert record["code"] == "modified" and record["analysis"] == "queued"
    stored = only_row(database)
    assert stored["sample_id"] == row["sample_id"] and stored["original_path"] == row["original_path"]
    assert stored["content_sha256"] == hashlib.sha256(tone(330, frames=960)).hexdigest()
    assert stored["content_sha256"] != row["content_sha256"]
    assert stored["imported_at"] == row["imported_at"]
    assert stored["frame_count"] == 960
    connection = open_database(str(database))
    previous = LibraryRepository(connection).get_sample(row["sample_id"], old_version)
    assert previous.analysis_version == old_version
    assert len(previous.sample.features.measurements) == len(MEASURES)
    connection.close()


def test_a_modification_invalidates_the_analysis_of_the_previous_bytes(tmp_path, capsys):
    """QA finding F1: the new bytes must never look analysed.

    The row already holds a legitimate analysis at the *current* version when
    the file's bytes change. That analysis was measured from the previous
    bytes, so the scan invalidates it in the same transaction as the content
    update: `analysis` is `queued`, the row is back in `pending_analysis`, and
    `get_sample` can no longer serve the old measurements under the new
    content identity.
    """

    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    store_analysis(database, row["sample_id"])
    version = indexer.current_analysis_version()
    connection = open_database(str(database))
    library = LibraryRepository(connection)
    assert library.get_sample(row["sample_id"]).analysis_version == version
    assert indexer.pending_analysis(connection) == ()
    connection.close()

    (root / "a.wav").write_bytes(tone(330, frames=960))
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["modified"] == 1
    record = summary["files"][0]
    assert record["code"] == "modified" and record["analysis"] == "queued"
    assert summary["counts"]["queued_analysis"] == 1
    stored = only_row(database)
    assert stored["sample_id"] == row["sample_id"]
    assert stored["original_path"] == row["original_path"]
    assert stored["imported_at"] == row["imported_at"]
    fingerprint = hashlib.sha256(tone(330, frames=960)).hexdigest()
    assert stored["content_sha256"] == fingerprint != row["content_sha256"]

    connection = open_database(str(database))
    for table in ("sample_features", "sample_keys"):
        remaining = connection.execute(
            "SELECT COUNT(*) FROM " + table + " WHERE sample_id = ?",
            (row["sample_id"],)).fetchone()[0]
        assert remaining == 0
    library = LibraryRepository(connection)
    assert library.has_current_analysis(fingerprint, version) is False
    requests = indexer.pending_analysis(connection)
    assert [(item.sample_id, item.fingerprint) for item in requests] == [
        (row["sample_id"], fingerprint)]
    with pytest.raises(IncompleteFeatures):
        library.get_sample(row["sample_id"])
    with pytest.raises(UnknownAnalysisVersion):
        library.get_sample(row["sample_id"], version)
    connection.close()

    # The next scan re-reports the file as unchanged with the work still queued.
    code, repeat = run_scan(capsys, root, database)
    assert code == 0
    assert repeat["counts"]["unchanged"] == 1
    assert repeat["counts"]["queued_analysis"] == 1



def test_a_renamed_file_keeps_its_record_and_queues_nothing(tmp_path, capsys):
    root = build(tmp_path, {"Kick.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    (root / "Kick.wav").rename(root / "sub-kick.wav")
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["moved"] == 1 and summary["counts"]["missing"] == 0
    assert summary["counts"]["added"] == 0
    stored = only_row(database)
    assert stored["sample_id"] == row["sample_id"]
    assert Path(stored["original_path"]).name == "sub-kick.wav"
    assert stored["file_status"] == "present" and stored["imported_at"] == row["imported_at"]


def test_a_case_only_rename_is_moved_not_added_and_missing(tmp_path, capsys):
    root = build(tmp_path, {"Kick.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    (root / "Kick.wav").rename(root / "kick.wav")
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["moved"] == 1
    assert summary["counts"]["added"] == 0 and summary["counts"]["missing"] == 0
    stored = only_row(database)
    assert stored["sample_id"] == row["sample_id"]
    assert Path(stored["original_path"]).name == "kick.wav"


def test_a_content_identity_already_in_the_library_is_reported_duplicate(tmp_path, capsys):
    """Documented deviation: version-1 `samples.content_sha256` is UNIQUE.

    The criterion asks for a second row for the second path. The table refuses
    it (`UNIQUE constraint failed: samples.content_sha256`), so the scan reports
    the duplicate and leaves the holding row untouched instead of writing a row
    that cannot exist.
    """

    root = build(tmp_path, {"a.wav": tone(110), "copy.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["added"] == 1 and summary["counts"]["duplicate"] == 1
    codes = {record["path"]: record["code"] for record in summary["files"]}
    assert codes == {"a.wav": "added", "copy.wav": "duplicate"}
    assert sample_rows(database)[0]["original_path"].endswith("a.wav")


def test_a_duplicate_under_a_second_root_leaves_the_first_root_untouched(tmp_path, capsys):
    first = build(tmp_path, {"a.wav": tone(110)}, name="first")
    second = build(tmp_path, {"copy.wav": tone(110)}, name="second")
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, first, database)[0] == 0
    before = table_dump(database)
    code, summary = run_scan(capsys, second, database, role="kick")
    assert code == 0
    assert summary["counts"]["duplicate"] == 1 and summary["counts"]["added"] == 0
    assert summary["files"][0]["path"] == "copy.wav"
    assert table_dump(database) == before


def test_an_edited_file_that_copies_another_record_is_not_rewritten(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    before = table_dump(database)
    (root / "a.wav").write_bytes(tone(220))
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert {record["path"]: record["code"] for record in summary["files"]} == {"a.wav": "duplicate"}
    assert table_dump(database) == before


# ---------------------------------------------------------------------------
# analysis state and the derived queue
# ---------------------------------------------------------------------------


def test_a_stale_analysis_version_reports_unchanged_with_queued_analysis(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    old_version = batch.digest({"batch": "synthetic-scan-previous"})
    store_analysis(database, row["sample_id"], version=old_version,
                   descriptor={"batch": "synthetic-scan-previous"})

    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["unchanged"] == 1
    assert summary["counts"]["queued_analysis"] == 1
    # An unchanged file is not listed in `files`; the queued work is the queue.
    assert summary["files"] == []
    requests = indexer.pending_analysis(open_database(str(database)))
    assert len(requests) == 1 and requests[0].analysis_version == indexer.current_analysis_version()


def test_a_current_analysis_reports_current_and_queues_nothing(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    row = only_row(database)
    store_analysis(database, row["sample_id"])
    code, summary = run_scan(capsys, root, database)
    assert code == 0
    assert summary["counts"]["unchanged"] == 1
    assert summary["counts"]["queued_analysis"] == 0
    assert indexer.pending_analysis(open_database(str(database))) == ()


def test_the_pending_queue_is_one_request_per_content_identity(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110), "b.wav": tone(220)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    rows = sample_rows(database)
    store_analysis(database, rows[0]["sample_id"])
    connection = open_database(str(database))
    requests = indexer.pending_analysis(connection)
    connection.close()
    assert len(requests) == 1
    request = requests[0]
    assert request.sample_id == rows[1]["sample_id"]
    assert request.fingerprint == rows[1]["content_sha256"]
    assert request.path == rows[1]["original_path"]
    assert request.role == "bass"
    assert indexer.REQUEST_FIELDS == ("sample_id", "fingerprint", "analysis_version", "path", "role")
    connection = open_database(str(database))
    connection.execute("UPDATE samples SET file_status = 'missing' WHERE sample_id = ?",
                       (rows[1]["sample_id"],))
    connection.close()
    connection = open_database(str(database))
    assert indexer.pending_analysis(connection) == ()
    connection.close()


def test_a_scan_touches_only_the_library_tables(tmp_path, capsys):
    root = build(tmp_path, {"a.wav": tone(110)})
    database = tmp_path / "library.sqlite3"
    assert run_scan(capsys, root, database)[0] == 0
    connection = open_database(str(database))
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
    connection.close()
    assert tables == ["analysis_versions", "sample_features", "sample_keys", "sample_packs",
                      "sample_tags", "samples"]
    assert table_dump(database)["sample_tags"] == []
    for name in ("projects", "palettes", "palette_items"):
        assert name not in tables
