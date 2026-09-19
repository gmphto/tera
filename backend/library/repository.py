"""Repository operations over the local sample library database.

`LibraryRepository` is the only supported way to read or write stored samples.
It wraps one `sqlite3.Connection` produced by `backend.library.schema.open_database`.

Rules this module enforces:

- Identity. `sample_id` is the contract identity; `content_sha256` is the
  content identity. A content-addressed `sample_id` must spell
  `sha256:<content_sha256>`. `path_key` is the normalised local path and is
  unique, so two records can never claim the same file.
- Completeness. One (`sample_id`, `analysis_version`) pair owns exactly the 19
  `MEASURES` rows plus one key row; a read that finds anything less raises
  `IncompleteFeatures` instead of returning a partial `Sample`.
- Versions. Features are stored per analysis version and are never overwritten
  by another version. The stored descriptor is re-digested on every read, so a
  tampered row raises `AnalysisVersionMismatch`.
- Transactions. Every public write runs in exactly one `transaction`; a
  failure rolls it back and raises a coded `LibraryError` with the sqlite3
  error as `__cause__`. Reads use individual statements and never hold a
  transaction open between calls.
- No audio. Import never opens, stats or reads the audio file, and no column
  holds audio bytes, a Jev payload or a credential.

Only the standard library and `backend.contracts` / `backend.analysis.batch`
are imported here; `json` is used to parse a stored descriptor back so it can
be re-digested. No third-party package is imported.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import PureWindowsPath
import sqlite3

from backend.analysis.batch import ROLES, canonical, digest
from backend.contracts import (
    AudioFeatures,
    AudioMetadata,
    MEASURES,
    Measurement,
    MusicalKey,
    Sample,
)
from backend.library.errors import (
    AnalysisVersionMismatch,
    DatabaseLocked,
    DuplicateContent,
    IncompleteFeatures,
    InvalidContentIdentity,
    InvalidFileStatus,
    InvalidSample,
    InvalidTag,
    LibraryError,
    PathConflict,
    UnknownAnalysisVersion,
    UnknownSample,
    WriteFailed,
)
from backend.library.schema import FILE_STATUSES, utc_now


HEX_DIGITS = "0123456789abcdef"
TAG_MAX_LENGTH = 64


@dataclass(frozen=True)
class ImportResult:
    """What one `import_sample` call did.

    `created` is True when a new `samples` row was written, `path_changed` is
    True when an existing row moved to a different stored path, and
    `file_status` is the status now stored.
    """

    sample_id: str
    analysis_version: str
    created: bool
    path_changed: bool
    file_status: str


@dataclass(frozen=True)
class StoredSample:
    """One stored sample read back as a validated contract object.

    `sample` is a `backend.contracts.Sample`, never a dict, and
    `analysis_version` always equals `sample.analysis_version`.
    """

    sample: Sample
    analysis_version: str
    pack_id: str | None
    tags: tuple
    file_status: str
    imported_at: str
    updated_at: str


@dataclass(frozen=True)
class LibraryPathRecord:
    """One `samples` row read without its analysis (issue #22).

    The scanner reconciles paths, roles, content identities and availability, so
    it reads rows at this level. `get_sample`, `find_sample_by_path`,
    `find_by_content` and `list_samples` instead rebuild a full contract
    `Sample`, and therefore raise `IncompleteFeatures` for a row whose features
    have not been written yet — a path is indexed before it is analysed.

    `path` is `samples.original_path` (the local path exactly as stored) and
    `path_key` is its normalised form.
    """

    sample_id: str
    role: str
    content_sha256: str
    path: str
    path_key: str
    filename: str
    file_status: str
    sample_rate_hz: int
    channels: int
    frame_count: int
    duration_ms: float
    imported_at: str
    updated_at: str


@contextmanager
def transaction(connection: sqlite3.Connection):
    """Run the enclosed statements as one atomic write transaction.

    Uses `BEGIN IMMEDIATE`, so the write lock is taken up front and a busy
    database fails fast instead of at commit time. Any exception rolls the
    whole block back and is re-raised unchanged.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        _rollback(connection)
        raise


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _coded(error: sqlite3.Error) -> LibraryError:
    if isinstance(error, sqlite3.OperationalError) and "locked" in str(error).lower():
        return DatabaseLocked("The database is locked by another writer.")
    return WriteFailed(f"The database refused the write: {error}")


def _is_hex(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in HEX_DIGITS for character in value))


