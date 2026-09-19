"""Run the frozen four-arm ranking comparison and render the committed #19 report.

This module is issue #19's whole runner. It consumes the frozen protocol document, the
landed arms (#12 `backend.palette.ranking.rank_candidates`, #15
`backend.palette.ranking.rank_hybrid`, #13 `backend.intelligence.questions.build_question`
and #14 `backend.intelligence.jev`) and the #65/#17/#18 private artifacts, and it produces
one committed report plus one private machine-readable run.

Declared inputs, each a CLI path with the default shown (the flag overrides it):

  _docs/evaluation-protocol.md                              --protocol-doc
  .local-evaluation/prepared-dataset.json                   --dataset
  .local-evaluation/pair-rating/split-manifest.json         --split-manifest
  .local-evaluation/pair-rating/pairs.json                  --pairs
  .local-evaluation/pair-rating/assignment.json             --assignment
  .local-evaluation/pair-rating/sessions/                   --sessions
  .local-evaluation/analysis/kicks-manifest.json            --analysis-manifest
  .local-evaluation/analysis/basses-manifest.json           --analysis-manifest
  #14 JevScoringRun documents                                --jev-run (repeatable)
  tuning rating set, only under --tuning-claim              --tuning-ratings

Files this module writes -- the only files it ever creates, replaces or deletes:

  _docs/ranking-comparison-19.md                            the committed report
  .local-evaluation/ranking-comparison/runs/<run_key>/run.json
  .local-evaluation/ranking-comparison/runs/<run_key>/records.json
  .local-evaluation/ranking-comparison/runs/<run_key>/latency/<request>.json
  .local-evaluation/ranking-comparison/runs/<run_key>/latency/<request>.out.json
  .local-evaluation/ranking-comparison/tuning/<digest>.json   (only with --tuning-claim)

Nothing else is opened for writing. No audio is decoded here, no sample is invented and no
value in the committed report is synthesized: an absent or too-small input is recorded as
insufficient evidence with its exact shortfall, never as a pass, a fail, a zero or an
assumption. The runner reaches the network only through `backend.intelligence.jev` and only
when --jev-live is passed; without it the run is offline and reads recorded #14 runs.

Import list (deliberately free of `backend.analysis`, numpy, soundfile and every
audio-decoding module): argparse, collections, dataclasses, hashlib, json, math, os,
pathlib, platform, random, subprocess, sys, tempfile, time, types; backend.contracts;
backend.intelligence.questions; backend.intelligence.jev; backend.palette.compatibility;
backend.palette.ranking. The protocol table and the #18 session records are parsed by the
small readers below rather than by importing #17's module, so this runner pulls in no audio
reader. #17's own `python -m backend.evaluation.rating validate --session` is run as that
exact command line, once per session, with file-backed stdio.

Stable codes and their exit status (the code table this runner documents):

  exit 2, nothing written   protocol_version_mismatch, schema_mismatch,
                            split_manifest_digest_mismatch, pair_not_held_out,
                            unknown_sample_id, duplicate_pair, pair_unassigned,
                            rated_pair_not_sampled, multiple_analysis_versions,
                            missing_analysis_entry, analysis_entry_incomplete,
                            synthetic_fixture_record, dataset_version_mismatch,
                            candidate_set_mismatch, public_rating_source,
                            invalid_input_path, usage
  exit 1, report written    any evidence gap: a declared input is absent, or a count is
                            below its minimum (split_manifest_missing, pair_list_missing,
                            assignment_missing, ratings_missing, analysis_manifest_missing,
                            dataset_missing, pair_counts_below_minimum,
                            protocol_document_missing, below_minimum)
  exit 0                    every gate has data and every gate is supported or not supported

Preflight reports the nine recorded checks in this order, one JSON line each:
protocol_document, split_manifest, pair_list, pair_membership, evaluator_assignment,
pair_counts, rated_pairs, analysis_manifests, dataset_provenance.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import tempfile
import time

from backend.contracts import Sample
from backend.intelligence.jev import (ADAPTER_VERSION, JevAdapterConfig, JevScoringRequest,
                                      JevScoringRun, score_questions)
from backend.intelligence.questions import DIMENSIONS, PROMPT_VERSION, build_question
from backend.palette.compatibility import (Availability, FilterInputError, FilterPolicy,
                                           filter_candidates)
from backend.palette.ranking import (DEFAULT_WEIGHT_TABLE_ID, HYBRID_RANKING_VERSION,
                                     HYBRID_WEIGHT_TABLE_ID, JEV_ABSENT, JEV_LABEL_SCORES,
                                     MODE_DSP_ONLY, MODE_HYBRID, RANKING_VERSION, RankingInputError,
                                     RankingPolicy, HybridEvidence, HybridPolicy, rank_candidates,
                                     rank_hybrid)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_DOCUMENT = Path("_docs") / "evaluation-protocol.md"
REPORT_PATH = Path("_docs") / "ranking-comparison-19.md"
PRIVATE_ROOT = Path(".local-evaluation") / "ranking-comparison"
RUNS_DIRECTORY = PRIVATE_ROOT / "runs"
TUNING_DIRECTORY = PRIVATE_ROOT / "tuning"
DEFAULT_DATASET = Path(".local-evaluation") / "prepared-dataset.json"
DEFAULT_PAIR_ROOT = Path(".local-evaluation") / "pair-rating"
DEFAULT_ANALYSIS_ROOT = Path(".local-evaluation") / "analysis"

SCHEMA_VERSION = "1.0"
FALLBACK_REASON = "hybrid_fell_back_to_dsp"
NO_LIVE_JEV_REASON = "no_live_jev_outcomes"
DOUBLE_NOT_EVIDENCE = "contract-double, not evidence"

# --- the frozen protocol constants this runner declares --------------------
# Every name below appears once, with the value and class the protocol document
# freezes. tests/test_comparison_protocol_conformance.py parses that document's
# frozen-constants table and fails, naming the constant, when one differs.

PROTOCOL_VERSION = "tera-eval-protocol-v1"
SPLIT_SEED = "tera-eval-split-16-v1"
PAIR_SAMPLER_SEED = "tera-eval-pairs-16-v1"
ASSIGNMENT_SEED = "tera-eval-assignment-16-v1"
ORDER_SEED = "tera-eval-order-16-v1"
RANDOM_ARM_SEED = "tera-eval-random-16-v1"
BOOTSTRAP_SEED = "tera-eval-bootstrap-16-v1"
TUNING_FRACTION = 0.25
MIN_POOL_KICKS = 80
MIN_POOL_BASSES = 8
MIN_EVALUATORS = 5
TARGET_EVALUATORS = 8
MIN_RATINGS_PER_PAIR = 2
TARGET_RATINGS_PER_PAIR = 3
MIN_HELDOUT_QUERIES = 60
TARGET_HELDOUT_QUERIES = 75
RATED_CANDIDATES_PER_QUERY = 5
MIN_HELDOUT_PAIRS = 300
MIN_TUNING_PAIRS = 40
MIN_PAIR_COVERAGE = 0.80
MIN_AGREEMENT_PAIRS = 40
MAX_MEAN_ABSOLUTE_DEVIATION = 1.00
MAX_RECOGNISED_RATE = 0.20
MIN_SCORED_SHARE_PER_QUERY = 0.50
MIN_RATED_CANDIDATES_FOR_TOP10_GATE = 11
MIN_LATENCY_REQUESTS_PER_ARM = 30
MIN_LATENCY_QUERY_KICKS = 10
BOOTSTRAP_RESAMPLES = 10000
MIN_PAIRWISE_ACCURACY = 0.60
TARGET_PAIRWISE_ACCURACY = 0.68
MIN_LIFT_OVER_RANDOM = 0.10
TARGET_LIFT_OVER_RANDOM = 0.18
MIN_LIFT_OVER_DSP = 0.10
TARGET_LIFT_OVER_DSP = 0.15
MIN_DSP_PAIRWISE_ACCURACY = 0.55
MIN_JEV_ONLY_PAIRWISE_ACCURACY = 0.55
MIN_TOP1_MARGIN = 0.30
TARGET_TOP1_MARGIN = 0.50
MIN_TOP_K_MEAN = 2.00
TARGET_TOP_K_MEAN = 2.30
MIN_TOP_K_MEAN_LIFT_OVER_RANDOM = 0.30
MIN_TOP_K_MEAN_LIFT_OVER_DSP = 0.15
MAX_COLD_RECOMMENDATION_P95_MS = 2000
TARGET_COLD_RECOMMENDATION_P95_MS = 1000
MAX_WARM_RECOMMENDATION_P95_MS = 500
TARGET_WARM_RECOMMENDATION_P95_MS = 250
MAX_AUDITION_START_P95_MS = 250
RETENTION_WINDOW_DAYS = 14
MIN_RETENTION_RATE = 0.20
TARGET_RETENTION_RATE = 0.30
MIN_RETENTION_SELECTIONS = 30

# name -> (value, class). The class is the protocol's own first word.
FROZEN_CONSTANTS = {
    "PROTOCOL_VERSION": (PROTOCOL_VERSION, "version"),
    "SPLIT_SEED": (SPLIT_SEED, "seed"),
    "PAIR_SAMPLER_SEED": (PAIR_SAMPLER_SEED, "seed"),
    "ASSIGNMENT_SEED": (ASSIGNMENT_SEED, "seed"),
    "ORDER_SEED": (ORDER_SEED, "seed"),
    "RANDOM_ARM_SEED": (RANDOM_ARM_SEED, "seed"),
    "BOOTSTRAP_SEED": (BOOTSTRAP_SEED, "seed"),
    "TUNING_FRACTION": (TUNING_FRACTION, "rule"),
    "MIN_POOL_KICKS": (MIN_POOL_KICKS, "minimum"),
    "MIN_POOL_BASSES": (MIN_POOL_BASSES, "minimum"),
    "MIN_EVALUATORS": (MIN_EVALUATORS, "minimum"),
    "TARGET_EVALUATORS": (TARGET_EVALUATORS, "target"),
    "MIN_RATINGS_PER_PAIR": (MIN_RATINGS_PER_PAIR, "minimum"),
    "TARGET_RATINGS_PER_PAIR": (TARGET_RATINGS_PER_PAIR, "target"),
    "MIN_HELDOUT_QUERIES": (MIN_HELDOUT_QUERIES, "minimum"),
    "TARGET_HELDOUT_QUERIES": (TARGET_HELDOUT_QUERIES, "target"),
    "RATED_CANDIDATES_PER_QUERY": (RATED_CANDIDATES_PER_QUERY, "rule"),
    "MIN_HELDOUT_PAIRS": (MIN_HELDOUT_PAIRS, "minimum"),
    "MIN_TUNING_PAIRS": (MIN_TUNING_PAIRS, "minimum"),
    "MIN_PAIR_COVERAGE": (MIN_PAIR_COVERAGE, "minimum"),
    "MIN_AGREEMENT_PAIRS": (MIN_AGREEMENT_PAIRS, "minimum"),
    "MAX_MEAN_ABSOLUTE_DEVIATION": (MAX_MEAN_ABSOLUTE_DEVIATION, "minimum"),
    "MAX_RECOGNISED_RATE": (MAX_RECOGNISED_RATE, "minimum"),
    "MIN_SCORED_SHARE_PER_QUERY": (MIN_SCORED_SHARE_PER_QUERY, "minimum"),
    "MIN_RATED_CANDIDATES_FOR_TOP10_GATE": (MIN_RATED_CANDIDATES_FOR_TOP10_GATE, "rule"),
    "MIN_LATENCY_REQUESTS_PER_ARM": (MIN_LATENCY_REQUESTS_PER_ARM, "minimum"),
    "BOOTSTRAP_RESAMPLES": (BOOTSTRAP_RESAMPLES, "rule"),
    "MIN_PAIRWISE_ACCURACY": (MIN_PAIRWISE_ACCURACY, "minimum"),
    "TARGET_PAIRWISE_ACCURACY": (TARGET_PAIRWISE_ACCURACY, "target"),
    "MIN_LIFT_OVER_RANDOM": (MIN_LIFT_OVER_RANDOM, "minimum"),
    "TARGET_LIFT_OVER_RANDOM": (TARGET_LIFT_OVER_RANDOM, "target"),
    "MIN_LIFT_OVER_DSP": (MIN_LIFT_OVER_DSP, "minimum"),
    "TARGET_LIFT_OVER_DSP": (TARGET_LIFT_OVER_DSP, "target"),
    "MIN_DSP_PAIRWISE_ACCURACY": (MIN_DSP_PAIRWISE_ACCURACY, "minimum"),
    "MIN_JEV_ONLY_PAIRWISE_ACCURACY": (MIN_JEV_ONLY_PAIRWISE_ACCURACY, "minimum"),
    "MIN_TOP1_MARGIN": (MIN_TOP1_MARGIN, "minimum"),
    "TARGET_TOP1_MARGIN": (TARGET_TOP1_MARGIN, "target"),
    "MIN_TOP_K_MEAN": (MIN_TOP_K_MEAN, "minimum"),
    "TARGET_TOP_K_MEAN": (TARGET_TOP_K_MEAN, "target"),
    "MIN_TOP_K_MEAN_LIFT_OVER_RANDOM": (MIN_TOP_K_MEAN_LIFT_OVER_RANDOM, "minimum"),
    "MIN_TOP_K_MEAN_LIFT_OVER_DSP": (MIN_TOP_K_MEAN_LIFT_OVER_DSP, "minimum"),
    "MAX_COLD_RECOMMENDATION_P95_MS": (MAX_COLD_RECOMMENDATION_P95_MS, "minimum"),
    "TARGET_COLD_RECOMMENDATION_P95_MS": (TARGET_COLD_RECOMMENDATION_P95_MS, "target"),
    "MAX_WARM_RECOMMENDATION_P95_MS": (MAX_WARM_RECOMMENDATION_P95_MS, "minimum"),
    "TARGET_WARM_RECOMMENDATION_P95_MS": (TARGET_WARM_RECOMMENDATION_P95_MS, "target"),
    "MAX_AUDITION_START_P95_MS": (MAX_AUDITION_START_P95_MS, "minimum"),
    "RETENTION_WINDOW_DAYS": (RETENTION_WINDOW_DAYS, "rule"),
    "MIN_RETENTION_RATE": (MIN_RETENTION_RATE, "minimum"),
    "TARGET_RETENTION_RATE": (TARGET_RETENTION_RATE, "target"),
    "MIN_RETENTION_SELECTIONS": (MIN_RETENTION_SELECTIONS, "minimum"),
}

SEEDS = {"SPLIT_SEED": SPLIT_SEED, "PAIR_SAMPLER_SEED": PAIR_SAMPLER_SEED,
         "ASSIGNMENT_SEED": ASSIGNMENT_SEED, "ORDER_SEED": ORDER_SEED,
         "RANDOM_ARM_SEED": RANDOM_ARM_SEED, "BOOTSTRAP_SEED": BOOTSTRAP_SEED}

ARM_NAMES = ("random", "dsp-only", "jev-only", "hybrid")
COMPARISON_ARMS = ("random", "dsp-only", "hybrid")
JEV_ARMS = ("jev-only", "hybrid")
ALL_ARMS = ARM_NAMES

# The arms each gated constant gates. An evidence minimum gates every arm; the
# consistency rule ties MIN_PAIRWISE_ACCURACY, MIN_LIFT_OVER_DSP and
# MIN_TOP1_MARGIN to hybrid; the reachability rule ties the three top-k minima
# to every arm that reports a top-k mean.
GATE_ARMS = {
    "MIN_POOL_KICKS": ALL_ARMS,
    "MIN_POOL_BASSES": ALL_ARMS,
    "MIN_EVALUATORS": ALL_ARMS,
    "MIN_RATINGS_PER_PAIR": ALL_ARMS,
    "MIN_HELDOUT_QUERIES": ALL_ARMS,
    "MIN_HELDOUT_PAIRS": ALL_ARMS,
    "MIN_PAIR_COVERAGE": ALL_ARMS,
    "MIN_AGREEMENT_PAIRS": ALL_ARMS,
    "MAX_MEAN_ABSOLUTE_DEVIATION": ALL_ARMS,
    "MIN_SCORED_SHARE_PER_QUERY": ALL_ARMS,
    "MAX_RECOGNISED_RATE": ALL_ARMS,
    "MIN_PAIRWISE_ACCURACY": ("hybrid",),
    "MIN_LIFT_OVER_RANDOM": ALL_ARMS,
    "MIN_LIFT_OVER_DSP": ("hybrid",),
    "MIN_DSP_PAIRWISE_ACCURACY": ("dsp-only",),
    "MIN_JEV_ONLY_PAIRWISE_ACCURACY": ("jev-only",),
    "MIN_TOP1_MARGIN": ("hybrid",),
    "MIN_TOP_K_MEAN": ALL_ARMS,
    "MIN_TOP_K_MEAN_LIFT_OVER_RANDOM": ALL_ARMS,
    "MIN_TOP_K_MEAN_LIFT_OVER_DSP": ALL_ARMS,
    "MIN_LATENCY_REQUESTS_PER_ARM": ALL_ARMS,
    "MAX_COLD_RECOMMENDATION_P95_MS": ALL_ARMS,
    "MAX_WARM_RECOMMENDATION_P95_MS": ALL_ARMS,
}
OUT_OF_SCOPE_GATES = ("MAX_AUDITION_START_P95_MS", "RETENTION_WINDOW_DAYS",
                      "MIN_RETENTION_RATE", "TARGET_RETENTION_RATE",
                      "MIN_RETENTION_SELECTIONS")
# A minimum that applies only under a claim this run does not make.
CONDITIONAL_GATES = ("MIN_TUNING_PAIRS",)

REPORT_SECTIONS = ("## State", "## Versions and digests", "## Eligibility accounting",
                   "## Quality", "## Uncertainty", "## Agreement", "## Latency",
                   "## Evidence gaps", "## Deviations", "## No-held-out-tuning attestation",
                   "## Reproduction", "## Out of scope")

ATTESTATION = ("No tuning used held-out results: no held-out rating, arm output, score or rank"
               " was read while the split, seeds and versions were chosen.")
FABRICATION_SENTENCE = "No rating was fabricated and no private record left the device."
ADAPTER_VERDICT = ("real TypeSafe Jev integration: UNVERIFIED - TERA_JEV_ENDPOINT/"
                   "TERA_JEV_API_KEY are not set")

EXIT_OK = 0
EXIT_INSUFFICIENT = 1
EXIT_FAILURE = 2
MECHANISM_CODES = ("protocol_version_mismatch", "schema_mismatch",
                   "split_manifest_digest_mismatch", "pair_not_held_out", "unknown_sample_id",
                   "duplicate_pair", "pair_unassigned", "rated_pair_not_sampled",
                   "multiple_analysis_versions", "missing_analysis_entry",
                   "analysis_entry_incomplete", "synthetic_fixture_record",
                   "dataset_version_mismatch", "candidate_set_mismatch",
                   "public_rating_source", "invalid_input_path")
GAP_CODES = ("protocol_document_missing", "split_manifest_missing", "pair_list_missing",
             "assignment_missing", "ratings_missing", "analysis_manifest_missing",
             "dataset_missing", "pair_counts_below_minimum", "below_minimum")


class ComparisonError(Exception):
    """A mechanism failure: a stable code and the exit status 2 it produces."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class EvidenceGap(Exception):
    """An evidence gap: a stable code and the shortfall it records."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


# --- small deterministic helpers -------------------------------------------

def canonical(value):
    """Canonical JSON: sorted keys, no NaN, no insignificant whitespace."""
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))


def file_digest(path):
    try:
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def percentile(values, fraction):
    """The linear-interpolation percentile of a sorted copy, documented in the report.

    position = (n - 1) * fraction; the value is interpolated between the two
    neighbours. One value returns itself and an empty sequence returns None.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def percentile_interval(values):
    """The 95% percentile interval (2.5th and 97.5th) of one value list."""
    return [percentile(values, 0.025), percentile(values, 0.975)]


