"""Bounded TypeSafe Jev transport adapter; see _docs/jev-adapter.md.

score_questions is the one entry point. It sends the #13 question payload unchanged to exactly
one transport, at most MAX_BATCH_SIZE questions per call, and returns exactly one explicit
JevOutcome per request in input order whatever happens: the interface judged the question,
abstained, returned something #13 rejects, failed, timed out, was cancelled, ran out of batch
budget, or was never reached at all because credentials are absent. Nothing is merged or cached:
two requests that share one question_id get two outcomes, two sends and two attempt counts.

The adapter scores the questions it is given and never builds one.
backend.intelligence.questions owns the payload and backend.intelligence.decisions owns response
validation. This module is standard library only, imports this product's question and decision
modules, and never imports the contract-double module, so a double can never be reached
implicitly. It carries no sample identity: the payload is the only data it
sends and the endpoint is the only destination. No file is read and no audio is touched.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from backend.contracts import JevJudgment
from backend.intelligence.decisions import (RESPONSE_ERROR_CODES, JevResponseError,
                                            validate_response)
from backend.intelligence.questions import (PROMPT_VERSION, QUESTION_UNAVAILABLE_CODES,
                                            JevQuestion, UnavailableQuestion)

# The adapter record version; any change to what a run records needs a new value.
ADAPTER_VERSION = "jev-adapter-v1"

# One send's timeout in seconds, and the published bounds a configuration may use.
DEFAULT_TIMEOUT_S = 20.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 120.0

# Sends attempted for one question, and the published bounds a configuration may use.
DEFAULT_MAX_ATTEMPTS = 3
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 5

# The first backoff wait in seconds, its multiplier and the clamp the default config uses.
DEFAULT_BACKOFF_S = 0.5
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF_S = 8.0

# The wall-clock budget for one call in seconds, and the published bounds.
DEFAULT_MAX_BATCH_SECONDS = 120.0
MIN_MAX_BATCH_SECONDS = 1.0
MAX_MAX_BATCH_SECONDS = 600.0

# The hard ceiling on questions per call; the caller issues successive bounded calls.
MAX_BATCH_SIZE = 100

# Every state one outcome can carry, in the documented order.
OUTCOME_STATES = ("judged", "abstained", "not_asked", "not_attempted", "invalid_result",
                  "service_error", "timed_out", "unavailable")

# Every code a transport failure can carry.
TRANSPORT_ERROR_CODES = ("connection_failed", "service_unavailable", "service_error",
                         "rate_limited", "timeout", "invalid_credentials")

# The transport codes that are retried while attempts remain and the batch has budget.
RETRYABLE_TRANSPORT_CODES = ("connection_failed", "service_unavailable", "service_error",
                             "rate_limited", "timeout")

# The codes for a request the adapter never sent because the run stopped.
NOT_ATTEMPTED_CODES = ("cancelled", "batch_deadline_exceeded")

# The codes for a request that could not be reached at all; the documented frozenset.
UNAVAILABLE_CODES = frozenset({"credentials_absent"})

# Every code an adapter error can carry; all of them are raised before any send.
ADAPTER_ERROR_CODES = ("invalid_configuration", "batch_too_large", "duplicate_request_id",
                       "invalid_requests")

# The transport sources the adapter accepts; anything else cannot be recorded as a run source.
TRANSPORT_SOURCES = ("interface", "double")

# The environment variables this adapter reads, and nothing else.
ENV_ENDPOINT = "TERA_JEV_ENDPOINT"
ENV_API_KEY = "TERA_JEV_API_KEY"
ENV_TIMEOUT_S = "TERA_JEV_TIMEOUT_S"
ENV_MAX_ATTEMPTS = "TERA_JEV_MAX_ATTEMPTS"
ENV_MAX_BATCH_SECONDS = "TERA_JEV_MAX_BATCH_SECONDS"

# The single run source that means the interface was never reached.
UNAVAILABLE_SOURCE = "unavailable"

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


def _require(condition, code, message):
    if not condition:
        raise JevAdapterError(code, message)


def _finite(value):
    """True for a finite int/float without importing the math module."""
    return value == value and value not in (float("inf"), float("-inf"))


def _milliseconds(seconds):
    """Whole milliseconds from a clock delta, rounded to nearest and never negative."""
    return max(0, int(round(seconds * 1000)))


class JevAdapterError(ValueError):
    """A rejected adapter input or record; inspect code, not message text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class JevTransportError(Exception):
    """One transport failure with a stable code from TRANSPORT_ERROR_CODES."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _url_host(endpoint):
    """The authority host of an absolute URL, lowercased, without userinfo or port."""
    _, separator, remainder = endpoint.partition("://")
    if not separator:
        return ""
    authority = remainder.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        return host.split("]", 1)[0].lower() + "]"
    return host.rsplit(":", 1)[0].lower() if ":" in host else host.lower()


def _check_endpoint(endpoint):
    """https anywhere, or plain http only on the loopback host; the value is never echoed."""
    problem = (f"{ENV_ENDPOINT} must be an absolute https:// URL, or an http:// URL whose host is"
               " localhost or 127.0.0.1.")
    _require(type(endpoint) is str and bool(endpoint.strip()), "invalid_configuration", problem)
    _require(not any(character.isspace() for character in endpoint), "invalid_configuration",
             problem)
    scheme, separator, _ = endpoint.partition("://")
    host = _url_host(endpoint)
    _require(bool(separator) and bool(host), "invalid_configuration", problem)
    if scheme.lower() == "https":
        return
    _require(scheme.lower() == "http" and host in _LOOPBACK_HOSTS, "invalid_configuration",
             problem)


def _bounded_number(label, value, low, high):
    message = f"{label} must be a finite number in [{low}, {high}]."
    if type(value) is bool or type(value) not in (int, float):
        raise JevAdapterError("invalid_configuration", message)
    if not _finite(value) or not (low <= value <= high):
        raise JevAdapterError("invalid_configuration", message)


def _bounded_count(label, value, low, high):
    if type(value) is not int or not (low <= value <= high):
        raise JevAdapterError("invalid_configuration",
                              f"{label} must be a whole number in [{low}, {high}].")


def _positive_number(label, value):
    message = f"{label} must be a positive finite number."
    if type(value) is bool or type(value) not in (int, float):
        raise JevAdapterError("invalid_configuration", message)
    if not _finite(value) or value <= 0:
        raise JevAdapterError("invalid_configuration", message)


def _environment(values):
    if not hasattr(values, "get"):
        raise JevAdapterError("invalid_configuration", "environ must be a mapping of names.")
    return values


def _env_number(values, name, default, low, high):
    raw = values.get(name)
    if raw is None:
        return default
    if type(raw) is not str or not raw.strip():
        raise JevAdapterError("invalid_configuration",
                              f"{name} must be a finite number in [{low}, {high}].")
    try:
        parsed = float(raw.strip())
    except (TypeError, ValueError) as error:
        raise JevAdapterError("invalid_configuration",
                              f"{name} must be a finite number in [{low}, {high}].") from error
    _bounded_number(name, parsed, low, high)
    return parsed


def _env_count(values, name, default, low, high):
    raw = values.get(name)
    if raw is None:
        return default
    if type(raw) is not str or not raw.strip():
        raise JevAdapterError("invalid_configuration",
                              f"{name} must be a whole number in [{low}, {high}].")
    try:
        parsed = int(raw.strip())
    except (TypeError, ValueError) as error:
        raise JevAdapterError("invalid_configuration",
                              f"{name} must be a whole number in [{low}, {high}].") from error
    _bounded_count(name, parsed, low, high)
    return parsed


@dataclass(frozen=True)
class JevCredentials:
    """One endpoint and one key, read from the environment and never rendered.

    Both values are trimmed; a blank value counts as absent. The endpoint must be https, or
    plain http on localhost/127.0.0.1, and the key is rendered as <redacted> everywhere.
    """

    endpoint: str
    api_key: str

    def __post_init__(self):
        _check_endpoint(self.endpoint)
        _require(type(self.api_key) is str and bool(self.api_key.strip()),
                 "invalid_configuration", f"{ENV_API_KEY} must be nonblank text.")

    def __repr__(self):
        return "JevCredentials(endpoint=<redacted>, api_key=<redacted>)"

    def __str__(self):
        return self.__repr__()

    @classmethod
    def from_env(cls, environ=None):
        """The configured credentials, or None unless both variables are non-blank."""
        values = _environment(os.environ if environ is None else environ)
        endpoint = values.get(ENV_ENDPOINT)
        api_key = values.get(ENV_API_KEY)
        if type(endpoint) is not str or not endpoint.strip():
            return None
        if type(api_key) is not str or not api_key.strip():
            return None
        return cls(endpoint=endpoint.strip(), api_key=api_key.strip())


@dataclass(frozen=True)
class JevAdapterConfig:
    """The bounded budget of one call; every field is published and enforced before a send."""

    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_s: float = DEFAULT_BACKOFF_S
    max_backoff_s: float = MAX_BACKOFF_S
    max_batch_seconds: float = DEFAULT_MAX_BATCH_SECONDS
    max_batch_size: int = MAX_BATCH_SIZE

    def __post_init__(self):
        _bounded_number("timeout_s", self.timeout_s, MIN_TIMEOUT_S, MAX_TIMEOUT_S)
        _bounded_count("max_attempts", self.max_attempts, MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS)
        _positive_number("backoff_s", self.backoff_s)
        _positive_number("max_backoff_s", self.max_backoff_s)
        _bounded_number("max_batch_seconds", self.max_batch_seconds, MIN_MAX_BATCH_SECONDS,
                        MAX_MAX_BATCH_SECONDS)
        _bounded_count("max_batch_size", self.max_batch_size, 1, MAX_BATCH_SIZE)

    @classmethod
    def from_env(cls, environ=None):
        """This configuration with the three documented environment overrides applied."""
        values = _environment(os.environ if environ is None else environ)
        return cls(
            timeout_s=_env_number(values, ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S,
                                  MAX_TIMEOUT_S),
            max_attempts=_env_count(values, ENV_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS,
                                    MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS),
            max_batch_seconds=_env_number(values, ENV_MAX_BATCH_SECONDS,
                                          DEFAULT_MAX_BATCH_SECONDS, MIN_MAX_BATCH_SECONDS,
                                          MAX_MAX_BATCH_SECONDS))


@dataclass(frozen=True)
class JevScoringRequest:
    """One caller identity paired with one #13 question record."""

    request_id: str
    question: JevQuestion | UnavailableQuestion


