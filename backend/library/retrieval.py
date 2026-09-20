"""Storage-facing normalized retrieval, the recall report and the CLI (issue #25).

`retrieve_shortlist` reads one local library through issue #21's repository,
admits candidates with issue #11's `filter_candidates` and orders the admitted
ones with the pure `backend.palette.retrieval`. `reference_for` and
`recall_metrics` measure that order against the feasibility evaluation set
(#16's pair list and #17's export documents) without ever deriving the reference
from the retrieval's own output. `recall_report` assembles the version-complete
private report and `main` is its `recall` command.

Rules this module enforces:

- No SQL string here: every read goes through #21's
  `backend.library.repository`, and the one read this issue added is
  `LibraryRepository.list_retrieval_rows`. `retrieve_shortlist` performs two
  library reads (the query kick's current-version sample and the candidate-role
  population with its analysis state and availability), each one statement, and
  never a query inside a per-candidate or per-dimension loop.
- Admission stays with #11: `filter_candidates` runs before the normalization
  is fitted, the result carries its `FilterResult` unchanged, and every id in
  `ranked` and `shortlist` is eligible.
- The analysis version is derived here as
  `backend.analysis.batch.digest(backend.analysis.batch.analysis_descriptor())`
  and never accepted from a caller.
- Availability comes from the stored `samples.file_status` (`present`,
  `missing`, `unknown`, or the forward-compatible `unreadable`); no path is
  probed, opened, statted or decoded.
- The report is private local data. It is written only under the caller's
  `--output`, through a flushed, fsynced temporary file in the same directory
  and an atomic replace, and it carries no timestamp, hostname, pid or duration.
- Import list: the standard library, `backend.contracts`,
  `backend.analysis.batch`, `backend.evaluation.rating`,
  `backend.evaluation.manifest`, `backend.library.*`,
  `backend.palette.compatibility` and `backend.palette.retrieval`.
  `backend.evaluation.comparison` is reached at call time only, because
  importing it pulls in `backend.intelligence` and `backend.palette.ranking`,
  which this module must not import.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile

from backend.analysis.batch import (BatchError, analysis_descriptor, canonical, digest,
                                    local_path)
from backend.evaluation import rating
from backend.evaluation.manifest import fields, read_json
from backend.library.errors import LibraryError
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database
from backend.palette.compatibility import (Availability, FilterInputError, FilterPolicy,
                                           FilterResult, filter_candidates)
from backend.palette.retrieval import (DEFAULT_SHORTLIST_SIZE, REPRESENTATION_VERSION,
                                       RETRIEVAL_CANDIDATE_ROLES, RETRIEVAL_POLICY_VERSION,
                                       SHORTLIST_MAX, SHORTLIST_MIN, RetrievalInputError,
                                       RetrievalPolicy, RetrievalResult, fit_normalization,
                                       mask_counts, normalization_id, select_shortlist)


RETRIEVAL_SCHEMA = "1.0"

# The diagnostic curve's sizes; the product shortlist is only ever the
# configured 50-100 size, so a curve point is computed by truncating `ranked`
# and never by running a smaller policy.
RECALL_AT_SIZES = (5, 10, 20, 50, 100)

# The labels a positive reference is built from, and the numeric values they
# answer to in #19's landed vocabulary (a test asserts the two agree).
RECALL_POSITIVE_LABELS = ("good", "excellent")

REPORT_STATES = ("measured", "insufficient_evidence")
REPORT_BLOCKED_REASONS = ("pair_list_absent", "no_sessions", "no_positive_references")

# Why one pair-list query could not be measured. Only the first two are library
# faults, and the CLI exits 1 when a report carries either.
NOT_EVALUABLE_REASONS = ("query_kick_missing", "no_available_candidates",
                         "no_positive_references")

# Why one declared reference is not in the shortlist. `filter_excluded` carries
# #11's own codes, so a filtered or unanalysed reference is never reported as a
# retrieval failure.
MISS_CLASSIFICATIONS = ("cut", "filter_excluded", "missing_from_library")

REPORT_ERROR_CODES = ("invalid_arguments", "invalid_retrieval", "malformed_pair_list",
                      "malformed_export", "unwritable_output", "aliased_output")

# The stored availability vocabulary (#22/#24) mapped onto #11's states. A
# state the schema does not store today maps to UNKNOWN rather than to
# available, and an unreadable row is reported as such.
_AVAILABILITY = {
    "present": Availability.AVAILABLE,
    "missing": Availability.MISSING,
    "unknown": Availability.UNKNOWN,
    "unreadable": Availability.UNREADABLE,
}

class RetrievalReportError(ValueError):
    """A recall command failure: a stable `code` and a path-free message.

    The code is one of `REPORT_ERROR_CODES`; the message never contains a
    local path, a sample name or an audio byte, and the CLI prints exactly one
    code plus that message.
    """

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class _ArgumentRefusal(Exception):
    """One argument-level `recall` refusal: `invalid_arguments`, exit 2.

    argparse's own `error` prints a usage table and raises `SystemExit(2)`.
    The command-line contract here is one code plus one path-free sentence and
    `main(argv)` returning the code, so `_ArgumentParser.error` raises this
    instead and `main` reports it through the ordinary exit-2 path.
    """

    code = "invalid_arguments"


class _ArgumentParser(argparse.ArgumentParser):
    """An `ArgumentParser` that refuses with `_ArgumentRefusal`, never a dump."""

    def error(self, message):
        raise _ArgumentRefusal(message)


@dataclass(frozen=True)
class StoredRetrieval:
    """One `retrieve_shortlist` call: the pure result and the storage evidence.

    `result` is the pure `RetrievalResult`; `filter_result` is #11's result
    unchanged, so the admission evidence is never re-derived. The counts are
    storage facts: `population_count` is the collapsed current-version
    population the normalization was fitted over, `duplicate_candidates` how
    many rows the identity collapse removed, and `skipped_stale_analysis` /
    `skipped_absent_analysis` how many candidate-role rows hold only another
    analysis version or none at all. `domain_rejected` is the per-dimension
    count of stored values the transform's domain masked.

    `representative_paths` is local-only: the chosen path of each collapsed
    identity. It is never written into a report, never returned by `to_dict`
    and never posted anywhere; every other field is path-free.
    """

    result: RetrievalResult
    filter_result: FilterResult
    population_count: int
    duplicate_candidates: int
    skipped_stale_analysis: int
    skipped_absent_analysis: int
    domain_rejected: tuple
    representative_paths: tuple

    def to_dict(self) -> dict:
        return {
            "result": self.result.to_dict(),
            "filter": {
                "policy_version": self.filter_result.policy_version,
                "eligible_ids": list(self.filter_result.eligible_ids),
                "excluded": [
                    {"sample_id": entry.sample_id,
                     "codes": [reason.code for reason in entry.reasons]}
                    for entry in self.filter_result.excluded],
            },
            "population_count": self.population_count,
            "duplicate_candidates": self.duplicate_candidates,
            "skipped_stale_analysis": self.skipped_stale_analysis,
            "skipped_absent_analysis": self.skipped_absent_analysis,
            "domain_rejected": dict(self.domain_rejected),
        }


@dataclass(frozen=True)
class RecallQuery:
    """One pair-list query kick: its declared references and its positive ones."""

    query_kick_id: str
    declared: tuple
    positive: tuple

    def to_dict(self) -> dict:
        return {"query_kick_id": self.query_kick_id, "declared": list(self.declared),
                "positive": list(self.positive)}


@dataclass(frozen=True)
class RecallReference:
    """The arm-independent reference built from the pair list and the exports.

    Built before any retrieval runs, from those two inputs only: never from a
    `RetrievalResult`, a `FilterResult`, a DSP-only or hybrid order, a Jev
    judgment or a compatibility score. `positive` is the declared references
    whose pair aggregate is positive under #19's landed label vocabulary.
    """

    pair_list_digest: str | None
    dataset_version: str | None
    split_manifest_digest: str | None
    sampler_seed: str | None
    assignment_seed: str | None
    export_digests: tuple
    positive_labels: tuple
    min_ratings_per_pair: int
    queries: tuple

    def to_dict(self) -> dict:
        return {
            "pair_list_digest": self.pair_list_digest,
            "dataset_version": self.dataset_version,
            "split_manifest_digest": self.split_manifest_digest,
            "sampler_seed": self.sampler_seed,
            "assignment_seed": self.assignment_seed,
            "export_digests": list(self.export_digests),
            "positive_labels": list(self.positive_labels),
            "min_ratings_per_pair": self.min_ratings_per_pair,
        }


@dataclass(frozen=True)
class RecallMiss:
    """One declared reference that is not in the shortlist, and why."""

    sample_id: str
    classification: str
    codes: tuple

    def to_dict(self) -> dict:
        return {"sample_id": self.sample_id, "classification": self.classification,
                "codes": list(self.codes)}


@dataclass(frozen=True)
class RecallQueryRow:
    """One measured pair-list query: its denominators, recall and misses."""

    query_kick_id: str
    declared_count: int
    available_count: int
    positive_count: int
    retrieved_declared: int
    retrieved_positive: int
    recall: float | None
    not_evaluable_reason: str | None
    misses: tuple

    def to_dict(self) -> dict:
        return {
            "query_kick_id": self.query_kick_id,
            "declared_count": self.declared_count,
            "available_count": self.available_count,
            "positive_count": self.positive_count,
            "retrieved_declared": self.retrieved_declared,
            "retrieved_positive": self.retrieved_positive,
            "recall": self.recall,
            "not_evaluable_reason": self.not_evaluable_reason,
            "misses": [miss.to_dict() for miss in self.misses],
        }


@dataclass(frozen=True)
class RecallCurvePoint:
    """One diagnostic recall/coverage pair at a size at or below the policy size."""

    size: int
    recall: float | None
    coverage: float | None

    def to_dict(self) -> dict:
        return {"size": self.size, "recall": self.recall, "coverage": self.coverage}


@dataclass(frozen=True)
class RecallTotals:
    """The report's aggregate counts and its recall at the configured size."""

    queries: int
    measurable_queries: int
    queries_not_evaluable: int
    declared_total: int
    available_total: int
    positive_total: int
    retrieved_declared_total: int
    retrieved_positive_total: int
    recall_at_size: float | None
    coverage_at_size: float | None

    def to_dict(self) -> dict:
        return {
            "queries": self.queries,
            "measurable_queries": self.measurable_queries,
            "queries_not_evaluable": self.queries_not_evaluable,
            "declared_total": self.declared_total,
            "available_total": self.available_total,
            "positive_total": self.positive_total,
            "retrieved_declared_total": self.retrieved_declared_total,
            "retrieved_positive_total": self.retrieved_positive_total,
            "recall_at_size": self.recall_at_size,
            "coverage_at_size": self.coverage_at_size,
        }


