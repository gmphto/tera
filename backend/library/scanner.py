"""Reconcile one local folder with the SQLite sample index (issue #22).

    python -m backend.library.scanner FOLDER --role ROLE --database DB.sqlite3
                                     [--summary OUT.json]

One scan keeps the index current: it enumerates the folder with the traversal
policy #9 uses, hashes each .wav byte snapshot, gives every file exactly one
reconciliation code, updates the stored path rows through
backend.library.repository and prints an import summary.

It never decodes audio, extracts a feature, runs or waits on analysis, calls
Jev, uses the network, writes telemetry, touches a project, palette or pack row,
creates a pack from a folder name, mutates a source file or watches the folder.
Nothing is queued: what still needs analysis is the derived query
backend.library.indexer.pending_analysis.

See _docs/library-scan.md for the scan policy, both code tables and their
precedence, the summary schema, the exit codes and the recovery procedure.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import sys
import tempfile
import unicodedata

from backend.analysis import batch
from backend.library import indexer
from backend.library.errors import DuplicateContent, LibraryError
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database


SCAN_SCHEMA = "1.0"
SCAN_POLICY_VERSION = "library-scan-v1"

ADDED = "added"
UNCHANGED = "unchanged"
MODIFIED = "modified"
MOVED = "moved"
DUPLICATE = "duplicate"
MISSING = "missing"
INACCESSIBLE = "inaccessible"
UNSUPPORTED = "unsupported"

# One reconciliation code per file, from a closed set.
RECONCILIATION_CODES = (ADDED, UNCHANGED, MODIFIED, MOVED, DUPLICATE, MISSING, INACCESSIBLE,
                        UNSUPPORTED)

ANALYSIS_STATES = ("queued", "current", "none")

# The stable codes a failed read keeps, from #9's reader and snapshot.
READ_ERROR_CODES = ("not_found", "access_denied", "io_error", "not_file", "source_changed")
DISCOVERY_ERROR_CODES = ("enumeration_failed", "entry_unavailable")

STAGE_READ = "read"
STAGE_DISCOVERY = "discovery"

WAV_SUFFIX = ".wav"
RIFF_MAGIC = b"RIFF"
WAVE_MAGIC = b"WAVE"
WAV_HEADER_BYTES = 12

# Codes that make the run exit 1: work the operator must do before the scan can
# index the file. A missing file is a normal reconciliation outcome, not a
# failure.
FAILED_CODES = (INACCESSIBLE, UNSUPPORTED)

STATE_COMPLETE = "complete"
STATE_INTERRUPTED = "interrupted"


class ScanError(Exception):
    """Invalid command, root, database or summary destination (exit 2)."""


# ---------------------------------------------------------------------------
# traversal: the shared scan policy
# ---------------------------------------------------------------------------


def traverse(root):
    """The scanned WAV paths, discovery errors and skipped linked paths.

    Returns (wav_paths, discovery_errors, linked_paths) for a canonical root.
    Regular files with a case-insensitive .wav suffix, recursively, as POSIX
    relative paths in deterministic lexical order. A symbolic link, Windows
    junction or reparse point is never entered, never imported and never given a
    row: its relative path is reported in linked_paths. A nested directory that
    cannot be enumerated is a discovery error and the walk continues; a root
    that cannot be enumerated raises backend.analysis.batch.BatchError.
    """

    root = Path(root)
    found, failures, linked = [], [], []

    def relative(path):
        return path.relative_to(root).as_posix()

    def visit(folder):
        try:
            with os.scandir(folder) as stream:
                children = sorted(stream, key=lambda item: item.name)
        except OSError as error:
            if folder == root:
                raise batch.BatchError(f"Cannot enumerate input root: {error}") from error
            failures.append({"path": relative(folder),
                             "error": _failure("discovery", "enumeration_failed", error)})
            return
        for child in children:
            path = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
                if _linked(info):
                    linked.append(relative(path))
                elif stat.S_ISDIR(info.st_mode):
                    visit(path)
                elif stat.S_ISREG(info.st_mode) and path.suffix.lower() == WAV_SUFFIX:
                    found.append(relative(path))
            except OSError as error:
                failures.append({"path": relative(path),
                                 "error": _failure("discovery", "entry_unavailable", error)})

    visit(root)
    return sorted(found), failures, sorted(linked)


def _linked(info) -> bool:
    """A symbolic link, a Windows junction or any other reparse point."""

    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _failure(stage, code, message):
    """#9's discovery error record shape, built from a local cause."""

    return {"stage": stage, "code": code, "message": str(message) or str(code)}


