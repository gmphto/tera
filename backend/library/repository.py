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
- Palettes (issue #24). A palette is one active item per `MVP_SLOTS` slot, and
  a mutation is one transaction that compares the caller's `expected_revision`
  with the stored one, bumps it by exactly one and writes the new row. A read
  resolves each item against `samples` without writing anything and never
  raises for a sample that is missing, unknown or pruned.
- Retrieval (issue #25). `list_retrieval_rows` is one statement per read: a
  row's analysis state (`current`, `stale`, `absent`), its stored availability
  and, for a current row, the validated `Sample` of that version, so retrieval
  fits a normalization and maps availability without a per-candidate query.
- API reads (issue #27). `page_samples` is one statement for one page, keyed
  by content identity and filtered by role and file name, so a page's cost does
  not grow with the library; `sample_detail` serves one sample's stored
  versions and measurements; `sample_counts` is what `/health` reports. A
  sample is addressed as `sha256:<content_sha256>` because that is the identity
  a stored sample cannot change, and the row's own `library:...` id stays
  inside this module.

Only the standard library and `backend.contracts` / `backend.analysis.batch`
are imported here; `json` is used to parse a stored descriptor back so it can
be re-digested. No third-party package is imported.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import os
from pathlib import PureWindowsPath
import sqlite3
import uuid

from backend.analysis.batch import ROLES, analysis_descriptor, canonical, digest
from backend.contracts import (
    AudioFeatures,
    AudioMetadata,
    MEASURES,
    Measurement,
    MusicalKey,
    Sample,
    SongContext,
)
from backend.library.errors import (
    AnalysisVersionMismatch,
    DatabaseLocked,
    DuplicateContent,
    IncompleteFeatures,
    InvalidContentIdentity,
    InvalidContext,
    InvalidFileStatus,
    InvalidSample,
    InvalidTag,
    LibraryError,
    PathConflict,
    RevisionConflict,
    RoleMismatch,
    UnknownAnalysisVersion,
    UnknownPalette,
    UnknownProject,
    UnknownSample,
    UnknownSlot,
    WriteFailed,
)
from backend.library.schema import FILE_STATUSES, utc_now
from backend.palette.model import (
    MVP_SLOTS,
    SLOT_ROLES,
    SONG_CONTEXT_ABSENT_REASON,
    PaletteContextState,
    PaletteItemRecord,
    PaletteMutation,
    PaletteRecord,
    ProjectRecord,
)


HEX_DIGITS = "0123456789abcdef"
TAG_MAX_LENGTH = 64

# The decision-cache tables (issue #26). These two names are the only place the
# cache's column order is written: `decision_cache` is inserted by naming every
# column, so a missing or extra value is a programming error rather than a
# silently misaligned row.
DECISION_CACHE_COLUMNS = (
    "cache_key", "cache_key_version", "decision_kind", "source", "interface_name",
    "adapter_version", "prompt_version", "model_version", "palette_hash",
    "palette_hash_version", "candidate_id", "candidate_content_fingerprint",
    "candidate_analysis_version", "dimension", "question_id", "kick_id",
    "kick_content_fingerprint", "kick_analysis_version", "questions_digest",
    "ranking_version", "weight_table_id", "baseline_ranking_version",
    "baseline_weight_table_id", "payload_json", "created_at",
)

DECISION_MODEL_VERSION_COLUMNS = (
    "interface_name", "source", "adapter_version", "prompt_version", "model_version",
    "first_observed_at", "observed_at",
)


# The one statement `list_retrieval_rows` runs (issue #25): the samples row, its
# measurements and key row at the requested version, and its analysis state.
_RETRIEVAL_SELECT = (
    "SELECT s.sample_id, s.schema_version, s.role, s.original_path, s.path_key, s.file_status, "
    "s.content_sha256, s.sample_rate_hz, s.channels, s.frame_count, s.duration_ms, "
    "f.measurement, f.unit, f.value, f.unavailable_reason, f.confidence, "
    "k.sample_id AS key_sample_id, k.tonic, k.mode, k.confidence AS key_confidence, "
    "k.unavailable_reason AS key_unavailable_reason, "
    "CASE WHEN f.sample_id IS NOT NULL OR k.sample_id IS NOT NULL THEN 'current' "
    "WHEN EXISTS (SELECT 1 FROM sample_features AS f2 WHERE f2.sample_id = s.sample_id) "
    "OR EXISTS (SELECT 1 FROM sample_keys AS k2 WHERE k2.sample_id = s.sample_id) "
    "THEN 'stale' ELSE 'absent' END AS analysis_state "
    "FROM samples AS s "
    "LEFT JOIN sample_features AS f ON f.sample_id = s.sample_id AND f.analysis_version = ? "
    "LEFT JOIN sample_keys AS k ON k.sample_id = s.sample_id AND k.analysis_version = ?"
)




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


@dataclass(frozen=True)
class RetrievalRow:
    """One samples row read for normalized retrieval (issue #25).

    `analysis_state` is `current` when the row stores the requested analysis
    version, `stale` when it stores another version and `absent` when it
    stores no analysis at all. `sample` is the validated contract `Sample`
    rebuilt from the row's 19 measurements and its key row at the requested
    version, or None for a stale or absent row. `path_key` is the normalised
    path the duplicate collapse compares, exactly as `pending_analysis` (#22)
    compares it, and `file_status` is the stored availability (#22/#24) that
    becomes the retrieval availability mapping.
    """

    sample_id: str
    role: str
    path: str
    path_key: str
    file_status: str
    content_sha256: str
    analysis_state: str
    sample: Sample | None


# The analysis state #27's routes report per sample. `current`, `stale` and
# `absent` are #25's read states; `pending` and `failed` fill the `absent`
# gap with what #23 knows about the work, so a client can tell "no analysis yet,
# one is queued" from "no analysis, the last attempt failed".
ANALYSIS_CURRENT = "current"
ANALYSIS_STALE = "stale"
ANALYSIS_PENDING = "pending"
ANALYSIS_FAILED = "failed"
ANALYSIS_ABSENT = "absent"

ANALYSIS_STATES = (ANALYSIS_CURRENT, ANALYSIS_STALE, ANALYSIS_PENDING, ANALYSIS_FAILED,
                   ANALYSIS_ABSENT)

# #23's item states that decide the queue half of a sample's analysis state.
# Spelled here rather than imported: `backend.library.queue` imports this module
# for `transaction`, so the two names are the values its CHECK constraint holds.
QUEUED_ITEM_STATES = ("pending", "running")
FAILED_ITEM_STATE = "failed"

# The API-facing identity of a stored sample. A `samples` row may carry a minted
# `library:...` id (#22 mints one so a move or an edit keeps the row's
# identity), so #27 addresses a sample by the one identity that never changes
# for stored content: its `content_sha256`. `job_items.sample_id` is already
# that content identity, which is why the queue join spells it the same way.
CONTENT_ID_PREFIX = "sha256:"

# The precedence one sample's analysis state is decided with, written once as
# SQL for a page and once as `_analysis_state` for a single sample: what is
# stored beats what is queued, and a recorded failure is the last thing before
# "nothing is known". Both use the same order.
_ANALYSIS_SQL_CASE = (
    "CASE "
    "WHEN EXISTS (SELECT 1 FROM sample_features AS f "
    "             WHERE f.sample_id = s.sample_id AND f.analysis_version = ?) "
    "  OR EXISTS (SELECT 1 FROM sample_keys AS k "
    "             WHERE k.sample_id = s.sample_id AND k.analysis_version = ?) THEN 'current' "
    "WHEN EXISTS (SELECT 1 FROM sample_features AS f WHERE f.sample_id = s.sample_id) "
    "  OR EXISTS (SELECT 1 FROM sample_keys AS k WHERE k.sample_id = s.sample_id) THEN 'stale' "
    "WHEN EXISTS (SELECT 1 FROM job_items AS j "
    "             WHERE j.sample_id = 'sha256:' || s.content_sha256 "
    "               AND j.analysis_version = ? AND j.state IN ('pending', 'running')) "
    "THEN 'pending' "
    "WHEN EXISTS (SELECT 1 FROM job_items AS j "
    "             WHERE j.sample_id = 'sha256:' || s.content_sha256 "
    "               AND j.analysis_version = ? AND j.state = 'failed') THEN 'failed' "
    "ELSE 'absent' END"
)

# The one statement `page_samples` runs: the samples row, its availability and
# its analysis state, ordered by content identity. `limit + 1` rows are read so
# "is there another page" needs no second statement.
_PAGE_SELECT = (
    "SELECT s.content_sha256, s.role, s.filename, s.file_status, s.sample_rate_hz, "
    "s.channels, s.frame_count, s.duration_ms, " + _ANALYSIS_SQL_CASE + " AS analysis_state "
    "FROM samples AS s"
)


def content_identity(content_sha256: str) -> str:
    """The contract identity of a stored content hash, as #27's routes spell it."""

    return CONTENT_ID_PREFIX + content_sha256


def content_fingerprint(sample_id: str) -> str:
    """The 64-hex content hash inside a `sha256:<hash>` sample id."""

    return sample_id[len(CONTENT_ID_PREFIX):]


def _analysis_state(stored_versions, analysis_version, item_state) -> str:
    """The state a stored sample and its queue row report, in the SQL order."""

    if analysis_version in stored_versions:
        return ANALYSIS_CURRENT
    if stored_versions:
        return ANALYSIS_STALE
    if item_state in QUEUED_ITEM_STATES:
        return ANALYSIS_PENDING
    if item_state == FAILED_ITEM_STATE:
        return ANALYSIS_FAILED
    return ANALYSIS_ABSENT


@dataclass(frozen=True)
class SamplePageRow:
    """One sample as a page of the library reports it (issue #27).

    Deliberately not a `Sample`: the row is read without its measurements, so a
    sample whose analysis has not been written yet is paged instead of raising
    `IncompleteFeatures`. `sample_id` is `sha256:<content_sha256>`, and
    `analysis_version` is the stored version only when `analysis_state` is
    `current`.
    """

    sample_id: str
    role: str
    file_name: str
    file_status: str
    analysis_state: str
    analysis_version: str | None
    sample_rate_hz: int
    channels: int
    frame_count: int
    duration_ms: float


@dataclass(frozen=True)
class SamplePage:
    """One page of samples and whether another page follows."""

    rows: tuple
    has_more: bool


@dataclass(frozen=True)
class SampleDetail:
    """One stored sample read for the feature-detail route (issue #27).

    `sample` is the validated contract `Sample` of `analysis_version`, or None
    when the state is not `current`; `stored_versions` lists every stored
    analysis version ascending; `analyzed_at`, `attempts` and `error_code` come
    from the sample's #23 item row at the current version and are None when that
    row does not exist. Nothing here is derived, normalised or imputed.
    """

    sample_id: str
    role: str
    file_name: str
    file_status: str
    analysis_state: str
    analysis_version: str | None
    analyzed_at: str | None
    attempts: int | None
    error_code: str | None
    stored_versions: tuple
    sample_rate_hz: int
    channels: int
    frame_count: int
    duration_ms: float
    sample: Sample | None


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


def coded_error(error: sqlite3.Error) -> LibraryError:
    """#21's mapping from a sqlite3 failure to the coded library error.

    Public so a caller that owns its own `transaction` block -- issue #26's
    cache, whose insert and pruning must commit as one -- maps a lock or a
    refused write exactly as a repository method does.
    """

    return _coded(error)

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


def _new_id(prefix: str) -> str:
    """An opaque identifier: a prefix and 32 random hex characters (uuid4)."""

    return f"{prefix}-{uuid.uuid4().hex}"


def _require_label(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WriteFailed(f"{name} must be a nonblank string.")
    return value


def _validated_context(value) -> SongContext:
    """A `SongContext` for a `SongContext` or wire dict, or `InvalidContext`.

    A `SongContext` instance is re-validated through its own `to_dict()`, so a
    tampered instance cannot reach the database, exactly as `_validated_sample`
    re-validates a `Sample`.
    """

    if isinstance(value, SongContext):
        try:
            payload = value.to_dict()
        except (TypeError, ValueError) as error:
            raise InvalidContext(f"song: {error}") from error
    elif isinstance(value, dict):
        payload = value
    else:
        raise InvalidContext("song: expected a SongContext payload.")
    try:
        return SongContext.from_dict(payload)
    except (TypeError, ValueError) as error:
        raise InvalidContext(f"song: {error}") from error


def _context_columns(song: SongContext) -> dict:
    """The stored column values of one validated song context."""

    return {
        "tempo_bpm": song.tempo.value,
        "tempo_confidence": song.tempo.confidence,
        "tempo_unavailable_reason": song.tempo.unavailable_reason,
        "key_tonic": song.key.tonic,
        "key_mode": song.key.mode,
        "key_confidence": song.key.confidence,
        "key_unavailable_reason": song.key.unavailable_reason,
        "genre": song.genre,
        "genre_unavailable_reason": song.genre_unavailable_reason,
    }


def _context_field_state(value, reason) -> str:
    if value is not None:
        return "known"
    return "unset" if reason is None else "unknown"


#: The columns each context field owns, so one field can be cleared (issue #32):
#: all NULL is the storage state `_context_field_state` reads as `unset`.
_CONTEXT_FIELD_COLUMNS = {
    "tempo": ("tempo_bpm", "tempo_confidence", "tempo_unavailable_reason"),
    "key": ("key_tonic", "key_mode", "key_confidence", "key_unavailable_reason"),
    "genre": ("genre", "genre_unavailable_reason"),
}


def _context_reason(reason, state):
    """The contract reason for one stored field.

    The contract has no third state for an absent value: a field whose columns
    are all NULL (the storage state `unset`) must still report a nonblank
    reason, and that reason is `SONG_CONTEXT_ABSENT_REASON`. A stored reason
    alongside a value is not written by this module and is dropped here rather
    than allowed to make a read raise.
    """

    if state == "unset":
        return SONG_CONTEXT_ABSENT_REASON
    if state == "known":
        return None
    return reason


def _context_of(row):
    """The stored song context and its per-field state, read from a palettes row.

    `context_state` is the storage truth (`known`, `unknown`, `unset`); `song`
    is the contract assembly of the same row, so a read always returns a valid
    `SongContext` and never raises for an unset palette.
    """

    state = PaletteContextState(
        tempo=_context_field_state(row["tempo_bpm"], row["tempo_unavailable_reason"]),
        key=_context_field_state(row["key_tonic"], row["key_unavailable_reason"]),
        genre=_context_field_state(row["genre"], row["genre_unavailable_reason"]))
    song = SongContext(
        tempo=Measurement(name="tempo", value=row["tempo_bpm"], unit="BPM",
                          unavailable_reason=_context_reason(
                              row["tempo_unavailable_reason"], state.tempo),
                          confidence=row["tempo_confidence"]),
        key=MusicalKey(tonic=row["key_tonic"], mode=row["key_mode"],
                       confidence=row["key_confidence"],
                       unavailable_reason=_context_reason(
                           row["key_unavailable_reason"], state.key)),
        genre=row["genre"],
        genre_unavailable_reason=_context_reason(row["genre_unavailable_reason"], state.genre))
    return song, state


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

    # -- analysis for an existing path row (issue #23) ---------------------

    def store_analysis(self, sample, *, content_sha256, sample_id=None, descriptor=None) -> str:
        """Store one analysis for an existing path row, in the caller's transaction.

        #22 indexes a path before it is analysed, and #9's extractor assembles a
        `Sample` whose identity is the *content* identity, while a scanned row's
        identity is minted from its path. This operation therefore names the row it
        writes for: `sample_id` defaults to the sample's own id and is the
        `samples` row whose features are written. It writes the version's
        `analysis_versions` row when `descriptor` is given, the 19
        `sample_features` rows and the `sample_keys` row, and changes no column of
        `samples`: the worker that stores an analysis never creates, moves or
        updates a sample row.

        The caller owns the transaction -- this method issues no BEGIN and no
        COMMIT, so an item's features, its analysis version and the queue row that
        records them commit or roll back together. It refuses to run outside one.
        With `descriptor` the version must re-digest from it; without it the
        version must already be registered.

        Raises `InvalidSample`, `invalid_content_identity` when the row no longer
        stores that content (the worker reads both that and `unknown_sample` as
        `sample_missing`), `unknown_analysis_version`, `analysis_version_mismatch`
        or `write_failed` outside a transaction.
        """

        if not self.connection.in_transaction:
            raise WriteFailed(
                "store_analysis must run inside a transaction(connection) block, so an "
                "analysis and the queue row that records it commit together.")
        validated = _validated_sample(sample)
        identity = _content_identity(validated.sample_id, content_sha256)
        target = validated.sample_id if sample_id is None else sample_id
        if not isinstance(target, str) or not target:
            raise InvalidSample("sample_id must be None or a non-empty string.")
        row = self._one("SELECT sample_id, content_sha256 FROM samples WHERE sample_id = ?",
                        (target,))
        if row is None:
            raise UnknownSample(f"No stored sample with sample_id {target}.")
        if row["content_sha256"] != identity:
            raise InvalidContentIdentity(
                f"sample_id {target} no longer stores the content identity {identity}.")
        version = validated.analysis_version
        if descriptor is None:
            self._require_version(version)
        else:
            try:
                stored = canonical(descriptor)
            except (TypeError, ValueError) as error:
                raise InvalidSample(
                    f"descriptor is not canonically serialisable: {error}") from error
            if digest(descriptor) != version:
                raise InvalidSample(
                    f"descriptor does not re-digest to the sample's analysis version {version}.")
            existing = self._one(
                "SELECT descriptor FROM analysis_versions WHERE analysis_version = ?", (version,))
            if existing is None:
                self.connection.execute(
                    "INSERT INTO analysis_versions (analysis_version, descriptor, created_at) "
                    "VALUES (?, ?, ?)", (version, stored, utc_now()))
            else:
                self._descriptor(version, existing["descriptor"])
        if target != validated.sample_id:
            validated = replace(validated, sample_id=target)
        self._write_features(validated)
        self._write_key(validated)
        return version

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

    def stored_role(self, sample_id: str):
        """The role one stored sample carries, or None when there is no row.

        Public so a caller can name the role a slot refused without reading the
        sample's features (issue #32's `role_mismatch` details).
        """

        row = self._one("SELECT role FROM samples WHERE sample_id = ?", (sample_id,))
        return None if row is None else row["role"]

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

    # -- retrieval reads (issue #25) ---------------------------------------

    def list_retrieval_rows(self, analysis_version, *, sample_ids=None, roles=None) -> tuple:
        """Every stored row an analysis version names, in one statement.

        Named by `sample_ids` this reads exactly those `samples` rows whatever
        their role; without it, every row whose role is in `roles` (every row
        when `roles` is None). The `samples` row is left-joined to that
        version's `sample_features` and `sample_keys` rows, and each row's
        analysis state is decided by correlated `EXISTS` lookups on the
        primary-key prefix, so the statement count does not grow with the number
        of rows. Rows come back ordered by `sample_id`.

        A row is `current` when it stores that version, `stale` when it stores
        another version and `absent` when it stores no analysis at all. A
        current row that lacks any of the 19 measurements or its key row raises
        `IncompleteFeatures`, and a current row whose registered descriptor no
        longer re-digests raises `AnalysisVersionMismatch`; a database with no
        current row is read without touching `analysis_versions`.
        """

        if not isinstance(analysis_version, str) or not analysis_version:
            raise InvalidSample("analysis_version must be a non-empty string.")
        clauses, parameters = [], [analysis_version, analysis_version]
        if sample_ids is not None:
            ids = tuple(sample_ids)
            if not ids:
                return ()
            if any(not isinstance(value, str) or not value for value in ids):
                raise InvalidSample("sample_ids must be non-empty strings.")
            clauses.append("s.sample_id IN (" + ", ".join("?" * len(ids)) + ")")
            parameters.extend(ids)
        elif roles is not None:
            role_list = tuple(roles)
            if not role_list:
                return ()
            clauses.append("s.role IN (" + ", ".join("?" * len(role_list)) + ")")
            parameters.extend(role_list)
        statement = (_RETRIEVAL_SELECT + ("" if not clauses else " WHERE " + " AND ".join(clauses))
                     + " ORDER BY s.sample_id ASC, f.measurement ASC")
        rows = self._all(statement, tuple(parameters))
        if any(row["analysis_state"] == "current" for row in rows):
            self._require_version(analysis_version)
        grouped = {}
        for row in rows:
            grouped.setdefault(row["sample_id"], []).append(row)
        return tuple(
            RetrievalRow(
                sample_id=items[0]["sample_id"], role=items[0]["role"],
                path=items[0]["original_path"], path_key=items[0]["path_key"],
                file_status=items[0]["file_status"],
                content_sha256=items[0]["content_sha256"],
                analysis_state=items[0]["analysis_state"],
                sample=(None if items[0]["analysis_state"] != "current"
                        else self._retrieval_sample(items, analysis_version)))
            for items in grouped.values())

    def _retrieval_sample(self, rows, analysis_version: str) -> Sample:
        """One current retrieval row rebuilt as a validated contract `Sample`."""

        first = rows[0]
        by_name = {row["measurement"]: row for row in rows if row["measurement"] is not None}
        if len(by_name) != len(MEASURES) or set(by_name) != set(MEASURES):
            raise IncompleteFeatures(
                f"sample_id {first['sample_id']} does not hold every measurement under "
                f"{analysis_version}.")
        if first["key_sample_id"] is None:
            raise IncompleteFeatures(
                f"sample_id {first['sample_id']} has no key row under {analysis_version}.")
        # MEASURES order, exactly as _stored_sample rebuilds it, so a retrieval
        # sample equals the sample every other read returns.
        measurements = tuple(
            Measurement(name=name, unit=by_name[name]["unit"], value=by_name[name]["value"],
                        unavailable_reason=by_name[name]["unavailable_reason"],
                        confidence=by_name[name]["confidence"])
            for name in MEASURES)
        return Sample(
            sample_id=first["sample_id"], role=first["role"],
            audio=AudioMetadata(local_path=first["original_path"],
                                sample_rate_hz=first["sample_rate_hz"],
                                channels=first["channels"], frame_count=first["frame_count"],
                                duration_ms=first["duration_ms"]),
            features=AudioFeatures(
                measurements=measurements,
                key=MusicalKey(tonic=first["tonic"], mode=first["mode"],
                               confidence=first["key_confidence"],
                               unavailable_reason=first["key_unavailable_reason"])),
            analysis_version=analysis_version, schema_version=first["schema_version"])

    # -- API reads (issue #27) ---------------------------------------------

    def page_samples(self, analysis_version, *, roles=None, text=None, after=None,
                     limit=50) -> SamplePage:
        """One page of stored samples, filtered by role and file name, in one statement.

        Ordered by content identity ascending, which `content_identity` spells
        `sha256:<content_sha256>`; `after` is such an identity and the read is
        strictly greater than it, so a cursor is a position and never a filter
        that could re-admit a row outside the request's roles or text. `roles`
        is OR-ed; `text` is a case-insensitive substring match on the stored
        file name only, never on a path. `limit + 1` rows are read, so
        `has_more` costs no second statement: one call is one SQL statement
        however large the page is.

        A row that stores no measurements is returned as well, with its
        `analysis_state` saying why: this read never rebuilds a `Sample`, so it
        cannot raise `IncompleteFeatures`.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise InvalidSample("limit must be a positive integer.")
        parameters = [analysis_version, analysis_version, analysis_version, analysis_version]
        clauses = []
        if roles:
            roles = tuple(roles)
            clauses.append("s.role IN (" + ", ".join("?" * len(roles)) + ")")
            parameters.extend(roles)
        if text is not None:
            clauses.append("instr(lower(s.filename), lower(?)) > 0")
            parameters.append(text)
        if after is not None:
            if not isinstance(after, str) or not after.startswith(CONTENT_ID_PREFIX):
                raise InvalidSample("after must be a sha256: content identity.")
            clauses.append("s.content_sha256 > ?")
            parameters.append(content_fingerprint(after))
        statement = (_PAGE_SELECT + ("" if not clauses else " WHERE " + " AND ".join(clauses))
                     + " ORDER BY s.content_sha256 ASC LIMIT ?")
        parameters.append(limit + 1)
        rows = self._all(statement, tuple(parameters))
        has_more = len(rows) > limit
        return SamplePage(
            rows=tuple(
                SamplePageRow(
                    sample_id=content_identity(row["content_sha256"]), role=row["role"],
                    file_name=row["filename"], file_status=row["file_status"],
                    analysis_state=row["analysis_state"],
                    analysis_version=(analysis_version if row["analysis_state"] == ANALYSIS_CURRENT
                                      else None),
                    sample_rate_hz=row["sample_rate_hz"], channels=row["channels"],
                    frame_count=row["frame_count"], duration_ms=row["duration_ms"])
                for row in rows[:limit]),
            has_more=has_more)

    def sample_detail(self, sample_id: str):
        """One `SampleDetail` for a content identity, or None when it is unknown.

        `sample_id` is `sha256:<content_sha256>`. The state is decided exactly
        as `page_samples` decides it, and `sample` is the validated contract
        `Sample` rebuilt by `get_sample` when that state is `current`, so the
        detail route serves the same measurements every other read returns.
        `analyzed_at` is the #23 item's `finished_at` for a complete item at
        the current version; a sample whose analysis was stored without a queue
        row reports it as null rather than inventing one.
        """

        fingerprint = content_fingerprint(sample_id)
        row = self._one("SELECT * FROM samples WHERE content_sha256 = ?", (fingerprint,))
        if row is None:
            return None
        versions = self.list_analysis_versions(row["sample_id"])
        version = digest(analysis_descriptor())
        item = self._one(
            "SELECT state, attempts, error_code, finished_at FROM job_items "
            "WHERE sample_id = ? AND analysis_version = ?",
            (content_identity(fingerprint), version))
        state = _analysis_state(versions, version, None if item is None else item["state"])
        complete = item is not None and item["state"] == "complete"
        return SampleDetail(
            sample_id=content_identity(fingerprint), role=row["role"], file_name=row["filename"],
            file_status=row["file_status"], analysis_state=state,
            analysis_version=(version if state == ANALYSIS_CURRENT else None),
            analyzed_at=(item["finished_at"] if complete else None),
            attempts=(None if item is None else item["attempts"]),
            error_code=(None if item is None else item["error_code"]),
            stored_versions=versions, sample_rate_hz=row["sample_rate_hz"],
            channels=row["channels"], frame_count=row["frame_count"],
            duration_ms=row["duration_ms"],
            sample=(None if state != ANALYSIS_CURRENT
                    else self.get_sample(row["sample_id"], version).sample))

    def sample_counts(self) -> dict:
        """The whole-library counts #27's `/health` reports, in two statements.

        `samples` is every stored row, `by_role` holds one count per
        `backend.analysis.batch.ROLES` member (zero when absent) and `roots` is
        the number of distinct directories that hold at least one stored sample,
        which is what a client shows as "folders in the library".
        `path_key` is the normalised stored path, so counting its parents needs
        no second path scheme.
        """

        counts = {role: 0 for role in ROLES}
        total = 0
        for row in self._all("SELECT role, COUNT(*) AS total FROM samples GROUP BY role"):
            counts[row["role"]] = counts.get(row["role"], 0) + row["total"]
            total += row["total"]
        roots = {os.path.dirname(row["path_key"])
                 for row in self._all("SELECT path_key FROM samples")}
        return {"samples": total, "by_role": counts, "roots": len(roots)}

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
                                   channels, frame_count, duration_ms, file_status="present",
                                   invalidate_analysis_version=None):
        """Replace one row's content identity and audio metadata in place.

        The stored `sample_id`, path, role and `imported_at` stay as they were:
        an edited file keeps its record identity. When
        `invalidate_analysis_version` names an analysis version, that sample's
        `sample_features` and `sample_keys` rows at that version are deleted in
        the *same* transaction as the update.

        Why the deletion belongs here: a feature row is addressed by
        (`sample_id`, `analysis_version`) and nothing records which bytes it was
        measured from, so after a content change the rows stored at the version
        that made the record look analysed would still be served by `get_sample`
        at that version and would keep the new bytes out of `pending_analysis`.
        Rows stored under other versions are left untouched and stay readable
        when that version is named; whether the schema can keep the previous
        content's analysis addressable after an edit is #166.

        Raises the codes `insert_path_record` documents, plus `UnknownSample`
        for an unknown row and `InvalidSample` for a malformed version argument.
        """

        if invalidate_analysis_version is not None \
                and not isinstance(invalidate_analysis_version, str):
            raise InvalidSample(
                "invalidate_analysis_version must be None or an analysis version string.")
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
            if invalidate_analysis_version is not None:
                self._delete_analysis(sample_id, invalidate_analysis_version)

    def _delete_analysis(self, sample_id: str, analysis_version: str) -> None:
        """Delete one sample's stored analysis rows for one version (issue #22)."""

        for table in ("sample_features", "sample_keys"):
            self.connection.execute(
                "DELETE FROM " + table + " WHERE sample_id = ? AND analysis_version = ?",
                (sample_id, analysis_version))

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

    # -- projects (issue #24) ----------------------------------------------

    def create_project(self, name, *, palette_name="Main") -> ProjectRecord:
        """Store one project and its single palette in one transaction.

        The MVP has exactly one palette per project (`palettes.project_id` is
        UNIQUE) and it starts at `revision` 0 with no item and a fully unset
        context. `name` and `palette_name` must be nonblank strings; they are
        stored exactly as given. Both identifiers are minted here and are opaque.

        Raises `write_failed`, `database_locked`.
        """

        _require_label(name, "name")
        _require_label(palette_name, "palette_name")
        project_id = _new_id("project")
        palette_id = _new_id("palette")
        now = utc_now()
        with self._writing():
            self.connection.execute(
                "INSERT INTO projects (project_id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)", (project_id, name, now, now))
            self.connection.execute(
                "INSERT INTO palettes (palette_id, project_id, name, revision, created_at, "
                "updated_at) VALUES (?, ?, ?, 0, ?, ?)",
                (palette_id, project_id, palette_name, now, now))
        return ProjectRecord(project_id=project_id, name=name, created_at=now, updated_at=now,
                             palette_id=palette_id)

    def get_project(self, project_id):
        """One `ProjectRecord`, or None when the project is unknown."""

        row = self._one("SELECT * FROM projects WHERE project_id = ?", (project_id,))
        return None if row is None else self._project_record(row)

    def list_projects(self) -> tuple:
        """Every stored project, ordered by `created_at`, then `project_id`."""

        rows = self._all("SELECT * FROM projects ORDER BY created_at ASC, project_id ASC")
        return tuple(self._project_record(row) for row in rows)

    def delete_project(self, project_id) -> bool:
        """Remove a project with its palette and items; False when it was unknown.

        The two `ON DELETE CASCADE` rules do the work in one transaction.
        `samples`, `sample_features`, `sample_keys` and `sample_tags` are never
        read, updated or deleted here.

        Raises `database_locked`, `write_failed`.
        """

        with self._writing():
            cursor = self.connection.execute("DELETE FROM projects WHERE project_id = ?",
                                             (project_id,))
            return cursor.rowcount == 1

    def _project_record(self, row) -> ProjectRecord:
        palette = self._one("SELECT palette_id FROM palettes WHERE project_id = ?",
                            (row["project_id"],))
        return ProjectRecord(project_id=row["project_id"], name=row["name"],
                             created_at=row["created_at"], updated_at=row["updated_at"],
                             palette_id=None if palette is None else palette["palette_id"])

    # -- palettes (issue #24) -----------------------------------------------

    def load_palette(self, palette_id):
        """One `PaletteRecord`, or None when the palette is unknown.

        A pure read: it resolves every item against `samples` and the analysis
        queue without writing, and it never raises for a sample that is missing,
        unknown, pruned or stored with another role.
        """

        row = self._one("SELECT * FROM palettes WHERE palette_id = ?", (palette_id,))
        return None if row is None else self._palette_record(row)

    def list_palettes(self, project_id) -> tuple:
        """Every palette of one project, ordered by `created_at`, then `palette_id`.

        Raises `unknown_project` for a project with no row.
        """

        if self._one("SELECT project_id FROM projects WHERE project_id = ?",
                     (project_id,)) is None:
            raise UnknownProject(f"No stored project with project_id {project_id}.")
        rows = self._all("SELECT * FROM palettes WHERE project_id = ? "
                         "ORDER BY created_at ASC, palette_id ASC", (project_id,))
        return tuple(self._palette_record(row) for row in rows)

    def set_palette_item(self, palette_id, slot, sample_id, *,
                         expected_revision) -> PaletteMutation:
        """Select `sample_id` for `slot`, replacing any active item atomically.

        In one transaction the caller's `expected_revision` is compared with the
        stored one, the palette's revision is incremented by exactly one, the
        previously active item (if any) is marked removed at that revision and a
        new item row is inserted with a new id and the sample's stored role.
        Re-selecting the sample that is already active in the slot is a no-op
        that writes nothing and leaves the revision alone.

        Raises `unknown_palette`, `unknown_slot` for a slot outside
        `MVP_SLOTS`, `invalid_sample` for a blank sample id, `unknown_sample`
        for an id with no `samples` row, `role_mismatch` when the stored role is
        not one the slot accepts, `revision_conflict` when `expected_revision`
        is stale, `database_locked` or `write_failed`.
        """

        if slot not in MVP_SLOTS:
            raise UnknownSlot(f"slot must be one of {', '.join(MVP_SLOTS)}; got {slot!r}.")
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise InvalidSample("sample_id must be a non-empty string.")
        with self._writing():
            palette = self._require_palette(palette_id)
            self._require_expected(palette, expected_revision)
            sample = self._one(
                "SELECT sample_id, role, file_status FROM samples WHERE sample_id = ?",
                (sample_id,))
            if sample is None:
                raise UnknownSample(f"No stored sample with sample_id {sample_id}.")
            active = self._active_item(palette_id, slot)
            if active is not None and active["sample_id"] == sample_id:
                return PaletteMutation(palette_id, palette["revision"], False,
                                       self._item_record(active), None)
            accepted = SLOT_ROLES[slot]
            role = sample["role"]
            if role not in accepted:
                raise RoleMismatch(
                    f"slot {slot} accepts roles {', '.join(accepted)}; sample {sample_id} is "
                    f"stored with role {role}.")
            revision = self._bump(palette_id, palette["revision"])
            now = utc_now()
            if active is not None:
                self.connection.execute(
                    "UPDATE palette_items SET removed_revision = ?, removed_at = ? "
                    "WHERE item_id = ?", (revision, now, active["item_id"]))
            item_id = _new_id("item")
            self.connection.execute(
                "INSERT INTO palette_items (item_id, palette_id, slot, sample_id, role, "
                "added_revision, added_at, removed_revision, removed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (item_id, palette_id, slot, sample_id, role, revision, now))
            previous = None if active is None else self._item_record(
                self._one("SELECT * FROM palette_items WHERE item_id = ?",
                          (active["item_id"],)))
        return PaletteMutation(palette_id, revision, True, PaletteItemRecord(
            item_id=item_id, slot=slot, sample_id=sample_id, role=role,
            added_revision=revision, added_at=now, removed_revision=None, removed_at=None,
            sample_state=sample["file_status"],
            sample_error_code=self._sample_error_code(sample_id),
            slot_role_mismatch=False), previous)

    def remove_palette_item(self, palette_id, slot, *, expected_revision) -> PaletteMutation:
        """Mark the active item for `slot` removed and increment the revision.

        The removed item comes back in `previous_item` and keeps its id, its
        `added_at` and its stored role. An empty, never-set or already-removed
        slot is a no-op that writes nothing and changes no revision; a stale
        `expected_revision` is a `revision_conflict` even then, so a caller that
        is out of date re-reads and retries.

        Raises `unknown_palette`, `unknown_slot`, `revision_conflict`,
        `database_locked`, `write_failed`.
        """

        if slot not in MVP_SLOTS:
            raise UnknownSlot(f"slot must be one of {', '.join(MVP_SLOTS)}; got {slot!r}.")
        with self._writing():
            palette = self._require_palette(palette_id)
            self._require_expected(palette, expected_revision)
            active = self._active_item(palette_id, slot)
            if active is None:
                return PaletteMutation(palette_id, palette["revision"], False, None, None)
            revision = self._bump(palette_id, palette["revision"])
            self.connection.execute(
                "UPDATE palette_items SET removed_revision = ?, removed_at = ? "
                "WHERE item_id = ?", (revision, utc_now(), active["item_id"]))
            removed = self._item_record(
                self._one("SELECT * FROM palette_items WHERE item_id = ?",
                          (active["item_id"],)))
        return PaletteMutation(palette_id, revision, True, None, removed)

    def set_palette_context(self, palette_id, song, *, expected_revision,
                            unset=()) -> PaletteMutation:
        """Store a whole song context in one transaction.

        Every field is written as the validated `SongContext` states it: the value
        and its confidence, or NULL plus the stored unavailable reason. Repeating
        the stored context is a no-op that changes no revision. The result carries
        no item, because a context change touches none.

        `unset` names fields to store as `unset` (issue #32): every column of the
        field is written NULL, which is the only way back to that state, because
        `backend.contracts` has no way to state a null value without a reason.
        A caller that passes nothing keeps the previous behaviour exactly.

        Raises `invalid_context` with the contract error as `__cause__` when
        `song` is not a valid `SongContext`, or for a name outside
        `CONTEXT_STATES`' fields; `unknown_palette`, `revision_conflict`,
        `database_locked` or `write_failed`.
        """

        validated = _validated_context(song)
        columns = _context_columns(validated)
        for name in unset:
            if name not in _CONTEXT_FIELD_COLUMNS:
                raise InvalidContext(f"{name!r} is not a song-context field.")
            for column in _CONTEXT_FIELD_COLUMNS[name]:
                columns[column] = None
        with self._writing():
            palette = self._require_palette(palette_id)
            self._require_expected(palette, expected_revision)
            if all(palette[name] == value for name, value in columns.items()):
                return PaletteMutation(palette_id, palette["revision"], False, None, None)
            revision = self._bump(palette_id, palette["revision"])
            self.connection.execute(
                "UPDATE palettes SET " + ", ".join(f"{name} = ?" for name in columns)
                + ", updated_at = ? WHERE palette_id = ?",
                (*columns.values(), utc_now(), palette_id))
        return PaletteMutation(palette_id, revision, True, None, None)

    def _require_palette(self, palette_id):
        row = self._one("SELECT * FROM palettes WHERE palette_id = ?", (palette_id,))
        if row is None:
            raise UnknownPalette(f"No stored palette with palette_id {palette_id}.")
        return row

    def _require_expected(self, palette, expected_revision) -> None:
        if palette["revision"] != expected_revision:
            raise RevisionConflict(expected_revision, palette["revision"])

    def _bump(self, palette_id, revision) -> int:
        """Compare-and-set one palette revision and return the new value."""

        updated = self.connection.execute(
            "UPDATE palettes SET revision = revision + 1, updated_at = ? "
            "WHERE palette_id = ? AND revision = ?", (utc_now(), palette_id, revision))
        if updated.rowcount != 1:
            current = self._one("SELECT revision FROM palettes WHERE palette_id = ?",
                                (palette_id,))
            raise RevisionConflict(revision, None if current is None else current["revision"])
        return revision + 1

    def _active_item(self, palette_id, slot):
        return self._one("SELECT * FROM palette_items WHERE palette_id = ? AND slot = ? "
                         "AND removed_at IS NULL", (palette_id, slot))

    def _palette_record(self, row) -> PaletteRecord:
        song, state = _context_of(row)
        active = self._all("SELECT * FROM palette_items WHERE palette_id = ? "
                           "AND removed_at IS NULL ORDER BY slot ASC", (row["palette_id"],))
        removed = self._all("SELECT * FROM palette_items WHERE palette_id = ? "
                            "AND removed_at IS NOT NULL "
                            "ORDER BY removed_revision ASC, item_id ASC", (row["palette_id"],))
        return PaletteRecord(palette_id=row["palette_id"], project_id=row["project_id"],
                             name=row["name"], revision=row["revision"], song=song,
                             context_state=state,
                             active_items=tuple(self._item_record(item) for item in active),
                             removed_items=tuple(self._item_record(item) for item in removed))

    def _item_record(self, row) -> PaletteItemRecord:
        """One item row enriched with the referenced sample's current state.

        The item's stored role is what was selected; the record reports the
        sample row's current role and flags the mismatch when #72 has changed it
        to one the slot no longer accepts. Nothing is written and nothing is
        re-roled.
        """

        sample = self._one(
            "SELECT role, file_status, content_sha256 FROM samples WHERE sample_id = ?",
            (row["sample_id"],))
        if sample is None:
            role, state, code = row["role"], "removed", None
        else:
            role, state = sample["role"], sample["file_status"]
            code = self._sample_error_code(sample["content_sha256"])
        return PaletteItemRecord(
            item_id=row["item_id"], slot=row["slot"], sample_id=row["sample_id"], role=role,
            added_revision=row["added_revision"], added_at=row["added_at"],
            removed_revision=row["removed_revision"], removed_at=row["removed_at"],
            sample_state=state, sample_error_code=code,
            slot_role_mismatch=role not in SLOT_ROLES[row["slot"]])

    def _sample_error_code(self, content_sha256):
        """The analysis-queue error code stored for one sample row, or None.

        #22's scan keeps its errors in the scan summary and #23's queue persists
        the read, decode or extract failure against the row's *content identity*
        (`sha256:` plus `samples.content_sha256`, which is how `queue.enqueue`
        keys an item), so the queue is the only stored per-row code a palette
        read can report; the most recent item wins. A sample with no stored
        failure reports None.
        """

        row = self._one("SELECT error_code FROM job_items WHERE sample_id = ? "
                        "AND error_code IS NOT NULL ORDER BY item_id DESC LIMIT 1",
                        ("sha256:" + content_sha256,))
        return None if row is None else row["error_code"]

    # -- decision cache (issue #26) ----------------------------------------

    def insert_decision_cache_entry(self, values) -> bool:
        """Insert one decision-cache row, never replacing a committed key.

        The one statement is `INSERT ... ON CONFLICT(cache_key) DO NOTHING`, so
        the first committed entry wins and a later store under an unchanged key
        writes nothing and returns False. The caller owns the transaction: the
        insert and the pruning that follows it commit or roll back together.
        `values` is a mapping holding every name in `DECISION_CACHE_COLUMNS`;
        a missing one is a KeyError, not a misaligned statement.
        """

        names = ", ".join(DECISION_CACHE_COLUMNS)
        marks = ", ".join("?" for _name in DECISION_CACHE_COLUMNS)
        cursor = self.connection.execute(
            f"INSERT INTO decision_cache ({names}) VALUES ({marks}) "
            "ON CONFLICT(cache_key) DO NOTHING",
            tuple(values[name] for name in DECISION_CACHE_COLUMNS))
        return cursor.rowcount == 1

    def decision_cache_row(self, cache_key):
        """One whole decision-cache row, or None when that key has no row."""

        return self._one("SELECT * FROM decision_cache WHERE cache_key = ?", (cache_key,))

    def decision_cache_created_at(self, cache_key):
        """The stored `created_at` of one key, or None when it has no row."""

        row = self._one("SELECT created_at FROM decision_cache WHERE cache_key = ?",
                        (cache_key,))
        return None if row is None else row["created_at"]

    def decision_cache_totals(self):
        """The cache's entry count and stored payload bytes as one row."""

        return self._one(
            "SELECT COUNT(*) AS entries, "
            "COALESCE(SUM(length(payload_json)), 0) AS bytes FROM decision_cache")

    def decision_cache_oldest_keys(self, exclude_key=None):
        """Every cache key oldest first, with its payload length, minus one key.

        Ordered by `(created_at ASC, cache_key ASC)`, the documented pruning
        order. `exclude_key` is the row a store just wrote and must never
        evict; None excludes nothing.
        """

        statement = ("SELECT cache_key, length(payload_json) AS payload_bytes "
                     "FROM decision_cache")
        parameters = ()
        if exclude_key is not None:
            statement += " WHERE cache_key <> ?"
            parameters = (exclude_key,)
        statement += " ORDER BY created_at ASC, cache_key ASC"
        return self._all(statement, parameters)

    def decision_cache_stats(self):
        """The cache's counts, payload bytes and `created_at` extremes as one row."""

        return self._one(
            "SELECT COUNT(*) AS entries, "
            "COALESCE(SUM(decision_kind = 'judgment'), 0) AS judgments, "
            "COALESCE(SUM(decision_kind = 'candidate_decision'), 0) AS candidate_decisions, "
            "COALESCE(SUM(length(payload_json)), 0) AS bytes, "
            "MIN(created_at) AS oldest_created_at, MAX(created_at) AS newest_created_at "
            "FROM decision_cache")

    def delete_decision_cache_key(self, cache_key) -> bool:
        """Delete exactly one cache row; True when it existed."""

        cursor = self.connection.execute("DELETE FROM decision_cache WHERE cache_key = ?",
                                         (cache_key,))
        return cursor.rowcount == 1

    def delete_decision_cache_candidate(self, candidate_id) -> int:
        """Delete every cache row for one candidate, both kinds and every palette."""

        cursor = self.connection.execute("DELETE FROM decision_cache WHERE candidate_id = ?",
                                         (candidate_id,))
        return cursor.rowcount

    def decision_model_version_pin(self, *, interface_name, source, adapter_version,
                                  prompt_version):
        """The stored model-version pin for one interface identity, or None."""

        return self._one(
            "SELECT * FROM decision_model_versions WHERE interface_name = ? AND source = ? "
            "AND adapter_version = ? AND prompt_version = ?",
            (interface_name, source, adapter_version, prompt_version))

    def upsert_decision_model_version_pin(self, values) -> None:
        """Record one observed model version, counting a repeat observation.

        `first_observed_at` is set once and kept; `observed_at` and
        `observation_count` move on every observation. The caller owns the
        transaction and owns reading the pin back.
        """

        self.connection.execute(
            "INSERT INTO decision_model_versions (interface_name, source, adapter_version, "
            "prompt_version, model_version, first_observed_at, observed_at, observation_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(interface_name, source, adapter_version, prompt_version) DO UPDATE SET "
            "model_version = excluded.model_version, observed_at = excluded.observed_at, "
            "observation_count = decision_model_versions.observation_count + 1",
            tuple(values[name] for name in DECISION_MODEL_VERSION_COLUMNS))

    # -- producer outcomes (issue #29) -------------------------------------

    def record_outcome(self, submission):
        """Append one validated producer action to local history."""

        from backend.library.outcomes import record_outcome
        return record_outcome(self.connection, submission)

    def list_outcomes(self, *, project_id=None, palette_id=None, run_id=None,
                      candidate_id=None, event_types=None, after_event_id=None,
                      limit=50):
        """Read outcome history by insertion order, without resolving a sample."""

        from backend.library.outcomes import list_outcomes
        return list_outcomes(
            self.connection, project_id=project_id, palette_id=palette_id,
            run_id=run_id, candidate_id=candidate_id, event_types=event_types,
            after_event_id=after_event_id, limit=limit)

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