@dataclass(frozen=True)
class RecallMetrics:
    """`recall_metrics`' result: the per-query rows, the curve and the totals."""

    queries: tuple
    curve: tuple
    totals: RecallTotals

    def to_dict(self) -> dict:
        return {"queries": [row.to_dict() for row in self.queries],
                "curve": [point.to_dict() for point in self.curve],
                "totals": self.totals.to_dict()}


def _comparison():
    """The landed #19 comparison helpers, imported at call time.

    `backend.evaluation.comparison` imports `backend.intelligence` and
    `backend.palette.ranking` at module level, and retrieval must import
    neither, so the reference and label helpers are reached through this call
    and never through a module-level import of this module.
    """

    from backend.evaluation import comparison

    return comparison


def _availability(file_status) -> Availability:
    """#21's stored availability as one of #11's states.

    `present` is available, `missing` is missing, `unknown` (and a row with
    no recorded state) is unverified and `unreadable` is unreadable. No path is
    probed to decide this.
    """

    return _AVAILABILITY.get(file_status, Availability.UNKNOWN)


def _collapse(rows) -> tuple:
    """Collapse rows sharing one sample id; count the analysis states.

    The representative is the row with the lexicographically smallest
    normalised path, the same rule #22's `pending_analysis` uses, so a scanned
    duplicate can never produce two entries in one shortlist. Stale and absent
    rows carry no current analysis, so they are counted and skipped. Returns the
    current population as samples in sample_id order, the counts and the
    local-only representative paths.
    """

    groups = {}
    for row in rows:
        groups.setdefault(row.sample_id, []).append(row)
    population, representatives = [], []
    stale = absent = duplicates = 0
    for sample_id in sorted(groups):
        items = groups[sample_id]
        for row in items:
            if row.analysis_state == "stale":
                stale += 1
            elif row.analysis_state == "absent":
                absent += 1
        current = [row for row in items if row.analysis_state == "current"]
        if not current:
            continue
        chosen = min(current, key=lambda row: row.path_key)
        duplicates += len(current) - 1
        if len(current) > 1:
            representatives.append((sample_id, chosen.path))
        population.append(chosen.sample)
    counts = {"population": len(population), "duplicates": duplicates,
              "stale": stale, "absent": absent}
    return population, counts, tuple(representatives)