def median_lower_middle(values):
    """The per-pair aggregate: the median, the lower middle value on an even count."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def seeded_generator(*parts):
    """A deterministic random.Random from the seed string and the given parts."""
    material = "|".join(str(part) for part in parts).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:16], "big"))


def seeded_order(values, *parts):
    """A seeded draw without replacement over the given values, in draw order."""
    generator = seeded_generator(*parts)
    pool = list(values)
    drawn = []
    while pool:
        drawn.append(pool.pop(generator.randrange(len(pool))))
    return tuple(drawn)
# --- the protocol document and the private input documents -----------------

MARK = chr(96)


def section_lines(text, heading):
    """The lines of one '### heading' section, up to the next heading."""
    lines, collecting = [], False
    for line in text.splitlines():
        if line.strip().startswith("#"):
            if collecting:
                break
            collecting = line.strip() == heading
            continue
        if collecting:
            lines.append(line)
    return lines


def table_rows(text, heading):
    """The markdown table rows of one section, as lists of stripped cells."""
    rows = []
    for line in section_lines(text, heading):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)
    return rows


def constant_value(raw):
    """One frozen-constant cell: a marked string, a number, or 'number (arithmetic)'."""
    cell = raw.strip()
    if cell.startswith(MARK) and cell.endswith(MARK) and len(cell) > 2:
        return cell.strip(MARK), None
    try:
        return float(cell), None
    except ValueError:
        pass
    head, _, tail = cell.partition("(")
    try:
        return float(head.strip()), tail.strip().rstrip(")").strip() or None
    except ValueError:
        return None, None


def frozen_constants(text):
    """Parse the frozen-constants table: name -> value, class word and derivation."""
    parsed = {}
    for cells in table_rows(text, "### Frozen constants"):
        if len(cells) < 4:
            continue
        name = cells[0].strip(MARK)
        if not name or not name.replace("_", "").isalnum() or not name.isupper():
            continue
        value, derivation = constant_value(cells[1])
        if value is None:
            continue
        parsed[name] = {"value": value, "class": cells[3].strip().split()[0].lower(),
                        "derivation": derivation, "raw": cells[1].strip()}
    return parsed


@dataclass(frozen=True)
class ProtocolDocument:
    """The frozen protocol document, its digest and the constants it declares."""

    path: str
    present: bool
    version: str | None
    constants: dict
    digest: str | None


def load_protocol_document(path):
    """Read the protocol document; an absent or constant-less document stays explicit."""
    target = Path(path)
    if not target.is_file():
        return ProtocolDocument(str(path), False, None, {}, None)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ProtocolDocument(str(path), False, None, {}, None)
    constants = frozen_constants(text)
    version = constants.get("PROTOCOL_VERSION", {}).get("value")
    return ProtocolDocument(str(path), True, version if isinstance(version, str) else None,
                            constants, file_digest(target))


def read_json(path):
    """Read one JSON document, rejecting duplicate keys and non-finite numbers."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON number")

    document = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    if type(document) is not dict:
        raise ValueError("the document is not an object")
    return document


@dataclass(frozen=True)
class Document:
    """One declared JSON input: whether it is there, what it holds and its digest."""

    name: str
    path: str
    present: bool
    value: dict | None
    digest: str | None
    malformed: bool = False

    @property
    def missing(self):
        return not self.present


def load_document(name, path, *, reader=read_json):
    """Load one declared input; a malformed document is a stable mechanism failure."""
    target = Path(path)
    if not target.is_file():
        return Document(name, str(path), False, None, None)
    try:
        value = reader(target)
    except (OSError, ValueError) as error:
        raise ComparisonError("schema_mismatch",
                             name + " is present but malformed: " + str(error)) from error
    return Document(name, str(path), True, value, file_digest(target))


# --- the #65 artifacts this runner expects ---------------------------------

HELD_OUT_KEYS = ("held_out", "held-out", "heldout")
TUNING_KEYS = ("tuning", "tune")
SAMPLE_ID_FIELDS = ("sample_id", "id")


def sample_ids(container, role):
    """The sample ids one split partition lists for one role, in file order."""
    if container is None:
        return None
    values = container.get(role)
    if values is None:
        values = container.get(role + "s")
    if type(values) is not list or not all(type(item) is str and item for item in values):
        return None
    return tuple(values)


def split_partitions(document):
    """The tuning and held-out ids of a split manifest, or None when malformed."""
    for key in HELD_OUT_KEYS:
        if key in document:
            held = document[key]
            break
    else:
        return None
    tuning = None
    for key in TUNING_KEYS:
        if key in document:
            tuning = document[key]
            break
    if type(held) is not dict:
        return None
    held_out = {role: sample_ids(held, role) for role in ("kicks", "bass", "basses")}
    if held_out["kicks"] is None:
        return None
    basses = held_out["bass"] if held_out["bass"] is not None else held_out["basses"]
    if basses is None:
        return None
    tuning_ids = {"kicks": (), "basses": ()}
    if type(tuning) is dict:
        kicks = sample_ids(tuning, "kicks")
        bass_values = sample_ids(tuning, "bass")
        if bass_values is None:
            bass_values = sample_ids(tuning, "basses")
        tuning_ids = {"kicks": kicks or (), "basses": bass_values or ()}
    return {"held_out": {"kicks": held_out["kicks"], "basses": basses}, "tuning": tuning_ids}


def assignment_table(document):
    """The assignment's (pair id -> evaluator ids) map, or None when malformed."""
    entries = document.get("assignments")
    table = {}
    if type(entries) is dict:
        for pair_id, evaluators in entries.items():
            if type(evaluators) is not list or not all(type(item) is str and item
                                                      for item in evaluators):
                return None
            table[pair_id] = tuple(evaluators)
        return table
    if type(entries) is list:
        for record in entries:
            if type(record) is not dict:
                return None
            pair_id = record.get("pair_id")
            evaluators = record.get("evaluators")
            if type(pair_id) is not str or not pair_id:
                return None
            if type(evaluators) is not list or not all(type(item) is str and item
                                                       for item in evaluators):
                return None
            table[pair_id] = tuple(evaluators)
        return table
    return None


# --- #18 sessions ----------------------------------------------------------

SESSION_FIELDS = ("session_id", "evaluator_id", "protocol_version", "dataset_version",
                  "split_manifest_digest", "order_seed", "playback_gain_db",
                  "monitoring_description", "started_at", "finished_at")
RATING_FIELDS = ("pair_id", "kick_sample_id", "bass_sample_id", "presentation_index", "rating",
                 "skip", "skip_reason", "recognised", "playback_completed", "responded_at",
                 "correction_of")
LABEL_VALUES = {"poor": 0.0, "acceptable": 1.0, "good": 2.0, "excellent": 3.0}
TOOLING_PREFIX = "dry-run"
VALIDATE_S = 300.0


@dataclass(frozen=True)
class Rating:
    """One valid effective rating of one pair by one evaluator."""

    pair_id: str
    kick_id: str
    bass_id: str
    value: float
    evaluator_id: str
    session_id: str


@dataclass(frozen=True)
class SessionSummary:
    """One #18 session directory and everything this run learned about it."""

    session_id: str
    directory: str
    validate_exit: int
    violations: tuple
    evaluator_id: str | None
    tooling: bool
    counted: bool
    reason: str | None
    dataset_version: str | None
    split_manifest_digest: str | None
    presentations: int
    answered: int
    recognised: int
    ratings: tuple
    protocol_version: str | None = None
    order_seed: str | None = None
    playback_gain_db: float | None = None

    def as_dict(self):
        return {"session_id": self.session_id, "validate_exit": self.validate_exit,
                "violations": list(self.violations), "tooling": self.tooling,
                "counted": self.counted, "reason": self.reason,
                "presentations": self.presentations, "answered": self.answered,
                "recognised": self.recognised, "valid_ratings": len(self.ratings),
                "protocol_version": self.protocol_version, "order_seed": self.order_seed,
                "playback_gain_db": self.playback_gain_db,
                "dataset_version": self.dataset_version,
                "split_manifest_digest": self.split_manifest_digest}


def git_tracks(path):
    """Whether git tracks the given path; file-backed stdio, never a pipe."""
    root = Path(path).resolve()
    parent = root if root.is_dir() else root.parent
    if not (REPOSITORY_ROOT / ".git").exists():
        return False
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "ls-files.out"
        error = Path(scratch) / "ls-files.err"
        with output.open("wb") as out, error.open("wb") as err:
            result = subprocess.run(["git", "-c", "safe.directory=*", "ls-files", "--",
                                     str(root)], cwd=str(REPOSITORY_ROOT), stdout=out, stderr=err)
        if result.returncode != 0:
            return False
        return bool(output.read_text(encoding="utf-8", errors="replace").strip())


def rating_source_problem(directory):
    """A mechanism failure when a ratings source is public, otherwise None."""
    resolved = Path(directory).resolve()
    tests = (REPOSITORY_ROOT / "tests").resolve()
    if resolved == tests or str(resolved).lower().startswith(str(tests).lower() + os.sep):
        raise ComparisonError("public_rating_source",
                              "A ratings source resolves under tests/ and may not be evidence.")
    if git_tracks(resolved):
        raise ComparisonError("public_rating_source",
                              "A ratings source is tracked by git and may not be evidence.")
    return None


def child_workspace(directory):
    """The cwd a #17 validate child needs, so its private root contains this session.

    #17 refuses any session outside <cwd>/.local-evaluation/pair-rating/, so the child
    runs from the workspace that holds the private root, with this repository on
    PYTHONPATH. A session outside such a workspace keeps the repository cwd and #17
    records its own outside_private_root refusal.
    """
    for parent in Path(directory).resolve().parents:
        if parent.name == ".local-evaluation":
            return parent.parent
    return REPOSITORY_ROOT


def run_validate(directory):
    """Run #17's own validate command line with file-backed stdio; return (exit, report)."""
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "validate.out"
        error = Path(scratch) / "validate.err"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")])
        with output.open("wb") as out, error.open("wb") as err:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "backend.evaluation.rating", "validate",
                     "--session", str(directory)], cwd=str(child_workspace(directory)),
                    stdout=out, stderr=err, env=environment, timeout=VALIDATE_S)
            except (OSError, subprocess.SubprocessError):
                return None, None
        text = output.read_text(encoding="utf-8", errors="replace").strip()
    try:
        return result.returncode, json.loads(text)
    except ValueError:
        return result.returncode, None


def effective_records(records):
    """#17's supersede rule: the last record for one presentation index is the answer."""
    effective = {}
    for record in records:
        if type(record) is dict and type(record.get("presentation_index")) is int:
            effective[record["presentation_index"]] = record
    return effective


def load_session(directory):
    """Read one session directory without repairing it; never raises for bad content."""
    path = Path(directory)
    session_path = path / "session.json"
    if not session_path.is_file():
        return SessionSummary(path.name, str(path), 2, ("session_unreadable",), None, False,
                              False, "session_unreadable", None, None, 0, 0, 0, ())
    try:
        document = read_json(session_path)
    except (OSError, ValueError):
        return SessionSummary(path.name, str(path), 2, ("session_unreadable",), None, False,
                              False, "session_unreadable", None, None, 0, 0, 0, ())
    exit_code, report = run_validate(path)
    violations = ()
    if type(report) is dict and type(report.get("violations")) is list:
        violations = tuple(item.get("code") if type(item) is dict else str(item)
                           for item in report["violations"])
    if exit_code is None:
        violations = violations + ("validate_unavailable",)
    if exit_code != 0:
        return SessionSummary(path.name, str(path), exit_code if exit_code is not None else 2,
                              violations, document.get("evaluator_id"), False, False,
                              "validate_failed", document.get("dataset_version"),
                              document.get("split_manifest_digest"), 0, 0, 0, ())
    export_path = path / "export.json"
    try:
        export = read_json(export_path)
        ratings = export.get("ratings")
        if type(ratings) is not list:
            raise ValueError("export.json carries no ratings list")
    except (OSError, ValueError):
        return SessionSummary(path.name, str(path), exit_code, violations,
                              document.get("evaluator_id"), False, False, "export_unreadable",
                              document.get("dataset_version"),
                              document.get("split_manifest_digest"), 0, 0, 0, ())
    presentations = document.get("presentations")
    total = len(presentations) if type(presentations) is list else 0
    effective = effective_records(ratings)
    monitoring = document.get("monitoring_description")
    tooling = type(monitoring) is str and monitoring.startswith(TOOLING_PREFIX)
    valid, recognised = [], 0
    for record in effective.values():
        if type(record) is not dict:
            continue
        if record.get("recognised") is True:
            recognised += 1
            continue
        label = record.get("rating")
        if (record.get("skip") is False and record.get("playback_completed") is True
                and label in LABEL_VALUES and type(record.get("pair_id")) is str):
            valid.append(Rating(record["pair_id"], record.get("kick_sample_id"),
                                record.get("bass_sample_id"), LABEL_VALUES[label],
                                document.get("evaluator_id"), document.get("session_id")))
    reason = None
    counted = True
    if tooling:
        counted, reason = False, "tooling_session"
    elif total and recognised / total > MAX_RECOGNISED_RATE:
        counted, reason = False, "recognised_rate_above_maximum"
    return SessionSummary(path.name, str(path), exit_code, violations,
                          document.get("evaluator_id"), tooling, counted, reason,
                          document.get("dataset_version"), document.get("split_manifest_digest"),
                          total, len(effective), recognised, tuple(valid),
                          document.get("protocol_version"), document.get("order_seed"),
                          document.get("playback_gain_db"))


def load_sessions(root):
    """Every session directory under one ratings root, in stable session-id order."""
    path = Path(root)
    if not path.is_dir():
        return None
    directories = sorted((item for item in path.iterdir() if item.is_dir()),
                         key=lambda item: item.name)
    return tuple(load_session(directory) for directory in directories)


def session_field_problems(summary, *, dataset_version, split_manifest_digest):
    """The five field values #19 must see, checked against the run's own records."""
    problems = []
    if summary.protocol_version != PROTOCOL_VERSION:
        problems.append("protocol_version")
    if summary.playback_gain_db != -6.0:
        problems.append("playback_gain_db")
    if summary.order_seed != ORDER_SEED:
        problems.append("order_seed")
    if dataset_version is not None and summary.dataset_version != dataset_version:
        problems.append("dataset_version")
    if (split_manifest_digest is not None
            and summary.split_manifest_digest != split_manifest_digest):
        problems.append("split_manifest_digest")
    return problems
# --- declared inputs and the nine recorded preflight checks ----------------

@dataclass(frozen=True)
class Paths:
    """Every input and output this runner declares."""

    protocol_doc: Path
    dataset: Path
    split_manifest: Path
    pairs: Path
    assignment: Path
    sessions: Path
    analyses: tuple
    jev_runs: tuple
    report: Path
    runs: Path
    tuning_ratings: Path
    tuning_claim: bool
    jev_live: bool


def default_paths():
    return Paths(PROTOCOL_DOCUMENT, DEFAULT_DATASET,
                 DEFAULT_PAIR_ROOT / "split-manifest.json", DEFAULT_PAIR_ROOT / "pairs.json",
                 DEFAULT_PAIR_ROOT / "assignment.json", DEFAULT_PAIR_ROOT / "sessions",
                 (DEFAULT_ANALYSIS_ROOT / "kicks-manifest.json",
                  DEFAULT_ANALYSIS_ROOT / "basses-manifest.json"),
                 (), REPORT_PATH, RUNS_DIRECTORY, TUNING_DIRECTORY, False, False)


