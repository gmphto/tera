"""The two local producer-outcome operations (issue #29)."""

from __future__ import annotations

from dataclasses import asdict
import re

from backend.api import schemas
from backend.api.errors import ApiError
from backend.library.errors import LibraryError
from backend.library.outcomes import (
    OUTCOME_EVENT_TYPES,
    OutcomeSubmission,
    validate_submission,
)
from backend.library.repository import LibraryRepository


_COMMON = ("client_event_id", "event_type")
_RUN_FIELDS = (
    "project_id", "palette_id", "candidate_id", "run_id",
    "palette_revision", "ranking_version", "mode",
    "candidate_analysis_version",
)
_REMOVE_FIELDS = ("removes_event_id",)
_QUERY_FIELDS = (
    "project_id", "palette_id", "run_id", "candidate_id",
    "event_type", "limit", "cursor",
)
_SAFE_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _field(name):
    return name if _SAFE_FIELD.fullmatch(name) else "outcome"


def parse_submission(body) -> OutcomeSubmission:
    """Validate the closed POST shape before any database call."""

    if type(body) is not dict:
        raise ApiError("invalid_body")
    for name in _COMMON:
        if name not in body:
            raise ApiError("missing_field", details={"field": name})
        if type(body[name]) is not str:
            raise ApiError("invalid_field_type", details={"field": name})
    if body["event_type"] not in OUTCOME_EVENT_TYPES:
        raise ApiError("invalid_outcome")
    names = _COMMON + (_REMOVE_FIELDS if body["event_type"] == "removed"
                       else _RUN_FIELDS)
    for name in names:
        if name not in body:
            raise ApiError("missing_field", details={"field": name})
    for name in body:
        if name not in names:
            if name in _COMMON + _RUN_FIELDS + _REMOVE_FIELDS:
                raise ApiError("invalid_outcome")
            raise ApiError("unknown_field", details={"field": _field(name)})
    for name in names:
        expected = int if name in ("palette_revision", "removes_event_id") else str
        if type(body[name]) is not expected:
            raise ApiError("invalid_field_type", details={"field": name})
    submission = OutcomeSubmission(**body)
    try:
        return validate_submission(submission)
    except LibraryError as error:
        raise ApiError(error.code) from None


def _failure(error: LibraryError, *, submission=None) -> ApiError:
    details = {}
    if error.code == "idempotency_conflict":
        details["event_id"] = error.event_id
    elif error.code == "unknown_selection" and submission is not None:
        details["removes_event_id"] = submission.removes_event_id
    elif error.code in ("unknown_project", "unknown_palette",
                        "unknown_palette_revision", "cross_project_reference",
                        "selection_not_in_palette", "removal_not_reflected",
                        "outcome_conflict") and submission is not None:
        if submission.project_id is not None:
            details["project_id"] = submission.project_id
        if submission.palette_id is not None:
            details["palette_id"] = submission.palette_id
        if submission.candidate_id is not None:
            details["candidate_id"] = submission.candidate_id
        if submission.removes_event_id is not None:
            details["removes_event_id"] = submission.removes_event_id
    return ApiError(error.code, details=details)


def create_outcome(context) -> schemas.Response:
    """POST /outcomes: append or return the one stored idempotent event."""

    submission = parse_submission(context.body)
    try:
        written = context.app.database.run(
            lambda connection: LibraryRepository(connection).record_outcome(submission))
    except LibraryError as error:
        raise _failure(error, submission=submission) from None
    return schemas.Response(201 if written.created else 200, {
        "api_schema": context.app.api_schema,
        "created": written.created,
        "outcome": asdict(written.event),
    })


def _query(pairs):
    filters = {name: None for name in _QUERY_FIELDS[:4]}
    event_types = []
    limit, after = schemas.DEFAULT_PAGE_SIZE, None
    for name, value in pairs:
        if name not in _QUERY_FIELDS:
            raise ApiError("unknown_query_parameter", details={
                "parameter": _field(name)})
        if name == "event_type":
            if value not in OUTCOME_EVENT_TYPES:
                raise ApiError("invalid_outcome", details={"parameter": name})
            event_types.append(value)
        elif name == "limit":
            limit = schemas.require_limit(value)
        elif name == "cursor":
            after = schemas.decode_outcome_cursor(value)
        else:
            filters[name] = value
    return filters, tuple(dict.fromkeys(event_types)), limit, after


def list_outcomes(context) -> schemas.Response:
    """GET /outcomes: one bounded page in insertion order."""

    filters, event_types, limit, after = _query(context.query)
    try:
        rows = context.app.database.run(
            lambda connection: LibraryRepository(connection).list_outcomes(
                **filters, event_types=event_types or None,
                after_event_id=after, limit=limit + 1))
    except LibraryError as error:
        raise _failure(error) from None
    items = rows[:limit]
    has_more = len(rows) > limit
    return schemas.Response(200, {
        "api_schema": context.app.api_schema,
        "items": [asdict(row) for row in items],
        "page": {
            "limit": limit, "count": len(items),
            "next_cursor": (schemas.encode_outcome_cursor(items[-1].event_id)
                            if has_more else None),
            "has_more": has_more,
        },
        "query": {**filters, "event_types": list(event_types)},
    })
