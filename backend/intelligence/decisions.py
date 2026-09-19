"""Strict validation of one Jev response into a schema-1 JevJudgment.

See _docs/jev-questions.md. This module is pure local validation: no transport,
no Jev call, no file access and no network. A response is a JSON object with
exactly the eight documented fields; it echoes the asked `question_id`,
`dimension` and `prompt_version`, carries either the five label probabilities
with the model's own confidence or an explicit `model_abstained` abstention, and
is converted into an existing `backend.contracts.JevJudgment` with no clamping,
rounding or substitution. A malformed response raises `JevResponseError` with a
stable `.code` from `RESPONSE_ERROR_CODES`, and an `UnavailableQuestion` can
never receive an accepted judgment. Nothing here reconciles a judgment with a
measured fact; #15 owns any weighting or thresholding.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import get_args

from backend.contracts import JevJudgment, Label, LabelProbability
from backend.intelligence.questions import DIMENSIONS, JevQuestion, UnavailableQuestion

# The single stable abstention token. No model-authored free text is ever stored.
MODEL_ABSTAINED = "model_abstained"

# The schema-1 contract's absolute tolerance for the five probabilities.
PROBABILITY_SUM_TOLERANCE = 1e-6

# The response object's exact fields, in documented order.
RESPONSE_FIELDS = ("question_id", "dimension", "label", "probabilities", "confidence",
                   "model_version", "prompt_version", "unavailable_reason")

# The five schema-1 label literals in the contract literal's own order.
PROBABILITY_LABELS = get_args(Label)

RESPONSE_ERROR_CODES = frozenset({
    "unexpected_field", "missing_field", "invalid_response", "invalid_question",
    "question_unavailable", "question_id_mismatch", "dimension_mismatch",
    "prompt_version_mismatch", "unsupported_dimension", "unsupported_label",
    "invalid_probabilities", "probability_out_of_range", "probability_sum",
    "label_probability_mismatch", "invalid_confidence", "invalid_abstention", "invalid_version",
})


class JevResponseError(ValueError):
    """A response that cannot become a schema-1 JevJudgment; inspect code, not message text."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _require(condition, code, message):
    if not condition:
        raise JevResponseError(code, message)


def _payload(response):
    """A response mapping, or JSON text parsed the way the schema-1 contract parses it."""
    if isinstance(response, Mapping):
        return dict(response)
    if type(response) is not str:
        raise JevResponseError("invalid_response", "Response must be JSON text or a mapping.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise JevResponseError("invalid_response", f"Duplicate JSON field: {key}.")
            result[key] = item
        return result

    def constant(value):
        raise JevResponseError("invalid_response", f"Non-finite JSON number: {value}.")

    try:
        parsed = json.loads(response, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, json.JSONDecodeError) as error:
        raise JevResponseError("invalid_response", "Invalid JSON response.") from error
    if type(parsed) is not dict:
        raise JevResponseError("invalid_response", "Response JSON must be an object.")
    return parsed


def _confidence(value):
    """The model's self-reported confidence, recorded unchanged and never derived."""
    if type(value) is bool or type(value) not in (int, float):
        raise JevResponseError("invalid_confidence", "Confidence must be a number.")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    _require(finite and 0 <= value <= 1, "invalid_confidence",
             "Confidence must be a finite number in [0, 1].")
    return value


def _probabilities(value, label):
    """Five labels exactly once, each in [0, 1] summing to 1, ordered by the contract."""
    if not isinstance(value, (list, tuple)) or len(value) != len(PROBABILITY_LABELS):
        raise JevResponseError("invalid_probabilities",
                               "Probabilities must list each of the five labels exactly once.")
    report = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item.keys()) != {"label", "probability"}:
            raise JevResponseError("invalid_probabilities",
                                   "Each probability entry needs exactly label and probability.")
        name, probability = item["label"], item["probability"]
        if type(name) is not str or name not in PROBABILITY_LABELS or name in report:
            raise JevResponseError("invalid_probabilities",
                                   "Probabilities must list each of the five labels exactly once.")
        report[name] = probability
    if set(report) != set(PROBABILITY_LABELS):
        raise JevResponseError("invalid_probabilities",
                               "Probabilities must list each of the five labels exactly once.")
    for probability in report.values():
        if type(probability) is bool or type(probability) not in (int, float):
            raise JevResponseError("invalid_probabilities", "Every probability must be a number.")
    for probability in report.values():
        try:
            finite = math.isfinite(probability)
        except OverflowError:
            finite = False
        if not finite or not 0 <= probability <= 1:
            raise JevResponseError("probability_out_of_range",
                                   "Every probability must be a finite number in [0, 1].")
    _require(math.isclose(sum(report.values()), 1, rel_tol=0, abs_tol=PROBABILITY_SUM_TOLERANCE),
             "probability_sum", "The five probabilities must sum to 1 within 1e-6.")
    _require(report[label] == max(report.values()), "label_probability_mismatch",
             "The label must be one of the labels attaining the highest probability.")
    return tuple(LabelProbability(label=name, probability=report[name])
                 for name in PROBABILITY_LABELS)


