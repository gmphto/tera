"""The palette and project operations of the local service (issue #32).

Four routes: read the stored palette, create the first project, set one slot's
item and replace the whole song context. Every storage rule behind them is
#24's -- the revision compare-and-set, the one-active-item-per-slot rule, the
no-op on re-selecting the active sample, the slot and role acceptance and the
error codes -- and every transport rule is #27's. This module holds no SQL, no
weight, no threshold, no dimension, no label and no Jev import: it validates a
request, calls #24's operations and projects the result.
"""

from __future__ import annotations

import math
import re

from backend.api import schemas
from backend.api.errors import ApiError
from backend.api.schemas import RUN_ID_PATTERN
from backend.contracts import Measurement, MusicalKey, SongContext
from backend.library.errors import LibraryError
from backend.library.repository import LibraryRepository
from backend.palette.model import (
    CONTEXT_STATES,
    MVP_SLOTS,
    SLOT_ROLES,
    SONG_CONTEXT_ABSENT_REASON,
)

PALETTE_PATH = "/palette"
PROJECTS_PATH = "/projects"
PALETTE_ITEM_PATH = "/palette/items/{slot}"
PALETTE_CONTEXT_PATH = "/palette/context"

#: The most characters one context text (a reason, a genre) may carry. The
#: client asserts the same bound.
MAX_CONTEXT_TEXT_LENGTH = 64

#: The most characters a project or palette name may carry.
MAX_NAME_LENGTH = 80

#: What a producer-declared context value claims.
#:
#: The producer typed this for their own song: it is a declaration, not a
#: measurement, and it is deliberately above the reliability gate the ranking
#: layers apply to song tempo and key, so a value entered here is actually used
#: by the locks it feeds. An absent field is never given this value.
MANUAL_CONTEXT_CONFIDENCE = 1.0

#: The unit the tempo measurement carries, as `backend.contracts` spells it.
TEMPO_UNIT = "BPM"

_SAFE_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _field(name) -> str:
    """A submitted key, echoed only when it looks like one."""

    return name if isinstance(name, str) and _SAFE_FIELD.fullmatch(name) else "field"


def _reject_unknown(body: dict, allowed) -> None:
    for name in body:
        if name not in allowed:
            raise ApiError("unknown_field", details={"field": _field(name)})


def _require_all(body: dict, names) -> None:
    for name in names:
        if name not in body:
            raise ApiError("missing_field", details={"field": name})


def _pattern_id(value, field: str, code: str) -> str:
    if not isinstance(value, str) or not RUN_ID_PATTERN.fullmatch(value):
        raise ApiError(code, details={"field": field})
    return value


def _revision(value) -> int:
    if type(value) is not int or value < 0:
        raise ApiError("invalid_revision", details={"field": "expected_revision"})
    return value


def _name(value, field: str) -> str:
    if not isinstance(value, str):
        raise ApiError("invalid_field_type", details={"field": field})
    if not value.strip() or len(value) > MAX_NAME_LENGTH:
        raise ApiError("invalid_field_type", details={"field": field})
    return value


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------


def handle_read(context) -> schemas.Response:
    """`GET /palette`: the stored palette, by project or by the MVP's one project.

    The route issues no write statement and its statement count does not depend
    on how many items the palette holds.
    """

    project_id = parse_palette_query(context.query)
    app = context.app
    document = app.database.run(lambda connection: _read(connection, project_id))
    return schemas.Response(200, {"api_schema": app.api_schema, "palette": document})


def handle_create_project(context) -> schemas.Response:
    """`POST /projects`: create the single project the MVP works in."""

    name, palette_name = parse_project_request(context.body)
    app = context.app
    document = app.database.run(lambda connection: _create(connection, name, palette_name))
    return schemas.Response(201, {"api_schema": app.api_schema, "palette": document})


def handle_set_item(context) -> schemas.Response:
    """`PUT /palette/items/{slot}`: select or replace one slot's item."""

    slot = context.params["slot"]
    if slot not in MVP_SLOTS:
        raise ApiError("unknown_slot", details={"slot": _field(slot)})
    palette_id, sample_id, expected_revision = parse_item_request(context.body)

    app = context.app

    def mutate(connection):
        repository = LibraryRepository(connection)
        try:
            mutation = repository.set_palette_item(
                palette_id, slot, sample_id, expected_revision=expected_revision)
        except LibraryError as error:
            raise _failure(error, repository=repository, slot=slot, sample_id=sample_id) from None
        return {"changed": mutation.changed, "palette": _document(repository, palette_id)}

    result = app.database.run(mutate)
    return schemas.Response(200, {"api_schema": app.api_schema, **result})