# ---------------------------------------------------------------------------
# format policy: the pre-filter and the header the schema needs
# ---------------------------------------------------------------------------


def snapshot_format_code(data):
    """The pre-filter refusal code for one byte snapshot, or None.

    The rule is exactly the one the reader applies first: fewer than 12 bytes,
    or no RIFF at offset 0 and WAVE at offset 8.
    """

    if (len(data) < WAV_HEADER_BYTES or data[:4] != RIFF_MAGIC or data[8:12] != WAVE_MAGIC):
        return "unsupported_format"
    return None


def read_wav_header(data):
    """The audio metadata the samples table stores, or the code that refuses it.

    Returns ((sample_rate_hz, channels, frame_count, duration_ms), None) when the
    RIFF header carries values the library schema accepts, otherwise
    (None, code): unsupported_channels for a layout wider than the two channels
    the schema stores, unsupported_format for a header with no usable
    format/data chunk pair.

    Chunk headers are read from the immutable snapshot; no sample data is
    decoded, the reader is never loaded and the RIFF size field is not checked,
    because a container problem the reader reports deeper is an analysis-time
    error owned by #23, not a scan refusal.
    """

    chunks = {}
    offset = WAV_HEADER_BYTES
    body = len(data)
    while offset + 8 <= body:
        name = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        if start + size > body:
            break
        if name in (b"fmt ", b"data") and name not in chunks:
            chunks[name] = (start, size)
        offset = start + size + (size % 2)
    if b"fmt " not in chunks or b"data" not in chunks:
        return None, "unsupported_format"
    start, size = chunks[b"fmt "]
    if size < 16:
        return None, "unsupported_format"
    _tag, channels, rate, _byte_rate, align, _bits = struct.unpack_from("<HHIIHH", data, start)
    if channels > 2:
        return None, "unsupported_channels"
    if channels == 0 or rate == 0 or align == 0:
        return None, "unsupported_format"
    frame_count = chunks[b"data"][1] // align
    return (rate, channels, frame_count, frame_count / rate * 1000.0), None


# ---------------------------------------------------------------------------
# path identity
# ---------------------------------------------------------------------------


def _identity_path(relative) -> str:
    """The case-insensitive, Unicode-normalised form used for path matching."""

    return unicodedata.normalize("NFC", PurePosixPath(relative).as_posix()).casefold()


def _relative(root, path) -> str:
    text = os.path.relpath(os.fspath(path), os.fspath(root)).replace("\\", "/")
    return PurePosixPath(text).as_posix()


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("--role", required=True, choices=batch.ROLES)
    parser.add_argument("--database", required=True)
    parser.add_argument("--summary")
    arguments = parser.parse_args(argv)
    try:
        return run(arguments.folder, arguments.role, arguments.database, arguments.summary)
    except KeyboardInterrupt:
        print("Interrupted before the scan finished; every committed file stays committed.",
              file=sys.stderr)
        return 130
    except ScanError as error:
        print(f"Scan command/root/database/summary failure: {error}", file=sys.stderr)
        return 2
    except LibraryError as error:
        print(f"Scan storage failure: {error} ({error.code})", file=sys.stderr)
        return 2


def run(folder, role, database, summary_path=None) -> int:
    """Scan one folder, print the summary and return the exit code."""

    root = validate_root(folder)
    destination = _summary_destination(summary_path)
    # The traversal and the entry-collision refusal happen before the database
    # is opened, so a folder that cannot be scanned byte-for-byte creates no
    # database at all (criterion `duplicate`, QA finding F2).
    paths, discovery_errors, linked_paths = _discover(root)
    database_path = _database_path(database)
    with closing(_open_database(database_path)) as connection:
        scan = _Scan(root, role, database_path, connection)
        state = scan.execute(paths, discovery_errors, linked_paths)
        summary = scan.summary(state)
        print(batch.canonical(summary), flush=True)
        if destination is not None:
            _write_summary(destination, summary)
    if state == STATE_INTERRUPTED:
        return 130
    return 1 if scan.failed else 0


