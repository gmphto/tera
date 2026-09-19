"""Numbered migrations and connection policy for the local sample library.

The database is one local SQLite file. It holds sample identity, versioned
analysis results, tags and pack identity; it never holds audio bytes and it
never leaves the device. See `_docs/library-storage.md` for the full storage
model and `_docs/batch-analysis.md` for the identity and path conventions this
module reuses.

Design rules:

- `SCHEMA_VERSION` is the version this module knows. `MIGRATIONS` is an ordered
  tuple of `(version, script)` pairs. A fresh database applies every migration
  in ascending version order and stores the highest version in
  `PRAGMA user_version`; a database already at that version is left untouched.
- The stored version is read before any DDL runs and before the journal mode
  is switched. A database whose version is newer than every available migration
  is refused rather than reset, and that refusal leaves the file's bytes
  untouched even when it is not in WAL. A header-valid file that fails an
  integrity check is refused the same way.
- Every migration runs inside one transaction, so a failing migration leaves
  the version, the tables and every row exactly as they were.
- Only the standard library and `backend.contracts` / `backend.analysis.batch`
  are imported here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

from backend.analysis.batch import BatchError, local_path
from backend.contracts import MEASURES
from backend.library.errors import (
    DatabaseCorrupt,
    InvalidDatabasePath,
    MigrationFailed,
    NotADatabase,
    SchemaVersionNewer,
)


SCHEMA_VERSION = 1

SQLITE_MAGIC = b"SQLite format 3\x00"

# Applied to every connection this module returns, in this order: switching to
# WAL resets the synchronous setting, so synchronous is set last.
PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
)

TONICS = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
FILE_STATUSES = ("present", "missing", "unknown")

# The measurement and unit vocabularies come from the contract, so the CHECK
# constraints cannot drift from `MEASURES`.
MEASURE_NAMES = tuple(MEASURES)
UNITS = tuple(sorted({unit for unit, _low, _high in MEASURES.values()}))


def _quoted(values):
    return ", ".join("'" + value + "'" for value in values)


_MIGRATION_1 = f"""
CREATE TABLE analysis_versions (
    analysis_version TEXT PRIMARY KEY
        CHECK (length(analysis_version) = 64 AND analysis_version NOT GLOB '*[^0-9a-f]*'),
    descriptor TEXT NOT NULL CHECK (descriptor <> ''),
    created_at TEXT NOT NULL
);

CREATE TABLE sample_packs (
    pack_id TEXT PRIMARY KEY CHECK (pack_id = trim(pack_id) AND pack_id <> ''),
    name TEXT NOT NULL CHECK (name = trim(name) AND name <> ''),
    vendor TEXT NULL CHECK (vendor IS NULL OR (vendor = trim(vendor) AND vendor <> '')),
    created_at TEXT NOT NULL
);

CREATE TABLE samples (
    sample_id TEXT PRIMARY KEY CHECK (sample_id <> ''),
    schema_version TEXT NOT NULL CHECK (schema_version = '1.0'),
    content_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    role TEXT NOT NULL CHECK (role IN ('kick', 'bass', 'sub-bass')),
    original_path TEXT NOT NULL CHECK (original_path <> ''),
    path_key TEXT NOT NULL UNIQUE CHECK (path_key <> ''),
    filename TEXT NOT NULL CHECK (filename <> ''),
    pack_id TEXT NULL REFERENCES sample_packs(pack_id) ON DELETE SET NULL,
    file_status TEXT NOT NULL CHECK (file_status IN ('present', 'missing', 'unknown')),
    sample_rate_hz INTEGER NOT NULL CHECK (sample_rate_hz > 0),
    channels INTEGER NOT NULL CHECK (channels IN (1, 2)),
    frame_count INTEGER NOT NULL CHECK (frame_count >= 0),
    duration_ms REAL NOT NULL CHECK (duration_ms >= 0),
    imported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (sample_id NOT LIKE 'sha256:%' OR sample_id = 'sha256:' || content_sha256)
);

CREATE INDEX idx_samples_role ON samples(role);

