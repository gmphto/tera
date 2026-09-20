"""Local, append-only producer outcome history (issue #29)."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import get_args, get_type_hints

from backend.contracts import RecommendationBatch
from backend.library.errors import (
    CrossProjectReference,
    IdempotencyConflict,
    InvalidOutcome,
    OutcomeConflict,
    RemovalNotReflected,
    SelectionNotInPalette,
    UnknownPalette,
    UnknownPaletteRevision,
    UnknownProject,
    UnknownSelection,
)
from backend.library.repository import coded_error, transaction
from backend.library import schema
from backend.palette.model import MVP_SLOTS


OUTCOME_EVENT_TYPES = ("auditioned", "selected", "rejected", "removed")
OUTCOME_SLOT = MVP_SLOTS[MVP_SLOTS.index("bass")]
MAX_ID_LENGTH = 128
_MODES = get_args(get_type_hints(RecommendationBatch)["mode"])
_COLUMNS = (
    "event_id", "client_event_id", "event_type", "project_id", "palette_id",
    "palette_revision", "run_id", "candidate_id", "ranking_version", "mode",
    "candidate_analysis_version", "removes_event_id", "recorded_at",
)
_IDENTITY = (
    "event_type", "project_id", "palette_id", "palette_revision", "run_id",
    "candidate_id", "ranking_version", "mode", "candidate_analysis_version",
    "removes_event_id",
)


@dataclass(frozen=True)
class OutcomeEvent:
    event_id: int
    client_event_id: str
    event_type: str
    project_id: str
    palette_id: str
    palette_revision: int
    run_id: str
    candidate_id: str
    ranking_version: str
    mode: str
    candidate_analysis_version: str
    removes_event_id: int | None
    recorded_at: str


@dataclass(frozen=True)
class OutcomeSubmission:
    client_event_id: str
    event_type: str
    project_id: str | None = None
    palette_id: str | None = None
    palette_revision: int | None = None
    run_id: str | None = None
    candidate_id: str | None = None
    ranking_version: str | None = None
    mode: str | None = None
    candidate_analysis_version: str | None = None
    removes_event_id: int | None = None


@dataclass(frozen=True)
class OutcomeWrite:
    created: bool
    event: OutcomeEvent


def _id(value) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH
            and not any(character in "/\\" or character.isspace() or ord(character) < 32
                        or ord(character) == 127 for character in value))


def validate_submission(submission: OutcomeSubmission) -> OutcomeSubmission:
    """Reject a malformed event before opening a write transaction."""

    if not isinstance(submission, OutcomeSubmission):
        raise InvalidOutcome("An outcome submission is required.")
    if not _id(submission.client_event_id):
        raise InvalidOutcome("The client event id is malformed.")
    if submission.event_type not in OUTCOME_EVENT_TYPES:
        raise InvalidOutcome("The event type is not supported.")
    if submission.event_type == "removed":
        if (type(submission.removes_event_id) is not int
                or submission.removes_event_id < 1
                or any(getattr(submission, name) is not None for name in (
                    "project_id", "palette_id", "palette_revision", "run_id",
                    "candidate_id", "ranking_version", "mode",
                    "candidate_analysis_version"))):
            raise InvalidOutcome("A removal carries only the selection it closes.")
    elif (not all(_id(getattr(submission, name)) for name in (
            "project_id", "palette_id", "run_id", "candidate_id",
            "ranking_version", "candidate_analysis_version"))
          or type(submission.palette_revision) is not int
          or submission.palette_revision < 0
          or submission.mode not in _MODES
          or submission.removes_event_id is not None):
        raise InvalidOutcome("The outcome identity is malformed.")
    return submission


def _event(row) -> OutcomeEvent:
    return OutcomeEvent(*(row[name] for name in _COLUMNS))


def _same(submission: OutcomeSubmission, event: OutcomeEvent) -> bool:
    names = ("event_type", "removes_event_id") if submission.event_type == "removed" else _IDENTITY
    return all(getattr(submission, name) == getattr(event, name) for name in names)


def _existing(connection, client_event_id):
    row = connection.execute(
        "SELECT * FROM recommendation_outcomes WHERE client_event_id = ?",
        (client_event_id,)).fetchone()
    return None if row is None else _event(row)


def _active(connection, palette_id, candidate_id) -> bool:
    return connection.execute(
        "SELECT 1 FROM palette_items WHERE palette_id = ? AND slot = ? "
        "AND sample_id = ? AND removed_at IS NULL LIMIT 1",
        (palette_id, OUTCOME_SLOT, candidate_id)).fetchone() is not None


def _live(connection, palette_id, candidate_id):
    row = connection.execute(
        "SELECT selected.* FROM recommendation_outcomes AS selected "
        "WHERE selected.palette_id = ? AND selected.candidate_id = ? "
        "AND selected.event_type = 'selected' AND NOT EXISTS ("
        "SELECT 1 FROM recommendation_outcomes AS removed "
        "WHERE removed.removes_event_id = selected.event_id) "
        "ORDER BY selected.event_id LIMIT 1",
        (palette_id, candidate_id)).fetchone()
    return None if row is None else _event(row)


def _identity(connection, submission: OutcomeSubmission) -> dict:
    if submission.event_type == "removed":
        row = connection.execute(
            "SELECT * FROM recommendation_outcomes WHERE event_id = ?",
            (submission.removes_event_id,)).fetchone()
        if row is None or row["event_type"] != "selected":
            raise UnknownSelection("The removal does not name a selection.")
        source = _event(row)
        if connection.execute(
            "SELECT 1 FROM recommendation_outcomes WHERE removes_event_id = ?",
            (source.event_id,)).fetchone() is not None:
            raise OutcomeConflict("The selection is already closed.")
        if _active(connection, source.palette_id, source.candidate_id):
            raise RemovalNotReflected("The candidate is still active in the palette.")
        return {name: getattr(source, name) for name in _IDENTITY
                if name != "removes_event_id"} | {
                    "event_type": submission.event_type,
                    "removes_event_id": source.event_id,
                }

    project = connection.execute(
        "SELECT 1 FROM projects WHERE project_id = ?",
        (submission.project_id,)).fetchone()
    if project is None:
        raise UnknownProject("The project is unknown.")
    palette = connection.execute(
        "SELECT project_id, revision FROM palettes WHERE palette_id = ?",
        (submission.palette_id,)).fetchone()
    if palette is None:
        raise UnknownPalette("The palette is unknown.")
    if palette["project_id"] != submission.project_id:
        raise CrossProjectReference("The palette belongs to another project.")
    if submission.palette_revision > palette["revision"]:
        raise UnknownPaletteRevision("The palette revision has not existed.")
    if submission.event_type == "selected":
        if not _active(connection, submission.palette_id, submission.candidate_id):
            raise SelectionNotInPalette("The candidate is not active in the bass slot.")
    elif submission.event_type == "rejected":
        if _live(connection, submission.palette_id, submission.candidate_id) is not None:
            raise OutcomeConflict("A live selection cannot be rejected.")
    return {name: getattr(submission, name) for name in _IDENTITY}


def record_outcome(connection: sqlite3.Connection,
                   submission: OutcomeSubmission) -> OutcomeWrite:
    """Append one event, or return an idempotent or already-live selection."""

    validate_submission(submission)
    try:
        with transaction(connection):
            existing = _existing(connection, submission.client_event_id)
            if existing is not None:
                if not _same(submission, existing):
                    raise IdempotencyConflict(existing.event_id)
                return OutcomeWrite(False, existing)
            identity = _identity(connection, submission)
            if submission.event_type == "selected":
                live = _live(connection, submission.palette_id, submission.candidate_id)
                if live is not None:
                    return OutcomeWrite(False, live)
            values = {"client_event_id": submission.client_event_id,
                      **identity, "recorded_at": schema.utc_now()}
            names = tuple(name for name in _COLUMNS if name != "event_id")
            cursor = connection.execute(
                "INSERT INTO recommendation_outcomes ("
                + ", ".join(names) + ") VALUES ("
                + ", ".join("?" for _ in names) + ")",
                tuple(values[name] for name in names))
            row = connection.execute(
                "SELECT * FROM recommendation_outcomes WHERE event_id = ?",
                (cursor.lastrowid,)).fetchone()
            return OutcomeWrite(True, _event(row))
    except sqlite3.IntegrityError as error:
        # A concurrent writer may have inserted this key after our first read.
        try:
            existing = _existing(connection, submission.client_event_id)
            if existing is not None:
                if not _same(submission, existing):
                    raise IdempotencyConflict(existing.event_id) from error
                return OutcomeWrite(False, existing)
            if submission.event_type == "removed":
                closed = connection.execute(
                    "SELECT 1 FROM recommendation_outcomes WHERE removes_event_id = ?",
                    (submission.removes_event_id,)).fetchone()
                if closed is not None:
                    raise OutcomeConflict("The selection was already closed.") from error
        except sqlite3.Error as read_error:
            raise coded_error(read_error) from read_error
        raise coded_error(error) from error
    except sqlite3.Error as error:
        raise coded_error(error) from error


def list_outcomes(connection: sqlite3.Connection, *, project_id=None, palette_id=None,
                  run_id=None, candidate_id=None, event_types=None,
                  after_event_id=None, limit=50) -> tuple[OutcomeEvent, ...]:
    """Read one ordered page from the outcome table alone."""

    if type(limit) is not int or limit < 1:
        raise InvalidOutcome("The outcome page limit must be positive.")
    if after_event_id is not None and (type(after_event_id) is not int or after_event_id < 0):
        raise InvalidOutcome("The outcome cursor is malformed.")
    for value in (project_id, palette_id, run_id, candidate_id):
        if value is not None and not _id(value):
            raise InvalidOutcome("An outcome filter is malformed.")
    if event_types is not None and any(value not in OUTCOME_EVENT_TYPES
                                       for value in event_types):
        raise InvalidOutcome("An outcome event type is unsupported.")
    predicates, values = ["event_id > ?"], [after_event_id or 0]
    for name, value in (("project_id", project_id), ("palette_id", palette_id),
                        ("run_id", run_id), ("candidate_id", candidate_id)):
        if value is not None:
            predicates.append(name + " = ?")
            values.append(value)
    if event_types:
        predicates.append("event_type IN (" + ", ".join("?" for _ in event_types) + ")")
        values.extend(event_types)
    try:
        rows = connection.execute(
            "SELECT * FROM recommendation_outcomes WHERE "
            + " AND ".join(predicates) + " ORDER BY event_id LIMIT ?",
            (*values, limit)).fetchall()
        return tuple(_event(row) for row in rows)
    except sqlite3.Error as error:
        raise coded_error(error) from error