def handle_set_context(context) -> schemas.Response:
    """`PUT /palette/context`: replace the whole song context in one call."""

    palette_id, expected_revision, song, unset = parse_context_request(context.body)

    app = context.app

    def mutate(connection):
        repository = LibraryRepository(connection)
        try:
            mutation = repository.set_palette_context(
                palette_id, song, expected_revision=expected_revision, unset=unset)
        except LibraryError as error:
            raise _failure(error, repository=repository) from None
        return {"changed": mutation.changed, "palette": _document(repository, palette_id)}

    result = app.database.run(mutate)
    return schemas.Response(200, {"api_schema": app.api_schema, **result})


# ---------------------------------------------------------------------------
# request parsers: pure, so the fixture runner replays them without a service
# ---------------------------------------------------------------------------


def parse_palette_query(pairs):
    """The optional `project_id` of `GET /palette`."""

    project_id = None
    for name, value in pairs:
        if name != "project_id":
            raise ApiError("unknown_query_parameter", details={"parameter": _field(name)})
        if not isinstance(value, str) or not RUN_ID_PATTERN.fullmatch(value):
            raise ApiError("invalid_project_id", details={"field": "project_id"})
        project_id = value
    return project_id


def parse_project_request(body):
    """`POST /projects` as `[name, palette_name]`."""

    if type(body) is not dict:
        raise ApiError("invalid_body")
    _reject_unknown(body, ("name", "palette_name"))
    _require_all(body, ("name",))
    name = _name(body["name"], "name")
    palette_name = _name(body["palette_name"], "palette_name") if "palette_name" in body else None
    return [name, palette_name]


def parse_item_request(body):
    """`PUT /palette/items/{slot}` as `[palette_id, sample_id, expected_revision]`."""

    if type(body) is not dict:
        raise ApiError("invalid_body")
    fields = ("palette_id", "sample_id", "expected_revision")
    _reject_unknown(body, fields)
    _require_all(body, fields)
    return [
        _pattern_id(body["palette_id"], "palette_id", "invalid_palette_id"),
        schemas.require_sample_id(body["sample_id"]),
        _revision(body["expected_revision"]),
    ]


def parse_context_request(body):
    """`PUT /palette/context` as `(palette_id, expected_revision, song, unset)`.

    `unset` names the fields the producer cleared: the contract has no way to
    state a null value and no reason, so the cleared fields are reported beside
    the assembled `SongContext` and the storage layer writes their columns NULL.
    """

    if type(body) is not dict:
        raise ApiError("invalid_body")
    fields = ("palette_id", "expected_revision", "tempo", "key", "genre")
    _reject_unknown(body, fields)
    _require_all(body, fields)
    song, unset = _song(body["tempo"], body["key"], body["genre"])
    return (
        _pattern_id(body["palette_id"], "palette_id", "invalid_palette_id"),
        _revision(body["expected_revision"]),
        song,
        unset,
    )


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _read(connection, project_id):
    repository = LibraryRepository(connection)
    if project_id is None:
        projects = repository.list_projects()
        if not projects:
            raise ApiError("unknown_project", details={"project_id": None})
        project = projects[0]
    else:
        project = repository.get_project(project_id)
        if project is None:
            raise ApiError("unknown_project", details={"project_id": project_id})
    if project.palette_id is None:
        raise ApiError("unknown_palette", details={"palette_id": None})
    record = repository.load_palette(project.palette_id)
    if record is None:
        raise ApiError("unknown_palette", details={"palette_id": project.palette_id})
    return schemas.palette_document(project, record)


def _document(repository, palette_id: str) -> dict:
    record = repository.load_palette(palette_id)
    if record is None:
        raise ApiError("unknown_palette", details={"palette_id": palette_id})
    project = repository.get_project(record.project_id)
    if project is None:
        raise ApiError("unknown_project", details={"project_id": record.project_id})
    return schemas.palette_document(project, record)


def _create(connection, name: str, palette_name):
    repository = LibraryRepository(connection)
    existing = repository.list_projects()
    if existing:
        raise ApiError("project_exists", details={"project_id": existing[0].project_id})
    if palette_name is None:
        project = repository.create_project(name)
    else:
        project = repository.create_project(name, palette_name=palette_name)
    return _document(repository, project.palette_id)


def _failure(error: LibraryError, *, repository, slot=None, sample_id=None) -> ApiError:
    """One storage refusal as the one #27 envelope, with #24's code."""

    code = error.code
    if code == "unknown_sample":
        return ApiError(code, details={"sample_id": sample_id})
    if code == "role_mismatch":
        return ApiError(code, details={
            "slot": slot,
            "accepted_roles": list(SLOT_ROLES[slot]),
            "stored_role": repository.stored_role(sample_id),
            "sample_id": sample_id,
        })
    if code == "revision_conflict":
        return ApiError(code, details={
            "expected_revision": error.expected_revision,
            "current_revision": error.current_revision,
        })
    return ApiError(code)