def reconcile(root, role, connection, database=None) -> dict:
    """Reconcile one root through an open connection and return the summary.

    This is `run` without its command: it traverses `root`, refuses colliding
    entries, reconciles the stored rows through the caller's connection and
    returns the same summary object, printing nothing and opening nothing.
    Issue #27 drives a folder import from its own runner thread, where the
    summary must not reach stdout because it carries the root's path.
    """

    paths, discovery_errors, linked_paths = _discover(root)
    scan = _Scan(root, role, "" if database is None else database, connection)
    state = scan.execute(paths, discovery_errors, linked_paths)
    return scan.summary(state)


def _discover(root):
    """`traverse` plus the entry-collision refusal, before any database write."""

    try:
        paths, discovery_errors, linked_paths = traverse(root)
    except batch.BatchError as error:
        raise ScanError(f"root: {error}") from error
    _reject_entry_collisions(paths)
    return paths, discovery_errors, linked_paths


class _Scan:
    """One reconciliation run over one canonical root."""

    def __init__(self, root, role, database, connection):
        self.root = Path(root)
        self.role = role
        self.database = str(database)
        self.connection = connection
        self.repository = LibraryRepository(connection)
        self.version = indexer.current_analysis_version()
        self.counts = {code: 0 for code in RECONCILIATION_CODES}
        self.records = []
        self.discovery_errors = []
        self.discovered = 0
        self.skipped_linked = 0

    # -- the run -----------------------------------------------------------

    def execute(self, paths, discovery_errors, linked_paths) -> str:
        self.discovery_errors = list(discovery_errors)
        self.discovered = len(paths)
        self.skipped_linked = len(linked_paths)
        rows = self.repository.list_path_records(self.root)
        _reject_row_collisions(self.root, rows)
        by_path = {}
        for row in rows:
            by_path.setdefault(_identity_path(_relative(self.root, row.path)), row)
        enumerated = {_identity_path(relative) for relative in paths}
        # Rows under this root that this run did not enumerate, one bucket per
        # content identity, smallest path first: they are the move candidates.
        pending_rows = {}
        for key, row in by_path.items():
            if key not in enumerated:
                pending_rows.setdefault(row.content_sha256, []).append(row)
        for bucket in pending_rows.values():
            bucket.sort(key=lambda item: _identity_path(_relative(self.root, item.path)))
        readable = []
        state = STATE_COMPLETE
        try:
            for relative in sorted(paths, key=_identity_path):
                outcome = self._read(relative, by_path.get(_identity_path(relative)))
                if outcome is not None:
                    readable.append(outcome)
            for relative, fingerprint, metadata in readable:
                self._reconcile(relative, fingerprint, metadata, by_path, pending_rows)
            for bucket in pending_rows.values():
                for row in bucket:
                    self._missing(row)
        except KeyboardInterrupt:
            state = STATE_INTERRUPTED
        return state

    # -- one file ----------------------------------------------------------

    def _read(self, relative, row):
        """Hash and pre-filter one enumerated file.

        Returns (relative, fingerprint, metadata) when the file is readable,
        otherwise None after recording the outcome.
        """

        path = self.root.joinpath(*relative.split("/"))
        failure = None
        try:
            data = batch.snapshot(path)
        except batch.SourceError as error:
            failure = (str(error.code), str(error))
        except OSError as error:
            failure = (_read_code(error), _read_message(error))
        if failure is not None:
            self._record(relative, INACCESSIBLE, "none", _content_identity(row),
                         error_code=failure[0], stage=STAGE_READ, message=failure[1])
            self._unavailable(row)
            return None
        fingerprint = hashlib.sha256(data).hexdigest()
        code = snapshot_format_code(data)
        if code is not None:
            self._unsupported(relative, row, code,
                              "The byte snapshot is shorter than 12 bytes or does not carry "
                              "RIFF at offset 0 and WAVE at offset 8; nothing was imported.")
            return None
        metadata, code = read_wav_header(data)
        if metadata is None:
            self._unsupported(relative, row, code, (
                "The header declares a channel layout wider than the two channels the library "
                "schema stores; nothing was imported."
                if code == "unsupported_channels" else
                "The header carries no usable format and data chunk pair; nothing was imported."))
            return None
        return relative, fingerprint, metadata

    def _unsupported(self, relative, row, code, message):
        self._record(relative, UNSUPPORTED, "none", _content_identity(row), error_code=code,
                     stage=STAGE_READ, message=message)
        # No library row is created, changed or queued for an unsupported file.

    def _reconcile(self, relative, fingerprint, metadata, by_path, pending_rows):
        row = by_path.get(_identity_path(relative))
        identity = "sha256:" + fingerprint
        path = self.root.joinpath(*relative.split("/"))
        if row is not None:
            if row.content_sha256 == fingerprint:
                if row.path != str(path):
                    # A case-only rename: the same record under a new on-disk
                    # casing, so the stored path keeps the casing on disk.
                    self.repository.relocate_path_record(row.sample_id, path,
                                                         file_status="present")
                    self._record(relative, MOVED, self._analysis(fingerprint), identity)
                else:
                    if row.file_status != "present":
                        self.repository.mark_file_status(row.sample_id, "present")
                    self._record(relative, UNCHANGED, self._analysis(fingerprint), identity)
            else:
                # The bytes changed, so the analysis stored for the previous
                # bytes is invalidated in the same transaction as the content
                # update: otherwise the row would look analysed for the new
                # content and `get_sample` would serve the previous
                # measurements (QA finding F1, criterion `modified`).
                try:
                    self.repository.update_path_record_content(
                        row.sample_id, content_sha256=fingerprint, file_status="present",
                        invalidate_analysis_version=self.version,
                        **_metadata_fields(metadata))
                except DuplicateContent:
                    self._record(relative, DUPLICATE, self._analysis(fingerprint), identity,
                                 message="The edited file now holds bytes that another library "
                                         "row already has; this record keeps its previous content "
                                         "identity and nothing was written.")
                    return
                self._record(relative, MODIFIED, self._analysis(fingerprint), identity)
            return
        bucket = pending_rows.get(fingerprint)
        if bucket:
            moved = bucket.pop(0)
            self.repository.relocate_path_record(moved.sample_id, path, file_status="present")
            self._record(relative, MOVED, self._analysis(fingerprint), identity)
            return
        holders = self.repository.find_path_records_by_content(fingerprint)
        if holders:
            available = any(item.file_status != "missing" for item in holders)
            self._record(relative, DUPLICATE, self._analysis(fingerprint, available), identity,
                         message="The content identity is already held by a library row at "
                                 "another path; the scan neither merges nor reindexes it.")
            return
        try:
            self.repository.insert_path_record(path, role=self.role, content_sha256=fingerprint,
                                               file_status="present",
                                               sample_id=_record_identity(path),
                                               **_metadata_fields(metadata))
        except DuplicateContent:
            self._record(relative, DUPLICATE, self._analysis(fingerprint), identity,
                         message="The content identity was stored concurrently; nothing was "
                                 "written for this path.")
            return
        self._record(relative, ADDED, "queued", identity)

    def _missing(self, row):
        self.repository.mark_file_status(row.sample_id, "missing")
        self._record(_relative(self.root, row.path), MISSING, "none",
                     "sha256:" + row.content_sha256, error_code="not_found", stage=STAGE_DISCOVERY,
                     message="The file is no longer in the scanned folder; the row is kept with "
                             "its identity, role and features and becomes available again when "
                             "the file returns.")

    def _unavailable(self, row):
        """Keep an existing row for a file that was enumerated but not read."""

        if row is not None and row.file_status != "missing":
            self.repository.mark_file_status(row.sample_id, "missing")

    # -- records -----------------------------------------------------------

    def _analysis(self, fingerprint, available=True) -> str:
        if self.repository.has_current_analysis(fingerprint, self.version):
            return "current"
        return "queued" if available else "none"

    def _record(self, relative, code, analysis, sample_id=None, error_code=None, stage=None,
                message=None):
        self.counts[code] += 1
        self.records.append({"path": relative, "code": code, "analysis": analysis,
                             "sample_id": sample_id, "error_code": error_code, "stage": stage,
                             "message": message})

    @property
    def failed(self) -> bool:
        return bool(self.discovery_errors) or any(
            record["code"] in FAILED_CODES for record in self.records)

    def summary(self, state) -> dict:
        counts = {"discovered": self.discovered, "skipped_linked": self.skipped_linked}
        counts.update({code: self.counts[code] for code in RECONCILIATION_CODES})
        counts["queued_analysis"] = len(indexer.pending_analysis(self.connection))
        counts["discovery_errors"] = len(self.discovery_errors)
        files = [record for record in self.records if record["code"] != UNCHANGED]
        files.sort(key=lambda record: _identity_path(record["path"]))
        return {"scan_schema": SCAN_SCHEMA,
                "scan_policy_version": SCAN_POLICY_VERSION,
                "root": str(self.root),
                "role": self.role,
                "database": self.database,
                "analysis_version": self.version,
                "state": state,
                "counts": counts,
                "files": files,
                "discovery_errors": self.discovery_errors}