@dataclass(frozen=True)
class Finding:
    """One recorded check: its name, stable code, expected value, actual value and status."""

    name: str
    code: str
    expected: str
    actual: str
    status: str
    detail: str = ""

    @property
    def failed(self):
        return self.status != "ok"

    @property
    def mechanism(self):
        return self.status in ("malformed", "inconsistent")

    def as_dict(self):
        return {"name": self.name, "code": self.code, "expected": self.expected,
                "actual": self.actual, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class Query:
    """One held-out query kick: its sampled candidates, eligible set and rated subset."""

    kick_id: str
    sampled: tuple
    eligible: tuple
    rated: dict
    ratings: dict
    filter_error: str | None = None

    @property
    def rated_size(self):
        return len(self.rated)


@dataclass
class Findings:
    """Everything one evaluation of the declared inputs learned, plus its mechanism errors."""

    paths: Paths
    protocol: ProtocolDocument
    dataset: Document
    split: Document
    pairs: Document
    assignment: Document
    analyses: tuple
    sessions: tuple
    jev_runs: tuple
    items: tuple
    mechanism: tuple
    query: tuple
    ratings: tuple
    evaluators: tuple
    pool: dict
    split_counts: dict
    accounting: dict
    deviations: list
    analysis_versions: tuple
    samples: dict
    analyses_available: bool
    pair_ratings: dict
    tuning: dict
    run_identity: dict

    @property
    def exit_code(self):
        if any(item.mechanism for item in self.items):
            return EXIT_FAILURE
        if any(item.failed for item in self.items):
            return EXIT_INSUFFICIENT
        return EXIT_OK

    @property
    def gaps(self):
        return tuple(item for item in self.items if item.failed)


def document_shape_problems(name, document):
    """Structural checks shared by the private dataset and split documents."""
    if type(document.get("schema_version")) is not str or document["schema_version"] != SCHEMA_VERSION:
        return name + ".schema_version"
    if type(document.get("dataset_version")) is not str or not document["dataset_version"].strip():
        return name + ".dataset_version"
    return None


def inspect_dataset(document):
    """The pool document's shape: sources, selected records and their provenance fields."""
    problem = document_shape_problems("dataset", document)
    if problem:
        raise ComparisonError("schema_mismatch", problem + " is missing or wrong")
    if type(document.get("sources")) is not dict or not document["sources"]:
        raise ComparisonError("schema_mismatch", "dataset.sources is missing")
    selected = document.get("selected")
    if type(selected) is not list or not selected:
        raise ComparisonError("schema_mismatch", "dataset.selected is missing")
    for record in selected:
        if (type(record) is not dict or type(record.get("sample_id")) is not str
                or not record["sample_id"] or record.get("role") not in ("kick", "bass")
                or type(record.get("mapping")) is not dict
                or type(record["mapping"].get("source")) is not str
                or type(record["mapping"].get("path")) is not str
                or type(record.get("provenance_kind")) is not str):
            raise ComparisonError("schema_mismatch", "a dataset.selected record is malformed")
    return True


def inspect_split(document):
    """The split manifest's shape: its schema, digest and two sample-id partitions."""
    problem = document_shape_problems("split_manifest", document)
    if problem:
        raise ComparisonError("schema_mismatch", problem + " is missing or wrong")
    digest_value = document.get("split_manifest_digest")
    if type(digest_value) is not str or not digest_value.startswith("sha256:") \
            or len(digest_value) != 71:
        raise ComparisonError("schema_mismatch", "split_manifest.split_manifest_digest is missing")
    if split_partitions(document) is None:
        raise ComparisonError("schema_mismatch", "split_manifest has no held-out partition")
    return True


def inspect_pair_list(document, *, dataset_version):
    """#17's pair-list schema, exactly the one rating.py validates."""
    fields = {"schema_version", "dataset_version", "split_manifest_digest", "sampler_seed",
              "assignment_seed", "pairs"}
    if set(document) != fields:
        raise ComparisonError("schema_mismatch", "pair_list has missing or unexpected fields")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ComparisonError("schema_mismatch", "pair_list.schema_version is not 1.0")
    if type(document["dataset_version"]) is not str or not document["dataset_version"].strip():
        raise ComparisonError("schema_mismatch", "pair_list.dataset_version is missing")
    digest_value = document["split_manifest_digest"]
    if type(digest_value) is not str or not digest_value.startswith("sha256:") \
            or len(digest_value) != 71:
        raise ComparisonError("schema_mismatch", "pair_list.split_manifest_digest is malformed")
    if document["dataset_version"] != dataset_version:
        raise ComparisonError("dataset_version_mismatch",
                              "pair_list and dataset disagree on dataset_version")
    for name in ("sampler_seed", "assignment_seed"):
        if document[name] != (PAIR_SAMPLER_SEED if name == "sampler_seed" else ASSIGNMENT_SEED):
            raise ComparisonError("schema_mismatch", "pair_list." + name + " is not the frozen seed")
    pairs = document["pairs"]
    if type(pairs) is not list or not pairs:
        raise ComparisonError("schema_mismatch", "pair_list.pairs is missing or empty")
    seen = set()
    for pair in pairs:
        if type(pair) is not dict or set(pair) != {"pair_id", "kick_sample_id", "bass_sample_id"}:
            raise ComparisonError("schema_mismatch", "a pair_list.pairs record is malformed")
        if not all(type(pair[field]) is str and pair[field].strip() for field in pair):
            raise ComparisonError("schema_mismatch", "a pair_list.pairs record has a blank field")
        if pair["pair_id"] in seen:
            raise ComparisonError("duplicate_pair", "pair_id appears twice in the pair list")
        seen.add(pair["pair_id"])
    return True


def inspect_assignment(document):
    """The assignment document's shape: at least one mapping of pair to evaluator ids."""
    problem = document_shape_problems("assignment", document)
    if problem:
        raise ComparisonError("schema_mismatch", problem + " is missing or wrong")
    if assignment_table(document) is None:
        raise ComparisonError("schema_mismatch", "assignment.assignments is missing or malformed")
    if document.get("assignment_seed") != ASSIGNMENT_SEED:
        raise ComparisonError("schema_mismatch", "assignment.assignment_seed is not the frozen seed")
    return True


ANALYSIS_ENTRY_FIELDS = {"status", "fingerprint", "sample_id", "result", "error", "disposition"}


def inspect_analysis(document, path):
    """The #9 batch manifest's shape and its usable entries."""
    if document.get("manifest_schema") != SCHEMA_VERSION:
        raise ComparisonError("schema_mismatch", path + " has another manifest_schema")
    if document.get("role") not in ("kick", "bass"):
        raise ComparisonError("schema_mismatch", path + ".role is not kick or bass")
    if type(document.get("analysis_digest")) is not str or len(document["analysis_digest"]) != 64:
        raise ComparisonError("schema_mismatch", path + ".analysis_digest is malformed")
    entries = document.get("entries")
    if type(entries) is not dict:
        raise ComparisonError("schema_mismatch", path + ".entries is missing")
    for name, entry in entries.items():
        if type(entry) is not dict or set(entry) != ANALYSIS_ENTRY_FIELDS:
            raise ComparisonError("schema_mismatch", path + " has a malformed entry")
        if entry["status"] not in ("pending", "complete", "error"):
            raise ComparisonError("schema_mismatch", path + " has an unknown entry status")
    return True


def analysis_samples(analyses):
    """Every complete analysed Sample of the loaded manifests, by sample id."""
    samples, problems = {}, []
    for document, path in analyses:
        for name, entry in document["entries"].items():
            if entry["status"] != "complete" or entry["result"] is None:
                continue
            sample_id = entry["sample_id"]
            if sample_id in samples:
                problems.append(("duplicate_analysis_entry", sample_id))
                continue
            try:
                samples[sample_id] = Sample.from_dict(entry["result"])
            except (TypeError, ValueError):
                problems.append(("analysis_entry_incomplete", sample_id))
    return samples, problems


def analysis_versions(samples, used_ids):
    return tuple(sorted({samples[sample_id].analysis_version for sample_id in used_ids
                         if sample_id in samples}))


def load_jev_runs(paths):
    """Load every declared #14 run document; a malformed run is a mechanism failure."""
    runs = []
    for path in paths:
        target = Path(path)
        if not target.is_file():
            raise ComparisonError("schema_mismatch", "a declared --jev-run file is absent")
        try:
            runs.append(JevScoringRun.from_json(target.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise ComparisonError("schema_mismatch",
                                  "a --jev-run document is malformed: " + str(error)) from error
    return tuple(runs)


def jev_key(kick_id, candidate_id, dimension):
    return "jev|" + kick_id + "|" + candidate_id + "|" + dimension


def jev_outcomes_by_key(runs):
    """The recorded outcomes by request id; a live run beats a double run, later wins."""
    ordered = sorted(range(len(runs)), key=lambda index: (runs[index].source == "interface", index))
    table = {}
    for index in ordered:
        for outcome in runs[index].outcomes:
            table[outcome.request_id] = (outcome, runs[index])
    return table
# --- reading the #65 artifacts, the analysis manifests and the ratings ------

def pair_records(pair_list):
    if pair_list is None:
        return ()
    return tuple(pair_list["pairs"])


def sampled_queries(pairs):
    """The sampled pairs grouped by query kick; candidates ordered by bass id."""
    table = defaultdict(list)
    for pair in pairs:
        table[pair["kick_sample_id"]].append(pair["bass_sample_id"])
    return {kick: tuple(sorted(basses)) for kick, basses in table.items()}


def duplicate_bass_pairs(pairs):
    """Sampled pairs that hold the same bass twice for one query, or share none."""
    problems = []
    for kick, basses in sampled_queries(pairs).items():
        if len(basses) != len(set(basses)):
            problems.append((kick, "duplicate_candidate"))
    return problems


def ratings_by_pair(ratings):
    table = defaultdict(list)
    for rating in ratings:
        table[rating.pair_id].append(rating)
    return {pair_id: tuple(items) for pair_id, items in table.items()}


def pair_aggregates(table, *, drop_evaluators=()):
    """The per-pair aggregate over valid ratings, at most one rating per evaluator."""
    aggregates = {}
    for pair_id, items in table.items():
        counted, seen = [], set()
        for rating in items:
            if rating.evaluator_id in drop_evaluators or rating.evaluator_id in seen:
                continue
            seen.add(rating.evaluator_id)
            counted.append(rating.value)
        if len(counted) >= MIN_RATINGS_PER_PAIR:
            aggregates[pair_id] = {"value": median_lower_middle(counted), "count": len(counted),
                                   "values": tuple(sorted(counted))}
    return aggregates


def build_queries(queries_in, aggregates, pairs, samples, *, analyses_available):
    """One Query per sampled query kick: eligible candidate set and rated subset."""
    by_pair = {(pair["kick_sample_id"], pair["bass_sample_id"]): pair for pair in pairs}
    built = []
    for kick_id in sorted(queries_in):
        basses = queries_in[kick_id]
        eligible, filter_error = (), None
        kick = samples.get(kick_id)
        if analyses_available and kick is not None:
            candidates = [samples[bass] for bass in basses if bass in samples]
            availability = {sample.sample_id: Availability.AVAILABLE for sample in candidates}
            availability[kick_id] = Availability.AVAILABLE
            try:
                filtered = filter_candidates(kick, candidates, policy=FilterPolicy(),
                                             availability=availability)
                eligible = tuple(filtered.eligible_ids)
            except FilterInputError as error:
                filter_error = error.code
                eligible = ()
        rated, counts = {}, {}
        for bass in eligible:
            pair_id = by_pair[(kick_id, bass)]["pair_id"]
            if pair_id in aggregates:
                rated[bass] = aggregates[pair_id]["value"]
                counts[bass] = aggregates[pair_id]["count"]
        built.append(Query(kick_id, basses, eligible, rated, counts, filter_error))
    return tuple(built)


# --- the four arms ---------------------------------------------------------

@dataclass(frozen=True)
class ArmOrder:
    """One arm's ordering of one query's eligible candidate set."""

    arm: str
    order: tuple
    scored: tuple
    version: str
    weight_table_id: str
    mode: str | None = None
    jev_status: str | None = None


def random_order(kick_id, candidates, dataset_version):
    """The documented seeded draw without replacement over one query's candidates."""
    return seeded_order(candidates, RANDOM_ARM_SEED, dataset_version, "random", kick_id)


def jev_requests_for(queries, samples):
    """The six #13 questions per (query, eligible candidate), in a stable order."""
    requests = []
    for query in queries:
        kick = samples.get(query.kick_id)
        if kick is None:
            continue
        for candidate_id in query.eligible:
            candidate = samples.get(candidate_id)
            if candidate is None:
                continue
            for dimension in DIMENSIONS:
                question = build_question(dimension, kick, candidate)
                requests.append(JevScoringRequest(jev_key(query.kick_id, candidate_id, dimension),
                                                  question))
    return tuple(requests)


def jev_evidence_for(kick_id, candidate_id, outcome_table):
    """The documented #14 outcome-to-#15 evidence mapping, hole by hole."""
    entries = []
    for dimension in DIMENSIONS:
        record = outcome_table.get(jev_key(kick_id, candidate_id, dimension))
        if record is None:
            continue
        outcome = record[0]
        if outcome.state in ("judged", "abstained"):
            entries.append(HybridEvidence(dimension, judgment=outcome.judgment))
        else:
            entries.append(HybridEvidence(dimension, unavailable_reason=outcome.code))
    return tuple(entries)


def jev_only_scores(kick_id, candidates, outcome_table, *, variant="label"):
    """The #16 jev-only arithmetic: equal 1/6 weights, renormalized, exclusions kept.

    Returns candidate id -> score, confidence (the covered weight) and the dimensions
    that carried a usable judgment. Nothing from backend.palette is read here: no DSP
    compatibility value, weight table or baseline can change this arm.
    """
    weights = {dimension: 1.0 / len(DIMENSIONS) for dimension in DIMENSIONS}
    scored = {}
    for candidate_id in candidates:
        total, covered, used = 0.0, 0.0, []
        for dimension in DIMENSIONS:
            record = outcome_table.get(jev_key(kick_id, candidate_id, dimension))
            if record is None:
                continue
            outcome = record[0]
            if outcome.state != "judged" or outcome.judgment is None \
                    or outcome.judgment.label is None:
                continue
            judgment = outcome.judgment
            if variant == "label":
                value = JEV_LABEL_SCORES[judgment.label]
            else:
                value = sum(item.probability * JEV_LABEL_SCORES[item.label]
                            for item in judgment.probabilities)
            total += weights[dimension] * value
            covered += weights[dimension]
            used.append(dimension)
        if covered > 0:
            scored[candidate_id] = {"score": total / covered, "confidence": covered,
                                    "dimensions": tuple(used)}
    return scored


def build_arm(name, *, kick, candidates, samples, dataset_version, baseline=None,
              outcome_table=None, variant="label"):
    """Build one arm's order for one query; the wiring #19 must prove is exactly here.

    dsp-only and hybrid consume rank_candidates/rank_hybrid; jev-only consumes no
    baseline at all and reads only #13/#14 evidence; random draws from the seed.
    """
    candidate_samples = [samples[candidate_id] for candidate_id in candidates
                         if candidate_id in samples]
    if name == "random":
        order = random_order(kick.sample_id, candidates, dataset_version)
        return ArmOrder(name, order, tuple(order), RANKING_VERSION, "random-draw")
    if name == "dsp-only":
        result = rank_candidates(kick, candidate_samples, policy=RankingPolicy())
        ranked = tuple(record.candidate_id for record in result.ranked)
        unscored = tuple(record.candidate_id for record in result.unscored)
        return ArmOrder(name, ranked + unscored, ranked, result.ranking_version,
                        result.weight_table_id)
    if name == "hybrid":
        if baseline is None:
            baseline = rank_candidates(kick, candidate_samples, policy=RankingPolicy())
        evidence = {candidate_id: jev_evidence_for(kick.sample_id, candidate_id, outcome_table)
                    for candidate_id in candidates}
        result = rank_hybrid(baseline, evidence, policy=HybridPolicy())
        ranked = tuple(record.candidate_id for record in result.ranked)
        unscored = tuple(record.candidate_id for record in result.unscored)
        return ArmOrder(name, ranked + unscored, ranked, result.ranking_version,
                        result.weight_table_id, result.mode, result.jev_status)
    if name == "jev-only":
        if outcome_table is None:
            outcome_table = {}
        scores = jev_only_scores(kick.sample_id, candidates, outcome_table, variant=variant)
        ranked = tuple(sorted(scores, key=lambda candidate_id: (-scores[candidate_id]["score"],
                                                                candidate_id)))
        unscored = tuple(sorted(candidate_id for candidate_id in candidates
                                if candidate_id not in scores))
        return ArmOrder(name, ranked + unscored, ranked, "jev-only-v1", "equal-1/6-weights")
    raise ComparisonError("schema_mismatch", "unknown arm name " + repr(name))


# --- metrics, uncertainty and verdicts -------------------------------------

METRIC_NAMES = ("pairwise_accuracy", "top1_margin", "topk_mean")


def mean(values):
    return sum(values) / len(values) if values else None


def query_metrics(rated, order):
    """The protocol's per-query metrics over one arm's order and one rated subset."""
    subset = [candidate for candidate in order if candidate in rated]
    values = [rated[candidate] for candidate in subset]
    decided, correct = 0, 0
    for first in range(len(subset)):
        for second in range(first + 1, len(subset)):
            ahead, behind = subset[first], subset[second]
            if rated[ahead] == rated[behind]:
                continue
            decided += 1
            if rated[ahead] > rated[behind]:
                correct += 1
    k = min(10, len(subset)) if subset else 0
    return {
        "rated_size": len(rated),
        "scored_in_rated": len(subset),
        "decided_pairs": decided,
        "pairwise_accuracy": (correct / decided) if decided and len(subset) >= 3 else None,
        "top1_margin": (rated[subset[0]] - mean(values)) if len(subset) >= 2 else None,
        "topk_mean": mean([rated[candidate] for candidate in subset[:k]]) if subset else None,
        "k": k,
        "rating_mean": mean(values),
        "descriptive_k": bool(subset) and k == len(subset),
    }


@dataclass(frozen=True)
class QueryEvaluation:
    """One arm's eligibility and metrics for one query."""

    arm: str
    query_id: str
    sampled_size: int
    eligible_size: int
    rated_size: int
    order: tuple
    scored: tuple
    unscored: tuple
    scored_share: float | None
    unscored_share: float | None
    eligible: bool
    reason: str | None
    error_code: str | None
    metrics: dict

    def as_dict(self):
        return {"arm": self.arm, "eligible": self.eligible, "reason": self.reason,
                "error_code": self.error_code, "sampled": self.sampled_size,
                "eligible_candidates": self.eligible_size, "rated": self.rated_size,
                "order": list(self.order), "scored": list(self.scored),
                "unscored": list(self.unscored), "scored_share": self.scored_share,
                "unscored_share": self.unscored_share, "metrics": self.metrics}


def evaluate_query(arm_order, query):
    """One arm against one query: the share floor, the tail and the metric values."""
    if set(arm_order.order) != set(query.eligible) or len(arm_order.order) != len(set(arm_order.order)):
        raise ComparisonError("candidate_set_mismatch",
                              "arm " + arm_order.arm + " would order a different candidate set")
    scored = tuple(candidate for candidate in arm_order.order if candidate in set(arm_order.scored))
    unscored = tuple(candidate for candidate in arm_order.order if candidate not in set(scored))
    rated_size = len(query.rated)
    scored_in_rated = sum(1 for candidate in query.rated if candidate in set(scored))
    share = (scored_in_rated / rated_size) if rated_size else None
    reason = None
    eligible = True
    if query.filter_error is not None:
        eligible, reason = False, "filter_error"
    elif not query.eligible:
        eligible, reason = False, "no_eligible_candidate_set"
    elif rated_size < MIN_RATINGS_PER_PAIR:
        eligible, reason = False, "rated_subset_below_minimum_ratings"
    elif share is None or share < MIN_SCORED_SHARE_PER_QUERY:
        eligible, reason = False, "scored_share_below_minimum"
    metrics = query_metrics(query.rated, arm_order.order)
    return QueryEvaluation(arm_order.arm, query.kick_id, len(query.sampled),
                           len(query.eligible), rated_size, arm_order.order, scored, unscored,
                           share, None if share is None else 1.0 - share, eligible, reason,
                           query.filter_error, metrics)


def arm_summary(evaluations):
    """Per-arm metric means over the queries eligible for that metric and that arm."""
    summary = {}
    for metric in METRIC_NAMES:
        values = [item.metrics[metric] for item in evaluations
                  if item.eligible and item.metrics[metric] is not None]
        summary[metric] = {"value": mean(values), "queries": len(values)}
    scored_in_rated = sum(item.metrics["scored_in_rated"] for item in evaluations if item.eligible)
    rated_total = sum(item.metrics["rated_size"] for item in evaluations if item.eligible)
    summary["unscored_share"] = {
        "value": (1.0 - scored_in_rated / rated_total) if rated_total else None,
        "scored_in_rated": scored_in_rated, "rated_total": rated_total}
    summary["eligible_queries"] = sum(1 for item in evaluations if item.eligible)
    summary["queries"] = len(evaluations)
    return summary


def paired_differences(first, second):
    """The per-query paired differences over the intersection of eligible queries."""
    table = {item.query_id: item for item in second if item.eligible}
    differences = []
    for item in first:
        if not item.eligible or item.query_id not in table:
            continue
        other = table[item.query_id]
        for metric in METRIC_NAMES:
            if item.metrics[metric] is None or other.metrics[metric] is None:
                continue
            differences.append((item.query_id, metric, item.metrics[metric] - other.metrics[metric]))
    return differences


def bootstrap_values(values, *, resamples=None, seed=BOOTSTRAP_SEED, label="metric"):
    """The cluster bootstrap: resample query kicks with replacement, recompute the mean."""
    resamples = BOOTSTRAP_RESAMPLES if resamples is None else resamples
    if not values:
        return {"value": None, "interval": [None, None], "draws": 0, "resamples": resamples}
    draws = []
    for index in range(resamples):
        generator = seeded_generator(seed, label, index)
        total = 0.0
        for _ in range(len(values)):
            total += values[generator.randrange(len(values))]
        draws.append(total / len(values))
    return {"value": mean(values), "interval": percentile_interval(draws), "draws": len(draws),
            "resamples": resamples, "seed": seed}


def verdict_for(value, interval, minimum, *, evidence_met, inconclusive=False, lift=False):
    """The protocol's four verdicts, in the protocol's own precedence."""
    if not evidence_met:
        return "insufficient"
    if value is None or interval is None or interval[0] is None:
        return "insufficient"
    if inconclusive:
        return "inconclusive"
    if value >= minimum and interval[0] > 0:
        return "supported"
    if interval[1] < minimum:
        return "not supported"
    return "inconclusive"


def gate_class(verdict):
    return {"supported": "supported", "not supported": "not supported",
            "inconclusive": "inconclusive", "insufficient": "insufficient"}[verdict]


def agreement_table(table):
    """Mean absolute deviation, exact-agreement share and per-evaluator means."""
    deviations, agreements, pairs_with_two = [], 0, 0
    per_evaluator = defaultdict(list)
    for pair_id, items in table.items():
        values = {}
        for rating in items:
            values.setdefault(rating.evaluator_id, rating.value)
            per_evaluator[rating.evaluator_id].append(rating.value)
        if len(values) < MIN_RATINGS_PER_PAIR:
            continue
        pairs_with_two += 1
        ordered = sorted(values.items())
        deviations.append(mean([abs(ordered[first][1] - ordered[second][1])
                                for first in range(len(ordered))
                                for second in range(len(ordered)) if first != second]))
        if len({value for _, value in ordered}) == 1:
            agreements += 1
    return {
        "mean_absolute_deviation": mean(deviations),
        "exact_agreement_share": (agreements / pairs_with_two) if pairs_with_two else None,
        "pairs_with_at_least_two_ratings": pairs_with_two,
        "evaluators": tuple(sorted((evaluator, mean(values))
                                   for evaluator, values in per_evaluator.items())),
    }
# --- one evaluation of the declared inputs ---------------------------------

def load_or_record(name, path, mechanism, *, reader=read_json):
    """Load one declared input; a malformed document is recorded, never repaired."""
    try:
        document = load_document(name, path, reader=reader)
    except ComparisonError as error:
        mechanism.append(error)
        return Document(name, str(path), True, None, None, malformed=True)
    return document


def dataset_pool(document):
    """Pool counts per role and per provenance kind."""
    pool = {"kicks": 0, "basses": 0, "reserves": 0, "provenance": Counter(), "records": 0}
    if document is None:
        return pool
    for record in document.get("selected") or ():
        if type(record) is not dict:
            continue
        pool["records"] += 1
        pool["provenance"][record.get("provenance_kind")] += 1
        if record.get("role") == "kick":
            pool["kicks"] += 1
        elif record.get("role") in ("bass", "sub-bass"):
            pool["basses"] += 1
    reserves = document.get("reserves")
    if type(reserves) is list:
        pool["reserves"] = len(reserves)
    return pool


def dataset_roles(document):
    roles = {}
    if document is None:
        return roles
    for record in document.get("selected") or ():
        if type(record) is dict and type(record.get("sample_id")) is str:
            roles.setdefault(record["sample_id"], record.get("role"))
    return roles


def evaluate(paths):
    """Run the nine recorded checks and collect every mechanism error and evidence gap."""
    mechanism = []
    deviations = []
    protocol = load_protocol_document(paths.protocol_doc)
    dataset = load_or_record("dataset", paths.dataset, mechanism)
    split = load_or_record("split_manifest", paths.split_manifest, mechanism)
    pairs = load_or_record("pair_list", paths.pairs, mechanism)
    assignment = load_or_record("assignment", paths.assignment, mechanism)
    analysis_documents, analysis_missing = [], []
    for path in paths.analyses:
        target = Path(path)
        if not target.is_file():
            analysis_missing.append(str(path))
            continue
        document = load_or_record("analysis_manifest", path, mechanism)
        if document.value is not None:
            try:
                inspect_analysis(document.value, str(path))
            except ComparisonError as error:
                mechanism.append(error)
                continue
            analysis_documents.append((document.value, str(path)))
    if dataset.value is not None and not dataset.malformed:
        try:
            inspect_dataset(dataset.value)
        except ComparisonError as error:
            mechanism.append(error)
            dataset = replace(dataset, value=None, malformed=True)
    if split.value is not None and not split.malformed:
        try:
            inspect_split(split.value)
        except ComparisonError as error:
            mechanism.append(error)
            split = replace(split, value=None, malformed=True)
    if dataset.value is not None and pairs.value is not None and not pairs.malformed:
        try:
            inspect_pair_list(pairs.value, dataset_version=dataset.value["dataset_version"])
        except ComparisonError as error:
            mechanism.append(error)
            pairs = replace(pairs, value=None, malformed=True)
    if assignment.value is not None and not assignment.malformed:
        try:
            inspect_assignment(assignment.value)
        except ComparisonError as error:
            mechanism.append(error)
            assignment = replace(assignment, value=None, malformed=True)
    sessions = list(load_sessions(paths.sessions) or ())
    expected_version = (dataset.value or {}).get("dataset_version")
    expected_digest = (split.value or {}).get("split_manifest_digest")
    for index, summary in enumerate(sessions):
        if summary.validate_exit != 0 or not summary.counted:
            continue
        problems = session_field_problems(summary, dataset_version=expected_version,
                                          split_manifest_digest=expected_digest)
        if problems:
            sessions[index] = replace(summary, counted=False, reason="session_field_mismatch",
                                      ratings=(),
                                      violations=tuple(summary.violations) + tuple(problems))
    try:
        jev_runs = load_jev_runs(paths.jev_runs)
    except ComparisonError as error:
        mechanism.append(error)
        jev_runs = ()
    # A ratings source inside the repository or tests/ may never be evidence.
    if sessions:
        for summary in sessions:
            try:
                rating_source_problem(summary.directory)
            except ComparisonError as error:
                mechanism.append(error)
                break
    paired = pair_records(pairs.value)
    roles = dataset_roles(dataset.value)
    partitions = split_partitions(split.value) if split.value is not None else None
    held_out_kicks = set(partitions["held_out"]["kicks"]) if partitions else set()
    held_out_basses = set(partitions["held_out"]["basses"]) if partitions else set()
    counts = Counter()
    membership_problems, assignment_problems, rated_problems = [], [], []
    if pairs.value is not None and partitions is not None:
        table = assignment_table(assignment.value) if assignment.value is not None else None
        for pair in paired:
            kick, bass = pair["kick_sample_id"], pair["bass_sample_id"]
            if roles.get(kick) != "kick" or roles.get(bass) not in ("bass", "sub-bass"):
                membership_problems.append(("unknown_sample_id", pair["pair_id"]))
                continue
            if kick not in held_out_kicks or bass not in held_out_basses:
                membership_problems.append(("pair_not_held_out", pair["pair_id"]))
            if table is not None:
                assigned = table.get(pair["pair_id"], ())
                if len(set(assigned)) < MIN_RATINGS_PER_PAIR:
                    assignment_problems.append(("pair_unassigned", pair["pair_id"]))
    counted = tuple(sorted({summary.evaluator_id for summary in sessions or ()
                            if summary.counted and summary.evaluator_id}))
    ratings = tuple(rating for summary in sessions or () if summary.counted
                    for rating in summary.ratings)
    table = ratings_by_pair(ratings)
    sampled_pair_ids = {pair["pair_id"] for pair in paired}
    for pair_id in table:
        if pair_id not in sampled_pair_ids:
            rated_problems.append(("rated_pair_not_sampled", pair_id))
    aggregates = pair_aggregates(table)
    if pairs.value is not None and partitions is not None:
        queries_in = sampled_queries(paired)
    else:
        queries_in = {}
    samples, sample_problems = analysis_samples(analysis_documents)
    used_ids = {pair["kick_sample_id"] for pair in paired} | {pair["bass_sample_id"]
                                                              for pair in paired}
    missing_entries = sorted(sample_id for sample_id in used_ids if sample_id not in samples)
    versions = analysis_versions(samples, used_ids)
    if sample_problems and not mechanism:
        mechanism.extend(ComparisonError(code, "an analysis entry is unusable: " + str(sample_id))
                         for code, sample_id in sample_problems)
    if len(versions) > 1:
        mechanism.append(ComparisonError("multiple_analysis_versions",
                                         "the run resolves more than one analysis_version"))
    if missing_entries and analysis_documents:
        mechanism.append(ComparisonError("missing_analysis_entry",
                                         "a used held-out sample has no complete analysis entry"
                                         " (" + str(len(missing_entries)) + " samples)"))
    queries = build_queries(queries_in, aggregates, paired, samples,
                            analyses_available=bool(analysis_documents))
    # --- the nine recorded checks, in order --------------------------------
    items = []
    if not protocol.present:
        items.append(Finding("protocol_document", "protocol_document_missing",
                             "PROTOCOL_VERSION = " + PROTOCOL_VERSION, "absent", "missing"))
    elif protocol.version != PROTOCOL_VERSION:
        items.append(Finding("protocol_document", "protocol_version_mismatch",
                             "PROTOCOL_VERSION = " + PROTOCOL_VERSION,
                             str(protocol.version), "inconsistent"))
    else:
        items.append(Finding("protocol_document", "ok", "PROTOCOL_VERSION = " + PROTOCOL_VERSION,
                             str(protocol.version), "ok"))
    digest_value = split.value.get("split_manifest_digest") if split.value and not split.malformed \
        else None
    if not split.present:
        items.append(Finding("split_manifest", "split_manifest_missing",
                             "schema_version 1.0 and split_manifest_digest", "absent", "missing"))
    elif split.malformed:
        items.append(Finding("split_manifest", "schema_mismatch",
                             "schema_version 1.0 and split_manifest_digest", "malformed",
                             "malformed"))
    else:
        items.append(Finding("split_manifest", "ok", "schema_version 1.0 and split_manifest_digest",
                             str(digest_value), "ok"))
    pair_digest = pairs.value.get("split_manifest_digest") if pairs.value and not pairs.malformed \
        else None
    if not pairs.present:
        items.append(Finding("pair_list", "pair_list_missing",
                             "schema_version 1.0 and split_manifest_digest = "
                             + (str(digest_value) if digest_value else "the split manifest's"),
                             "absent", "missing"))
    elif pairs.malformed:
        items.append(Finding("pair_list", "schema_mismatch",
                             "schema_version 1.0 and split_manifest_digest = "
                             + (str(digest_value) if digest_value else "the split manifest's"),
                             "malformed", "malformed"))
    elif digest_value is not None and pair_digest != digest_value:
        items.append(Finding("pair_list", "split_manifest_digest_mismatch",
                             "split_manifest_digest = " + str(digest_value), str(pair_digest),
                             "inconsistent"))
    else:
        items.append(Finding("pair_list", "ok",
                             "split_manifest_digest = " + str(digest_value), str(pair_digest),
                             "ok"))
    if not pairs.present or not split.present:
        items.append(Finding("pair_membership", "missing_prerequisite",
                             "every sampled pair's kick and bass are held-out members of the split",
                             "0 of 0 pairs checkable", "missing"))
    elif membership_problems:
        items.append(Finding("pair_membership", membership_problems[0][0],
                             "every sampled pair's kick and bass are held-out members of the split",
                             str(len(membership_problems)) + " of " + str(len(paired))
                             + " pairs violate it", "inconsistent",
                             detail=membership_problems[0][1]))
    else:
        items.append(Finding("pair_membership", "ok",
                             "every sampled pair's kick and bass are held-out members of the split",
                             str(len(paired)) + " of " + str(len(paired)) + " pairs held-out",
                             "ok"))
    if not assignment.present:
        items.append(Finding("evaluator_assignment", "assignment_missing",
                             "every pair assigned to at least " + str(MIN_RATINGS_PER_PAIR)
                             + " distinct evaluators", "absent", "missing"))
    elif assignment.malformed:
        items.append(Finding("evaluator_assignment", "schema_mismatch",
                             "every pair assigned to at least " + str(MIN_RATINGS_PER_PAIR)
                             + " distinct evaluators", "malformed", "malformed"))
    elif assignment_problems:
        items.append(Finding("evaluator_assignment", "pair_unassigned",
                             "every pair assigned to at least " + str(MIN_RATINGS_PER_PAIR)
                             + " distinct evaluators",
                             str(len(assignment_problems)) + " of " + str(len(paired))
                             + " pairs short", "inconsistent",
                             detail=assignment_problems[0][1]))
    else:
        items.append(Finding("evaluator_assignment", "ok",
                             "every pair assigned to at least " + str(MIN_RATINGS_PER_PAIR)
                             + " distinct evaluators",
                             str(len(paired)) + " of " + str(len(paired)) + " pairs assigned",
                             "ok"))
    sampled_count, query_count = len(paired), len(queries_in)
    if sampled_count < MIN_HELDOUT_PAIRS or query_count < MIN_HELDOUT_QUERIES:
        items.append(Finding("pair_counts", "pair_counts_below_minimum",
                             str(MIN_HELDOUT_PAIRS) + " sampled pairs over "
                             + str(MIN_HELDOUT_QUERIES) + " held-out query kicks",
                             str(sampled_count) + " of " + str(MIN_HELDOUT_PAIRS)
                             + " sampled pairs; " + str(query_count) + " of "
                             + str(MIN_HELDOUT_QUERIES) + " query kicks", "below_minimum"))
    else:
        items.append(Finding("pair_counts", "ok",
                             str(MIN_HELDOUT_PAIRS) + " sampled pairs over "
                             + str(MIN_HELDOUT_QUERIES) + " held-out query kicks",
                             str(sampled_count) + " sampled pairs; " + str(query_count)
                             + " query kicks", "ok"))
    if not sessions:
        items.append(Finding("rated_pairs", "ratings_missing",
                             "every rated pair is a sampled pair",
                             "0 sessions, 0 rated pairs", "missing"))
    elif rated_problems:
        items.append(Finding("rated_pairs", "rated_pair_not_sampled",
                             "every rated pair is a sampled pair",
                             str(len(rated_problems)) + " of " + str(len(table))
                             + " rated pairs outside the pair list", "inconsistent"))
    else:
        items.append(Finding("rated_pairs", "ok", "every rated pair is a sampled pair",
                             str(len(table)) + " of " + str(len(table)) + " rated pairs sampled",
                             "ok"))
    if not analysis_documents:
        items.append(Finding("analysis_manifests", "analysis_manifest_missing",
                             "every held-out sample used resolves to one complete entry with a"
                             " single analysis_version",
                             str(len(analysis_missing)) + " of " + str(len(paths.analyses))
                             + " analysis manifests present", "missing"))
    elif missing_entries:
        items.append(Finding("analysis_manifests", "missing_analysis_entry",
                             "every held-out sample used resolves to one complete entry with a"
                             " single analysis_version",
                             str(len(missing_entries)) + " of " + str(len(used_ids))
                             + " used samples unresolved", "inconsistent"))
    elif len(versions) > 1:
        items.append(Finding("analysis_manifests", "multiple_analysis_versions",
                             "a single analysis_version across the run", str(list(versions)),
                             "inconsistent"))
    else:
        items.append(Finding("analysis_manifests", "ok",
                             "a single analysis_version across the run",
                             str(list(versions)) + " over " + str(len(used_ids))
                             + " used samples", "ok"))
    provenance = Counter(record.get("provenance_kind") for record in
                         (dataset.value.get("selected") or ()) if type(record) is dict) \
        if dataset.value is not None else Counter()
    used_records = [record for record in (dataset.value.get("selected") or ())
                    if type(record) is dict and record.get("sample_id") in used_ids] \
        if dataset.value is not None else []
    synthetic = [record for record in used_records
                 if record.get("provenance_kind") != "real_library_sample"]
    if dataset.value is None:
        items.append(Finding("dataset_provenance", "dataset_missing",
                             "every dataset record used has provenance_kind = real_library_sample",
                             "absent", "missing"))
    elif synthetic:
        items.append(Finding("dataset_provenance", "synthetic_fixture_record",
                             "every dataset record used has provenance_kind = real_library_sample",
                             str(len(synthetic)) + " of " + str(len(used_records))
                             + " used records are not real_library_sample", "inconsistent"))
    else:
        items.append(Finding("dataset_provenance", "ok",
                             "every dataset record used has provenance_kind = real_library_sample",
                             str(len(used_records)) + " used records; pool provenance "
                             + canonical(dict(sorted(provenance.items()))), "ok"))
    for summary in sessions or ():
        if summary.validate_exit != 0 or not summary.counted or summary.violations:
            deviations.append({"kind": "session", "session_id": summary.session_id,
                               "validate_exit": summary.validate_exit,
                               "violations": list(summary.violations),
                               "reason": summary.reason or "validate_or_counting"})
    pool = dataset_pool(dataset.value)
    split_counts = {"tuning_kicks": len(partitions["tuning"]["kicks"]) if partitions else 0,
                    "tuning_basses": len(partitions["tuning"]["basses"]) if partitions else 0,
                    "held_out_kicks": len(held_out_kicks), "held_out_basses": len(held_out_basses)}
    accounting = {
        "pool_kicks": pool["kicks"], "pool_basses": pool["basses"],
        "pool_records": pool["records"], "pool_reserves": pool["reserves"],
        "sampled_pairs": sampled_count, "sampled_queries": query_count,
        "eligible_queries": sum(1 for query in queries if query.eligible),
        "rated_pairs": len(aggregates), "rated_pairs_sampled": sum(1 for pair_id in aggregates
                                                                  if pair_id in sampled_pair_ids),
        "multi_rated_pairs": sum(1 for item in table.values()
                                 if len({rating.evaluator_id for rating in item})
                                 >= MIN_RATINGS_PER_PAIR),
        "valid_ratings": len(ratings), "counted_evaluators": len(counted),
        "sessions": len(sessions or ()), "tooling_sessions": sum(1 for summary in sessions or ()
                                                                 if summary.tooling),
        "excluded_sessions": sum(1 for summary in sessions or ()
                                 if summary.validate_exit != 0 or not summary.counted),
        "recognised_ratings": sum(summary.recognised for summary in sessions or ()),
        "analysed_samples": len(samples), "used_samples": len(used_ids),
        "missing_analysis_entries": len(missing_entries),
        "live_jev_outcomes": 0, "jev_runs": len(jev_runs),
        "dataset_records": len(used_records), "synthetic_fixture_records": len(synthetic),
        "pair_coverage": ((len(aggregates) / sampled_count) if sampled_count else None),
    }
    return Findings(
        paths=paths, protocol=protocol, dataset=dataset, split=split, pairs=pairs,
        assignment=assignment, analyses=tuple(analysis_documents), sessions=tuple(sessions or ()),
        jev_runs=jev_runs, items=tuple(items), mechanism=tuple(mechanism), query=queries,
        ratings=ratings, evaluators=counted, pool=pool, split_counts=split_counts,
        accounting=accounting, deviations=deviations, analysis_versions=versions,
        samples=samples, analyses_available=bool(analysis_documents),
        pair_ratings=table, tuning={"claim": paths.tuning_claim}, run_identity={})
# --- the evaluation-scoped latency harness ---------------------------------

LATENCY_STAGES = ("feature_load", "filter", "retrieval", "jev", "ranking")
RETRIEVAL_NOT_IMPLEMENTED = "not_implemented"
INVALID_REQUEST = "request_failed"
INVALID_WARM = "warm_without_memo_hit"


@dataclass
class LatencyMemo:
    """The runner's evaluation-scoped memo; not the #26 product decision cache."""

    values: dict
    hits: int
    misses: int

    def key(self, *, arm, query_id, candidate_ids, dataset_version, analysis_version,
            ranking_version):
        return canonical({"arm": arm, "query": query_id, "candidates": list(candidate_ids),
                          "dataset_version": dataset_version,
                          "analysis_version": analysis_version,
                          "ranking_version": ranking_version})

    def get(self, key):
        if key in self.values:
            self.hits += 1
            return self.values[key]
        self.misses += 1
        return None

    def put(self, key, value):
        self.values[key] = value

    def as_dict(self):
        return {"entries": len(self.values), "hits": self.hits, "misses": self.misses,
                "key_fields": ["arm", "query", "candidates", "dataset_version",
                               "analysis_version", "ranking_version"],
                "note": "evaluation-scoped memo; not the #26 product decision cache"}


def time_request(arm, query, samples, *, dataset_version, outcome_table, memo=None):
    """The runner's request function: feature load, filter, retrieval, Jev and ranking.

    This is the timed path. It is the only thing the latency numbers measure: no client,
    no IPC and no audition start is included.
    """
    stages, total_start = {}, time.perf_counter()
    started = time.perf_counter()
    kick = samples.get(query.kick_id)
    candidates = [samples[candidate_id] for candidate_id in query.eligible
                  if candidate_id in samples]
    stages["feature_load"] = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    if kick is not None and candidates:
        availability = {sample.sample_id: Availability.AVAILABLE for sample in candidates}
        availability[kick.sample_id] = Availability.AVAILABLE
        filter_candidates(kick, candidates, policy=FilterPolicy(), availability=availability)
    stages["filter"] = (time.perf_counter() - started) * 1000.0
    stages["retrieval"] = RETRIEVAL_NOT_IMPLEMENTED
    started = time.perf_counter()
    if arm in JEV_ARMS and outcome_table is not None:
        jev_requests_for((query,), samples)
    stages["jev"] = (time.perf_counter() - started) * 1000.0 if arm in JEV_ARMS else None
    started = time.perf_counter()
    order = None
    if kick is not None and candidates:
        order = build_arm(arm, kick=kick, candidates=query.eligible, samples=samples,
                          dataset_version=dataset_version, outcome_table=outcome_table)
    stages["ranking"] = (time.perf_counter() - started) * 1000.0
    stages["total"] = (time.perf_counter() - total_start) * 1000.0
    return {"arm": arm, "query": query.kick_id, "stages": stages,
            "order": list(order.order) if order is not None else []}


def latency_child_command(request_path, output_path):
    return (sys.executable, "-m", "backend.evaluation.comparison", "latency-request",
            "--request", str(request_path), "--out", str(output_path))


def default_spawn(command):
    """Run one cold sample as a fresh process with file-backed stdio, never a pipe."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")])
    try:
        result = subprocess.run(list(command), cwd=str(REPOSITORY_ROOT),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=environment, timeout=VALIDATE_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode


def latency_plan(queries, arms, *, per_arm=MIN_LATENCY_REQUESTS_PER_ARM,
                 distinct_kicks=MIN_LATENCY_QUERY_KICKS):
    """At least per_arm requests per arm spread over at least distinct_kicks query kicks."""
    usable = [query for query in queries if query.eligible]
    plan = {}
    for arm in arms:
        requests = []
        if usable:
            kicks = usable[:max(1, distinct_kicks)]
            for index in range(per_arm):
                query = kicks[(index // 2) % len(kicks)]
                state = "cold" if index % 2 == 0 else "warm"
                requests.append((arm, query, state, index))
        plan[arm] = tuple(requests)
    return plan


def hardware_block():
    """The recorded hardware and process state; no host name and no timestamp."""
    return {"os": platform.system(), "os_release": platform.release(),
            "machine": platform.machine(), "cpu": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(), "python": platform.python_version(),
            "process_state": "cold: one fresh child process per cold sample;"
                             " warm: the identical request repeated in-process on a memo hit",
            "memo": "evaluation-scoped memo; not the #26 product decision cache"}


def measure_latency(plan, queries, samples, *, dataset_version, outcome_table, run_directory,
                    analysis_manifests=(), jev_runs=(), spawn=None, memo=None):
    """Run the latency plan; invalid samples are counted, never invented or replaced."""
    spawn = default_spawn if spawn is None else spawn
    memo = LatencyMemo({}, 0, 0) if memo is None else memo
    table = {"arms": {}, "samples": [], "invalid": [], "memo": memo,
             "hardware": hardware_block(), "stages": list(LATENCY_STAGES),
             "retrieval": RETRIEVAL_NOT_IMPLEMENTED,
             "command_template": ("python -m backend.evaluation.comparison"
                                  " latency-request --request"
                                  " <run-dir>/latency/<request>.json --out"
                                  " <run-dir>/latency/<request>.out.json")}
    for arm, requests in plan.items():
        samples_list, invalid = [], []
        for _, query, state, index in requests:
            key = memo.key(arm=arm, query_id=query.kick_id, candidate_ids=query.eligible,
                           dataset_version=dataset_version,
                           analysis_version=sorted({samples[candidate_id].analysis_version
                                                    for candidate_id in query.eligible
                                                    if candidate_id in samples}),
                           ranking_version=RANKING_VERSION if arm != "hybrid"
                           else HYBRID_RANKING_VERSION)
            if state == "warm":
                cached = memo.get(key)
                if cached is None:
                    invalid.append({"arm": arm, "query": query.kick_id, "state": state,
                                    "reason": INVALID_WARM})
                    continue
                record = dict(cached)
                record["state"] = "warm"
                samples_list.append(record)
                continue
            memo.misses += 1  # a cold measurement never finds a memo entry
            request_path = Path(run_directory) / "latency" / (str(index) + "-" + arm + ".json")
            output_path = Path(run_directory) / "latency" / (str(index) + "-" + arm + ".out.json")
            write_text(request_path, canonical({
                "arm": arm, "query": query.kick_id, "candidates": list(query.eligible),
                "dataset_version": dataset_version, "memo_key": key,
                "analysis_manifests": [str(path) for path in analysis_manifests],
                "jev_runs": [str(path) for path in jev_runs]}) + "\n")
            command = latency_child_command(request_path, output_path)
            code = spawn(command)
            record = None
            if code == 0 and output_path.is_file():
                try:
                    record = json.loads(output_path.read_text(encoding="utf-8"))
                except ValueError:
                    record = None
            if record is None:
                invalid.append({"arm": arm, "query": query.kick_id, "state": state,
                                "reason": INVALID_REQUEST, "command": list(command)})
                continue
            record["state"] = "cold"
            record["command"] = list(command)
            memo.put(key, record)
            samples_list.append(record)
        table["arms"][arm] = summarize_latency(arm, samples_list, invalid)
        table["samples"].extend(samples_list)
        table["invalid"].extend(invalid)
    table["memo"] = memo.as_dict()
    return table


def summarize_latency(arm, samples_list, invalid):
    """Per-arm p50/p95 per stage, the sample counts and the insufficiency reasons."""
    valid = [record for record in samples_list]
    kicks = {record["query"] for record in valid}
    summary = {"arm": arm, "valid_samples": len(valid), "cold_samples":
               sum(1 for record in valid if record["state"] == "cold"),
               "warm_samples": sum(1 for record in valid if record["state"] == "warm"),
               "distinct_query_kicks": len(kicks), "invalid_samples": len(invalid),
               "stages": {}, "reasons": []}
    for stage in LATENCY_STAGES + ("total",):
        values = [record["stages"].get(stage) for record in valid
                  if type(record["stages"].get(stage)) in (int, float)]
        summary["stages"][stage] = {"p50": percentile(values, 0.50),
                                    "p95": percentile(values, 0.95), "count": len(values)}
    if summary["valid_samples"] < MIN_LATENCY_REQUESTS_PER_ARM:
        summary["reasons"].append("latency_samples_below_minimum")
    if summary["distinct_query_kicks"] < MIN_LATENCY_QUERY_KICKS:
        summary["reasons"].append("latency_query_kicks_below_minimum")
    if invalid:
        summary["reasons"].append("invalid_samples_present")
    return summary


def latency_request(arguments, stdout):
    """One cold sample: the timed request function in its own fresh process."""
    request = read_json(arguments.request)
    samples = {}
    for path in request.get("analysis_manifests", ()):
        document = read_json(path)
        inspect_analysis(document, str(path))
        entries = document["entries"]
        for entry in entries.values():
            if entry["status"] == "complete" and entry["result"] is not None:
                samples[entry["sample_id"]] = Sample.from_dict(entry["result"])
    outcome_table = {}
    candidates = tuple(request["candidates"])
    kick_id = request["query"]
    kick = samples.get(kick_id)
    if kick is None:
        raise ComparisonError("schema_mismatch", "the request names an unanalysed query kick")
    query = Query(kick_id, candidates, candidates, {}, {})
    record = time_request(request["arm"], query, samples,
                          dataset_version=request["dataset_version"],
                          outcome_table=outcome_table, memo=None)
    write_text(arguments.out, canonical(record) + "\n")
    print(canonical({"arm": request["arm"], "query": kick_id,
                     "total_ms": record["stages"]["total"]}), file=stdout, flush=True)
    return EXIT_OK


def write_text(path, text):
    """Write one output atomically; the only writer in this module."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
# --- the comparison itself --------------------------------------------------

def arm_evidence_source(findings, outcome_table):
    """live only when a recorded outcome comes from a source == "interface" run."""
    for outcome, run in outcome_table.values():
        if run.source == "interface":
            return "live"
    return "none"


def live_outcome_count(outcome_table):
    return sum(1 for outcome, run in outcome_table.values() if run.source == "interface")


def live_jev_run(findings):
    """The one explicitly configured live call; without credentials it records absence."""
    requests = jev_requests_for(findings.query, findings.samples)
    if not requests:
        return None
    return score_questions(requests, config=JevAdapterConfig.from_env(), credentials=None)


def analyse(findings, paths, *, latency, outcome_table):
    """Build the four arms, their metrics, the intervals and the lifts."""
    runs = findings.jev_runs
    evidence_source = arm_evidence_source(findings, outcome_table)
    dataset_version = (findings.dataset.value or {}).get("dataset_version", "")
    arms = {}
    for arm in ARM_NAMES:
        evaluations = []
        version, weight_table = "not recorded", "not recorded"
        mode_counts, status_counts, variant_disagreements = Counter(), Counter(), 0
        for query in findings.query:
            kick = findings.samples.get(query.kick_id)
            placeholder = ArmOrder(arm, (), (), version, weight_table)
            if kick is None or not query.eligible:
                evaluations.append(evaluate_query(placeholder, query))
                continue
            try:
                if arm == "hybrid":
                    baseline = rank_candidates(kick, [findings.samples[candidate_id]
                                                      for candidate_id in query.eligible],
                                               policy=RankingPolicy())
                    arm_order = build_arm(arm, kick=kick, candidates=query.eligible,
                                          samples=findings.samples,
                                          dataset_version=dataset_version,
                                          baseline=baseline, outcome_table=outcome_table)
                else:
                    arm_order = build_arm(arm, kick=kick, candidates=query.eligible,
                                          samples=findings.samples,
                                          dataset_version=dataset_version,
                                          outcome_table=outcome_table)
            except (RankingInputError, FilterInputError) as error:
                failed = evaluate_query(placeholder, query)
                evaluations.append(replace(failed, eligible=False,
                                           reason="arm_error", error_code=error.code))
                continue
            version, weight_table = arm_order.version, arm_order.weight_table_id
            if arm_order.mode is not None:
                mode_counts[arm_order.mode] += 1
            if arm_order.jev_status is not None:
                status_counts[arm_order.jev_status] += 1
            evaluations.append(evaluate_query(arm_order, query))
        summary = arm_summary(evaluations)
        if arm == "jev-only":
            for query in findings.query:
                kick = findings.samples.get(query.kick_id)
                if kick is None or not query.eligible:
                    continue
                primary = next((item for item in evaluations if item.query_id == query.kick_id),
                               None)
                variant = build_arm("jev-only", kick=kick, candidates=query.eligible,
                                    samples=findings.samples, dataset_version=dataset_version,
                                    outcome_table=outcome_table, variant="probability")
                if primary is not None and primary.order != variant.order:
                    variant_disagreements += 1
        evaluations = tuple(evaluations)
        status, reason = arm_status(arm, summary, findings, evidence_source, mode_counts)
        arms[arm] = {"arm": arm, "version": version, "weight_table_id": weight_table,
                     "summary": summary, "evaluations": evaluations, "status": status,
                     "reason": reason, "evidence_source": evidence_source,
                     "mode_counts": dict(mode_counts), "status_counts": dict(status_counts),
                     "error_codes": sorted({item.error_code for item in evaluations
                                            if item.error_code}),
                     "identical_orderings": 0,
                     "unscored_share": summary["unscored_share"]["value"],
                     "variant_disagreements": variant_disagreements,
                     "publishable": evidence_source == "live" if arm in JEV_ARMS else True}
    eligible_sets = [{item.query_id for item in arms[arm]["evaluations"] if item.eligible}
                     for arm in ARM_NAMES]
    intersection = set.intersection(*eligible_sets) if eligible_sets else set()
    lifts = build_lifts(arms)
    identical = count_identical_orderings({arm: {item.query_id: item.order
                                                for item in arms[arm]["evaluations"]
                                                if item.eligible} for arm in ARM_NAMES})
    for arm in arms:
        arms[arm]["identical_orderings"] = identical.get(arm, 0)
    agreement = agreement_table(findings.pair_ratings)
    queries_with_11 = sum(1 for query in findings.query
                          if query.eligible and query.rated_size >= MIN_RATED_CANDIDATES_FOR_TOP10_GATE)
    topk_reachable = bool(findings.query) and queries_with_11 == len(
        [query for query in findings.query if query.eligible])
    return {"arms": arms, "lifts": lifts, "runs": runs,
            "evidence_source": evidence_source,
            "live_jev_outcomes": live_outcome_count(outcome_table),
            "memo": latency["memo"], "latency": latency, "agreement": agreement,
            "leave_one_out": (), "queries_with_11_rated_candidates": queries_with_11,
            "jev_variant_disagreements": arms["jev-only"]["variant_disagreements"],
            "intersection": len(intersection),
            "eligible_queries": len([query for query in findings.query if query.eligible]),
            "topk_reachable": topk_reachable, "double_runs": sum(1 for run in runs
                                                                 if run.source == "double")}


def arm_status(arm, summary, findings, evidence_source, mode_counts):
    """An arm is insufficient until its evidence minimums are met; never a silent zero."""
    if not findings.query:
        return "insufficient", "no_held_out_query"
    if not findings.analyses_available:
        return "insufficient", "no_analysis_manifests"
    if arm in JEV_ARMS and evidence_source != "live":
        return "insufficient", NO_LIVE_JEV_REASON
    if arm == "hybrid" and mode_counts and not mode_counts.get(MODE_HYBRID):
        return "insufficient", FALLBACK_REASON
    if summary["eligible_queries"] == 0:
        return "insufficient", "no_eligible_query"
    return "evaluated", None


def build_lifts(arms):
    """Every lift of Section "Metrics": per-query paired differences and their intervals."""
    lifts = {}
    for arm in ARM_NAMES:
        for other in ARM_NAMES:
            if arm == other:
                continue
            differences = paired_differences(arms[arm]["evaluations"], arms[other]["evaluations"])
            per_metric = {}
            for metric in METRIC_NAMES:
                values = [difference for _, name, difference in differences if name == metric]
                per_metric[metric] = {"value": mean(values), "queries": len(values),
                                      "interval": bootstrap_values(values, label=arm + "-" + other
                                                                   + "-" + metric)["interval"]}
            lifts[arm + " over " + other] = per_metric
    return lifts


def count_identical_orderings(orders):
    """Queries where two arms produce the identical ordering, counted, never dropped.

    orders maps each arm to its eligible queries' orderings; a shared query whose two
    orderings are equal is counted for both arms and stays in every denominator.
    """
    counts = Counter()
    names = sorted(orders)
    for first in range(len(names)):
        for second in range(first + 1, len(names)):
            one, other = names[first], names[second]
            shared = set(orders[one]) & set(orders[other])
            identical = sum(1 for query_id in shared
                            if orders[one][query_id] == orders[other][query_id])
            counts[one] += identical
            counts[other] += identical
    return counts


def rebuild_queries(findings, aggregates):
    """The same queries with one evaluator's ratings removed from the rated subsets."""
    index = {}
    for pair in pair_records(findings.pairs.value):
        index[(pair["kick_sample_id"], pair["bass_sample_id"])] = pair["pair_id"]
    queries = []
    for query in findings.query:
        rated, counts = {}, {}
        for candidate in query.eligible:
            pair_id = index.get((query.kick_id, candidate))
            if pair_id in aggregates:
                rated[candidate] = aggregates[pair_id]["value"]
                counts[candidate] = aggregates[pair_id]["count"]
        queries.append(Query(query.kick_id, query.sampled, query.eligible, rated, counts,
                             query.filter_error))
    return tuple(queries)


def recompute_with_aggregates(findings, arms, aggregates):
    """Re-evaluate the same arm orders against a rebuilt rated subset."""
    values = {}
    for arm in ARM_NAMES:
        previous = {item.query_id: item for item in arms[arm]["evaluations"]}
        evaluations = []
        for query in rebuild_queries(findings, aggregates):
            item = previous.get(query.kick_id)
            order = item.order if item is not None else ()
            scored = item.scored if item is not None else ()
            evaluations.append(evaluate_query(ArmOrder(arm, order, scored, arms[arm]["version"],
                                                       arms[arm]["weight_table_id"]), query))
        values[arm] = arm_summary(tuple(evaluations))
    return values


def point_class(value, minimum):
    """Which side of one minimum a recomputed point estimate sits on."""
    if value is None:
        return "absent"
    return "meets_minimum" if value >= minimum else "below_minimum"


def leave_one_evaluator_out(findings, arms, gates):
    """Recompute every gated metric with each evaluator left out in turn, class by class."""
    variants = []
    evaluators = sorted({rating.evaluator_id for rating in findings.ratings})
    for evaluator in evaluators:
        aggregates = pair_aggregates(findings.pair_ratings, drop_evaluators=(evaluator,))
        values = recompute_with_aggregates(findings, arms, aggregates)
        flipped = []
        for gate in gates:
            if gate.cls != "minimum" or gate.metric is None or gate.metric_arm is None \
                    or gate.minimum is None:
                continue
            variant = values[gate.metric_arm][gate.metric]["value"]
            if point_class(variant, gate.minimum) != point_class(gate.point, gate.minimum):
                flipped.append(gate.constant)
        variants.append({"evaluator": evaluator, "flipped": flipped,
                         "values": {arm: {metric: values[arm][metric]["value"]
                                          for metric in METRIC_NAMES} for arm in values}})
    return variants
# --- gate rows --------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    """One reported gate row: its constant, class, arms, value, interval and verdict."""

    constant: str
    arms: tuple
    value: str
    interval: str
    verdict: str
    shortfall: str
    source: str
    minimum: float | None = None
    metric_arm: str | None = None
    metric: str | None = None
    point: float | None = None

    @property
    def cls(self):
        return FROZEN_CONSTANTS[self.constant][1]

    def as_dict(self):
        return {"constant": self.constant, "value": FROZEN_CONSTANTS[self.constant][0],
                "class": self.cls, "arms": list(self.arms), "observed": self.value,
                "interval": self.interval, "verdict": self.verdict,
                "shortfall": self.shortfall, "source": self.source}


def shown(value, points=4):
    return "not computed" if value is None else format(value, "." + str(points) + "f")


def metric_gate(constant, arm, metric, *, analysis, minimum, inconclusive=False):
    """One metric gate: the arm's point estimate and its bootstrap interval."""
    summary = analysis["arms"][arm]["summary"][metric]
    value = summary["value"]
    interval = None
    evidence = analysis["arms"][arm]["status"] != "insufficient"
    if value is not None and evidence:
        interval = bootstrap_values(
            [item.metrics[metric] for item in analysis["arms"][arm]["evaluations"]
             if item.eligible and item.metrics[metric] is not None],
            label=arm + "-" + metric)["interval"]
    verdict = verdict_for(value, interval, minimum, evidence_met=evidence,
                          inconclusive=inconclusive)
    return Gate(constant, (arm,), shown(value), shown_interval(interval), verdict,
                "not computed" if value is None else "n of " + str(summary["queries"])
                + " eligible queries", "metric", minimum, arm, metric, value)


def lift_gate(constant, arm, other, metric, *, analysis, minimum):
    """One lift gate: the per-query paired difference and its interval."""
    entry = analysis["lifts"].get(arm + " over " + other, {}).get(metric)
    value = entry["value"] if entry else None
    interval = entry["interval"] if entry else None
    evidence = analysis["arms"][arm]["status"] != "insufficient"
    verdict = verdict_for(value, interval, minimum, evidence_met=evidence, lift=True)
    return Gate(constant, (arm,), shown_signed(value), shown_interval(interval), verdict,
                "n of " + str(entry["queries"]) + " paired queries" if entry else "0 paired queries",
                "lift", minimum, arm, metric, value)


def shown_signed(value):
    return "not computed" if value is None else ("+" if value >= 0 else "") + format(value, ".4f")


def shown_interval(interval, points=4):
    if not interval or interval[0] is None:
        return "not computed"
    return "[" + format(interval[0], "." + str(points) + "f") + ", " \
        + format(interval[1], "." + str(points) + "f") + "]"


def evidence_gate(constant, value, threshold, *, ok, shortfall):
    verdict = "met" if ok else "insufficient"
    return Gate(constant, ALL_ARMS, value, "not applicable (evidence minimum)", verdict,
                shortfall, "evidence", float(threshold))


def build_gates(findings, analysis, latency):
    """Every gate row the report carries: evidence, metric, lift, latency and the rest."""
    gates = []
    counts = findings.accounting
    gates.append(evidence_gate("MIN_POOL_KICKS", str(counts["pool_kicks"]) + " of "
                               + str(MIN_POOL_KICKS) + " pool kicks", MIN_POOL_KICKS,
                               ok=counts["pool_kicks"] >= MIN_POOL_KICKS,
                               shortfall="" if counts["pool_kicks"] >= MIN_POOL_KICKS
                               else str(MIN_POOL_KICKS - counts["pool_kicks"]) + " kicks short"))
    gates.append(evidence_gate("MIN_POOL_BASSES", str(counts["pool_basses"]) + " of "
                               + str(MIN_POOL_BASSES) + " pool basses", MIN_POOL_BASSES,
                               ok=counts["pool_basses"] >= MIN_POOL_BASSES,
                               shortfall="" if counts["pool_basses"] >= MIN_POOL_BASSES
                               else str(MIN_POOL_BASSES - counts["pool_basses"]) + " basses short"))
    gates.append(evidence_gate("MIN_EVALUATORS", str(counts["counted_evaluators"]) + " of "
                               + str(MIN_EVALUATORS) + " counted evaluators", MIN_EVALUATORS,
                               ok=counts["counted_evaluators"] >= MIN_EVALUATORS,
                               shortfall=str(counts["counted_evaluators"]) + " of "
                               + str(MIN_EVALUATORS) + " counted evaluators"))
    gates.append(evidence_gate("MIN_RATINGS_PER_PAIR", str(counts["rated_pairs"])
                               + " of " + str(counts["sampled_pairs"]) + " sampled pairs hold "
                               + str(MIN_RATINGS_PER_PAIR) + " valid ratings",
                               MIN_RATINGS_PER_PAIR,
                               ok=counts["rated_pairs"] >= 1 and counts["multi_rated_pairs"] >= 1,
                               shortfall=str(counts["rated_pairs"]) + " of "
                               + str(counts["sampled_pairs"]) + " sampled pairs rated"))
    gates.append(evidence_gate("MIN_HELDOUT_PAIRS", str(counts["sampled_pairs"]) + " of "
                               + str(MIN_HELDOUT_PAIRS) + " sampled pairs", MIN_HELDOUT_PAIRS,
                               ok=counts["sampled_pairs"] >= MIN_HELDOUT_PAIRS,
                               shortfall=str(counts["sampled_pairs"]) + " of "
                               + str(MIN_HELDOUT_PAIRS) + " rated pairs"))
    gates.append(evidence_gate("MIN_HELDOUT_QUERIES", str(findings.accounting["eligible_queries"])
                               + " of " + str(MIN_HELDOUT_QUERIES) + " eligible held-out queries",
                               MIN_HELDOUT_QUERIES,
                               ok=counts["eligible_queries"] >= MIN_HELDOUT_QUERIES,
                               shortfall=str(counts["eligible_queries"]) + " of "
                               + str(MIN_HELDOUT_QUERIES) + " eligible held-out queries"))
    coverage = counts["pair_coverage"]
    gates.append(evidence_gate("MIN_PAIR_COVERAGE",
                               "not computable (0 of 0 sampled pairs)" if coverage is None
                               else shown(coverage) + " rated share of sampled pairs",
                               MIN_PAIR_COVERAGE, ok=coverage is not None
                               and coverage >= MIN_PAIR_COVERAGE,
                               shortfall="0 of " + str(counts["sampled_pairs"])
                               + " sampled pairs rated" if coverage is None else ""))
    gates.append(evidence_gate("MIN_AGREEMENT_PAIRS", str(counts["multi_rated_pairs"]) + " of "
                               + str(MIN_AGREEMENT_PAIRS) + " pairs with at least two valid"
                               " ratings", MIN_AGREEMENT_PAIRS,
                               ok=counts["multi_rated_pairs"] >= MIN_AGREEMENT_PAIRS,
                               shortfall=str(counts["multi_rated_pairs"]) + " of "
                               + str(MIN_AGREEMENT_PAIRS) + " pairs with at least two valid"
                               " ratings"))
    deviation = analysis["agreement"]["mean_absolute_deviation"]
    gates.append(evidence_gate("MAX_MEAN_ABSOLUTE_DEVIATION",
                               "not computable (" + str(counts["multi_rated_pairs"])
                               + " of " + str(MIN_AGREEMENT_PAIRS) + " reportable pairs)"
                               if deviation is None else shown(deviation) + " rating points",
                               MAX_MEAN_ABSOLUTE_DEVIATION,
                               ok=deviation is not None
                               and deviation <= MAX_MEAN_ABSOLUTE_DEVIATION,
                               shortfall="0 of " + str(MIN_AGREEMENT_PAIRS)
                               + " pairs with at least two valid ratings" if deviation is None
                               else ""))
    shares = [analysis["arms"][arm]["summary"]["unscored_share"]["value"] for arm in ARM_NAMES]
    scored_share = None if any(share is None for share in shares) else 1.0 - max(shares)
    gates.append(evidence_gate("MIN_SCORED_SHARE_PER_QUERY",
                               "no eligible query" if scored_share is None
                               else "worst eligible query " + shown(scored_share)
                               + " scored share",
                               MIN_SCORED_SHARE_PER_QUERY,
                               ok=scored_share is not None
                               and scored_share >= MIN_SCORED_SHARE_PER_QUERY,
                               shortfall="0 of " + str(counts["eligible_queries"])
                               + " eligible queries"))
    gates.append(evidence_gate("MAX_RECOGNISED_RATE",
                               "0 sessions" if not counts["sessions"]
                               else str(counts["recognised_ratings"]) + " flagged of "
                               + str(counts["valid_ratings"]) + " ratings",
                               MAX_RECOGNISED_RATE, ok=bool(counts["sessions"]),
                               shortfall="0 of 1 session" if not counts["sessions"] else ""))
    gates.append(evidence_gate("MIN_LATENCY_REQUESTS_PER_ARM",
                               str(latency["arms"]["random"]["valid_samples"]) + " of "
                               + str(MIN_LATENCY_REQUESTS_PER_ARM) + " valid samples on the"
                               " least-sampled arm", MIN_LATENCY_REQUESTS_PER_ARM,
                               ok=all(entry["valid_samples"] >= MIN_LATENCY_REQUESTS_PER_ARM
                                      for entry in latency["arms"].values()),
                               shortfall="0 of " + str(MIN_LATENCY_REQUESTS_PER_ARM)
                               + " valid latency samples per arm"))
    gates.append(metric_gate("MIN_DSP_PAIRWISE_ACCURACY", "dsp-only", "pairwise_accuracy",
                             analysis=analysis, minimum=MIN_DSP_PAIRWISE_ACCURACY))
    gates.append(metric_gate("MIN_JEV_ONLY_PAIRWISE_ACCURACY", "jev-only", "pairwise_accuracy",
                             analysis=analysis, minimum=MIN_JEV_ONLY_PAIRWISE_ACCURACY,
                             inconclusive=bool(analysis["jev_variant_disagreements"])))
    gates.append(metric_gate("MIN_PAIRWISE_ACCURACY", "hybrid", "pairwise_accuracy",
                             analysis=analysis, minimum=MIN_PAIRWISE_ACCURACY))
    gates.append(lift_gate("MIN_LIFT_OVER_RANDOM", "hybrid", "random", "pairwise_accuracy",
                           analysis=analysis, minimum=MIN_LIFT_OVER_RANDOM))
    gates.append(lift_gate("MIN_LIFT_OVER_DSP", "hybrid", "dsp-only", "pairwise_accuracy",
                           analysis=analysis, minimum=MIN_LIFT_OVER_DSP))
    gates.append(metric_gate("MIN_TOP1_MARGIN", "hybrid", "top1_margin", analysis=analysis,
                             minimum=MIN_TOP1_MARGIN))
    reachable = analysis["topk_reachable"]
    for constant, minimum in (("MIN_TOP_K_MEAN", MIN_TOP_K_MEAN),
                              ("MIN_TOP_K_MEAN_LIFT_OVER_RANDOM",
                               MIN_TOP_K_MEAN_LIFT_OVER_RANDOM),
                              ("MIN_TOP_K_MEAN_LIFT_OVER_DSP", MIN_TOP_K_MEAN_LIFT_OVER_DSP)):
        verdict = "insufficient" if not reachable else "inconclusive"
        gates.append(Gate(constant, ALL_ARMS, "not computed (k = 5 is descriptive)",
                          "not computed", verdict,
                          str(analysis["queries_with_11_rated_candidates"]) + " of "
                          + str(analysis["eligible_queries"]) + " eligible queries hold "
                          + str(MIN_RATED_CANDIDATES_FOR_TOP10_GATE) + " rated candidates",
                          "metric", minimum, "hybrid", "topk_mean"))
    gates.append(latency_gate("MAX_COLD_RECOMMENDATION_P95_MS", latency))
    gates.append(latency_gate("MAX_WARM_RECOMMENDATION_P95_MS", latency))
    for constant in ("TARGET_EVALUATORS", "TARGET_RATINGS_PER_PAIR", "TARGET_HELDOUT_QUERIES",
                     "TARGET_PAIRWISE_ACCURACY", "TARGET_LIFT_OVER_RANDOM",
                     "TARGET_LIFT_OVER_DSP", "TARGET_TOP1_MARGIN", "TARGET_TOP_K_MEAN",
                     "TARGET_COLD_RECOMMENDATION_P95_MS", "TARGET_WARM_RECOMMENDATION_P95_MS"):
        gates.append(Gate(constant, GATE_ARMS.get(constant, ALL_ARMS), "descriptive",
                          "not applicable (target class)", "not evaluated (target class)",
                          "", "target"))
    for constant in OUT_OF_SCOPE_GATES:
        gates.append(Gate(constant, (), "not evaluated here", "not applicable",
                          "not evaluated (out of scope for #19)", "", "out_of_scope"))
    for constant in CONDITIONAL_GATES:
        gates.append(Gate(constant, (), "not evaluated here", "not applicable",
                          "not evaluated (no tuning claim is made)", "", "conditional"))
    return tuple(gates)


def latency_gate(constant, latency):
    """One cold or warm p95 gate: insufficient until every arm has its 30 samples."""
    threshold = FROZEN_CONSTANTS[constant][0]
    enough = all(entry["valid_samples"] >= MIN_LATENCY_REQUESTS_PER_ARM
                 for entry in latency["arms"].values())
    if not enough:
        return Gate(constant, ALL_ARMS, "not measured (0 of "
                    + str(MIN_LATENCY_REQUESTS_PER_ARM) + " valid samples per arm)",
                    "not applicable (no samples)", "insufficient",
                    "0 of " + str(MIN_LATENCY_REQUESTS_PER_ARM)
                    + " valid latency samples per arm", "latency", threshold)
    observed = max(entry["stages"]["total"]["p95"] or 0.0 for entry in latency["arms"].values())
    verdict = "supported" if observed <= threshold else "not supported"
    return Gate(constant, ALL_ARMS, shown(observed, 1) + " ms p95", "not applicable (p95)",
                verdict, "", "latency", threshold)


def apply_leave_one_out(gates, variants):
    """A gate whose class moves with one evaluator left out is inconclusive, not a verdict."""
    flipped = {constant for variant in variants for constant in variant["flipped"]}
    return tuple(replace(gate, verdict="inconclusive")
                 if gate.constant in flipped and gate.verdict in ("supported", "not supported")
                 else gate for gate in gates)
# --- the private run artifact and the committed report ----------------------

def identity_block(findings):
    """The identity the run key hashes: versions, digests, seeds and model versions."""
    dataset = findings.dataset.value or {}
    split = findings.split.value or {}
    model_versions = sorted({version for run in findings.jev_runs
                             for version in run.model_versions})
    return {
        "protocol_version": findings.protocol.version,
        "dataset_version": dataset.get("dataset_version"),
        "dataset_identity_digest": findings.dataset.digest,
        "split_manifest_version": split.get("schema_version"),
        "split_manifest_digest": split.get("split_manifest_digest"),
        "pair_list_digest": findings.pairs.digest,
        "seeds": SEEDS,
        "analysis_versions": list(findings.analysis_versions),
        "ranking_version": RANKING_VERSION,
        "ranking_weight_table_id": DEFAULT_WEIGHT_TABLE_ID,
        "hybrid_ranking_version": HYBRID_RANKING_VERSION,
        "hybrid_weight_table_id": HYBRID_WEIGHT_TABLE_ID,
        "adapter_version": ADAPTER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model_versions": model_versions,
    }


def identity_key(identity):
    return hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()


def other_run_keys(runs_root, key, identity):
    """Every other run key on the same dataset and split, with why it exists."""
    others = []
    root = Path(runs_root)
    if not root.is_dir():
        return others
    for directory in sorted(root.iterdir()):
        if directory.name == key:
            continue
        document_path = directory / "run.json"
        if not document_path.is_file():
            continue
        try:
            document = json.loads(document_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        other = document.get("identity", {})
        if (other.get("dataset_version") == identity["dataset_version"]
                and other.get("split_manifest_digest") == identity["split_manifest_digest"]):
            others.append(directory.name)
    return others


def input_table(findings):
    """The declared inputs, whether they were there, and their digests."""
    table = {}
    for name, document in (("dataset", findings.dataset), ("split_manifest", findings.split),
                           ("pair_list", findings.pairs), ("assignment", findings.assignment)):
        table[name] = {"path": str(document.path), "present": document.present,
                       "malformed": document.malformed, "digest": document.digest}
    table["analyses"] = [{"path": str(path), "digest": file_digest(path)}
                         for path in findings.paths.analyses]
    table["sessions"] = str(findings.paths.sessions)
    table["jev_runs"] = [str(path) for path in findings.paths.jev_runs]
    return table


def run_document(findings, analysis, gates, latency, key, identity, others):
    """run.json: inputs, digests, seeds, versions, counts, metrics, verdicts and deviations."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_key": key,
        "identity": identity,
        "inputs": input_table(findings),
        "seeds": SEEDS,
        "versions": {"ranking_version": RANKING_VERSION,
                     "ranking_weight_table_id": DEFAULT_WEIGHT_TABLE_ID,
                     "hybrid_ranking_version": HYBRID_RANKING_VERSION,
                     "hybrid_weight_table_id": HYBRID_WEIGHT_TABLE_ID,
                     "prompt_version": PROMPT_VERSION, "adapter_version": ADAPTER_VERSION,
                     "analysis_versions": list(findings.analysis_versions),
                     "model_versions": identity["model_versions"]},
        "counts": dict(findings.accounting),
        "split_counts": findings.split_counts,
        "intersection_eligible_queries": analysis["intersection"],
        "items": [item.as_dict() for item in findings.items],
        "arms": {arm: {"version": analysis["arms"][arm]["version"],
                       "weight_table_id": analysis["arms"][arm]["weight_table_id"],
                       "status": analysis["arms"][arm]["status"],
                       "reason": analysis["arms"][arm]["reason"],
                       "evidence_source": analysis["arms"][arm]["evidence_source"],
                       "publishable": analysis["arms"][arm]["publishable"],
                       "eligible_queries": analysis["arms"][arm]["summary"]["eligible_queries"],
                       "unscored_share": analysis["arms"][arm]["unscored_share"],
                       "metrics": {name: analysis["arms"][arm]["summary"][name]
                                   for name in METRIC_NAMES},
                       "mode_counts": analysis["arms"][arm]["mode_counts"],
                       "status_counts": analysis["arms"][arm]["status_counts"],
                       "error_codes": analysis["arms"][arm]["error_codes"],
                       "identical_orderings": analysis["arms"][arm]["identical_orderings"],
                       "variant_disagreements": analysis["arms"][arm]
                       ["variant_disagreements"]}
                 for arm in ARM_NAMES},
        "gates": [gate.as_dict() for gate in gates],
        "lifts": analysis["lifts"],
        "agreement": {"mean_absolute_deviation": analysis["agreement"]["mean_absolute_deviation"],
                      "exact_agreement_share": analysis["agreement"]["exact_agreement_share"],
                      "pairs_with_at_least_two_ratings":
                          analysis["agreement"]["pairs_with_at_least_two_ratings"],
                      "per_evaluator_means": [list(item) for item in
                                              analysis["agreement"]["evaluators"]]},
        "leave_one_evaluator_out": analysis["leave_one_out"],
        "latency": latency,
        "evidence_gaps": [item.as_dict() for item in findings.gaps],
        "deviations": list(findings.deviations) + [{"kind": "run_key", "run_key": item}
                                                   for item in others],
        "double_runs": {"count": analysis["double_runs"], "label": DOUBLE_NOT_EVIDENCE},
    }


def records_document(findings, analysis, latency, key):
    """records.json: per-query per-arm orderings, scores, unscored ids and timings."""
    queries = []
    for query in findings.query:
        entry = {"query": query.kick_id, "sampled": list(query.sampled),
                 "eligible": list(query.eligible),
                 "rated": {candidate: query.rated[candidate] for candidate in sorted(query.rated)},
                 "ratings_per_pair": {candidate: query.ratings[candidate]
                                      for candidate in sorted(query.ratings)},
                 "filter_error": query.filter_error, "arms": {}}
        for arm in ARM_NAMES:
            found = next((item for item in analysis["arms"][arm]["evaluations"]
                          if item.query_id == query.kick_id), None)
            entry["arms"][arm] = ({} if found is None else found.as_dict())
        queries.append(entry)
    return {"schema_version": SCHEMA_VERSION, "run_key": key,
            "queries": queries, "ratings": len(findings.ratings),
            "sessions": [summary.as_dict() for summary in findings.sessions],
            "latency": {"arms": latency["arms"], "samples": latency["samples"],
                        "invalid": latency["invalid"], "memo": latency["memo"]},
            "deviations": list(findings.deviations)}


# --- the committed report ---------------------------------------------------

def state_line(findings, analysis=None):
    if not findings.gaps and not findings.mechanism:
        return "comparison reported"
    causes = []
    codes = [item.code for item in findings.gaps]
    if "protocol_document_missing" in codes:
        causes.append("the protocol document is absent")
    if "split_manifest_missing" in codes or "pair_list_missing" in codes:
        causes.append("no held-out pair list exists (#65 has not landed the sampler, the split"
                      " manifest or the evaluator assignment)")
    if "pair_counts_below_minimum" in codes:
        causes.append(str(findings.accounting["sampled_pairs"]) + " of "
                      + str(MIN_HELDOUT_PAIRS) + " rated pairs and "
                      + str(findings.accounting["eligible_queries"]) + " of "
                      + str(MIN_HELDOUT_QUERIES) + " eligible held-out queries")
    if "ratings_missing" in codes:
        causes.append("no validated #18 rating session exists")
    if "assignment_missing" in codes:
        causes.append("no evaluator assignment exists")
    if "analysis_manifest_missing" in codes:
        causes.append("no #9 analysis manifest exists")
    if "dataset_missing" in codes:
        causes.append("no dataset document exists")
    if analysis is None or analysis["evidence_source"] != "live":
        causes.append("no live Jev outcome exists")
    if not causes:
        causes = [item.code for item in findings.gaps]
    return "insufficient evidence \u2014 " + "; ".join(causes)


def report_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return lines


def render_report(findings, analysis, gates, latency, key, identity, others, digests):
    """The committed report: the twelve sections in the contract's order, aggregates only."""
    counts = findings.accounting
    lines = ["# Ranking comparison report", "",
             "Comparison of the four frozen arms (random, dsp-only, jev-only, hybrid) over the",
             "held-out split of the frozen protocol. Every value below traces to a recorded",
             "input, seed and version; nothing here is synthesized.", "",
             "## State", state_line(findings, analysis), ""]
    lines += ["## Versions and digests", ""]
    lines += report_table(["Item", "Value"], [
        ["Protocol version", str(findings.protocol.version or "absent")],
        ["Protocol document", "present" if findings.protocol.present else "absent"],
        ["Protocol document digest", str(findings.protocol.digest or "not recorded")],
        ["Dataset version", str(identity["dataset_version"] or "not recorded")],
        ["Dataset identity digest", str(identity["dataset_identity_digest"] or "not recorded")],
        ["Split manifest schema version", str(identity["split_manifest_version"] or "not recorded")],
        ["Split manifest digest", str(identity["split_manifest_digest"] or "not recorded")],
        ["Pair list digest", str(identity["pair_list_digest"] or "not recorded")],
        ["Analysis version(s) at the recorded run",
         ", ".join(identity["analysis_versions"]) or "(none used)"],
        ["RANKING_VERSION", RANKING_VERSION],
        ["DSP weight-table id", DEFAULT_WEIGHT_TABLE_ID],
        ["HYBRID_RANKING_VERSION", HYBRID_RANKING_VERSION],
        ["Hybrid weight-table id", HYBRID_WEIGHT_TABLE_ID],
        ["PROMPT_VERSION", PROMPT_VERSION],
        ["ADAPTER_VERSION", ADAPTER_VERSION],
        ["Observed model version(s)", ", ".join(identity["model_versions"]) or "(none observed)"],
        ["Seeds", "; ".join(name + " = " + value for name, value in sorted(SEEDS.items()))],
        ["Run key", key],
    ])
    lines += ["", "## Eligibility accounting", ""]
    lines += report_table(["Count", "Value", "Threshold it is compared with"], [
        ["pool kicks", counts["pool_kicks"], "MIN_POOL_KICKS = " + str(MIN_POOL_KICKS)],
        ["pool basses / sub-basses", counts["pool_basses"],
         "MIN_POOL_BASSES = " + str(MIN_POOL_BASSES)],
        ["pool selected records", counts["pool_records"], "-"],
        ["pool reserves", counts["pool_reserves"], "-"],
        ["tuning kicks / basses", str(findings.split_counts["tuning_kicks"]) + " / "
         + str(findings.split_counts["tuning_basses"]),
         "TUNING_FRACTION = " + str(TUNING_FRACTION)],
        ["held-out kicks / basses", str(findings.split_counts["held_out_kicks"]) + " / "
         + str(findings.split_counts["held_out_basses"]), "-"],
        ["sampled pairs", str(counts["sampled_pairs"]) + " of " + str(MIN_HELDOUT_PAIRS),
         "MIN_HELDOUT_PAIRS = " + str(MIN_HELDOUT_PAIRS)],
        ["sampled query kicks", str(counts["sampled_queries"]) + " of "
         + str(MIN_HELDOUT_QUERIES), "MIN_HELDOUT_QUERIES = " + str(MIN_HELDOUT_QUERIES)],
        ["eligible held-out queries", str(counts["eligible_queries"]) + " of "
         + str(counts["sampled_queries"]), "MIN_HELDOUT_QUERIES = " + str(MIN_HELDOUT_QUERIES)],
        ["rated pairs", str(counts["rated_pairs"]) + " of " + str(counts["sampled_pairs"])],
        ["pair_coverage", "numerator " + str(counts["rated_pairs"]) + ", denominator "
         + str(counts["sampled_pairs"]) + " (" + ("undefined" if counts["pair_coverage"] is None
                                                  else shown(counts["pair_coverage"]) + " of "
                                                  + str(MIN_PAIR_COVERAGE)) + ")"],
        ["counted evaluators", str(counts["counted_evaluators"]) + " of "
         + str(MIN_EVALUATORS), "MIN_EVALUATORS = " + str(MIN_EVALUATORS)],
        ["sessions (validated / tooling / excluded)",
         str(counts["sessions"]) + " / " + str(counts["tooling_sessions"]) + " / "
         + str(counts["excluded_sessions"]), "-"],
        ["valid ratings consumed", counts["valid_ratings"], "-"],
        ["analysis manifests present", str(len(findings.analyses)) + " of "
         + str(len(findings.paths.analyses)), "-"],
        ["analysed samples / used samples", str(counts["analysed_samples"]) + " / "
         + str(counts["used_samples"]), "-"],
        ["unresolved used samples", counts["missing_analysis_entries"], "-"],
        ["dataset records used / synthetic", str(counts["dataset_records"]) + " / "
         + str(counts["synthetic_fixture_records"]), "provenance_kind = real_library_sample"],
    ])
    lines += ["", "| Arm | Eligible queries | Unscored share | Evidence source | Status |",
              "| --- | --- | --- | --- | --- |"]
    for arm in ARM_NAMES:
        entry = analysis["arms"][arm]
        lines.append("| " + arm + " | " + str(entry["summary"]["eligible_queries"]) + " of "
                     + str(counts["sampled_queries"]) + " | "
                     + (shown(entry["unscored_share"]) if entry["unscored_share"] is not None
                        else "undefined (0 rated candidates)")
                     + " | " + entry["evidence_source"] + " | " + entry["status"]
                     + ("" if entry["reason"] is None else " (" + entry["reason"] + ")") + " |")
    lines += ["", "Intersection of the four arms' eligible queries: " + str(analysis["intersection"])
              + " of " + str(counts["sampled_queries"]) + " sampled queries. Every arm received"
              " the same eligible candidate set per query (the sampled candidates that pass"
              " filter_candidates with FilterPolicy()); an arm ordering a different set is"
              " rejected with candidate_set_mismatch instead of being compared."]
    lines += ["", "## Quality", "",
              "Arm mechanics, latency and determinism are reported here and below; none of that",
              "evidence carries a rating, so this section cannot support any lift or quality",
              "claim.", "", "### Arm mechanics", ""]
    declared = {"random": (RANKING_VERSION, "random-draw"),
                "dsp-only": (RANKING_VERSION, DEFAULT_WEIGHT_TABLE_ID),
                "jev-only": ("jev-only-v1", "equal-1/6-weights"),
                "hybrid": (HYBRID_RANKING_VERSION, HYBRID_WEIGHT_TABLE_ID)}
    lines += report_table(["Arm", "Version", "Weight table / rule", "Verdict", "Mode counts",
                           "Jev status counts", "Identical orderings", "Arm error codes"], [
        [arm, analysis["arms"][arm]["version"] if analysis["arms"][arm]["evaluations"]
         else declared[arm][0] + " (declared; no ordering was produced)",
         analysis["arms"][arm]["weight_table_id"] if analysis["arms"][arm]["evaluations"]
         else declared[arm][1] + " (declared)",
         analysis["arms"][arm].get("verdict", "not evaluated"),
         canonical(analysis["arms"][arm]["mode_counts"]) if
         analysis["arms"][arm]["mode_counts"] else "(no ordering produced)",
         canonical(analysis["arms"][arm]["status_counts"]) if
         analysis["arms"][arm]["status_counts"] else "(no ordering produced)",
         analysis["arms"][arm]["identical_orderings"],
         ", ".join(analysis["arms"][arm]["error_codes"]) or "none"]
        for arm in ARM_NAMES])
    hybrid = analysis["arms"]["hybrid"]
    if hybrid["mode_counts"]:
        fallbacks = hybrid["mode_counts"].get(MODE_DSP_ONLY, 0)
        lines += ["", "The hybrid arm ran through the documented DSP-only fallback on "
                  + str(fallbacks) + " of " + str(sum(hybrid["mode_counts"].values()))
                  + " scored queries (" + FALLBACK_REASON + "): its lift over DSP is"
                  " structurally zero and its hybrid gate is insufficient, never a hybrid value."]
    lines += ["", "The random arm's analytic expectations are pairwise accuracy 0.50, top-1"
              " margin 0.00 and a top-k mean equal to the rated subset's mean; every random"
              " value above is reported beside them.", ""]
    lines += ["Jev-only variants: the primary label-based variant and the predeclared"
              " probability-weighted variant disagree on "
              + str(analysis["jev_variant_disagreements"]) + " of "
              + str(analysis["eligible_queries"]) + " eligible queries; when they disagree the"
              " Jev-only gate is inconclusive, never the better of the two.", ""]
    lines += ["Jev evidence: " + ADAPTER_VERDICT + ". The #14 live integration check"
              " (tests/test_jev_integration.py) is skipped and is recorded UNVERIFIED, never as a"
              " pass. A Jev-dependent gate with no live outcome is insufficient with reason "
              + NO_LIVE_JEV_REASON + ", never not supported.", "", "### Gates", ""]
    lines += report_table(["Constant (= value)", "Class", "Arms gated", "Observed value",
                           "95% interval", "Verdict", "Shortfall"], [
        [gate.constant + " = " + str(FROZEN_CONSTANTS[gate.constant][0]), gate.cls,
         ", ".join(gate.arms) or "none (not a comparison gate)", gate.value, gate.interval,
         gate.verdict, gate.shortfall or "-"] for gate in gates])
    lines += ["", "queries_with_11_rated_candidates: "
              + str(analysis["queries_with_11_rated_candidates"]) + " of "
              + str(analysis["eligible_queries"]) + " eligible queries. With k = 5 the top-k"
              " means are descriptive values, not top-10 values, and no k = 5 mean is compared"
              " with a top-10 constant.", ""]
    lines += ["## Uncertainty", "",
              "Bootstrap: " + str(BOOTSTRAP_RESAMPLES) + " resamples of the eligible query"
              " kicks with replacement under BOOTSTRAP_SEED = " + BOOTSTRAP_SEED + "; each draw"
              " is seeded from sha256(seed, metric, draw index), and the interval is the 95%"
              " percentile interval (linear interpolation between order statistics at position"
              " (n - 1) * fraction).", ""]
    uncertainty_rows = []
    for arm in ARM_NAMES:
        for metric in ("pairwise_accuracy", "top1_margin"):
            values = [item.metrics[metric] for item in analysis["arms"][arm]["evaluations"]
                      if item.eligible and item.metrics[metric] is not None]
            uncertainty_rows.append([
                metric + " (" + arm + ")",
                shown(analysis["arms"][arm]["summary"][metric]["value"]),
                shown_interval(bootstrap_values(values, label=arm + "-" + metric)["interval"]),
                analysis["arms"][arm]["summary"][metric]["queries"]])
    for other in ("random", "dsp-only"):
        for arm in ARM_NAMES:
            if arm == other:
                continue
            entry = analysis["lifts"].get(arm + " over " + other, {}).get("pairwise_accuracy")
            if entry is None:
                continue
            values = [difference for query, name, difference in paired_differences(
                analysis["arms"][arm]["evaluations"], analysis["arms"][other]["evaluations"])
                if name == "pairwise_accuracy"]
            uncertainty_rows.append(["pairwise_accuracy lift " + arm + " over " + other,
                                     shown_signed(entry["value"]),
                                     shown_interval(bootstrap_values(
                                         values, label=arm + " over " + other)["interval"]),
                                     entry["queries"]])
    lines += report_table(["Gated metric", "Point estimate", "95% interval", "Queries"],
                          uncertainty_rows)
    lines += ["", "The minimum detectable difference is the protocol's predeclared table: about"
              " 0.08 accuracy points for pairwise_accuracy (1.96 x 0.30 / sqrt(60)) and about"
              " 0.25 rating points for top1_margin (1.96 x 1.00 / sqrt(60)). Every gated lift is"
              " set at or above that floor (+0.10 over random, +0.10 over DSP, +0.30 top-1"
              " margin). The top-k gates are not evaluable at this design and are reported"
              " insufficient.", "",
              "Determinism: re-running the recorded command reproduces run.json ("
              + str(digests.get("run.json")) + "), records.json ("
              + str(digests.get("records.json")) + ") and this report byte for byte. The run key"
              " covers the protocol version, the dataset version and identity digest, the split"
              " manifest version and digest, the pair-list digest, every seed, the analysis and"
              " ranking versions and weight-table ids, ADAPTER_VERSION, PROMPT_VERSION and the"
              " observed model versions.", ""]
    if others:
        lines += ["Every other run key for this dataset and split: " + ", ".join(others)
                  + ". This report uses " + key + "; the other directories are earlier private"
                  " artifacts over the same dataset and split and none of them was read to"
                  " produce a result here.", ""]
    agreement = analysis["agreement"]
    lines += ["## Agreement", ""]
    lines += report_table(["Measure", "Value", "Threshold"], [
        ["pairs with at least two valid ratings",
         str(agreement["pairs_with_at_least_two_ratings"]) + " of " + str(MIN_AGREEMENT_PAIRS),
         "MIN_AGREEMENT_PAIRS = " + str(MIN_AGREEMENT_PAIRS)],
        ["mean absolute deviation between evaluators",
         shown(agreement["mean_absolute_deviation"]) if
         agreement["mean_absolute_deviation"] is not None else "not computable",
         "MAX_MEAN_ABSOLUTE_DEVIATION = " + str(MAX_MEAN_ABSOLUTE_DEVIATION)],
        ["exact-agreement share",
         shown(agreement["exact_agreement_share"]) if agreement["exact_agreement_share"]
         is not None else "not computable", "-"],
        ["per-evaluator means", str(len(agreement["evaluators"])) + " evaluator(s) with a valid"
         " rating" if agreement["evaluators"] else "no evaluator has a valid rating", "-"],
    ])
    for index, (_, value) in enumerate(agreement["evaluators"]):
        lines += ["", "Per-evaluator mean (anonymous evaluator " + str(index + 1) + "): "
                  + shown(value) + " rating points; the evaluator code stays in the private run"
                  " artifact."]
    lines += ["", "Leave-one-evaluator-out: " + (str(len(analysis["leave_one_out"]))
              + " variant(s) were recomputed; none flipped a gate class." if analysis
              ["leave_one_out"] else "no evaluator holds a rating, so no variant exists."), ""]
    lines += ["## Latency", ""]
    lines += report_table(["Item", "Value"], [
        ["OS", latency["hardware"]["os"] + " " + latency["hardware"]["os_release"]],
        ["CPU", latency["hardware"]["cpu"]],
        ["CPU count", str(latency["hardware"]["cpu_count"])],
        ["Python", latency["hardware"]["python"]],
        ["Process state", latency["hardware"]["process_state"]],
        ["Memo", latency["memo"]["note"] + "; key fields "
         + ", ".join(latency["memo"]["key_fields"]) + "; hits " + str(latency["memo"]["hits"])
         + ", misses " + str(latency["memo"]["misses"])],
        ["Cold sample command", latency["command_template"]],
        ["Timed path", "the runner's request function only: the client, IPC and audition start"
         " (MAX_AUDITION_START_P95_MS = " + str(MAX_AUDITION_START_P95_MS) + ") are excluded"],
        ["Retrieval stage", RETRIEVAL_NOT_IMPLEMENTED],
    ])
    lines += ["", ""]
    lines += report_table(["Arm", "Valid samples", "Distinct query kicks", "Cold p95", "Warm p95",
                           "Reasons"], [
        [arm, str(latency["arms"][arm]["valid_samples"]) + " of "
         + str(MIN_LATENCY_REQUESTS_PER_ARM), str(latency["arms"][arm]["distinct_query_kicks"])
         + " of " + str(MIN_LATENCY_QUERY_KICKS), "not measured", "not measured",
         ", ".join(latency["arms"][arm]["reasons"]) or "none"] for arm in ARM_NAMES])
    lines += ["", ""]
    stage_rows = []
    for stage in LATENCY_STAGES + ("total",):
        best = max(latency["arms"].values(), key=lambda entry: entry["valid_samples"])
        entry = best["stages"].get(stage, {})
        if stage == "retrieval":
            stage_rows.append([stage, RETRIEVAL_NOT_IMPLEMENTED, RETRIEVAL_NOT_IMPLEMENTED,
                               str(best["valid_samples"])])
        elif entry.get("p50") is None:
            stage_rows.append([stage, "not measured", "not measured", str(best["valid_samples"])])
        else:
            stage_rows.append([stage, shown(entry["p50"], 1) + " ms",
                               shown(entry["p95"], 1) + " ms", str(entry["count"])])
    lines += report_table(["Stage", "p50", "p95", "Samples"], stage_rows)
    lines += ["", "Cold and warm totals are compared with MAX_COLD_RECOMMENDATION_P95_MS = "
              + str(MAX_COLD_RECOMMENDATION_P95_MS) + " and MAX_WARM_RECOMMENDATION_P95_MS = "
              + str(MAX_WARM_RECOMMENDATION_P95_MS) + "; the targets TARGET_COLD_"
              "RECOMMENDATION_P95_MS = " + str(TARGET_COLD_RECOMMENDATION_P95_MS)
              + " and TARGET_WARM_RECOMMENDATION_P95_MS = "
              + str(TARGET_WARM_RECOMMENDATION_P95_MS) + " are non-gating. Per-stage p50 and p95"
              " for feature load, filter, retrieval, Jev calls (Jev arms only) and ranking are"
              " recorded in the private run artifact.", ""]
    lines += ["## Evidence gaps", ""]
    lines += report_table(["Check", "Code", "Expected", "Actual"], [
        [item.name, item.code, item.expected, item.actual] for item in findings.gaps])
    if not findings.gaps:
        lines += ["No evidence gap was recorded."]
    lines += ["", "## Deviations", "", "### Session rows", ""]
    if findings.sessions:
        lines += report_table(["Session (anonymous)", "validate exit", "counted", "reason",
                               "presentations", "valid ratings"], [
            [str(index), str(summary.validate_exit), "yes" if summary.counted else "no",
             summary.reason or "-", str(summary.presentations), str(len(summary.ratings))]
            for index, summary in enumerate(findings.sessions, start=1)])
    else:
        lines += ["No session exists, so no session row carries a validate exit status."]
    lines += [""]
    rendered = []
    for deviation in findings.deviations:
        rendered.append("session " + str(deviation["session_id"]) + ": validate exit "
                        + str(deviation["validate_exit"]) + ", codes "
                        + (", ".join(deviation["violations"]) or "none") + " (excluded, never"
                        " repaired)")
    for arm in ARM_NAMES:
        entry = analysis["arms"][arm]
        if entry["error_codes"]:
            rendered.append(arm + ": arm error codes " + ", ".join(entry["error_codes"]))
    rendered.append("unscored candidates: " + "; ".join(
        arm + " " + str(analysis["arms"][arm]["summary"]["unscored_share"]["scored_in_rated"])
        + " of " + str(analysis["arms"][arm]["summary"]["unscored_share"]["rated_total"])
        + " rated candidates scored" for arm in ARM_NAMES))
    rendered.append("identical orderings: " + "; ".join(
        arm + " " + str(analysis["arms"][arm]["identical_orderings"]) + " query pairs"
        for arm in ARM_NAMES))
    rendered.append("unrated queries: " + str(sum(
        1 for item in analysis["arms"]["dsp-only"]["evaluations"]
        if not item.eligible and item.reason == "rated_subset_below_minimum_ratings")))
    if analysis["double_runs"]:
        rendered.append("a source=double Jev run was present: it exercised the pipeline only,"
                        " is labelled '" + DOUBLE_NOT_EVIDENCE + "' in the private run artifact,"
                        " and no value derived from it appears in this report")
    if analysis["arms"]["hybrid"]["mode_counts"]:
        rendered.append("hybrid fallback on " + str(analysis["arms"]["hybrid"]["mode_counts"]
                                                    .get(MODE_DSP_ONLY, 0)) + " queries: "
                        + FALLBACK_REASON)
    rendered.append("the #65 split-manifest and evaluator-assignment schemas are this runner's"
                    " declared expectation, because #65 has not landed: schema_version 1.0,"
                    " dataset_version, the three seeds, tuning and held_out with kicks and"
                    " basses, and the manifest's recorded split_manifest_digest for the"
                    " manifest; schema_version 1.0, assignment_seed, split_manifest_digest and"
                    " assignments (pair id to evaluator ids) for the assignment. This runner"
                    " checks that the pair list's digest equals the manifest's recorded digest,"
                    " and it never re-derives a manifest digest, a seed, a split, an assignment"
                    " or a pair list")
    rendered.append("no threshold was lowered, no metric was changed and no denominator was"
                    " redefined for this run")
    rendered.append("(latency: cold samples run as one fresh child process per sample; the"
                    " exact spawned command is recorded in the private run artifact and the"
                    " report carries its template so no private path is published)")
    lines += ["- " + item for item in rendered]
    lines += ["", FABRICATION_SENTENCE, "",
              "## No-held-out-tuning attestation", "", ATTESTATION, "",
              "No tuning claim is made for this run: the published weight tables and constants"
              " are used unchanged, and no tuning rating set was read.", "",
              "## Reproduction", "", "Run from the repository root:", "",
              "~~~", "python -m backend.evaluation.comparison preflight",
              "python -m backend.evaluation.comparison report", "~~~", "",
              "Both commands read only the declared inputs and write only "
              + findings.paths.report.as_posix() + " and " + findings.paths.runs.as_posix()
              + "/" + key + "/. Private paths stay on the device; this report carries aggregate"
              " counts, published version, seed, weight-table and digest strings and the hardware"
              " class only.", ""]
    lines += ["## Out of scope", "",
              "- Setting, changing or reinterpreting a threshold, metric, seed, sampling rule,"
              " split rule or record field: #16 owns them.",
              "- The pair-rating utility, playback, resume, corrections, status and export: #17.",
              "- Collecting, recruiting for or running producer sessions: #18.",
              "- The pair sampler, split manifest and evaluator assignment: #65.",
              "- The proceed / revise / stop decision: #20.",
              "- Product latency work (#37), the recommendation API (#28), retrieval (#25), the"
              " decision cache (#26) and audition start (#39).",
              "- Unscored or failed candidates in the recommendation contracts (#62); bounded"
              " parallel Jev dispatch (#64).",
              "- Retention (#36) and personalisation evaluation (#58, #59).",
              "- Growing the pool (#67), recruiting a standing panel (#66) and the literal top-10"
              " budget (#69).",
              "- Confirming the assumed TypeSafe Jev HTTP mapping and the real model versions:"
              " #14 reports the live check UNVERIFIED; tracked as #68.", ""]
    return "\n".join(lines)


# --- commands ---------------------------------------------------------------

def preflight(paths, stdout):
    """Print one JSON summary line of every recorded check and exit on its worst status."""
    findings = evaluate(paths)
    summary = {"command": "preflight", "protocol_version": findings.protocol.version,
               "protocol_document": findings.protocol.path,
               "items": [item.as_dict() for item in findings.items],
               "exit": findings.exit_code}
    print(canonical(summary), file=stdout, flush=True)
    return findings.exit_code


def prepare_runs(findings, paths):
    """Run the one explicitly configured live call, before the run key is fixed."""
    if not paths.jev_live:
        return findings
    try:
        run = live_jev_run(findings)
    except (ValueError, OSError) as error:
        return replace(findings, mechanism=findings.mechanism + (
            ComparisonError("schema_mismatch", "the live Jev call failed: " + str(error)),))
    if run is None:
        return findings
    return replace(findings, jev_runs=findings.jev_runs + (run,))


def arm_verdict(arm, gates):
    """The protocol's global rule applied to one arm's applicable minimum-class gates."""
    applicable = [gate for gate in gates if arm in gate.arms and gate.cls == "minimum"]
    verdicts = [gate.verdict for gate in applicable]
    if not verdicts:
        return "not evaluated"
    if all(verdict in ("supported", "met") for verdict in verdicts):
        return "supported"
    if any(verdict == "not supported" for verdict in verdicts):
        return "not supported"
    if any(verdict == "insufficient" for verdict in verdicts):
        return "insufficient"
    return "inconclusive"


def run_report(paths, stdout):
    """Write the private run artifact and the committed report, or fail closed."""
    findings = prepare_runs(evaluate(paths), paths)
    problems = list(findings.mechanism) + [
        ComparisonError(item.code, item.name + ": " + item.actual) for item in findings.items
        if item.mechanism]
    if problems:
        for error in problems:
            print(str(error.code) + ": " + str(error), file=sys.stderr)
        return EXIT_FAILURE
    identity = identity_block(findings)
    key = identity_key(identity)
    others = other_run_keys(paths.runs, key, identity)
    run_directory = Path(paths.runs) / key
    dataset_version = (findings.dataset.value or {}).get("dataset_version", "")
    outcome_table = jev_outcomes_by_key(findings.jev_runs)
    latency = measure_latency(latency_plan(findings.query, ARM_NAMES), findings.query,
                              findings.samples, dataset_version=dataset_version,
                              outcome_table=outcome_table, run_directory=run_directory,
                              analysis_manifests=paths.analyses, jev_runs=paths.jev_runs)
    analysis = analyse(findings, paths, latency=latency, outcome_table=outcome_table)
    gates = build_gates(findings, analysis, latency)
    variants = leave_one_evaluator_out(findings, analysis["arms"], gates)
    gates = apply_leave_one_out(gates, variants)
    analysis["leave_one_out"] = variants
    for arm in ARM_NAMES:
        analysis["arms"][arm]["verdict"] = arm_verdict(arm, gates)
        analysis["arms"][arm]["gates"] = [gate.as_dict() for gate in gates if arm in gate.arms]
    records = records_document(findings, analysis, latency, key)
    write_text(run_directory / "records.json", canonical(records) + "\n")
    document = run_document(findings, analysis, gates, latency, key, identity, others)
    write_text(run_directory / "run.json", canonical(document) + "\n")
    digests = {"run.json": file_digest(run_directory / "run.json"),
               "records.json": file_digest(run_directory / "records.json")}
    report = render_report(findings, analysis, gates, latency, key, identity, others, digests)
    write_text(paths.report, report)
    exit_code = EXIT_OK if all(gate.verdict != "insufficient" for gate in gates) \
        else EXIT_INSUFFICIENT
    print(canonical({"command": "report", "run_key": key, "state": state_line(findings, analysis),
                     "exit": exit_code, "report": str(paths.report),
                     "report_digest": file_digest(paths.report)}) + "\n", end="", file=stdout,
          flush=True)
    return exit_code


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m backend.evaluation.comparison",
                                     description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("preflight", "run the recorded checks and print one JSON line"),
                            ("report", "write the private run and the committed report")):
        command = commands.add_parser(name, help=help_text)
        add_input_arguments(command)
        if name == "report":
            command.add_argument("--report", default=str(REPORT_PATH))
            command.add_argument("--runs", default=str(RUNS_DIRECTORY))
            command.add_argument("--jev-live", action="store_true",
                                 help="make the one explicitly configured live Jev call")
            command.add_argument("--tuning-claim", action="store_true")
            command.add_argument("--tuning-ratings", default=str(TUNING_DIRECTORY))
    request = commands.add_parser("latency-request",
                                  help="run one latency request in this fresh process")
    request.add_argument("--request", required=True)
    request.add_argument("--out", required=True)
    return parser


def add_input_arguments(parser):
    parser.add_argument("--protocol-doc", default=str(PROTOCOL_DOCUMENT))
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--split-manifest", default=str(DEFAULT_PAIR_ROOT / "split-manifest.json"))
    parser.add_argument("--pairs", default=str(DEFAULT_PAIR_ROOT / "pairs.json"))
    parser.add_argument("--assignment", default=str(DEFAULT_PAIR_ROOT / "assignment.json"))
    parser.add_argument("--sessions", default=str(DEFAULT_PAIR_ROOT / "sessions"))
    parser.add_argument("--analysis-manifest", action="append", dest="analyses")
    parser.add_argument("--jev-run", action="append", dest="jev_runs")


def paths_from(arguments):
    analyses = getattr(arguments, "analyses", None)
    if analyses is None:
        analyses = [str(DEFAULT_ANALYSIS_ROOT / "kicks-manifest.json"),
                    str(DEFAULT_ANALYSIS_ROOT / "basses-manifest.json")]
    jev_runs = getattr(arguments, "jev_runs", None) or []
    return Paths(Path(arguments.protocol_doc), Path(arguments.dataset),
                 Path(arguments.split_manifest), Path(arguments.pairs),
                 Path(arguments.assignment), Path(arguments.sessions),
                 tuple(Path(path) for path in analyses),
                 tuple(Path(path) for path in jev_runs),
                 Path(getattr(arguments, "report", REPORT_PATH)),
                 Path(getattr(arguments, "runs", RUNS_DIRECTORY)),
                 Path(getattr(arguments, "tuning_ratings", TUNING_DIRECTORY)),
                 bool(getattr(arguments, "tuning_claim", False)),
                 bool(getattr(arguments, "jev_live", False)))


def main(argv=None, stdout=None):
    arguments = build_parser().parse_args(argv)
    stream = sys.stdout if stdout is None else stdout
    try:
        if arguments.command == "preflight":
            return preflight(paths_from(arguments), stream)
        if arguments.command == "report":
            return run_report(paths_from(arguments), stream)
        return latency_request(arguments, stream)
    except ComparisonError as error:
        print(str(error.code) + ": " + str(error), file=sys.stderr)
        return EXIT_FAILURE
    except (OSError, ValueError, KeyError, TypeError):
        print("refused: local failure; nothing was accepted.", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