def retrieve_shortlist(connection, kick, *, policy, filter_policy=FilterPolicy(),
                       context=None, availability=None) -> StoredRetrieval:
    """Order one stored kick's eligible bass candidates and cut the shortlist.

    `kick` is a stored sample id. The analysis version is derived here as
    `digest(analysis_descriptor())`; a kick whose stored analysis is not that
    version, or which has no stored sample, raises `invalid_kick` or the
    repository's own code. The population is every stored candidate-role row at
    that version, after the duplicate collapse -- never the query's eligible
    set, never the available files and never the query kick -- so the same
    normalization record is shared by every query.

    #11 admits the candidates before the normalization is fitted, with the
    caller's `filter_policy`, the derived availability and the optional
    `context`, and the returned record carries that `FilterResult` unchanged.
    An explicit `availability` argument must map exactly the supplied ids
    (the kick plus every current candidate) and is validated by #11's rules, so
    a test can inject a state without touching the database. Nothing here opens,
    stats or decodes an audio file and nothing is written to the database.
    """

    if not isinstance(kick, str) or not kick.strip():
        raise RetrievalInputError("invalid_kick", "kick must be a stored sample id.")
    repository = LibraryRepository(connection)
    analysis_version = digest(analysis_descriptor())
    stored = repository.get_sample(kick, analysis_version)
    if stored is None:
        raise RetrievalInputError("invalid_kick", "No stored sample has that kick id.")
    rows = repository.list_retrieval_rows(analysis_version, roles=RETRIEVAL_CANDIDATE_ROLES)
    population, counts, representatives = _collapse(rows)
    supplied = {stored.sample.sample_id: _availability(stored.file_status)}
    for row in rows:
        if row.analysis_state == "current":
            supplied[row.sample_id] = _availability(row.file_status)
    if availability is not None:
        if not isinstance(availability, Mapping) or set(availability) != set(supplied) \
                or any(type(state) is not Availability for state in availability.values()):
            raise FilterInputError(
                "invalid_availability",
                "An explicit availability mapping must cover exactly the supplied ids.")
        supplied = dict(availability)
    filter_result = filter_candidates(stored.sample, population, policy=filter_policy,
                                      availability=supplied, context=context)
    eligible = set(filter_result.eligible_ids)
    admitted = [sample for sample in population if sample.sample_id in eligible]
    normalization = fit_normalization(population, analysis_version=analysis_version)
    result = select_shortlist(stored.sample, admitted, policy=policy,
                              normalization=normalization)
    domain_rejected = tuple((mask.name, mask.domain_rejected) for mask in
                            mask_counts(population, analysis_version=analysis_version))
    return StoredRetrieval(
        result=result, filter_result=filter_result, population_count=counts["population"],
        duplicate_candidates=counts["duplicates"],
        skipped_stale_analysis=counts["stale"], skipped_absent_analysis=counts["absent"],
        domain_rejected=domain_rejected, representative_paths=representatives)