@dataclass(frozen=True)
class JevOutcome:
    """One recorded result for one requested question; the caller's ID is never replaced.

    question_id is the asked question's own digest and stays None for a question this product
    refused to ask, because an UnavailableQuestion has no digest. attempts is the exact number
    of sends made for this request.
    """

    request_id: str
    question_id: str | None
    dimension: str
    state: str
    code: str | None
    attempts: int
    judgment: JevJudgment | None
    elapsed_ms: int

    def __post_init__(self):
        _require(type(self.request_id) is str and bool(self.request_id.strip()),
                 "invalid_configuration", "A recorded request_id must be nonblank text.")
        _require(self.question_id is None or (type(self.question_id) is str
                                             and bool(self.question_id.strip())),
                 "invalid_configuration", "A recorded question_id must be nonblank text or None.")
        _require(type(self.dimension) is str and bool(self.dimension.strip()),
                 "invalid_configuration", "A recorded dimension must be nonblank text.")
        _require(self.state in OUTCOME_STATES, "invalid_configuration",
                 f"Unknown outcome state: {self.state!r}.")
        _require(type(self.attempts) is int and self.attempts >= 0, "invalid_configuration",
                 "attempts must be a nonnegative whole number.")
        _require(type(self.elapsed_ms) is int and self.elapsed_ms >= 0, "invalid_configuration",
                 "elapsed_ms must be a nonnegative whole number.")
        if self.state in ("judged", "abstained"):
            _require(type(self.judgment) is JevJudgment and self.code is None,
                     "invalid_configuration",
                     f"A {self.state} outcome carries a judgment and no code.")
            _require(self.attempts >= 1, "invalid_configuration",
                     f"A {self.state} outcome carries at least one attempt.")
            if self.state == "abstained":
                _require(self.judgment.label is None
                         and self.judgment.unavailable_reason is not None,
                         "invalid_configuration", "An abstention carries no label and a reason.")
        elif self.state == "not_asked":
            _require(self.code in QUESTION_UNAVAILABLE_CODES and self.attempts == 0
                     and self.judgment is None and self.question_id is None,
                     "invalid_configuration",
                     "A not_asked outcome carries a #13 question code, no attempt and no digest.")
        elif self.state == "not_attempted":
            _require(self.code in NOT_ATTEMPTED_CODES and self.judgment is None,
                     "invalid_configuration",
                     "A not_attempted outcome carries a not-attempted code and no judgment.")
        elif self.state == "invalid_result":
            _require(self.code in RESPONSE_ERROR_CODES and self.attempts >= 1
                     and self.judgment is None, "invalid_configuration",
                     "An invalid_result outcome carries a #13 response code and no judgment.")
        elif self.state == "service_error":
            _require(self.code in TRANSPORT_ERROR_CODES and self.code != "timeout"
                     and self.attempts >= 1 and self.judgment is None, "invalid_configuration",
                     "A service_error outcome carries a transport code other than timeout.")
        elif self.state == "timed_out":
            _require(self.code == "timeout" and self.attempts >= 1 and self.judgment is None,
                     "invalid_configuration",
                     "A timed_out outcome carries the timeout code and no judgment.")
        else:
            _require(self.code in UNAVAILABLE_CODES and self.attempts == 0
                     and self.judgment is None, "invalid_configuration",
                     "An unavailable outcome carries an unavailable code and no attempt.")

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "question_id": self.question_id,
            "dimension": self.dimension,
            "state": self.state,
            "code": self.code,
            "attempts": self.attempts,
            "judgment": None if self.judgment is None else self.judgment.to_dict(),
            "elapsed_ms": self.elapsed_ms,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)

    @classmethod
    def from_dict(cls, payload):
        _exact_object(payload, _OUTCOME_FIELDS, "JevOutcome")
        judgment = payload["judgment"]
        if judgment is not None:
            _require(type(judgment) is dict, "invalid_configuration",
                     "A recorded judgment must be an object or null.")
            judgment = JevJudgment.from_dict(judgment)
        return cls(request_id=payload["request_id"], question_id=payload["question_id"],
                   dimension=payload["dimension"], state=payload["state"], code=payload["code"],
                   attempts=payload["attempts"], judgment=judgment,
                   elapsed_ms=payload["elapsed_ms"])

    @classmethod
    def from_json(cls, value):
        return cls.from_dict(_decoded(value, "JevOutcome"))


