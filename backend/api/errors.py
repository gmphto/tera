"""Coded failures and the one error envelope for the local service (issue #27).

Every non-2xx response this service writes is built from one `ApiError`: a code
from the closed `ERROR_CODES` table, that code's HTTP status, a constant
sentence and an object of ids, codes and counts. The vocabulary is closed and
mirrors the codes in `_docs/local-service.md`; a behaviour that needs a new code
needs a new issue, not a new string.

A message is fixed per code and never carries a path, a SQL fragment, a
traceback or anything a caller supplied, so a response can be logged and shown
without leaking the library. Only the standard library is imported here.
"""

from __future__ import annotations


#: The longest message any code may publish. `test_api_contracts` asserts it for
#: every code, so a new sentence cannot quietly grow past a client's bound.
MAX_MESSAGE_LENGTH = 200


# The closed table: one code and one HTTP status each. The tuple is the table;
# `STATUS_BY_CODE` and `MESSAGE_BY_CODE` below are derived from it, so a code
# cannot appear twice or drift from its status.
ERROR_CODES = (
    ("invalid_json", 400),
    ("invalid_body", 400),
    ("missing_field", 400),
    ("invalid_field_type", 400),
    ("unknown_field", 400),
    ("invalid_root", 400),
    ("invalid_role", 400),
    ("invalid_run_id", 400),
    ("invalid_sample_id", 400),
    ("invalid_query", 400),
    ("invalid_page_size", 400),
    ("unknown_query_parameter", 400),
    ("invalid_cursor", 400),
    ("invalid_palette_id", 400),
    ("invalid_revision", 400),
    ("invalid_limit", 400),
    ("invalid_outcome", 400),
    ("unknown_route", 404),
    ("unknown_import", 404),
    ("unknown_sample", 404),
    ("unknown_palette", 404),
    ("unknown_project", 404),
    ("unknown_selection", 404),
    ("method_not_allowed", 405),
    ("host_not_allowed", 403),
    ("origin_not_allowed", 403),
    ("import_already_running", 409),
    ("import_not_live", 409),
    ("revision_conflict", 409),
    ("palette_incomplete", 409),
    ("kick_unavailable", 409),
    ("unknown_palette_revision", 409),
    ("cross_project_reference", 409),
    ("selection_not_in_palette", 409),
    ("removal_not_reflected", 409),
    ("outcome_conflict", 409),
    ("idempotency_conflict", 409),
    ("length_required", 411),
    ("request_too_large", 413),
    ("unsupported_media_type", 415),
    ("internal_error", 500),
    ("write_failed", 500),
    ("database_unavailable", 503),
    ("database_locked", 503),
    ("server_busy", 503),
)

STATUS_BY_CODE = dict(ERROR_CODES)

#: The constant sentence per code. Path-free, SQL-free, traceback-free.
MESSAGE_BY_CODE = {
    "invalid_json": "The request body is not valid JSON.",
    "invalid_body": "The request body must be a JSON object.",
    "missing_field": "A required field is missing.",
    "invalid_field_type": "A field has the wrong type.",
    "unknown_field": "The object carries a field this operation does not accept.",
    "invalid_root": "The import root is not an existing local directory.",
    "invalid_role": "The role is not one of the supported roles.",
    "invalid_run_id": "The run id is malformed.",
    "invalid_sample_id": "The sample id is malformed.",
    "invalid_query": "The search text must be 1 to 200 characters.",
    "invalid_page_size": "The page size is outside the supported range.",
    "unknown_query_parameter": "The query carries a parameter this operation does not accept.",
    "invalid_cursor": "The page cursor is malformed or of an unsupported version.",
    "invalid_palette_id": "The palette id is malformed.",
    "invalid_revision": "The revision is not a nonnegative whole number.",
    "invalid_limit": "The result limit is outside the supported range.",
    "invalid_outcome": "The outcome is malformed or unsupported.",
    "unknown_route": "No operation is served at this path.",
    "unknown_import": "No import run has this id.",
    "unknown_sample": "No stored sample has this id.",
    "unknown_palette": "No stored palette has this id.",
    "unknown_project": "No stored project has this id.",
    "unknown_selection": "No stored selection has this id.",
    "method_not_allowed": "This path does not accept this method.",
    "host_not_allowed": "The request host is not allowed.",
    "origin_not_allowed": "The request origin is not allowed.",
    "import_already_running": "An import is already running.",
    "import_not_live": "The import is not running.",
    "revision_conflict": "The palette changed since the request was prepared.",
    "palette_incomplete": "The palette cannot be assembled into a request.",
    "kick_unavailable": "The palette's selected kick cannot be used.",
    "unknown_palette_revision": "The palette revision has not existed.",
    "cross_project_reference": "The palette belongs to another project.",
    "selection_not_in_palette": "The candidate is not selected in the palette.",
    "removal_not_reflected": "The candidate is still selected in the palette.",
    "outcome_conflict": "The outcome conflicts with the current history.",
    "idempotency_conflict": "The client event id belongs to another outcome.",
    "length_required": "The request must carry a Content-Length header.",
    "request_too_large": "The request body is larger than this service accepts.",
    "unsupported_media_type": "The request body must be application/json.",
    "internal_error": "The service failed to complete the request.",
    "write_failed": "The outcome could not be written.",
    "database_unavailable": "The local library database is not available.",
    "database_locked": "The local library database is temporarily locked.",
    "server_busy": "The service is at its request limit.",
}


class ApiError(Exception):
    """One refused request: a closed code, its status, a constant sentence.

    `details` carries ids, codes and counts only: never a path, never a message
    a user typed, never a stored file name. A code outside `ERROR_CODES` is a
    programming error and raises `KeyError` here rather than reaching a client.
    """

    def __init__(self, code: str, *, details=None, message=None):
        self.code = code
        self.status = STATUS_BY_CODE[code]
        self.message = MESSAGE_BY_CODE[code] if message is None else message
        if len(self.message) > MAX_MESSAGE_LENGTH:
            raise ValueError(f"The message for {code!r} exceeds {MAX_MESSAGE_LENGTH} characters.")
        self.details = {} if details is None else dict(details)
        super().__init__(self.message)

    def document(self, api_schema: str) -> dict:
        """The one error envelope every non-2xx response body holds."""

        return {"api_schema": api_schema,
                "error": {"code": self.code, "message": self.message,
                          "details": dict(self.details)}}