def _pair_list(value) -> tuple:
    """The loaded pair list and its `sha256:` byte digest.

    A path is read as bytes and parsed with #19's own reader parts
    (`manifest.read_json` plus `rating.check_pair_list_document`, which is
    exactly what `rating.load_pair_list` runs); an already-loaded document is
    accepted and digested over its canonical bytes. A malformed or unreadable
    pair list raises `malformed_pair_list`, a path-free refusal.
    """

    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        try:
            data = path.read_bytes()
            document = read_json(path)
        except (OSError, ValueError) as error:
            raise RetrievalReportError(
                "malformed_pair_list", "The pair list is missing or unreadable.") from error
        source = data
    elif isinstance(value, Mapping):
        document = dict(value)
        try:
            source = canonical(document).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise RetrievalReportError(
                "malformed_pair_list", "The pair list is not canonically serialisable.") from error
    else:
        raise RetrievalReportError(
            "malformed_pair_list", "A pair-list path or document is required.")
    if rating.check_pair_list_document(document):
        raise RetrievalReportError(
            "malformed_pair_list", "The pair list does not match the #16 pair-list schema.")
    return document, "sha256:" + hashlib.sha256(source).hexdigest()


def _export(index, value) -> tuple:
    """One loaded, validated #17 export document with its `sha256:` digest.

    The field sets and the label vocabulary are #17's own
    (`rating.EXPORT_FIELDS`, `rating.RATING_FIELDS`, `rating.SCHEMA_VERSION`,
    `rating.LABELS`); this module defines no second export schema. Returns
    `(digest, session_id, evaluator_id, document)`.
    """

    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        try:
            data = path.read_bytes()
            document = read_json(path)
        except (OSError, ValueError) as error:
            raise RetrievalReportError(
                "malformed_export", f"Export {index} is missing or unreadable.") from error
        source = data
    elif isinstance(value, Mapping):
        document = dict(value)
        try:
            source = canonical(document).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise RetrievalReportError(
                "malformed_export", f"Export {index} is not canonically serialisable.") from error
    else:
        raise RetrievalReportError(
            "malformed_export", f"Export {index} is not an export document or path.")
    try:
        fields(document, rating.EXPORT_FIELDS)
    except ValueError as error:
        raise RetrievalReportError(
            "malformed_export", f"Export {index} is not a #17 export document.") from error
    if document["schema_version"] != rating.SCHEMA_VERSION:
        raise RetrievalReportError(
            "malformed_export", f"Export {index} carries another schema version.")
    session, records = document["session"], document["ratings"]
    if type(session) is not dict or type(records) is not list:
        raise RetrievalReportError(
            "malformed_export", f"Export {index} carries no session or no rating list.")
    for name in ("session_id", "evaluator_id"):
        if type(session.get(name)) is not str or not session[name].strip():
            raise RetrievalReportError(
                "malformed_export", f"Export {index} carries no {name}.")
    for record in records:
        try:
            fields(record, " ".join(rating.RATING_FIELDS))
        except ValueError as error:
            raise RetrievalReportError(
                "malformed_export", f"Export {index} has a malformed rating record.") from error
        if type(record["skip"]) is not bool or type(record["presentation_index"]) is not int:
            raise RetrievalReportError(
                "malformed_export", f"Export {index} has a malformed rating record.")
        if record["rating"] is not None and record["rating"] not in rating.LABELS:
            raise RetrievalReportError(
                "malformed_export", f"Export {index} carries a label outside #17's vocabulary.")
        for name in ("pair_id", "kick_sample_id", "bass_sample_id"):
            if type(record[name]) is not str or not record[name].strip():
                raise RetrievalReportError(
                    "malformed_export", f"Export {index} has a malformed rating record.")
    return ("sha256:" + hashlib.sha256(source).hexdigest(), session["session_id"],
            session["evaluator_id"], document)