_OUTCOME_FIELDS = ("request_id", "question_id", "dimension", "state", "code", "attempts",
                   "judgment", "elapsed_ms")
_RUN_FIELDS = ("adapter_version", "source", "interface_name", "prompt_version", "model_versions",
               "cancelled", "outcomes", "elapsed_ms")


def _exact_object(payload, names, label):
    _require(type(payload) is dict and set(payload) == set(names), "invalid_configuration",
             f"A {label} record must carry exactly its documented fields.")


def _decoded(value, label):
    _require(type(value) is str, "invalid_configuration", f"A {label} record must be JSON text.")
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise JevAdapterError("invalid_configuration", f"Invalid {label} JSON.") from error


@dataclass(frozen=True)
class JevScoringRun:
    """What one call recorded for #15 and #19; a later task consumes it without a service call."""

    adapter_version: str
    source: str
    interface_name: str | None
    prompt_version: str
    model_versions: tuple[str, ...]
    cancelled: bool
    outcomes: tuple[JevOutcome, ...]
    elapsed_ms: int

    def __post_init__(self):
        _require(self.adapter_version == ADAPTER_VERSION, "invalid_configuration",
                 "A run carries this adapter's own version.")
        _require(self.source in TRANSPORT_SOURCES or self.source == UNAVAILABLE_SOURCE,
                 "invalid_configuration", f"Unknown run source: {self.source!r}.")
        _require(self.prompt_version == PROMPT_VERSION, "invalid_configuration",
                 "A run carries the #13 prompt version it asked with.")
        _require(self.interface_name is None or (type(self.interface_name) is str
                                                and bool(self.interface_name.strip())),
                 "invalid_configuration", "interface_name must be nonblank text or None.")
        _require(type(self.model_versions) is tuple
                 and all(type(item) is str and bool(item.strip())
                         for item in self.model_versions)
                 and list(self.model_versions) == sorted(set(self.model_versions)),
                 "invalid_configuration", "model_versions must be sorted unique nonblank text.")
        _require(type(self.cancelled) is bool, "invalid_configuration",
                 "cancelled must be true or false.")
        _require(type(self.outcomes) is tuple
                 and all(type(item) is JevOutcome for item in self.outcomes),
                 "invalid_configuration", "outcomes must be a tuple of JevOutcome records.")
        _require(type(self.elapsed_ms) is int and self.elapsed_ms >= 0, "invalid_configuration",
                 "elapsed_ms must be a nonnegative whole number.")

    def to_dict(self):
        return {
            "adapter_version": self.adapter_version,
            "source": self.source,
            "interface_name": self.interface_name,
            "prompt_version": self.prompt_version,
            "model_versions": list(self.model_versions),
            "cancelled": self.cancelled,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "elapsed_ms": self.elapsed_ms,
        }

    def to_json(self):
        return json.dumps(self.to_dict(), allow_nan=False, sort_keys=True)

    @classmethod
    def from_dict(cls, payload):
        _exact_object(payload, _RUN_FIELDS, "JevScoringRun")
        outcomes = payload["outcomes"]
        versions = payload["model_versions"]
        _require(type(outcomes) is list, "invalid_configuration", "outcomes must be an array.")
        _require(type(versions) is list and all(type(item) is str for item in versions),
                 "invalid_configuration", "model_versions must be an array of text.")
        return cls(adapter_version=payload["adapter_version"], source=payload["source"],
                   interface_name=payload["interface_name"],
                   prompt_version=payload["prompt_version"], model_versions=tuple(versions),
                   cancelled=payload["cancelled"],
                   outcomes=tuple(JevOutcome.from_dict(item) for item in outcomes),
                   elapsed_ms=payload["elapsed_ms"])

    @classmethod
    def from_json(cls, value):
        return cls.from_dict(_decoded(value, "JevScoringRun"))


