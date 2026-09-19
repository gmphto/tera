"""Deterministic TypeSafe Jev contract double; see _docs/jev-adapter.md.

JevContractDouble implements the adapter's JevTransport seam without a service, a socket, a
clock, a sleep, a file or randomness. It parses the payload it receives, echoes that payload's
question_id, dimension and prompt_version in the response it builds, and answers from a script
keyed by question_id. It can produce a labeled judgment from five probabilities given in contract
Label order, an abstention, a malformed text returned verbatim so #13 rejects it, and a scripted
transport error. It is contract evidence only: a source of "double" outcome must never be
reported as live Jev lift.
"""

from __future__ import annotations

import json

from backend.intelligence.decisions import MODEL_ABSTAINED, PROBABILITY_LABELS
from backend.intelligence.jev import TRANSPORT_ERROR_CODES, JevTransport, JevTransportError

# The interface name and record source every double outcome carries.
DOUBLE_INTERFACE_NAME = "jev-contract-double"
DOUBLE_SOURCE = "double"

# The synthetic model version a double uses unless a script entry names another one.
DEFAULT_MODEL_VERSION = "synthetic-jev-double-v1"

# The one fixed valid synthetic judgment used when no script entry and no default exist.
DEFAULT_PROBABILITIES = (0.05, 0.05, 0.10, 0.60, 0.20)
DEFAULT_CONFIDENCE = 0.60

# The four documented entry kinds; an entry carries exactly one of them.
ENTRY_KINDS = ("judgment", "abstain", "text", "error")

_ECHOED_FIELDS = ("question_id", "dimension", "prompt_version")


def _entry(value, label):
    """One validated (kind, spec) script entry, or a construction-time rejection."""
    if type(value) is not dict or len(value) != 1:
        raise ValueError(f"{label} must be an object carrying exactly one of {ENTRY_KINDS}.")
    kind = next(iter(value))
    if kind not in ENTRY_KINDS:
        raise ValueError(f"{label} must carry exactly one of {ENTRY_KINDS}.")
    spec = value[kind]
    if kind == "error":
        if spec not in TRANSPORT_ERROR_CODES:
            raise ValueError(f"{label} error code must be one of {TRANSPORT_ERROR_CODES}.")
        return kind, spec
    if kind == "text":
        if type(spec) is not str:
            raise ValueError(f"{label} text must be response text.")
        return kind, spec
    if type(spec) is not dict:
        raise ValueError(f"{label} {kind} must be an object.")
    if set(spec) - {"probabilities", "confidence", "model_version"}:
        raise ValueError(f"{label} {kind} carries only probabilities, confidence and"
                         " model_version.")
    model_version = spec.get("model_version")
    if model_version is not None and (type(model_version) is not str
                                      or not model_version.strip()):
        raise ValueError(f"{label} model_version must be nonblank text.")
    if kind == "abstain":
        return kind, {"model_version": model_version}
    probabilities = spec.get("probabilities")
    confidence = spec.get("confidence", DEFAULT_CONFIDENCE)
    if (type(probabilities) is not list and type(probabilities) is not tuple) \
            or len(probabilities) != len(PROBABILITY_LABELS):
        raise ValueError(f"{label} judgment needs one probability per contract label.")
    for probability in probabilities:
        if type(probability) is bool or type(probability) not in (int, float) \
                or not 0 <= probability <= 1:
            raise ValueError(f"{label} probabilities must be numbers in [0, 1].")
    if type(confidence) is bool or type(confidence) not in (int, float) \
            or not 0 <= confidence <= 1:
        raise ValueError(f"{label} confidence must be a number in [0, 1].")
    return kind, {"probabilities": tuple(probabilities), "confidence": confidence,
                  "model_version": model_version}


def _entries(value, label):
    """One scripted answer sequence, accepting a single entry as a one-entry sequence."""
    if type(value) is list:
        if not value:
            raise ValueError(f"{label} must script at least one entry.")
        return [(_entry(item, label)) for item in value]
    return [_entry(value, label)]


_DEFAULT_ENTRY = ("judgment", {"probabilities": DEFAULT_PROBABILITIES,
                               "confidence": DEFAULT_CONFIDENCE, "model_version": None})


class JevContractDouble(JevTransport):
    """A deterministic scripted Jev seam: same inputs, same call log, same answers."""

    interface_name = DOUBLE_INTERFACE_NAME
    source = DOUBLE_SOURCE

    def __init__(self, model_version=DEFAULT_MODEL_VERSION, default=None, script=None):
        if type(model_version) is not str or not model_version.strip():
            raise ValueError("model_version must be nonblank text.")
        self.model_version = model_version
        self.default = None if default is None else _entry(default, "default")
        self.script = {}
        if script is not None:
            if not hasattr(script, "items"):
                raise ValueError("script must be a mapping keyed by question_id.")
            for key, value in script.items():
                if type(key) is not str or not key.strip():
                    raise ValueError("Every script key must be a nonblank question_id.")
                self.script[key] = _entries(value, f"script[{key}]")
        self.calls = []
        self._used = {}

    def _payload(self, payload_json):
        if type(payload_json) is not str:
            raise ValueError("The double receives the adapter's payload as JSON text.")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as error:
            raise ValueError("The double receives the adapter's payload as JSON text.") from error
        if type(payload) is not dict:
            raise ValueError("The double receives the adapter's payload object.")
        for field in _ECHOED_FIELDS:
            if type(payload.get(field)) is not str or not payload[field].strip():
                raise ValueError(f"The received payload needs a nonblank {field}.")
        return payload

    def _next(self, question_id):
        entries = self.script.get(question_id)
        if entries is None:
            return self.default if self.default is not None else _DEFAULT_ENTRY
        index = self._used.get(question_id, 0)
        self._used[question_id] = index + 1
        return entries[min(index, len(entries) - 1)]

    def _model_version(self, spec):
        return spec.get("model_version") or self.model_version

    def send(self, payload_json, *, timeout_s):
        """Answer one payload from the script and record the ordered call log entry."""
        payload = self._payload(payload_json)
        question_id = payload["question_id"]
        self.calls.append((payload_json, timeout_s))
        kind, spec = self._next(question_id)
        if kind == "error":
            raise JevTransportError(spec, f"The contract double was scripted to fail: {spec}.")
        if kind == "text":
            return spec
        model_version = self._model_version(spec)
        if kind == "abstain":
            body = {"question_id": question_id, "dimension": payload["dimension"], "label": None,
                    "probabilities": None, "confidence": None, "model_version": model_version,
                    "prompt_version": payload["prompt_version"],
                    "unavailable_reason": MODEL_ABSTAINED}
        else:
            probabilities = spec["probabilities"]
            label = PROBABILITY_LABELS[probabilities.index(max(probabilities))]
            body = {"question_id": question_id, "dimension": payload["dimension"], "label": label,
                    "probabilities": [{"label": name, "probability": value}
                                      for name, value in zip(PROBABILITY_LABELS,
                                                             probabilities)],
                    "confidence": spec["confidence"], "model_version": model_version,
                    "prompt_version": payload["prompt_version"], "unavailable_reason": None}
        return json.dumps(body, allow_nan=False, sort_keys=True)