def reference_for(pair_list, exports) -> RecallReference:
    """Build the reference from the pair list and the export records only.

    Called before any retrieval runs and independent of it: the reference reads
    no ranking, no Jev judgment, no compatibility score and no `FilterResult`,
    so monkeypatching `select_shortlist` to raise leaves it unchanged. For each
    query kick ascending by `kick_sample_id`: `declared` is the distinct
    `bass_sample_id` values in ascending order and `positive` is the declared
    references whose aggregate under #19's landed `ratings_by_pair` /
    `pair_aggregates` (at least
    `MIN_RATINGS_PER_PAIR` distinct evaluators, lower-middle median) equals
    `LABEL_VALUES["good"]` or `LABEL_VALUES["excellent"]` -- the two labels
    `RECALL_POSITIVE_LABELS` names. A skipped presentation and a record whose
    `rating` is not one of #17's labels never contribute, and a record for a
    pair the list does not hold is ignored. Exports are processed in a stable
    `(session_id, evaluator_id, digest)` order so the aggregates do not depend
    on the caller's argument order.
    """

    comparison = _comparison()
    document, pair_list_digest = _pair_list(pair_list)
    loaded = [_export(index, value) for index, value in enumerate(exports or (), start=1)]
    loaded.sort(key=lambda item: (item[1], item[2], item[0]))
    pairs = {}
    for pair in document["pairs"]:
        pairs.setdefault(pair["pair_id"], pair)
    by_query = {}
    for pair_id in sorted(pairs):
        pair = pairs[pair_id]
        by_query.setdefault(pair["kick_sample_id"], {})[(pair["kick_sample_id"],
                                                         pair["bass_sample_id"])] = pair_id
    ratings = []
    for digest_value, session_id, evaluator_id, exported in loaded:
        for record in exported["ratings"]:
            label = record["rating"]
            if record["skip"] is True or label not in rating.LABELS:
                continue
            if record["pair_id"] not in pairs:
                continue
            ratings.append(comparison.Rating(
                record["pair_id"], pairs[record["pair_id"]]["kick_sample_id"],
                pairs[record["pair_id"]]["bass_sample_id"],
                comparison.LABEL_VALUES[label], evaluator_id, session_id))
    ratings.sort(key=lambda item: (item.pair_id, item.evaluator_id, item.session_id))
    aggregates = comparison.pair_aggregates(comparison.ratings_by_pair(ratings))
    positive_values = {comparison.LABEL_VALUES[label] for label in RECALL_POSITIVE_LABELS}
    positive_pairs = {pair_id for pair_id, aggregate in aggregates.items()
                      if aggregate["value"] in positive_values}
    queries = []
    for kick_id in sorted(by_query):
        declared = sorted({bass_id for _kick, bass_id in by_query[kick_id]})
        positive = tuple(bass_id for bass_id in declared
                         if by_query[kick_id][(kick_id, bass_id)] in positive_pairs)
        queries.append(RecallQuery(kick_id, tuple(declared), positive))
    return RecallReference(
        pair_list_digest=pair_list_digest, dataset_version=document["dataset_version"],
        split_manifest_digest=document["split_manifest_digest"],
        sampler_seed=document["sampler_seed"], assignment_seed=document["assignment_seed"],
        export_digests=tuple(sorted(item[0] for item in loaded)),
        positive_labels=RECALL_POSITIVE_LABELS,
        min_ratings_per_pair=comparison.MIN_RATINGS_PER_PAIR, queries=tuple(queries))


def _retrieval_records(retrieval) -> dict:
    """One `StoredRetrieval`, a sequence of them or a mapping, keyed by kick."""

    if retrieval is None:
        return {}
    if type(retrieval) is StoredRetrieval:
        return {retrieval.filter_result.kick_id: retrieval}
    if isinstance(retrieval, Mapping):
        items = list(retrieval.items())
    elif isinstance(retrieval, Sequence) and not isinstance(retrieval, (str, bytes)):
        items = [(record.filter_result.kick_id, record)
                 for record in retrieval if type(record) is StoredRetrieval]
        if len(items) != len(retrieval):
            raise RetrievalReportError(
                "invalid_retrieval", "Every retrieval entry must be a StoredRetrieval.")
    else:
        raise RetrievalReportError(
            "invalid_retrieval", "A StoredRetrieval, a sequence of them or a kick-keyed mapping is required.")
    records = {}
    for key, record in items:
        if type(record) is not StoredRetrieval or not isinstance(key, str):
            raise RetrievalReportError(
                "invalid_retrieval", "Every retrieval entry must be a StoredRetrieval keyed by kick id.")
        records[key] = record
    return records