class JevTransport:
    """The one bounded outbound seam: hand one payload over once and answer with text."""

    interface_name = None
    source = None

    def send(self, payload_json, *, timeout_s):
        """Send one payload and return the response text, or raise JevTransportError."""
        raise NotImplementedError


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect, so the payload cannot move to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpJevTransport(JevTransport):
    """The assumed wire mapping: an HTTPS POST of the payload with a bearer credential.

    Marked assumed in _docs/jev-adapter.md until a credentialed run observes otherwise.
    """

    interface_name = "typesafe-jev-http"
    source = "interface"

    def __init__(self, credentials, *, opener=None):
        _require(type(credentials) is JevCredentials, "invalid_configuration",
                 "HttpJevTransport needs JevCredentials.")
        if opener is not None:
            _require(callable(getattr(opener, "open", None)), "invalid_configuration",
                     "opener must provide a callable open attribute.")
        self.credentials = credentials
        self.opener = (urllib.request.build_opener(NoRedirectHandler) if opener is None
                       else opener)

    def send(self, payload_json, *, timeout_s):
        _require(type(payload_json) is str, "invalid_configuration",
                 "A transport send needs the payload as JSON text.")
        request = urllib.request.Request(self.credentials.endpoint,
                                          data=payload_json.encode("utf-8"), method="POST")
        request.headers["Content-Type"] = "application/json"
        request.headers["Authorization"] = "Bearer " + self.credentials.api_key
        opener_call = getattr(self.opener, "open")
        try:
            response = opener_call(request, timeout=timeout_s)
        except urllib.error.HTTPError as error:
            raise JevTransportError(_status_code(error.code),
                                    "The Jev interface rejected the request.") from error
        except TimeoutError as error:
            raise JevTransportError("timeout", "The Jev interface timed out.") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise JevTransportError("timeout", "The Jev interface timed out.") from error
            raise JevTransportError("connection_failed",
                                    "The Jev interface connection failed.") from error
        except OSError as error:
            raise JevTransportError("connection_failed",
                                    "The Jev interface connection failed.") from error
        try:
            raw = response.read()
        except TimeoutError as error:
            raise JevTransportError("timeout", "The Jev interface timed out.") from error
        except OSError as error:
            raise JevTransportError("connection_failed",
                                    "The Jev interface connection failed.") from error
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise JevTransportError("service_error",
                                    "The Jev response body is not UTF-8 text.") from error


