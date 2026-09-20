"""Request validation and response shapes for the local service (issue #27).

Every function here is pure: it turns an already-parsed JSON body or an
already-parsed query string into a value, or raises the one `ApiError` code the
request deserves. Nothing here reads the database, the filesystem or a clock, so
the same cases can be replayed from `tests/fixtures/api/request-cases.json`
without a service. Paths, media types, hosts and origins are `service`'s
concern; the `root` value this module accepts is validated against #22's root
rules by the caller, because that is a filesystem question.

The numeric bounds live here because they are request bounds: a page is
`DEFAULT_PAGE_SIZE` samples unless the client asks otherwise, and the client may
ask for `PAGE_SIZE_MIN` to `PAGE_SIZE_MAX`. Only the standard library, #9's
`ROLES` and the canonical-JSON helper of `backend.analysis.batch` are used.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import re

from backend.analysis.batch import ROLES, canonical
from backend.api.errors import ApiError
from backend.palette.model import MVP_SLOTS


#: The page size a request gets when it names none.
DEFAULT_PAGE_SIZE = 50

#: The smallest and the largest page a client may ask for, inclusive.
PAGE_SIZE_MIN = 1
PAGE_SIZE_MAX = 200

#: The longest search text a request may carry, in characters.
MAX_QUERY_LENGTH = 200

#: The one cursor encoding this service reads and writes.
CURSOR_VERSION = 1

#: `run_id` and `sample_id` are the only identifiers a path may carry.
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SAMPLE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")

#: A cursor is base64url and nothing else: no `+`, no `/`, no whitespace, so a
#: standard-alphabet or re-wrapped encoding is refused rather than misread.
CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z")

_IMPORT_FIELDS = ("root", "role")
_QUERY_PARAMETERS = ("role", "q", "limit", "cursor")

# The fields the request shapes name in an error's `details`. Always a fixed
# name from the request schema, never a value a client supplied.
_FIELD = "field"
_PARAMETER = "parameter"


@dataclass(frozen=True)
class Response:
    """One response a handler returns: status, JSON body and extra headers."""

    status: int
    body: dict
    headers: tuple = ()


@dataclass(frozen=True)
class RequestContext:
    """One request as a handler receives it.

    `app` is the service application (it owns the database, the run registry and
    the published bounds), `template` is the matched route template, `params`
    are its path parameters, `query` is the already-parsed query as
    `(name, value)` pairs and `body` is the parsed JSON document of a request
    whose route accepts one.
    """

    app: object
    method: str
    template: str
    params: dict = field(default_factory=dict)
    query: tuple = ()
    body: object | None = None


@dataclass(frozen=True)
class ImportRequest:
    """A validated `POST /imports` body. `root` is still the client's string."""

    root: str
    role: str


@dataclass(frozen=True)
class SampleQuery:
    """A validated `GET /library/samples` query.

    `roles` is the OR-ed role filter in request order (empty when none was
    given), `text` is the file-name substring or None, `limit` is the page size
    and `after` is the sample id a cursor named, or None.
    """

    roles: tuple = ()
    text: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    after: str | None = None

    def as_value(self) -> dict:
        """The value a fixture case compares against."""

        return {"roles": list(self.roles), "text": self.text, "limit": self.limit,
                "after": self.after}


def import_request(payload) -> ImportRequest:
    """The validated body of `POST /imports`.

    The body must be an object; `root` and `role` must be present strings; no
    other field is accepted; `role` must be one of #9's `ROLES`. The root's
    #22 rules are the caller's, because they read the filesystem.
    """

    if type(payload) is not dict:
        raise ApiError("invalid_body")
    unknown = sorted(set(payload) - set(_IMPORT_FIELDS))
    if unknown:
        raise ApiError("unknown_field", details={_FIELD: unknown[0]})
    for name in _IMPORT_FIELDS:
        if name not in payload:
            raise ApiError("missing_field", details={_FIELD: name})
    for name in _IMPORT_FIELDS:
        if not isinstance(payload[name], str):
            raise ApiError("invalid_field_type", details={_FIELD: name})
    if payload["role"] not in ROLES:
        raise ApiError("invalid_role", details={_FIELD: "role"})
    return ImportRequest(root=payload["root"], role=payload["role"])


def sample_query(pairs) -> SampleQuery:
    """The validated query of `GET /library/samples`.

    `pairs` is an already-parsed query: a sequence of `(name, value)` strings
    (`urllib.parse.parse_qsl`) or the mapping `urllib.parse.parse_qs` returns.
    `role` may repeat and is OR-ed, `q` is the file-name substring, `limit` is
    the page size and `cursor` is a position. Any other parameter, and any value
    outside this module's bounds, is refused with the code that names it.
    """

    roles, text, limit, after = [], None, DEFAULT_PAGE_SIZE, None
    for name, value in _pairs(pairs):
        if name == "role":
            if value not in ROLES:
                raise ApiError("invalid_role", details={_PARAMETER: "role"})
            roles.append(value)
        elif name == "q":
            text = require_text(value)
        elif name == "limit":
            limit = require_limit(value)
        elif name == "cursor":
            after = decode_cursor(value)
        else:
            raise ApiError("unknown_query_parameter", details={_PARAMETER: name})
    return SampleQuery(roles=tuple(roles), text=text, limit=limit, after=after)


def require_text(value) -> str:
    """One search text: 1 to `MAX_QUERY_LENGTH` characters, or `invalid_query`."""

    if not isinstance(value, str) or not 1 <= len(value) <= MAX_QUERY_LENGTH:
        raise ApiError("invalid_query", details={_PARAMETER: "q"})
    return value