# ---------------------------------------------------------------------------
# the song context
# ---------------------------------------------------------------------------


def _song(tempo, key, genre):
    """One `(SongContext, unset_fields)` pair from the three submitted fields."""

    tempo_state, tempo_payload = _context_field("tempo", tempo, ("bpm",))
    key_state, key_payload = _context_field("key", key, ("tonic", "mode"))
    genre_state, genre_payload = _context_field("genre", genre, ("genre",))
    genre_value, genre_reason = _genre(genre_state, genre_payload)
    unset = tuple(
        name for name, state in (("tempo", tempo_state), ("key", key_state),
                                 ("genre", genre_state)) if state == "unset")
    return (
        SongContext(
            tempo=_tempo(tempo_state, tempo_payload),
            key=_key(key_state, key_payload),
            genre=genre_value,
            genre_unavailable_reason=genre_reason,
        ),
        unset,
    )


def _context_field(name: str, value, value_keys):
    """One context field as `(#24 state, payload)`.

    `unset` carries no payload, `unknown` carries its reason verbatim and
    `known` carries the field's value keys. A key that does not belong to the
    state is refused by name, which is how `{"state": "unset", "bpm": 5}`
    answers `unknown_field` for `tempo.bpm`.
    """

    if type(value) is not dict:
        raise ApiError("invalid_field_type", details={"field": name})
    state = value.get("state")
    if state not in CONTEXT_STATES:
        raise ApiError("invalid_context", details={"field": name})
    allowed = {"state", "reason"} | set(value_keys)
    for key in value:
        if key not in allowed:
            raise ApiError("unknown_field", details={"field": f"{name}.{_field(key)}"})
    if state == "unset":
        for key in value:
            if key != "state":
                raise ApiError("unknown_field", details={"field": f"{name}.{_field(key)}"})
        return state, None
    if state == "unknown":
        for key in value:
            if key not in ("state", "reason"):
                raise ApiError("unknown_field", details={"field": f"{name}.{_field(key)}"})
        reason = value.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_CONTEXT_TEXT_LENGTH:
            raise ApiError("invalid_context", details={"field": f"{name}.reason"})
        return state, reason
    if "reason" in value:
        raise ApiError("unknown_field", details={"field": f"{name}.reason"})
    for key in value_keys:
        if key not in value:
            raise ApiError("missing_field", details={"field": f"{name}.{key}"})
    return state, {key: value[key] for key in value_keys}


def _tempo(state: str, payload) -> Measurement:
    if state == "unset":
        return Measurement(name="tempo", value=None, unit=TEMPO_UNIT,
                           unavailable_reason=SONG_CONTEXT_ABSENT_REASON)
    if state == "unknown":
        return Measurement(name="tempo", value=None, unit=TEMPO_UNIT,
                           unavailable_reason=payload)
    bpm = payload["bpm"]
    if type(bpm) is bool or not isinstance(bpm, (int, float)) or not math.isfinite(bpm):
        raise ApiError("invalid_context", details={"field": "tempo.bpm"})
    try:
        return Measurement(name="tempo", value=float(bpm), unit=TEMPO_UNIT,
                           unavailable_reason=None, confidence=MANUAL_CONTEXT_CONFIDENCE)
    except (TypeError, ValueError):
        raise ApiError("invalid_context", details={"field": "tempo.bpm"}) from None


def _key(state: str, payload) -> MusicalKey:
    if state == "unset":
        return MusicalKey(tonic=None, mode=None, confidence=None,
                          unavailable_reason=SONG_CONTEXT_ABSENT_REASON)
    if state == "unknown":
        return MusicalKey(tonic=None, mode=None, confidence=None,
                          unavailable_reason=payload)
    try:
        return MusicalKey(tonic=payload["tonic"], mode=payload["mode"],
                          confidence=MANUAL_CONTEXT_CONFIDENCE, unavailable_reason=None)
    except (TypeError, ValueError):
        raise ApiError("invalid_context", details={"field": "key"}) from None


def _genre(state: str, payload):
    if state == "unset":
        return None, SONG_CONTEXT_ABSENT_REASON
    if state == "unknown":
        return None, payload
    genre = payload["genre"]
    if not isinstance(genre, str) or not genre.strip() or len(genre) > MAX_CONTEXT_TEXT_LENGTH:
        raise ApiError("invalid_context", details={"field": "genre"})
    return genre, None