# ---------------------------------------------------------------------------
# command validation and small helpers
# ---------------------------------------------------------------------------


def validate_root(folder) -> Path:
    """One canonical import root, or `ScanError` for anything unusable.

    The whole #22 root rule: a local path with no UNC prefix, no mapped network
    drive and no linked ancestor, and an existing directory. Public because
    issue #27 validates the root of a `POST /imports` request with exactly this
    call before it opens a run, so the service and the command can never
    disagree about what a root is.
    """

    try:
        root = batch.local_path(folder)
    except (batch.BatchError, OSError, TypeError, ValueError) as error:
        raise ScanError(f"root: {error}") from error
    if not root.is_dir():
        raise ScanError(f"root: {root} is not an existing local directory.")
    return root


def _database_path(database) -> Path:
    try:
        return batch.local_path(database)
    except (batch.BatchError, OSError, TypeError, ValueError) as error:
        raise ScanError(f"database: {error}") from error


def _open_database(database):
    try:
        return open_database(database)
    except LibraryError as error:
        raise ScanError(f"database: {error} ({error.code})") from error


def _summary_destination(path):
    if path is None:
        return None
    try:
        destination = batch.local_path(path)
    except (batch.BatchError, OSError, TypeError, ValueError) as error:
        raise ScanError(f"summary: {error}") from error
    if destination.is_dir():
        raise ScanError("summary: the summary path is a directory.")
    if not destination.parent.is_dir():
        raise ScanError("summary: the summary parent is not an existing directory.")
    return destination


