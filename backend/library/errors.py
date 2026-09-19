"""Coded failures for the local SQLite sample library (issue #21).

Every failure the storage layer reports is a `LibraryError` subclass with a
stable `code` string, so callers can branch on the code instead of parsing a
message. The code set is closed and mirrors the error codes listed in the
storage specification; behaviour that needs a new code needs a new issue.

Nothing here depends on a third-party package.
"""

from __future__ import annotations


class LibraryError(Exception):
    """Base failure for library storage; every subclass carries a `code`."""

    code = "library_error"


class InvalidDatabasePath(LibraryError):
    """The database path is a directory, non-local, linked, or cannot be written."""

    code = "invalid_database_path"


class NotADatabase(LibraryError):
    """The file exists, is not empty, and its bytes are not a SQLite database."""

    code = "not_a_database"


class DatabaseCorrupt(LibraryError):
    """The file carries a SQLite header but fails an integrity check."""

    code = "database_corrupt"


class SchemaVersionNewer(LibraryError):
    """The stored schema version is newer than any migration available here."""

    code = "schema_version_newer"


class MigrationFailed(LibraryError):
    """A migration raised; its transaction was rolled back with nothing applied."""

    code = "migration_failed"


class DatabaseLocked(LibraryError):
    """Another connection held the write lock past this connection's busy timeout."""

    code = "database_locked"


class WriteFailed(LibraryError):
    """The database refused a write; the sqlite3 error is the `__cause__`."""

    code = "write_failed"


class InvalidSample(LibraryError):
    """A payload failed contract validation, or a descriptor is not serialisable."""

    code = "invalid_sample"


class InvalidContentIdentity(LibraryError):
    """The supplied content hash is malformed or disagrees with the stored identity."""

    code = "invalid_content_identity"


class DuplicateContent(LibraryError):
    """Another sample_id already stores this content hash; the message names it."""

    code = "duplicate_content"


class PathConflict(LibraryError):
    """Another sample_id already stores this normalised library path."""

    code = "path_conflict"


class UnknownAnalysisVersion(LibraryError):
    """No analysis_versions row, and no stored features, for that version."""

    code = "unknown_analysis_version"


class AnalysisVersionMismatch(LibraryError):
    """A stored descriptor no longer re-digests to its own analysis_version."""

    code = "analysis_version_mismatch"


class IncompleteFeatures(LibraryError):
    """A stored version does not hold the 19 measurements plus its key row."""

    code = "incomplete_features"


class UnknownSample(LibraryError):
    """No samples row for that sample_id."""

    code = "unknown_sample"


class InvalidTag(LibraryError):
    """A tag is not a string, or is empty or longer than 64 characters once trimmed."""

    code = "invalid_tag"


class InvalidFileStatus(LibraryError):
    """A file status is not one of present, missing or unknown."""

    code = "invalid_file_status"