CREATE TABLE sample_features (
    sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL REFERENCES analysis_versions(analysis_version) ON DELETE RESTRICT,
    measurement TEXT NOT NULL CHECK (measurement IN ({_quoted(MEASURE_NAMES)})),
    unit TEXT NOT NULL CHECK (unit IN ({_quoted(UNITS)})),
    value REAL NULL,
    unavailable_reason TEXT NULL,
    confidence REAL NULL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    PRIMARY KEY (sample_id, analysis_version, measurement),
    CHECK ((value IS NULL) = (unavailable_reason IS NOT NULL))
);

CREATE INDEX idx_sample_features_analysis ON sample_features(analysis_version);

CREATE TABLE sample_keys (
    sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL REFERENCES analysis_versions(analysis_version) ON DELETE RESTRICT,
    tonic TEXT NULL CHECK (tonic IS NULL OR tonic IN ({_quoted(TONICS)})),
    mode TEXT NULL CHECK (mode IS NULL OR mode IN ('major', 'minor')),
    confidence REAL NULL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    unavailable_reason TEXT NULL,
    PRIMARY KEY (sample_id, analysis_version),
    CHECK (
        (tonic IS NULL AND mode IS NULL AND confidence IS NULL
            AND unavailable_reason IS NOT NULL AND trim(unavailable_reason) <> '')
        OR (tonic IS NOT NULL AND mode IS NOT NULL AND confidence IS NOT NULL
            AND unavailable_reason IS NULL)
    )
);

CREATE TABLE sample_tags (
    sample_id TEXT NOT NULL REFERENCES samples(sample_id) ON DELETE CASCADE,
    tag TEXT NOT NULL CHECK (tag = lower(trim(tag)) AND length(tag) BETWEEN 1 AND 64),
    added_at TEXT NOT NULL,
    PRIMARY KEY (sample_id, tag)
);
"""

MIGRATIONS = ((1, _MIGRATION_1),)


def utc_now() -> str:
    """The current UTC instant as `YYYY-MM-DDTHH:MM:SSZ`."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_database_path() -> Path:
    r"""The absolute local database path, honouring `TERA_LIBRARY_DB`.

    Without the override this is `%LOCALAPPDATA%\tera\library.sqlite3` on
    Windows and `$XDG_DATA_HOME/tera/library.sqlite3` (or
    `~/.local/share/tera/library.sqlite3`) elsewhere. The path is not created
    here; `open_database` creates it.
    """

    override = os.environ.get("TERA_LIBRARY_DB")
    if override:
        return Path(os.path.abspath(override))
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = os.path.join(str(Path.home()), "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME")
        if not base:
            base = os.path.join(str(Path.home()), ".local", "share")
    return Path(os.path.abspath(os.path.join(base, "tera", "library.sqlite3")))


def verify(connection: sqlite3.Connection) -> tuple:
    """Every integrity or foreign-key problem in the database, or `()`.

    Runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Each problem
    is a human-readable string; a healthy database returns an empty tuple.
    """

    problems = []
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as error:
        problems.append(f"integrity_check: {error}")
    else:
        problems.extend(f"integrity_check: {row[0]}" for row in rows if row[0] != "ok")
    try:
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError as error:
        problems.append(f"foreign_key_check: {error}")
    else:
        problems.extend(
            f"foreign_key_check: {row[0]} rowid {row[1]} references {row[2]}" for row in rows
        )
    return tuple(problems)


def migrate(connection: sqlite3.Connection, migrations=MIGRATIONS) -> int:
    """Apply every pending migration and return the resulting schema version.

    The stored `PRAGMA user_version` is read before any DDL is attempted. A
    version newer than every available migration raises `SchemaVersionNewer`
    and changes nothing. Each migration is one transaction; a failure rolls it
    back and raises `MigrationFailed`. Calling this again with the same
    sequence is a no-op.
    """

    ordered = _ordered(migrations)
    current = _refuse_newer_schema(connection, _target(ordered))
    for version, script in ordered:
        if version > current:
            _apply(connection, version, script)
    return _user_version(connection)