def _content_identity(sample_id: str, content_sha256) -> str:
    if not _is_hex(content_sha256):
        raise InvalidContentIdentity("content_sha256 must be 64 lowercase hex characters.")
    if sample_id.startswith("sha256:") and sample_id != "sha256:" + content_sha256:
        raise InvalidContentIdentity(
            f"Content-addressed sample_id {sample_id} disagrees with content_sha256.")
    return content_sha256


def _normalise_tag(tag) -> str:
    if not isinstance(tag, str):
        raise InvalidTag("A tag must be a string.")
    normalised = tag.strip().lower()
    if not 1 <= len(normalised) <= TAG_MAX_LENGTH:
        raise InvalidTag(
            f"A tag must be 1 to {TAG_MAX_LENGTH} characters once trimmed; got {len(normalised)}.")
    return normalised


def _require_file_status(file_status) -> str:
    if file_status not in FILE_STATUSES:
        raise InvalidFileStatus(
            f"file_status must be one of {', '.join(FILE_STATUSES)}; got {file_status!r}.")
    return file_status


def _path_key(path):
    """`os.path.normcase(os.path.abspath(path))`, or None for an unusable value."""

    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError):
        return None


def _root_prefix(root) -> str:
    """The `path_key` prefix that every row inside `root` starts with."""

    key = _path_key(root)
    if key is None:
        raise InvalidSample("root must be a filesystem path.")
    return os.path.join(key, "")


def _path_record(row) -> LibraryPathRecord:
    return LibraryPathRecord(
        sample_id=row["sample_id"], role=row["role"], content_sha256=row["content_sha256"],
        path=row["original_path"], path_key=row["path_key"], filename=row["filename"],
        file_status=row["file_status"], sample_rate_hz=row["sample_rate_hz"],
        channels=row["channels"], frame_count=row["frame_count"], duration_ms=row["duration_ms"],
        imported_at=row["imported_at"], updated_at=row["updated_at"])


def _require_path(path) -> str:
    try:
        text = os.fspath(path)
    except TypeError:
        raise InvalidSample("path must be a non-empty local filesystem path.") from None
    if not isinstance(text, str) or not text:
        raise InvalidSample("path must be a non-empty local filesystem path.")
    key = _path_key(text)
    if key is None:
        raise InvalidSample("path must be a non-empty local filesystem path.")
    return key


def _validated_metadata(content_sha256, sample_rate_hz, channels, frame_count, duration_ms):
    """Check the audio metadata a path row stores, returning the content hash."""

    if not _is_hex(content_sha256):
        raise InvalidContentIdentity("content_sha256 must be 64 lowercase hex characters.")
    if not isinstance(sample_rate_hz, int) or isinstance(sample_rate_hz, bool) \
            or sample_rate_hz <= 0:
        raise InvalidSample("sample_rate_hz must be a positive integer.")
    if isinstance(channels, bool) or channels not in (1, 2):
        raise InvalidSample("channels must be 1 or 2.")
    if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count < 0:
        raise InvalidSample("frame_count must be a non-negative integer.")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) \
            or duration_ms < 0:
        raise InvalidSample("duration_ms must be a non-negative number.")
    return content_sha256


def _validated_sample(value) -> Sample:
    """A `Sample` for a `Sample` or wire dict, or `InvalidSample`.

    A `Sample` instance is re-validated through its own `to_dict()`, so a
    tampered instance cannot reach the database. `MEASURES` is re-checked here
    as well, because a measurement/unit pair that disagrees with the contract
    must be refused before anything is written.
    """

    if isinstance(value, Sample):
        try:
            payload = value.to_dict()
        except (TypeError, ValueError) as error:
            raise InvalidSample(f"sample: {error}") from error
    elif isinstance(value, dict):
        payload = value
    else:
        raise InvalidSample("sample: expected a Sample payload.")
    try:
        sample = Sample.from_dict(payload)
    except (TypeError, ValueError) as error:
        raise InvalidSample(f"sample: {error}") from error
    for measurement in sample.features.measurements:
        unit, _low, _high = MEASURES[measurement.name]
        if measurement.unit != unit:
            raise InvalidSample(
                f"{measurement.name}: unit {measurement.unit!r} disagrees with MEASURES {unit!r}.")
    return sample