def _abstention(payload):
    _require(payload["unavailable_reason"] == MODEL_ABSTAINED
             and payload["confidence"] is None
             and payload["probabilities"] in (None, [], ()),
             "invalid_abstention",
             "An abstention carries exactly the model_abstained reason, no confidence and no"
             " probabilities.")


def validate_response(question, response) -> JevJudgment:
    """Validate one response for one asked question into a schema-1 JevJudgment.

    `response` may be JSON text or an already-parsed mapping. The response must
    echo the asked `question_id`, `dimension` and `prompt_version`, contain
    exactly the eight documented fields, and carry either a label with all five
    probabilities and a confidence or the `model_abstained` abstention. Every
    failure raises JevResponseError; a question that was not asked
    (`UnavailableQuestion`) can never receive an accepted judgment.
    """
    if type(question) is UnavailableQuestion:
        raise JevResponseError("question_unavailable",
                               f"Question {question.dimension!r} was not asked"
                               f" ({question.code}); it cannot receive a judgment.")
    if type(question) is not JevQuestion:
        raise JevResponseError("invalid_question", "Expected a JevQuestion.")
    payload = _payload(response)
    unknown = payload.keys() - set(RESPONSE_FIELDS)
    _require(not unknown, "unexpected_field", f"Unexpected response fields: {sorted(unknown)}.")
    missing = set(RESPONSE_FIELDS) - payload.keys()
    _require(not missing, "missing_field", f"Missing response fields: {sorted(missing)}.")
    _require(payload["question_id"] == question.question_id, "question_id_mismatch",
             "Response does not echo the asked question_id.")
    dimension = payload["dimension"]
    _require(dimension in DIMENSIONS, "unsupported_dimension",
             f"Unsupported response dimension: {dimension!r}.")
    _require(dimension == question.dimension, "dimension_mismatch",
             "Response does not echo the asked dimension.")
    _require(payload["prompt_version"] == question.prompt_version, "prompt_version_mismatch",
             "Response does not echo the asked prompt_version.")
    model_version = payload["model_version"]
    _require(type(model_version) is str and bool(model_version.strip()), "invalid_version",
             "model_version must be nonblank text.")
    label = payload["label"]
    if label is None:
        _abstention(payload)
        return JevJudgment(dimension=question.dimension, label=None, confidence=None, probabilities=(),
                           model_version=model_version, prompt_version=question.prompt_version,
                           unavailable_reason=MODEL_ABSTAINED)
    _require(type(label) is str and label in PROBABILITY_LABELS, "unsupported_label",
             f"Unsupported label: {label!r}.")
    _require(payload["unavailable_reason"] is None, "invalid_abstention",
             "A labeled response must not carry an unavailable_reason.")
    confidence = _confidence(payload["confidence"])
    probabilities = _probabilities(payload["probabilities"], label)
    return JevJudgment(dimension=question.dimension, label=label, confidence=confidence,
                       probabilities=probabilities, model_version=model_version,
                       prompt_version=question.prompt_version)
