"""The library read operations of the local service (issue #27).

Two routes live here: `GET /library/samples` pages stored samples by role and
file name, and `GET /library/samples/{sample_id}` serves one sample's stored
measurements. Both read through #21's `LibraryRepository`, both project stored
values and derive nothing, and neither returns a path: the file name is the
basename the scan stored, and `AudioMetadata.local_path` is dropped by
construction rather than sanitised afterwards.

This is the only route pair that returns measurements, and it returns them
exactly as stored: no similarity, compatibility, score, warning or ranking
field is added, nothing is imputed, normalised, rounded or renamed, and an
unknown measurement keeps its stored `unavailable_reason` with no confidence.
"""

from __future__ import annotations

from backend.api import schemas
from backend.api.errors import ApiError
from backend.contracts import SCHEMA_VERSION
from backend.library import indexer
from backend.library.repository import LibraryRepository


#: How many stored analysis versions one detail response lists before it says so.
MAX_STORED_VERSIONS = 8

#: The most SQL statements one page may execute, however large the page is.
#: `page_samples` reads its rows and one sentinel row in a single statement, so
#: the budget holds at 1 and this bound is what a test asserts it against.
READ_STATEMENT_BUDGET = 3


def list_samples(context) -> schemas.Response:
    """`GET /library/samples`: one page of stored samples, in one statement.

    The page is ordered by content identity, so the cursor of the last item
    resumes exactly after it; `next_cursor` is non-null exactly when
    `has_more` is true. `analysis.analysis_version` is the stored version only
    for a `current` sample, which is what "the measurements I can show you are
    of this version" means to a client.
    """

    query = schemas.sample_query(context.query)
    app = context.app
    page = app.database.run(
        lambda connection: LibraryRepository(connection).page_samples(
            indexer.current_analysis_version(), roles=query.roles or None, text=query.text,
            after=query.after, limit=query.limit))
    items = [
        {"sample_id": row.sample_id, "role": row.role, "file_name": row.file_name,
         "file_status": row.file_status,
         "analysis": {"state": row.analysis_state,
                      "analysis_version": row.analysis_version},
         "audio": {"sample_rate_hz": row.sample_rate_hz, "channels": row.channels,
                   "frame_count": row.frame_count, "duration_ms": row.duration_ms}}
        for row in page.rows]
    return schemas.Response(200, {
        "api_schema": app.api_schema,
        "items": items,
        "page": {"limit": query.limit, "count": len(items),
                 "next_cursor": (schemas.encode_cursor(items[-1]["sample_id"])
                                 if page.has_more else None),
                 "has_more": page.has_more},
        "query": {"roles": list(query.roles), "text": query.text},
    })


def sample(context) -> schemas.Response:
    """`GET /library/samples/{sample_id}`: the stored measurements of one sample.

    `sample_id` is the content identity. `features` is the stored contract
    `AudioFeatures` projected field for field and is non-null only when the
    analysis is `current`; every other state is still 200 with the structured
    `analysis` object, which is the recoverable answer for an unavailable
    analysis.
    """

    sample_id = schemas.require_sample_id(context.params["sample_id"])
    app = context.app
    detail = app.database.run(
        lambda connection: LibraryRepository(connection).sample_detail(sample_id))
    if detail is None:
        raise ApiError("unknown_sample", details={"sample_id": sample_id})
    versions = detail.stored_versions
    return schemas.Response(200, {
        "api_schema": app.api_schema,
        "sample": {
            "sample_id": detail.sample_id,
            "role": detail.role,
            "file_name": detail.file_name,
            "file_status": detail.file_status,
            "analysis": {
                "state": detail.analysis_state,
                "analysis_version": detail.analysis_version,
                "analyzed_at": detail.analyzed_at,
                "attempts": detail.attempts,
                "error_code": detail.error_code,
                "stored_versions": list(versions[:MAX_STORED_VERSIONS]),
                "stored_versions_truncated": len(versions) > MAX_STORED_VERSIONS,
            },
            "audio": {"sample_rate_hz": detail.sample_rate_hz, "channels": detail.channels,
                      "frame_count": detail.frame_count, "duration_ms": detail.duration_ms},
            "features": (None if detail.sample is None
                         else _features(detail.sample.features, detail.sample.analysis_version)),
        },
    })


def _features(features, analysis_version: str) -> dict:
    """One stored `AudioFeatures` projected field for field.

    Every `MEASURES` name appears exactly once in contract order, with the
    contract unit for that name. A known value carries its confidence (which may
    be null where the contract makes it optional); an unknown value keeps
    `value: null` and its stored reason and carries no `confidence` key at all,
    because the contract forbids an unknown value from claiming one.
    """

    measurements = []
    for item in features.measurements:
        document = {"name": item.name, "value": item.value, "unit": item.unit,
                    "unavailable_reason": item.unavailable_reason}
        if item.value is not None:
            document["confidence"] = item.confidence
        measurements.append(document)
    return {"schema_version": SCHEMA_VERSION,
            "analysis_version": analysis_version,
            "measurements": measurements,
            "key": {"tonic": features.key.tonic, "mode": features.key.mode,
                    "confidence": features.key.confidence,
                    "unavailable_reason": features.key.unavailable_reason}}