def _query_evidence(query, record) -> tuple:
    """The per-query denominators, ids and misses for one retrieval record."""

    supplied = {sample_id for sample_id, _version in record.filter_result.analysis_versions}
    available = tuple(item for item in query.declared if item in supplied)
    positive = tuple(item for item in query.positive if item in supplied)
    shortlist = set(record.result.shortlist)
    eligible = set(record.filter_result.eligible_ids)
    excluded = {entry.sample_id: tuple(reason.code for reason in entry.reasons)
                for entry in record.filter_result.excluded}
    declared = set(query.declared)
    misses = []
    for item in query.declared:
        if item in shortlist:
            continue
        if item in eligible:
            misses.append(RecallMiss(item, "cut", ()))
        elif item in excluded:
            misses.append(RecallMiss(item, "filter_excluded", excluded[item]))
        else:
            misses.append(RecallMiss(item, "missing_from_library", ()))
    return available, positive, shortlist & declared, shortlist & set(positive), tuple(misses)


def recall_metrics(reference, retrieval) -> RecallMetrics:
    """Measure one reference against the retrieval runs made for it.

    `retrieval` is one `StoredRetrieval`, a sequence of them or a mapping keyed
    by query kick; a query with no record could not be evaluated from the
    library and carries `query_kick_missing`. The helpers take no ranking, Jev
    or filter argument: the admission evidence they read is #11's result, which
    the record carries unchanged.

    A query is measurable only with `positive_count >= 1`; a query with no
    positive reference is counted in `queries_not_evaluable` with reason
    `no_positive_references`, is excluded from both numerator and denominator
    and never contributes a recall of 1. Every miss is classified as `cut`,
    `filter_excluded` (with #11's codes) or `missing_from_library`, so a
    filtered or unanalysed reference is never reported as a retrieval failure.
    The curve is diagnostic only and is computed from `ranked[:k]`, never by
    running a smaller policy.
    """

    if type(reference) is not RecallReference:
        raise RetrievalReportError("invalid_retrieval", "A RecallReference is required.")
    records = _retrieval_records(retrieval)
    sizes = {record.result.policy.shortlist_size for record in records.values()}
    if len(sizes) > 1:
        raise RetrievalReportError(
            "invalid_retrieval", "Every retrieval run must use one shortlist size.")
    size = sizes.pop() if sizes else None
    rows, prepared = [], []
    for query in reference.queries:
        record = records.get(query.query_kick_id)
        if record is None:
            rows.append(RecallQueryRow(query.query_kick_id, len(query.declared), 0, 0, 0, 0,
                                       None, "query_kick_missing", ()))
            continue
        available, positive, hit_declared, hit_positive, misses = _query_evidence(query, record)
        if not available:
            reason = "no_available_candidates"
        elif not positive:
            reason = "no_positive_references"
        else:
            reason = None
        recall = (len(hit_positive) / len(positive)) if positive else None
        rows.append(RecallQueryRow(query.query_kick_id, len(query.declared), len(available),
                                   len(positive), len(hit_declared), len(hit_positive), recall,
                                   reason, misses))
        prepared.append((query, record, available, positive, reason))
    curve = []
    if size is not None:
        measurable = [item for item in prepared if item[4] is None]
        for point_size in RECALL_AT_SIZES:
            if point_size > size:
                continue
            numerator = coverage_numerator = denominator = coverage_denominator = 0
            for query, record, available, positive, _reason in measurable:
                top = {candidate.sample_id for candidate in record.result.ranked[:point_size]}
                numerator += len(top & set(positive))
                denominator += len(positive)
                coverage_numerator += len(top & set(available))
                coverage_denominator += len(available)
            curve.append(RecallCurvePoint(
                point_size,
                (numerator / denominator) if denominator else None,
                (coverage_numerator / coverage_denominator) if coverage_denominator else None))
    totals = RecallTotals(
        queries=len(rows),
        measurable_queries=sum(1 for row in rows if row.recall is not None),
        queries_not_evaluable=sum(1 for row in rows if row.not_evaluable_reason is not None),
        declared_total=sum(row.declared_count for row in rows),
        available_total=sum(row.available_count for row in rows),
        positive_total=sum(row.positive_count for row in rows),
        retrieved_declared_total=sum(row.retrieved_declared for row in rows),
        retrieved_positive_total=sum(row.retrieved_positive for row in rows),
        recall_at_size=None, coverage_at_size=None)
    measurable = [item for item in prepared if item[4] is None]
    if totals.positive_total:
        totals = RecallTotals(**{**totals.to_dict(),
                                 "recall_at_size": totals.retrieved_positive_total
                                 / totals.positive_total})
    # Coverage is the recall shape over `available`: the shortlist, not the whole
    # ranked order, over the declared references that hold a current row.
    coverage_denominator = sum(len(item[2]) for item in measurable)
    coverage_numerator = sum(len(set(item[1].result.shortlist) & set(item[2]))
                             for item in measurable)
    if coverage_denominator:
        totals = RecallTotals(**{**totals.to_dict(),
                                 "coverage_at_size": coverage_numerator / coverage_denominator})
    return RecallMetrics(queries=tuple(rows), curve=tuple(curve), totals=totals)