def _status_code(status):
    if status in (401, 403):
        return "invalid_credentials"
    if status == 429:
        return "rate_limited"
    if status in (503, 504):
        return "service_unavailable"
    return "service_error"


def _record(request, state, code, attempts, judgment, elapsed_ms):
    question = request.question
    return JevOutcome(
        request_id=request.request_id,
        question_id=question.question_id if type(question) is JevQuestion else None,
        dimension=question.dimension, state=state, code=code, attempts=attempts,
        judgment=judgment, elapsed_ms=elapsed_ms)


def _validated_requests(requests, config):
    """The batch, or the adapter error that rejects it before any send."""
    _require(type(requests) in (list, tuple), "invalid_requests",
             "requests must be a list or tuple of JevScoringRequest.")
    _require(len(requests) <= config.max_batch_size, "batch_too_large",
             f"At most {config.max_batch_size} questions are sent per call.")
    seen = set()
    checked = []
    for request in requests:
        _require(type(request) is JevScoringRequest, "invalid_requests",
                 "Every request must be a JevScoringRequest.")
        _require(type(request.request_id) is str and bool(request.request_id.strip()),
                 "invalid_requests", "Every request needs a nonblank request_id.")
        _require(request.request_id not in seen, "duplicate_request_id",
                 "A batch cannot repeat one request_id.")
        question = request.question
        _require(type(question) in (JevQuestion, UnavailableQuestion), "invalid_requests",
                 "Every question must be a #13 JevQuestion or UnavailableQuestion.")
        seen.add(request.request_id)
        checked.append(request)
    return checked