def require_limit(value) -> int:
    """One page size: an integer in `[PAGE_SIZE_MIN, PAGE_SIZE_MAX]`.

    A float, a blank, a negative number and an empty string are all
    `invalid_page_size`, because the parameter is an integer count and this
    service does not coerce one shape into another.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ApiError("invalid_page_size", details={_PARAMETER: "limit"})
    try:
        number = int(str(value), 10)
    except ValueError:
        raise ApiError("invalid_page_size", details={_PARAMETER: "limit"}) from None
    if not PAGE_SIZE_MIN <= number <= PAGE_SIZE_MAX:
        raise ApiError("invalid_page_size", details={_PARAMETER: "limit"})
    return number


def require_run_id(value) -> str:
    """One path `run_id`: `[A-Za-z0-9_-]{1,64}`, or `invalid_run_id`."""

    if not isinstance(value, str) or not RUN_ID_PATTERN.fullmatch(value):
        raise ApiError("invalid_run_id")
    return value


def require_sample_id(value) -> str:
    """One path `sample_id`: `sha256:<64 lowercase hex>`, or `invalid_sample_id`."""

    if not isinstance(value, str) or not SAMPLE_ID_PATTERN.fullmatch(value):
        raise ApiError("invalid_sample_id")
    return value


def encode_cursor(sample_id: str) -> str:
    """The cursor that resumes a page strictly after `sample_id`.

    `base64url(canonical_json({"v": 1, "after": "<sample_id>"}))`: the payload is
    #9's canonical JSON, so two encoders of the same position produce one string
    and a cursor can be compared, logged or asserted without parsing it first.
    """

    payload = canonical({"v": CURSOR_VERSION, "after": sample_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(value) -> str:
    """The sample id a cursor names, or `invalid_cursor`.

    A cursor of another version, of another alphabet, of another shape, or one
    naming something that is not a sample id, is refused. A cursor naming a
    sample that no longer exists is accepted: it is a position, and the read
    that follows is strictly greater than it.
    """

    if not isinstance(value, str) or not CURSOR_PATTERN.fullmatch(value):
        raise ApiError("invalid_cursor")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError):
        raise ApiError("invalid_cursor") from None
    if type(payload) is not dict or set(payload) != {"v", "after"}:
        raise ApiError("invalid_cursor")
    if type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION:
        raise ApiError("invalid_cursor")
    after = payload["after"]
    if not isinstance(after, str) or not SAMPLE_ID_PATTERN.fullmatch(after):
        raise ApiError("invalid_cursor")
    return after


def encode_outcome_cursor(event_id: int) -> str:
    """Encode an outcome insertion position with the service's cursor version."""

    payload = canonical({"v": CURSOR_VERSION, "after": event_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_outcome_cursor(value) -> int:
    """Decode an outcome position; deleted event ids remain valid positions."""

    if not isinstance(value, str) or not CURSOR_PATTERN.fullmatch(value):
        raise ApiError("invalid_cursor")
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, binascii.Error):
        raise ApiError("invalid_cursor") from None
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != value.rstrip("="):
        raise ApiError("invalid_cursor")
    if (type(payload) is not dict or set(payload) != {"v", "after"}
            or type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION
            or type(payload["after"]) is not int or payload["after"] < 0):
        raise ApiError("invalid_cursor")
    return payload["after"]


def _pairs(value):
    """An already-parsed query as a flat sequence of `(name, value)` strings."""

    if isinstance(value, dict):
        items = []
        for name, entry in value.items():
            entries = entry if isinstance(entry, (list, tuple)) else (entry,)
            items.extend((name, item) for item in entries)
        return items
    pairs = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ApiError("unknown_query_parameter", details={_PARAMETER: ""})
        pairs.append((entry[0], entry[1]))
    return pairs


# ---------------------------------------------------------------------------
# the palette projection (issue #32)
# ---------------------------------------------------------------------------


def palette_document(project, record) -> dict:
    """One stored palette as the one documented projection.

    The shape is closed: `palette_id`, `project`, `name`, `revision`, `context`
    and `items`, with exactly the `MVP_SLOTS` keys and exactly the three context
    keys. Every value is copied from #24's records -- no default tempo, key or
    genre is invented, no timestamp and no path appears, and a context field is
    reported by its stored *state* rather than by inspecting its reason string.
    """

    return {
        "palette_id": record.palette_id,
        "project": {"project_id": project.project_id, "name": project.name},
        "name": record.name,
        "revision": record.revision,
        "context": {
            "tempo": _context_document(
                record.context_state.tempo, {"bpm": record.song.tempo.value},
                record.song.tempo.unavailable_reason),
            "key": _context_document(
                record.context_state.key,
                {"tonic": record.song.key.tonic, "mode": record.song.key.mode},
                record.song.key.unavailable_reason),
            "genre": _context_document(
                record.context_state.genre, {"genre": record.song.genre},
                record.song.genre_unavailable_reason),
        },
        "items": {slot: _item_document(record, slot) for slot in MVP_SLOTS},
    }


def _context_document(state: str, values: dict, reason) -> dict:
    if state == "known":
        return {"state": "known", **values}
    if state == "unknown":
        return {"state": "unknown", "reason": reason}
    return {"state": "unset"}


def _item_document(record, slot: str):
    for item in record.active_items:
        if item.slot == slot:
            return {
                "slot": item.slot,
                "sample_id": item.sample_id,
                "role": item.role,
                "added_revision": item.added_revision,
                "sample_state": item.sample_state,
                "sample_error_code": item.sample_error_code,
                "slot_role_mismatch": item.slot_role_mismatch,
            }
    return None