def _write_summary(destination, summary) -> None:
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                                 dir=destination.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(batch.canonical(summary) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as error:
        raise ScanError(f"summary: {error}") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _reject_entry_collisions(paths) -> None:
    """Refuse two enumerated entries whose normalised paths collide."""

    seen = {}
    for relative in paths:
        key = _identity_path(relative)
        previous = seen.get(key)
        if previous is not None:
            raise ScanError(
                f"root: two entries differ only by case or Unicode normal form: {previous} and "
                f"{relative}; rename one of them, nothing was imported.")
        seen[key] = relative


def _reject_row_collisions(root, rows) -> None:
    """Refuse two stored rows inside the root whose normalised paths collide."""

    seen = {}
    for row in rows:
        key = _identity_path(_relative(root, row.path))
        previous = seen.get(key)
        if previous is not None:
            raise ScanError(
                f"root: two library rows differ only by case or Unicode normal form: "
                f"{previous.sample_id} and {row.sample_id}; repair the library by hand, the scan "
                "neither picks one nor merges them.")
        seen[key] = row


def _record_identity(path) -> str:
    """The stable record identity of a newly indexed path.

    Deliberately not the content identity: the samples table requires a
    `sha256:` sample_id to spell its own content_sha256, so a content-addressed
    record could not survive an edit (a modified row keeps its identity) and a
    palette reference to it could not stay valid. The id is minted once from the
    normalised path with #9's shared digest helper and is never rewritten; a
    later move or edit keeps it.
    """

    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    return "library:" + batch.digest({"library_path": key})


def _metadata_fields(metadata) -> dict:
    """The header tuple as the keyword arguments the repository stores."""

    sample_rate_hz, channels, frame_count, duration_ms = metadata
    return {"sample_rate_hz": sample_rate_hz, "channels": channels, "frame_count": frame_count,
            "duration_ms": duration_ms}


def _content_identity(row):
    return None if row is None else "sha256:" + row.content_sha256


def _read_code(error) -> str:
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, IsADirectoryError):
        return "not_file"
    if isinstance(error, PermissionError):
        return "access_denied"
    return "io_error"


def _read_message(error) -> str:
    if isinstance(error, FileNotFoundError):
        return "The file disappeared between enumeration and read: it was enumerated, then gone."
    if isinstance(error, IsADirectoryError):
        return "The enumerated entry is no longer a regular file."
    if isinstance(error, PermissionError):
        return "Permission denied while reading the file; release the lock or fix the rights."
    return f"Filesystem error while reading the file: {error}"


if __name__ == "__main__":
    raise SystemExit(main())