class LibraryRepository:
    """Reads and writes one local sample library through one connection."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    # -- analysis versions -------------------------------------------------

    def register_analysis_version(self, descriptor) -> str:
        """Store `canonical(descriptor)` and return `digest(descriptor)`.

        Idempotent: registering the same descriptor again returns the same
        version and leaves one row. A descriptor that cannot be serialised
        raises `InvalidSample`; a stored row that no longer re-digests raises
        `AnalysisVersionMismatch`.
        """

        try:
            stored = canonical(descriptor)
        except (TypeError, ValueError) as error:
            raise InvalidSample(f"descriptor is not canonically serialisable: {error}") from error
        version = digest(descriptor)
        existing = self._one(
            "SELECT descriptor FROM analysis_versions WHERE analysis_version = ?", (version,))
        if existing is not None:
            self._descriptor(version, existing["descriptor"])
            return version
        with self._writing():
            self.connection.execute(
                "INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                "VALUES (?, ?, ?)", (version, stored, utc_now()))
        return version

    def _descriptor(self, analysis_version: str, stored: str):
        try:
            value = json.loads(stored)
        except (TypeError, ValueError) as error:
            raise AnalysisVersionMismatch(
                f"The stored descriptor for {analysis_version} is not JSON.") from error
        if canonical(value) != stored or digest(value) != analysis_version:
            raise AnalysisVersionMismatch(
                f"The stored descriptor for {analysis_version} does not re-digest to its version.")
        return value

    def _require_version(self, analysis_version: str):
        row = self._one("SELECT descriptor FROM analysis_versions WHERE analysis_version = ?",
                        (analysis_version,))
        if row is None:
            raise UnknownAnalysisVersion(
                f"analysis_version {analysis_version} is not registered.")
        return self._descriptor(analysis_version, row["descriptor"])

    # -- import and read ---------------------------------------------------

    def import_sample(self, sample, *, content_sha256, pack_id=None, tags=(), file_status="unknown"):
        """Store one analysed sample and return an `ImportResult`.

        The `samples` row, its 19 `sample_features` rows, its `sample_keys`
        row, its tags and its pack link are written in one transaction, so a
        failure leaves no partial rows. The audio file is never opened or
        statted, so an import succeeds for a sample whose file has since
        disappeared. `content_sha256` is required, must be 64 lowercase hex,
        and must match a content-addressed `sample_id`.

        Raises `InvalidSample`, `InvalidContentIdentity`, `InvalidTag`,
        `InvalidFileStatus`, `UnknownAnalysisVersion`, `DuplicateContent`,
        `PathConflict`, `DatabaseLocked` or `WriteFailed`.
        """

        validated = _validated_sample(sample)
        identity = _content_identity(validated.sample_id, content_sha256)
        _require_file_status(file_status)
        if isinstance(tags, str):
            raise InvalidTag("tags: expected a sequence of tags, not a single string.")
        normalised_tags = tuple(dict.fromkeys(_normalise_tag(tag) for tag in tags))
        path_key = os.path.normcase(os.path.abspath(validated.audio.local_path))
        filename = PureWindowsPath(validated.audio.local_path).name
        now = utc_now()

        with self._writing():
            self._require_version(validated.analysis_version)
            stored = self._one("SELECT * FROM samples WHERE sample_id = ?",
                               (validated.sample_id,))
            if stored is None:
                created, path_changed = True, False
                clash = self._one("SELECT sample_id FROM samples WHERE content_sha256 = ?",
                                  (identity,))
                if clash is not None:
                    raise DuplicateContent(
                        f"content_sha256 is already stored for {clash['sample_id']}.")
                clash = self._one("SELECT sample_id FROM samples WHERE path_key = ?", (path_key,))
                if clash is not None:
                    raise PathConflict(f"The path is already stored for {clash['sample_id']}.")
                self._require_pack(pack_id)
                self.connection.execute(
                    "INSERT INTO samples (sample_id, schema_version, content_sha256, role, "
                    "original_path, path_key, filename, pack_id, file_status, sample_rate_hz, "
                    "channels, frame_count, duration_ms, imported_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (validated.sample_id, validated.schema_version, identity, validated.role,
                     validated.audio.local_path, path_key, filename, pack_id, file_status,
                     validated.audio.sample_rate_hz, validated.audio.channels,
                     validated.audio.frame_count, validated.audio.duration_ms, now, now))
            else:
                created = False
                if stored["content_sha256"] != identity:
                    raise InvalidContentIdentity(
                        f"sample_id {validated.sample_id} is already stored with another "
                        "content_sha256.")
                clash = self._one(
                    "SELECT sample_id FROM samples WHERE path_key = ? AND sample_id <> ?",
                    (path_key, validated.sample_id))
                if clash is not None:
                    raise PathConflict(f"The path is already stored for {clash['sample_id']}.")
                self._require_pack(pack_id)
                path_changed = (stored["original_path"] != validated.audio.local_path
                                or stored["path_key"] != path_key)
                self.connection.execute(
                    "UPDATE samples SET original_path = ?, path_key = ?, filename = ?, "
                    "pack_id = ?, file_status = ?, updated_at = ? WHERE sample_id = ?",
                    (validated.audio.local_path, path_key, filename, pack_id, file_status, now,
                     validated.sample_id))
            self._write_features(validated)
            self._write_key(validated)
            for tag in normalised_tags:
                self.connection.execute(
                    "INSERT INTO sample_tags (sample_id, tag, added_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(sample_id, tag) DO NOTHING",
                    (validated.sample_id, tag, now))
        return ImportResult(validated.sample_id, validated.analysis_version, created, path_changed,
                            file_status)

    def _write_features(self, sample: Sample) -> None:
        for measurement in sample.features.measurements:
            unit, _low, _high = MEASURES[measurement.name]
            if measurement.unit != unit:
                raise InvalidSample(
                    f"{measurement.name}: unit {measurement.unit!r} disagrees with "
                    f"MEASURES {unit!r}.")
            self.connection.execute(
                "INSERT INTO sample_features (sample_id, analysis_version, measurement, unit, "
                "value, unavailable_reason, confidence) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sample_id, analysis_version, measurement) DO UPDATE SET "
                "unit = excluded.unit, value = excluded.value, "
                "unavailable_reason = excluded.unavailable_reason, "
                "confidence = excluded.confidence",
                (sample.sample_id, sample.analysis_version, measurement.name, measurement.unit,
                 measurement.value, measurement.unavailable_reason, measurement.confidence))

    def _write_key(self, sample: Sample) -> None:
        key = sample.features.key
        self.connection.execute(
            "INSERT INTO sample_keys (sample_id, analysis_version, tonic, mode, confidence, "
            "unavailable_reason) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sample_id, analysis_version) DO UPDATE SET tonic = excluded.tonic, "
            "mode = excluded.mode, confidence = excluded.confidence, "
            "unavailable_reason = excluded.unavailable_reason",
            (sample.sample_id, sample.analysis_version, key.tonic, key.mode, key.confidence,
             key.unavailable_reason))

    def get_sample(self, sample_id: str, analysis_version=None):
        """One `StoredSample`, or None when `sample_id` is unknown.

        Without `analysis_version` the stored version with the greatest
        `analysis_versions.created_at` wins, ties broken by `analysis_version`
        ascending. A known sample with no features under the requested version
        raises `UnknownAnalysisVersion`; a stored version that is missing rows
        raises `IncompleteFeatures`; a tampered descriptor raises
        `AnalysisVersionMismatch`.
        """

        stored = self._one("SELECT * FROM samples WHERE sample_id = ?", (sample_id,))
        if stored is None:
            return None
        if analysis_version is None:
            analysis_version = self._default_version(sample_id)
            if analysis_version is None:
                raise IncompleteFeatures(
                    f"sample_id {sample_id} has no stored analysis version.")
        elif not self._has_version(sample_id, analysis_version):
            raise UnknownAnalysisVersion(
                f"sample_id {sample_id} has no stored features under {analysis_version}.")
        self._require_version(analysis_version)
        return self._stored_sample(stored, analysis_version)

    def _has_version(self, sample_id: str, analysis_version: str) -> bool:
        row = self._one(
            "SELECT 1 FROM sample_features WHERE sample_id = ? AND analysis_version = ? "
            "UNION ALL SELECT 1 FROM sample_keys WHERE sample_id = ? AND analysis_version = ? "
            "LIMIT 1", (sample_id, analysis_version, sample_id, analysis_version))
        return row is not None

    def list_samples(self, role=None) -> tuple:
        """Every stored sample, optionally filtered by role, as `StoredSample`.

        Purely a database read: it never touches the filesystem. An unknown
        role simply matches nothing.
        """

        if role is None:
            rows = self._all("SELECT * FROM samples ORDER BY sample_id ASC")
        else:
            rows = self._all("SELECT * FROM samples WHERE role = ? ORDER BY sample_id ASC",
                             (role,))
        return tuple(self._stored(row) for row in rows)

    def list_analysis_versions(self, sample_id: str) -> tuple:
        """Every stored analysis version for one sample, ascending.

        Returns `()` for an unknown sample. A tampered descriptor raises
        `AnalysisVersionMismatch`.
        """

        rows = self._all(
            "SELECT av.analysis_version FROM analysis_versions AS av WHERE "
            "av.analysis_version IN "
            "(SELECT analysis_version FROM sample_features WHERE sample_id = ?) "
            "OR av.analysis_version IN "
            "(SELECT analysis_version FROM sample_keys WHERE sample_id = ?) "
            "ORDER BY av.analysis_version ASC", (sample_id, sample_id))
        for row in rows:
            self._require_version(row["analysis_version"])
        return tuple(row["analysis_version"] for row in rows)

    def find_sample_by_path(self, path):
        """The `StoredSample` whose normalised path equals `path`, or None.

        The argument is normalised with `os.path.normcase(os.path.abspath(...))`,
        exactly like the stored `path_key`, so a case or separator difference
        still matches.
        """

        try:
            path_key = os.path.normcase(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            return None
        row = self._one("SELECT * FROM samples WHERE path_key = ?", (path_key,))
        return None if row is None else self._stored(row)

    def find_by_content(self, content_sha256: str):
        """The `StoredSample` with that content hash, or None."""

        row = self._one("SELECT * FROM samples WHERE content_sha256 = ?", (content_sha256,))
        return None if row is None else self._stored(row)

    def _default_version(self, sample_id: str):
        row = self._one(
            "SELECT av.analysis_version FROM analysis_versions AS av WHERE "
            "av.analysis_version IN "
            "(SELECT analysis_version FROM sample_features WHERE sample_id = ?) "
            "OR av.analysis_version IN "
            "(SELECT analysis_version FROM sample_keys WHERE sample_id = ?) "
            "ORDER BY av.created_at DESC, av.analysis_version ASC LIMIT 1",
            (sample_id, sample_id))
        return None if row is None else row["analysis_version"]

    def _stored(self, row) -> StoredSample:
        analysis_version = self._default_version(row["sample_id"])
        if analysis_version is None:
            raise IncompleteFeatures(
                f"sample_id {row['sample_id']} has no stored analysis version.")
        self._require_version(analysis_version)
        return self._stored_sample(row, analysis_version)

    def _stored_sample(self, row, analysis_version: str) -> StoredSample:
        features = self._all(
            "SELECT measurement, unit, value, unavailable_reason, confidence FROM sample_features "
            "WHERE sample_id = ? AND analysis_version = ?",
            (row["sample_id"], analysis_version))
        by_name = {item["measurement"]: item for item in features}
        if len(features) != len(MEASURES) or set(by_name) != set(MEASURES):
            raise IncompleteFeatures(
                f"sample_id {row['sample_id']} does not hold every measurement under "
                f"{analysis_version}.")
        key = self._one(
            "SELECT tonic, mode, confidence, unavailable_reason FROM sample_keys "
            "WHERE sample_id = ? AND analysis_version = ?", (row["sample_id"], analysis_version))
        if key is None:
            raise IncompleteFeatures(
                f"sample_id {row['sample_id']} has no key row under {analysis_version}.")
        # Emit measurements in contract (MEASURES) order so a stored Sample
        # rebuilds to exactly the payload that was imported.
        measurements = tuple(
            Measurement(name=name, unit=by_name[name]["unit"], value=by_name[name]["value"],
                        unavailable_reason=by_name[name]["unavailable_reason"],
                        confidence=by_name[name]["confidence"])
            for name in MEASURES)
        sample = Sample(
            sample_id=row["sample_id"], role=row["role"],
            audio=AudioMetadata(local_path=row["original_path"],
                                sample_rate_hz=row["sample_rate_hz"], channels=row["channels"],
                                frame_count=row["frame_count"], duration_ms=row["duration_ms"]),
            features=AudioFeatures(
                measurements=measurements,
                key=MusicalKey(tonic=key["tonic"], mode=key["mode"],
                               confidence=key["confidence"],
                               unavailable_reason=key["unavailable_reason"])),
            analysis_version=analysis_version, schema_version=row["schema_version"])
        tags = tuple(item["tag"] for item in self._all(
            "SELECT tag FROM sample_tags WHERE sample_id = ? ORDER BY tag ASC",
            (row["sample_id"],)))
        return StoredSample(sample=sample, analysis_version=analysis_version,
                            pack_id=row["pack_id"], tags=tags, file_status=row["file_status"],
                            imported_at=row["imported_at"], updated_at=row["updated_at"])

    # -- mutable sample state ----------------------------------------------

    def mark_file_status(self, sample_id: str, file_status):
        """Store a new `file_status` without deleting or rewriting any row.

        `InvalidFileStatus` for another value, `UnknownSample` for an unknown id.
        """

        _require_file_status(file_status)
        with self._writing():
            self._require_sample(sample_id)
            self.connection.execute(
                "UPDATE samples SET file_status = ?, updated_at = ? WHERE sample_id = ?",
                (file_status, utc_now(), sample_id))

    def delete_sample(self, sample_id: str) -> bool:
        """Delete one sample and every child row; True when a row was removed.

        `sample_features`, `sample_keys` and `sample_tags` follow through
        `ON DELETE CASCADE`. Registered analysis versions and packs stay.
        """

        with self._writing():
            cursor = self.connection.execute("DELETE FROM samples WHERE sample_id = ?",
                                             (sample_id,))
            return cursor.rowcount == 1

    # -- path rows (issue #22) ---------------------------------------------

    def list_path_records(self, root=None) -> tuple:
        """Every stored path row, or every row whose path lies inside `root`.

        A purely structural read: it touches no file and never needs features, so
        rows that have no analysis yet are returned too. Rows come back ordered
        by `path_key`; a row whose path is the root's sibling (a shared name
        prefix) is not included.
        """

        rows = self._all("SELECT * FROM samples ORDER BY path_key ASC")
        if root is None:
            return tuple(_path_record(row) for row in rows)
        prefix = _root_prefix(root)
        return tuple(_path_record(row) for row in rows if row["path_key"].startswith(prefix))

    def find_path_record(self, path):
        """The `LibraryPathRecord` whose normalised path equals `path`, or None.

        The argument is normalised exactly like the stored `path_key`, so a case
        or separator difference still matches.
        """

        key = _path_key(path)
        if key is None:
            return None
        row = self._one("SELECT * FROM samples WHERE path_key = ?", (key,))
        return None if row is None else _path_record(row)

    def find_path_records_by_content(self, content_sha256: str) -> tuple:
        """Every path row holding that content identity, ordered by `path_key`.

        The row-level counterpart of `find_by_content`, which needs a complete
        analysis to return anything.
        """

        rows = self._all("SELECT * FROM samples WHERE content_sha256 = ? ORDER BY path_key ASC",
                         (content_sha256,))
        return tuple(_path_record(row) for row in rows)

    def has_current_analysis(self, content_sha256: str, analysis_version: str) -> bool:
        """True when some row holding that content identity stores that version.

        "Stores" means a `sample_features` or `sample_keys` row exists for the
        version, which is how every read of a version starts.
        """

        row = self._one(
            "SELECT 1 FROM samples AS s WHERE s.content_sha256 = ? AND ("
            "EXISTS (SELECT 1 FROM sample_features AS f "
            "        WHERE f.sample_id = s.sample_id AND f.analysis_version = ?) "
            "OR EXISTS (SELECT 1 FROM sample_keys AS k "
            "           WHERE k.sample_id = s.sample_id AND k.analysis_version = ?)) LIMIT 1",
            (content_sha256, analysis_version, analysis_version))
        return row is not None

    def insert_path_record(self, path, *, role, content_sha256, sample_rate_hz, channels,
                           frame_count, duration_ms, file_status="present", sample_id=None):
        """Index one local path without analysis and return its path record.

        The row uses the given `sample_id`, or the content-addressed
        `sha256:<content_sha256>` when none is given. No feature, key, tag, pack
        or analysis-version row is written and nothing is registered: the row
        exists so the path, role and content identity survive until analysis
        (issue #23) stores features.

        The audio metadata is what the caller measured; this method never opens
        the file. Raises `InvalidContentIdentity` for a malformed hash,
        `InvalidSample` for a role or metadata the contract cannot hold,
        `InvalidFileStatus` for another status, `DuplicateContent` when another
        row already holds that content identity, `PathConflict` when another row
        already holds that path, `DatabaseLocked` or `WriteFailed`.
        """

        if role not in ROLES:
            raise InvalidSample(f"role must be one of {', '.join(ROLES)}; got {role!r}.")
        identity = _validated_metadata(content_sha256, sample_rate_hz, channels, frame_count,
                                       duration_ms)
        _require_file_status(file_status)
        if sample_id is None:
            sample_id = "sha256:" + identity
        elif not isinstance(sample_id, str) or not sample_id:
            raise InvalidSample("sample_id must be a non-empty string.")
        path_key = _require_path(path)
        filename = PureWindowsPath(str(path)).name
        now = utc_now()
        with self._writing():
            clash = self._one("SELECT sample_id FROM samples WHERE content_sha256 = ?", (identity,))
            if clash is not None:
                raise DuplicateContent(
                    f"content_sha256 is already stored for {clash['sample_id']}.")
            clash = self._one("SELECT sample_id FROM samples WHERE path_key = ?", (path_key,))
            if clash is not None:
                raise PathConflict(f"The path is already stored for {clash['sample_id']}.")
            self.connection.execute(
                "INSERT INTO samples (sample_id, schema_version, content_sha256, role, "
                "original_path, path_key, filename, pack_id, file_status, sample_rate_hz, "
                "channels, frame_count, duration_ms, imported_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample_id, "1.0", identity, role, str(path), path_key, filename, None,
                 file_status, sample_rate_hz, channels, frame_count, duration_ms, now, now))
            row = self._one("SELECT * FROM samples WHERE sample_id = ?", (sample_id,))
        return _path_record(row)

    def update_path_record_content(self, sample_id: str, *, content_sha256, sample_rate_hz,
                                   channels, frame_count, duration_ms, file_status="present"):
        """Replace one row's content identity and audio metadata in place.

        The stored `sample_id`, path, role, `imported_at` and every feature row
        stay as they were: an edited file keeps its record identity and its
        previous content's analysis stays readable under that content's
        `analysis_version`. Raises the codes `insert_path_record` documents,
        plus `UnknownSample` for an unknown row.
        """

        identity = _validated_metadata(content_sha256, sample_rate_hz, channels, frame_count,
                                       duration_ms)
        _require_file_status(file_status)
        with self._writing():
            self._require_sample(sample_id)
            clash = self._one(
                "SELECT sample_id FROM samples WHERE content_sha256 = ? AND sample_id <> ?",
                (identity, sample_id))
            if clash is not None:
                raise DuplicateContent(
                    f"content_sha256 is already stored for {clash['sample_id']}.")
            self.connection.execute(
                "UPDATE samples SET content_sha256 = ?, sample_rate_hz = ?, channels = ?, "
                "frame_count = ?, duration_ms = ?, file_status = ?, updated_at = ? "
                "WHERE sample_id = ?",
                (identity, sample_rate_hz, channels, frame_count, duration_ms, file_status,
                 utc_now(), sample_id))

    def relocate_path_record(self, sample_id: str, path, file_status="present"):
        """Point one row at its new local path and restore its availability.

        The row identity, role, content identity, `imported_at` and every feature
        row stay as they were, and nothing is queued: a moved file is the same
        content under a new path. Raises `PathConflict` when another row already
        holds that path, plus `UnknownSample`, `InvalidFileStatus`,
        `DatabaseLocked` or `WriteFailed`.
        """

        _require_file_status(file_status)
        path_key = _require_path(path)
        filename = PureWindowsPath(str(path)).name
        with self._writing():
            self._require_sample(sample_id)
            clash = self._one(
                "SELECT sample_id FROM samples WHERE path_key = ? AND sample_id <> ?",
                (path_key, sample_id))
            if clash is not None:
                raise PathConflict(f"The path is already stored for {clash['sample_id']}.")
            self.connection.execute(
                "UPDATE samples SET original_path = ?, path_key = ?, filename = ?, "
                "file_status = ?, updated_at = ? WHERE sample_id = ?",
                (str(path), path_key, filename, file_status, utc_now(), sample_id))

    # -- tags --------------------------------------------------------------

    def add_tag(self, sample_id: str, tag) -> bool:
        """Add one normalised tag; False when the sample already carries it.

        The tag is trimmed and lowercased first, so adding it again is a no-op
        that leaves `added_at` untouched. `InvalidTag` for a non-string, an
        empty tag, or one longer than 64 characters once trimmed;
        `UnknownSample` for an unknown id.
        """

        normalised = _normalise_tag(tag)
        with self._writing():
            self._require_sample(sample_id)
            existing = self._one(
                "SELECT added_at FROM sample_tags WHERE sample_id = ? AND tag = ?",
                (sample_id, normalised))
            if existing is not None:
                return False
            self.connection.execute(
                "INSERT INTO sample_tags (sample_id, tag, added_at) VALUES (?, ?, ?)",
                (sample_id, normalised, utc_now()))
        return True

    def remove_tag(self, sample_id: str, tag) -> bool:
        """Remove one normalised tag; True when a row was removed."""

        normalised = _normalise_tag(tag)
        with self._writing():
            self._require_sample(sample_id)
            cursor = self.connection.execute(
                "DELETE FROM sample_tags WHERE sample_id = ? AND tag = ?",
                (sample_id, normalised))
            return cursor.rowcount == 1

    def list_tags(self, sample_id: str) -> tuple:
        """Every stored tag for one sample, ascending. `UnknownSample` if absent."""

        self._require_sample(sample_id)
        return tuple(item["tag"] for item in self._all(
            "SELECT tag FROM sample_tags WHERE sample_id = ? ORDER BY tag ASC", (sample_id,)))

    # -- packs -------------------------------------------------------------

    def upsert_pack(self, pack_id: str, name: str, vendor=None) -> None:
        """Create or update a pack's `name` and `vendor`; idempotent.

        `created_at` is set once and kept. `WriteFailed` for a blank or
        whitespace-padded `pack_id`, `name` or `vendor`.
        """

        for field, value in (("pack_id", pack_id), ("name", name)):
            if not isinstance(value, str) or not value or value != value.strip():
                raise WriteFailed(
                    f"{field} must be a nonblank string without surrounding whitespace.")
        if vendor is not None and (not isinstance(vendor, str) or not vendor
                                   or vendor != vendor.strip()):
            raise WriteFailed(
                "vendor must be None or a nonblank string without surrounding whitespace.")
        with self._writing():
            self.connection.execute(
                "INSERT INTO sample_packs (pack_id, name, vendor, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(pack_id) DO UPDATE SET name = excluded.name, vendor = excluded.vendor",
                (pack_id, name, vendor, utc_now()))

    def set_pack(self, sample_id: str, pack_id) -> None:
        """Link a sample to a registered pack, or clear the link with None.

        `UnknownSample` for an unknown id; `WriteFailed` when the pack is not
        registered.
        """

        with self._writing():
            self._require_sample(sample_id)
            self._require_pack(pack_id)
            self.connection.execute(
                "UPDATE samples SET pack_id = ?, updated_at = ? WHERE sample_id = ?",
                (pack_id, utc_now(), sample_id))

    def _require_pack(self, pack_id) -> None:
        if pack_id is None:
            return
        if not isinstance(pack_id, str) or not pack_id or pack_id != pack_id.strip():
            raise WriteFailed(
                "pack_id must be None or a nonblank string without surrounding whitespace.")
        if self._one("SELECT pack_id FROM sample_packs WHERE pack_id = ?", (pack_id,)) is None:
            raise WriteFailed(f"pack {pack_id} is not registered; call upsert_pack first.")

    # -- internals ---------------------------------------------------------

    @contextmanager
    def _writing(self):
        try:
            with transaction(self.connection):
                yield
        except sqlite3.Error as error:
            raise _coded(error) from error

    def _require_sample(self, sample_id) -> None:
        if self._one("SELECT sample_id FROM samples WHERE sample_id = ?", (sample_id,)) is None:
            raise UnknownSample(f"No stored sample with sample_id {sample_id}.")

    def _one(self, statement: str, parameters=()):
        rows = self.connection.execute(statement, parameters).fetchall()
        return rows[0] if rows else None

    def _all(self, statement: str, parameters=()):
        return self.connection.execute(statement, parameters).fetchall()