def _reference_block(reference) -> dict:
    """The report's `reference` object, all-null when no pair list was read."""

    if reference is not None:
        return reference.to_dict()
    comparison = _comparison()
    return {
        "pair_list_digest": None,
        "dataset_version": None,
        "split_manifest_digest": None,
        "sampler_seed": None,
        "assignment_seed": None,
        "export_digests": [],
        "positive_labels": list(RECALL_POSITIVE_LABELS),
        "min_ratings_per_pair": comparison.MIN_RATINGS_PER_PAIR,
    }


def _empty_totals() -> dict:
    """The totals of a report whose pair list or exports do not exist."""

    return RecallTotals(0, 0, 0, 0, 0, 0, 0, 0, None, None).to_dict()


def recall_report(connection, *, pair_list_path, export_paths, size) -> dict:
    """Build the version-complete retrieval recall report.

    The report carries exactly `RETRIEVAL_SCHEMA`, `retrieval_version`,
    `representation_version`, `normalization_id`, `analysis_version`,
    `shortlist_size`, `policy`, `population_count`, `duplicate_candidates`,
    `skipped_stale_analysis`, `skipped_absent_analysis`, `reference`,
    `queries`, `curve`, `totals`, `state` and `blocked_reason`, and nothing
    else. It contains no timestamp, path, hostname, pid or duration.

    A missing pair list (`pair_list_absent`), no export documents
    (`no_sessions`) or exports with no positive pair (`no_positive_references`)
    is a documented blocker: the report is still written, `totals.recall_at_size`
    is null and the counts and denominators are reported, never a fabricated
    recall. `blocked_reason` is null in the `measured` state.
    """

    policy = RetrievalPolicy(size)
    analysis_version = digest(analysis_descriptor())
    repository = LibraryRepository(connection)
    rows = repository.list_retrieval_rows(analysis_version, roles=RETRIEVAL_CANDIDATE_ROLES)
    population, counts, _representatives = _collapse(rows)
    normalization = fit_normalization(population, analysis_version=analysis_version)
    document = {
        "retrieval_schema": RETRIEVAL_SCHEMA,
        "retrieval_version": RETRIEVAL_POLICY_VERSION,
        "representation_version": REPRESENTATION_VERSION,
        "normalization_id": normalization_id(normalization),
        "analysis_version": analysis_version,
        "shortlist_size": policy.shortlist_size,
        "policy": policy.to_dict(),
        "population_count": counts["population"],
        "duplicate_candidates": counts["duplicates"],
        "skipped_stale_analysis": counts["stale"],
        "skipped_absent_analysis": counts["absent"],
        "reference": _reference_block(None),
        "queries": [],
        "curve": [],
        "totals": _empty_totals(),
        "state": "insufficient_evidence",
        "blocked_reason": "pair_list_absent",
    }
    exports = tuple(export_paths or ())
    if pair_list_path is None:
        return document
    path = Path(pair_list_path)
    if not path.exists():
        return document
    if not path.is_file():
        raise RetrievalReportError(
            "malformed_pair_list", "The pair list is not a regular file.")
    reference = reference_for(pair_list_path, exports)
    retrievals = {}
    for query in reference.queries:
        try:
            retrievals[query.query_kick_id] = retrieve_shortlist(
                connection, query.query_kick_id, policy=policy)
        except (LibraryError, FilterInputError, RetrievalInputError):
            # A query kick this library cannot evaluate is reported as
            # query_kick_missing / no_available_candidates, never as a recall.
            continue
    metrics = recall_metrics(reference, retrievals)
    if not exports:
        blocked = "no_sessions"
    elif metrics.totals.positive_total < 1:
        blocked = "no_positive_references"
    else:
        blocked = None
    document.update({
        "reference": reference.to_dict(),
        "queries": [row.to_dict() for row in metrics.queries],
        "curve": [point.to_dict() for point in metrics.curve],
        "totals": metrics.totals.to_dict(),
        "state": "measured" if blocked is None else "insufficient_evidence",
        "blocked_reason": blocked,
    })
    return document


def _size_argument(value) -> int:
    """`--size` as an int inside the one bound; `invalid_shortlist_size` otherwise."""

    try:
        size = int(value)
    except (TypeError, ValueError):
        raise RetrievalInputError(
            "invalid_shortlist_size",
            f"--size must be an integer between {SHORTLIST_MIN} and {SHORTLIST_MAX}.") from None
    RetrievalPolicy(size)
    return size