def score_questions(requests, *, transport=None, config=None, credentials=None, environ=None,
                    cancelled=None, sleep=None, monotonic=None):
    """Score the given questions with one transport and record one JevOutcome per request.

    Validation happens before anything is sent: a bad configuration, a batch larger than
    config.max_batch_size, a repeated or blank request_id, or a value that is not a
    JevScoringRequest raises JevAdapterError with a code in ADAPTER_ERROR_CODES and returns no
    partial JevScoringRun. With no transport and no credentials nothing is sent and every
    request records the unavailable outcome instead. The adapter is sequential, never blocks on
    its own, reads time only through monotonic, waits only through sleep, and returns one
    JevScoringRun whatever the interface did.
    """
    if config is None:
        config = JevAdapterConfig()
    else:
        _require(type(config) is JevAdapterConfig, "invalid_configuration",
                 "config must be a JevAdapterConfig.")
    for name, injected in (("cancelled", cancelled), ("sleep", sleep), ("monotonic", monotonic)):
        _require(injected is None or callable(injected), "invalid_configuration",
                 f"{name} must be callable or None.")
    _require(credentials is None or type(credentials) is JevCredentials, "invalid_configuration",
             "credentials must be JevCredentials or None.")
    checked = _validated_requests(requests, config)
    clock = monotonic if monotonic is not None else time.monotonic
    delay_call = sleep if sleep is not None else time.sleep
    start = clock()
    if transport is None:
        if credentials is None:
            credentials = JevCredentials.from_env(environ)
        if credentials is None:
            outcomes = tuple(_record(request, "unavailable", "credentials_absent", 0, None,
                                     _milliseconds(clock() - start)) for request in checked)
            return JevScoringRun(adapter_version=ADAPTER_VERSION, source=UNAVAILABLE_SOURCE,
                                 interface_name=None, prompt_version=PROMPT_VERSION,
                                 model_versions=(), cancelled=False, outcomes=outcomes,
                                 elapsed_ms=_milliseconds(clock() - start))
        transport = HttpJevTransport(credentials)
    else:
        _require(getattr(transport, "source", None) in TRANSPORT_SOURCES, "invalid_configuration",
                 "A transport must declare the source it records.")
        _require(callable(getattr(transport, "send", None)), "invalid_configuration",
                 "A transport must provide a callable send attribute.")
    outcomes = []
    blocked = None
    for request in checked:
        question = request.question
        if blocked is not None:
            outcomes.append(_record(request, "not_attempted", blocked, 0, None,
                                    _milliseconds(clock() - start)))
            continue
        if cancelled is not None and cancelled():
            blocked = "cancelled"
            outcomes.append(_record(request, "not_attempted", blocked, 0, None,
                                    _milliseconds(clock() - start)))
            continue
        if type(question) is UnavailableQuestion:
            outcomes.append(_record(request, "not_asked", question.code, 0, None,
                                    _milliseconds(clock() - start)))
            continue
        payload_json = question.to_json()
        attempts = 0
        while True:
            elapsed = clock() - start
            if elapsed >= config.max_batch_seconds:
                blocked = "batch_deadline_exceeded"
                outcomes.append(_record(request, "not_attempted", blocked, attempts, None,
                                        _milliseconds(clock() - start)))
                break
            if cancelled is not None and cancelled():
                blocked = "cancelled"
                outcomes.append(_record(request, "not_attempted", blocked, attempts, None,
                                        _milliseconds(clock() - start)))
                break
            timeout_s = min(config.timeout_s, config.max_batch_seconds - elapsed)
            attempts += 1
            try:
                text = transport.send(payload_json, timeout_s=timeout_s)
            except JevTransportError as error:
                code = error.code if error.code in TRANSPORT_ERROR_CODES else "service_error"
                if code not in RETRYABLE_TRANSPORT_CODES or attempts >= config.max_attempts:
                    state = "timed_out" if code == "timeout" else "service_error"
                    outcomes.append(_record(request, state, code, attempts, None,
                                            _milliseconds(clock() - start)))
                    break
                if cancelled is not None and cancelled():
                    blocked = "cancelled"
                    outcomes.append(_record(request, "not_attempted", blocked, attempts, None,
                                            _milliseconds(clock() - start)))
                    break
                delay_call(min(config.backoff_s * BACKOFF_MULTIPLIER ** (attempts - 1),
                               config.max_backoff_s))
                continue
            try:
                judgment = validate_response(question, text)
            except JevResponseError as error:
                outcomes.append(_record(request, "invalid_result", error.code, attempts, None,
                                        _milliseconds(clock() - start)))
                break
            state = "abstained" if judgment.label is None else "judged"
            outcomes.append(_record(request, state, None, attempts, judgment,
                                    _milliseconds(clock() - start)))
            break
    versions = sorted({outcome.judgment.model_version for outcome in outcomes
                       if outcome.state in ("judged", "abstained")
                       and outcome.judgment.model_version.strip()})
    return JevScoringRun(adapter_version=ADAPTER_VERSION, source=transport.source,
                         interface_name=getattr(transport, "interface_name", None),
                         prompt_version=PROMPT_VERSION, model_versions=tuple(versions),
                         cancelled=blocked == "cancelled", outcomes=tuple(outcomes),
                         elapsed_ms=_milliseconds(clock() - start))