def open_database(path) -> sqlite3.Connection:
    """Open, verify, configure and migrate the local database at `path`.

    The path is resolved with `backend.analysis.batch.local_path`, so UNC
    paths, mapped network drives and linked ancestors are refused with
    `invalid_database_path`, as is a directory. The parent directory is
    created when missing. A new or zero-byte file becomes the current schema; a
    file that is not SQLite raises `not_a_database`; one that fails an
    integrity check raises `database_corrupt`; one whose stored version is
    newer than every migration raises `SchemaVersionNewer` and is never reset;
    that refusal runs before the connection is reconfigured, so a database that
    is not in WAL keeps its bytes exactly as they were.
    """

    resolved = _resolve(path)
    _require_sqlite_file(resolved)
    _make_parent(resolved)
    connection = sqlite3.connect(str(resolved), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        problems = verify(connection)
        if problems:
            raise DatabaseCorrupt("; ".join(problems))
        # Checked before the pragmas: switching the journal mode is the first
        # write and rewrites the file header, so a refusal must not touch a
        # database that is not already in WAL.
        _refuse_newer_schema(connection, _target(MIGRATIONS))
        _configure(connection)
        migrate(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def _resolve(path) -> Path:
    try:
        resolved = local_path(path)
    except (BatchError, OSError, TypeError, ValueError) as error:
        raise InvalidDatabasePath(f"The database path is not usable: {error}") from error
    if resolved.is_dir():
        raise InvalidDatabasePath(f"The database path is a directory: {resolved.name}")
    return resolved


def _make_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise InvalidDatabasePath(f"The database directory is not usable: {error}") from error


def _require_sqlite_file(path: Path) -> None:
    """Refuse a non-empty file that does not start with the SQLite header.

    Reading only the header keeps the file's bytes untouched; SQLite would
    otherwise report the same condition as a generic database error.
    """

    try:
        with path.open("rb") as stream:
            header = stream.read(len(SQLITE_MAGIC))
    except FileNotFoundError:
        return
    except OSError as error:
        raise InvalidDatabasePath(f"The database file is not readable: {error}") from error
    if header and header != SQLITE_MAGIC:
        raise NotADatabase("The database file is not a SQLite database.")


def _configure(connection: sqlite3.Connection) -> None:
    try:
        for statement in PRAGMAS:
            connection.execute(statement)
    except sqlite3.Error as error:
        raise InvalidDatabasePath(f"The database cannot be opened for writing: {error}") from error


def _target(migrations) -> int:
    """The highest migration version, or 0 when there are none."""

    return max((version for version, _script in migrations), default=0)


def _refuse_newer_schema(connection: sqlite3.Connection, target: int) -> int:
    """The stored schema version, refusing one newer than `target`.

    Reading the version is the only step here, so the caller can run this
    before any write; a refusal leaves the database's bytes unchanged.
    """

    current = _user_version(connection)
    if current > target:
        raise SchemaVersionNewer(
            f"Database schema version {current} is newer than the supported version {target}."
        )
    return current


def _user_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _ordered(migrations) -> tuple:
    entries = []
    for entry in migrations:
        version, script = entry
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise MigrationFailed(f"Migration version must be a positive integer: {version!r}")
        if not isinstance(script, str):
            raise MigrationFailed(f"Migration {version} must be a SQL string.")
        entries.append((version, script))
    entries.sort(key=lambda item: item[0])
    for previous, following in zip(entries, entries[1:]):
        if previous[0] == following[0]:
            raise MigrationFailed(f"Duplicate migration version {previous[0]}.")
    return tuple(entries)


def _statements(script: str) -> tuple:
    """Split a migration script on statement boundaries.

    `sqlite3.complete_statement` tracks trigger bodies and string literals, so
    a migration may contain either.
    """

    statements, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        statements.append(pending.strip())
    return tuple(statement for statement in statements if statement)


def _apply(connection: sqlite3.Connection, version: int, script: str) -> None:
    statements = _statements(script)
    if not statements:
        raise MigrationFailed(f"Migration {version} contains no statements.")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback(connection)
        raise MigrationFailed(f"Migration {version} failed: {error}") from error
    except BaseException:
        _rollback(connection)
        raise


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