def _unresolved(document) -> bool:
    """True when a report carries a pair-list query the library could not evaluate."""

    return any(row.get("not_evaluable_reason") in ("query_kick_missing",
                                                   "no_available_candidates")
               for row in document.get("queries", ()))


def _error_code(error) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "invalid_arguments"


def _error_message(error) -> str:
    """One path-free sentence; a foreign error's text may name a local path."""

    if isinstance(error, (RetrievalReportError, RetrievalInputError, FilterInputError,
                          _ArgumentRefusal)):
        return str(error) or "The retrieval request is not valid."
    return "The input could not be used; see the code."


def _write_report(path, document, protected=()) -> None:
    """Write the report through a flushed, fsynced temporary file and a replace.

    The mechanism is the landed private-write one (#10's `write_private`, #17's
    atomic text): a same-directory `tempfile.mkstemp` file, `flush`,
    `os.fsync`, then `os.replace`, with the output refused when it is not a
    regular unlinked file or when it resolves to one of the command's inputs.
    `backend.evaluation.manifest.write_private` is deliberately not called:
    it refuses to replace an existing file that does not carry a
    `dataset_version` field, which this report does not carry and may not
    carry, so a second run could not replace its own report under the same
    `--output`; the report is private local data and the replace is atomic.
    """

    try:
        target = local_path(path)
    except (BatchError, OSError, TypeError, ValueError) as error:
        raise RetrievalReportError(
            "unwritable_output", "The output path is not a usable local path.") from error
    if target.suffix.lower() != ".json":
        raise RetrievalReportError("invalid_arguments", "--output must end in .json.")
    if not target.parent.is_dir():
        raise RetrievalReportError(
            "unwritable_output", "The output directory does not exist.")
    for other in protected:
        if other is None:
            continue
        try:
            resolved = local_path(other)
        except (BatchError, OSError, TypeError, ValueError):
            continue
        if resolved == target:
            raise RetrievalReportError(
                "aliased_output", "The report would replace one of its own inputs.")
    try:
        info = target.lstat()
    except FileNotFoundError:
        info = None
    except OSError as error:
        raise RetrievalReportError(
            "unwritable_output", "The output path is not readable.") from error
    if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise RetrievalReportError(
            "aliased_output", "The output is not a regular unlinked file.")
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(document) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as error:
        raise RetrievalReportError(
            "unwritable_output",
            "The report could not be written; the previous content is unchanged.") from error
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv=None) -> int:
    """The `recall` command. Exit 0, 1, 2 or 130; never a traceback.

    `--database`, `--output` and the optional `--pair-list`, repeatable
    `--export` and `--size` are the whole command line; an argument-level
    refusal (no command, a missing required option or an unknown option) is
    `invalid_arguments`, one path-free line with no argparse usage table.
    Every failure prints exactly one code and one path-free sentence: 2 for an
    unreadable or unsupported database, a malformed pair list or export, or an
    unwritable or aliased output; 1 when a report was written and at least one
    pair-list query could not be evaluated from the library; 0 when a report was
    written, the insufficient-evidence report included; 130 when the process was
    interrupted before the atomic replace.
    """

    parser = _ArgumentParser(
        prog="python -m backend.library.retrieval",
        description="Normalized kick-to-bass retrieval recall report (#25).")
    commands = parser.add_subparsers(dest="command", required=True)
    recall = commands.add_parser("recall", help="write one retrieval recall report as JSON")
    recall.add_argument("--database", required=True, help="the local library database")
    recall.add_argument("--pair-list", default=None, help="the #16 pair-list document")
    recall.add_argument("--export", action="append", default=[],
                        help="one #17 export.json document, repeatable")
    recall.add_argument("--size", default=str(DEFAULT_SHORTLIST_SIZE),
                        help=f"the shortlist size, {SHORTLIST_MIN} to {SHORTLIST_MAX}")
    recall.add_argument("--output", required=True, help="the report destination, ending in .json")
    try:
        arguments = parser.parse_args(argv)
        size = _size_argument(arguments.size)
        if not arguments.output.lower().endswith(".json"):
            raise RetrievalReportError("invalid_arguments", "--output must end in .json.")
        connection = open_database(arguments.database)
        try:
            document = recall_report(connection, pair_list_path=arguments.pair_list,
                                     export_paths=tuple(arguments.export), size=size)
        finally:
            connection.close()
        protected = tuple(item for item in (arguments.pair_list, *arguments.export) if item)
        _write_report(arguments.output, document, protected)
        return 1 if _unresolved(document) else 0
    except KeyboardInterrupt:
        print("interrupted: no report was replaced", file=sys.stderr)
        return 130
    except (RetrievalReportError, RetrievalInputError, FilterInputError, LibraryError,
            BatchError, ValueError, _ArgumentRefusal) as error:
        print(f"{_error_code(error)}: {_error_message(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

