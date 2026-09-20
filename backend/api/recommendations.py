"""The recommendation operation of the local service (issue #28).

    POST /recommendations
    {"palette_id": "...", "revision": 3, "limit": 10, "filters": {"tempo_lock": true}}

One call loads the stored palette context, admits candidates through #11's
deterministic filters, cuts them through #25's bounded shortlist, resolves the
five weighted #13 questions through #14 -- from #26's cache first and live only
for the misses -- and ranks them through #15's hybrid ranking, answering with a
schema-1 RecommendationBatch projection plus one run block that records every
version, count and exclusion behind it.

Every awkward state has exactly one documented response: an empty or
all-excluded library is 200 with empty results, a stale or deleted palette is
409 revision_conflict, an unusable kick is 409 kick_unavailable carrying #11's
or #21's own reason code, unavailable Jev is a DSP-only 200, and a cache hit is
recorded as served from the cache, never as a live Jev outcome.

This module owns the operation and nothing else. Every step is a landed call:
#11 admits, #12 and #15 rank, #13 builds the questions, #14 scores them, #24
assembles the palette context, #25 retrieves the shortlist and #26 caches the
decisions. No weight table, label map, dimension tuple, tie-break, threshold,
similarity formula, filter or database statement is re-declared here, and this
module is the only backend/api module allowed to import backend.intelligence:
it reads no credential value itself, opens no socket of its own and returns no
path.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import math
import os
import sqlite3
import time

from backend.analysis.batch import analysis_descriptor, digest
from backend.api.errors import ApiError
from backend.api.schemas import RUN_ID_PATTERN, Response
from backend.contracts import AudioMetadata, PaletteContext, RankedCandidate, RecommendationBatch
import backend.intelligence.jev as jev_adapter
import backend.intelligence.questions as jev_questions
from backend.library import decision_cache
from backend.library.errors import LibraryError, PaletteIncomplete
from backend.library.repository import LibraryRepository, content_identity
from backend.library.schema import open_database
from backend.library.retrieval import retrieve_shortlist
from backend.palette import compatibility, ranking
from backend.palette.compatibility import Availability, FilterPolicy
from backend.palette.model import PALETTE_HASH_VERSION, palette_hash
from backend.palette.retrieval import RetrievalPolicy


RECOMMENDATION_METHOD = "POST"
RECOMMENDATION_PATH = "/recommendations"

#: The requested result bounds and the default: the documented [5, 20].
RESULT_LIMIT_MIN = 5
RESULT_LIMIT_MAX = 20
DEFAULT_RESULT_LIMIT = 10

#: The whole operation's budget, strictly below #27's socket timeout, so the
#: route always gets to write a body: a run that reaches it keeps the evidence it
#: obtained and reports the rest as not attempted.
RECOMMENDATION_BUDGET_SECONDS = 25.0

#: The closed filter key set, read from #11's own record and never re-declared.
FILTER_KEYS = tuple(field.name for field in fields(FilterPolicy))

#: The request body's fields.
REQUEST_FIELDS = ("palette_id", "revision", "limit", "filters")

#: The four non-path fields of one stored AudioMetadata: the one documented
#: deviation of the projection from the contract, exactly as #27's feature route
#: projects a sample's audio.
AUDIO_FIELDS = ("sample_rate_hz", "channels", "frame_count", "duration_ms")

#: The application attribute a caller may publish when the requesting client has
#: gone away. #27's route context carries no connection, so the signal is only
#: observable when the application carries it; see the deviation note in
#: _docs/recommendation-api.md.
CLIENT_GONE_ATTRIBUTE = "recommendation_client_gone"

# The stored availability vocabulary (#22/#24) mapped onto #11's states. A
# status the schema does not store today maps to UNKNOWN rather than to
# available, and an unreadable row stays unreadable.
_AVAILABILITY = {"present": Availability.AVAILABLE, "missing": Availability.MISSING,
                 "unreadable": Availability.UNREADABLE, "unknown": Availability.UNKNOWN}

#: The kick-side code #11's admission reports for each non-available state.
_AVAILABILITY_REASONS = {Availability.MISSING: "file_missing",
                         Availability.UNREADABLE: "file_unreadable",
                         Availability.UNKNOWN: "availability_unknown"}

# The #14 outcome states that carry a validated judgment, and the two transport
# sources #26 stores as a decision's origin.
_JUDGED_STATES = ("judged", "abstained")
_CACHE_FAILURES = (sqlite3.Error, LibraryError, ValueError, KeyError)


@dataclass(frozen=True)
class RecommendationRequest:
    """One validated POST /recommendations body.

    palette_id matches #27's own id rule (the run_id pattern, not a second one),
    revision is a nonnegative whole number, limit is inside
    [RESULT_LIMIT_MIN, RESULT_LIMIT_MAX] and filters is the #11 policy the
    request asked for.
    """

    palette_id: str
    revision: int
    limit: int = DEFAULT_RESULT_LIMIT
    filters: FilterPolicy = FilterPolicy()

    def as_value(self) -> dict:
        """The value a fixture case compares against."""

        return {"palette_id": self.palette_id, "revision": self.revision, "limit": self.limit,
                "filters": {"tempo_lock": self.filters.tempo_lock,
                            "exact_key_lock": self.filters.exact_key_lock}}


@dataclass(frozen=True)
class RecommendationCounts:
    """The seven documented counts; eligible plus excluded covers the candidate
    ids #11 was given, and returned is the smaller of requested and scored."""

    eligible: int
    excluded: int
    shortlisted: int
    scored: int
    unscored: int
    requested: int
    returned: int

    def as_value(self) -> dict:
        return {"eligible": self.eligible, "excluded": self.excluded,
                "shortlisted": self.shortlisted, "scored": self.scored,
                "unscored": self.unscored, "requested": self.requested,
                "returned": self.returned}


@dataclass(frozen=True)
class RecommendationEvidence:
    """Where every weighted dimension of every scored candidate's evidence came from.

    A decision served from #26's cache is counted only under cache, a live #14
    outcome under its transport's own source, and a dimension with no usable
    judgment under unavailable. cache_errors counts the cache reads that failed
    and were treated as misses; interface + double + unavailable + cache always
    equals five times the scored count.
    """

    interface: int = 0
    double: int = 0
    unavailable: int = 0
    cache: int = 0
    cache_errors: int = 0

    def as_value(self) -> dict:
        return {"interface": self.interface, "double": self.double,
                "unavailable": self.unavailable, "cache": self.cache,
                "cache_errors": self.cache_errors}


@dataclass(frozen=True)
class RecommendationRun:
    """One run's own closed block: the versions, counts, exclusions and evidence."""

    run_id: str
    palette_hash: str
    analysis_version: str
    policy_version: str
    filters: FilterPolicy
    retrieval: object
    duplicate_candidates: int
    skipped_stale_analysis: int
    skipped_absent_analysis: int
    ranking_version: str
    weight_table_id: str
    jev_status: str
    prompt_version: str
    adapter_version: str
    model_versions: tuple
    counts: RecommendationCounts
    exclusions: tuple
    evidence: RecommendationEvidence


@dataclass(frozen=True)
class RecommendationResult:
    """One completed operation: the projection's inputs and the run behind them."""

    run: RecommendationRun
    palette: PaletteContext
    samples: tuple
    results: tuple
    alternatives: tuple
    ranking_version: str
    mode: str


@dataclass(frozen=True)
class RecommendationCollaborators:
    """The injectable seams of one recommendation operation.

    transport, config, credentials and environ are handed to #14 exactly as
    received; cache is an object with lookup and store callables (the landed #26
    operations by default) and monotonic is the one clock the budget is measured
    with. A collaborator left None means the landed default: #26's own cache,
    os.environ and time.monotonic.
    """

    transport: object | None = None
    config: object | None = None
    credentials: object | None = None
    environ: object | None = None
    cache: object | None = None
    monotonic: object | None = None


def parse_recommendation_request(body) -> RecommendationRequest:
    """The validated body of POST /recommendations.

    The body is a closed object: palette_id and revision are required, limit and
    filters are optional, and every other key -- and every filter key #11 does
    not declare -- is refused. No refusal echoes a rejected value: details
    carries the fixed field name only.
    """

    if type(body) is not dict:
        raise ApiError("invalid_body")
    unknown = sorted(set(body) - set(REQUEST_FIELDS))
    if unknown:
        raise ApiError("unknown_field", details={"field": unknown[0]})
    for name in ("palette_id", "revision"):
        if name not in body:
            raise ApiError("missing_field", details={"field": name})
    palette_id = body["palette_id"]
    if not isinstance(palette_id, str) or not RUN_ID_PATTERN.fullmatch(palette_id):
        raise ApiError("invalid_palette_id")
    revision = body["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ApiError("invalid_revision")
    limit = body.get("limit", DEFAULT_RESULT_LIMIT)
    if (isinstance(limit, bool) or not isinstance(limit, int)
            or not RESULT_LIMIT_MIN <= limit <= RESULT_LIMIT_MAX):
        raise ApiError("invalid_limit")
    raw_filters = body.get("filters", {})
    if type(raw_filters) is not dict:
        raise ApiError("invalid_field_type", details={"field": "filters"})
    unknown_filters = sorted(set(raw_filters) - set(FILTER_KEYS))
    if unknown_filters:
        raise ApiError("unknown_field", details={"field": "filters." + unknown_filters[0]})
    for name in FILTER_KEYS:
        if name in raw_filters and type(raw_filters[name]) is not bool:
            raise ApiError("invalid_field_type", details={"field": "filters." + name})
    return RecommendationRequest(
        palette_id=palette_id, revision=revision, limit=limit,
        filters=FilterPolicy(**{name: raw_filters.get(name, False) for name in FILTER_KEYS}))


def run_id_for(palette, request, *, palette_hash_value, kick, retrieval, analysis_version) -> str:
    """The derived request-and-evidence identity of one run, as 64 lowercase hex.

    Exactly the documented inputs enter the digest: the palette id, hash and
    revision, the kick's id and analysis version, the request's limit and its two
    locks, the analysis version, #25's policy, representation and normalization
    versions with the ordered shortlist and its requested and returned sizes and
    limit reason, and #15's, #13's and #14's version constants. Nothing else: no
    clock, path, endpoint, transport, cache state or observed judgment.
    """

    payload = {
        "palette_id": palette.palette_id,
        "palette_hash": palette_hash_value,
        "revision": palette.revision,
        "kick_id": kick.sample_id,
        "kick_analysis_version": kick.analysis_version,
        "limit": request.limit,
        "tempo_lock": request.filters.tempo_lock,
        "exact_key_lock": request.filters.exact_key_lock,
        "analysis_version": analysis_version,
        "retrieval_policy_version": retrieval.policy_version,
        "representation_version": retrieval.representation_version,
        "normalization_id": retrieval.normalization_id,
        "shortlist": list(retrieval.shortlist),
        "shortlist_size_requested": retrieval.shortlist_size_requested,
        "shortlist_size_returned": retrieval.shortlist_size_returned,
        "limit_reason": retrieval.limit_reason,
        "hybrid_ranking_version": ranking.HYBRID_RANKING_VERSION,
        "hybrid_weight_table_id": ranking.HYBRID_WEIGHT_TABLE_ID,
        "prompt_version": jev_questions.PROMPT_VERSION,
        "adapter_version": jev_adapter.ADAPTER_VERSION,
    }
    return digest(payload)


def project_recommendation(result: RecommendationResult) -> dict:
    """The schema-1 RecommendationBatch projection of one completed operation.

    The contract record is built first, so the projected block is a validated
    batch; then every field is copied field for field, with the one documented
    deviation: a sample's audio carries the four non-path fields and no
    local_path.
    """

    batch = RecommendationBatch(
        run_id=result.run.run_id, palette=result.palette, samples=tuple(result.samples),
        results=tuple(result.results), ranking_version=result.ranking_version, mode=result.mode,
        alternatives=tuple(result.alternatives))
    return _project(batch)


def project_run(run: RecommendationRun) -> dict:
    """The one closed run block: versions, counts, exclusions and evidence."""

    retrieval = run.retrieval
    return {
        "run_id": run.run_id,
        "palette_hash": run.palette_hash,
        "analysis_version": run.analysis_version,
        "filters": {"policy_version": run.policy_version,
                    "tempo_lock": run.filters.tempo_lock,
                    "exact_key_lock": run.filters.exact_key_lock},
        "retrieval": {"policy_version": retrieval.policy_version,
                      "representation_version": retrieval.representation_version,
                      "normalization_id": retrieval.normalization_id,
                      "shortlist_size_requested": retrieval.shortlist_size_requested,
                      "shortlist_size_returned": retrieval.shortlist_size_returned,
                      "limit_reason": retrieval.limit_reason,
                      "duplicate_candidates": run.duplicate_candidates,
                      "skipped_stale_analysis": run.skipped_stale_analysis,
                      "skipped_absent_analysis": run.skipped_absent_analysis},
        "ranking": {"ranking_version": run.ranking_version,
                    "weight_table_id": run.weight_table_id, "jev_status": run.jev_status},
        "jev": {"prompt_version": run.prompt_version, "adapter_version": run.adapter_version,
                "model_versions": list(run.model_versions)},
        "counts": run.counts.as_value(),
        "exclusions": [{"code": code, "count": count} for code, count in run.exclusions],
        "evidence": run.evidence.as_value(),
    }


def handle_recommendation(context):
    """POST /recommendations: validate one body, run one operation, answer 200."""

    request = parse_recommendation_request(context.body)
    app = context.app
    collaborators = getattr(app, "recommendations", None) or RecommendationCollaborators()
    clock = time.monotonic if collaborators.monotonic is None else collaborators.monotonic
    started = clock()

    def cancelled():
        if clock() - started >= RECOMMENDATION_BUDGET_SECONDS:
            return True
        return bool(getattr(app, CLIENT_GONE_ATTRIBUTE, False))

    connection = _open_connection(app)
    try:
        result = run_recommendation(connection, request, collaborators=collaborators,
                                    cancelled=cancelled)
    finally:
        connection.close()
    return Response(200, {"api_schema": app.api_schema,
                          "recommendation": project_recommendation(result),
                          "run": project_run(result.run)})


def run_recommendation(connection, request, *, collaborators, cancelled) -> RecommendationResult:
    """Run one operation against one open library connection.

    The palette is read once and its revision checked against the request, the
    kick and the candidate population are read through #21, #25 retrieves the
    bounded shortlist, #12 ranks the baseline, the five weighted #13 questions
    are resolved through #26's cache first and #14 for the misses, and #15
    combines the two. The palette is re-read after ranking: a revision or a
    content hash the run did not start from refuses the whole operation before
    any decision is stored.
    """

    repository = LibraryRepository(connection)
    analysis_version = digest(analysis_descriptor())
    record = repository.load_palette(request.palette_id)
    if record is None:
        raise ApiError("unknown_palette", details={"palette_id": request.palette_id})
    if record.revision != request.revision:
        raise ApiError("revision_conflict", details={"expected_revision": request.revision,
                                                     "current_revision": record.revision,
                                                     "phase": "before_ranking"})
    try:
        palette = record.to_context()
    except PaletteIncomplete:
        raise ApiError("palette_incomplete", details={"palette_id": request.palette_id}) from None
    content_hash = palette_hash(record)
    kick = _kick_sample(repository, analysis_version, palette, record)
    stored = retrieve_shortlist(connection, palette.kick_id, policy=RetrievalPolicy(),
                                filter_policy=request.filters, context=palette.song)
    shortlist_samples, selected, fingerprints = _shortlist_samples(
        repository, analysis_version, stored.result.shortlist, palette.selected_bass_id, record)
    baseline = ranking.rank_candidates(kick, shortlist_samples, policy=ranking.RankingPolicy())
    stage = _Stage(connection, collaborators=collaborators, palette=palette,
                   content_hash=content_hash, kick=kick, samples=shortlist_samples,
                   fingerprints=fingerprints, analysis_version=analysis_version)
    stage.resolve(cancelled=cancelled)
    hybrid = ranking.rank_hybrid(baseline, stage.evidence(), policy=ranking.HybridPolicy())
    _require_same_palette(repository, request, record, content_hash)
    stage.commit()
    return _assemble(request, palette=palette, content_hash=content_hash, kick=kick, stored=stored,
                     hybrid=hybrid, shortlist_samples=shortlist_samples, selected=selected,
                     analysis_version=analysis_version, stage=stage)


# ---------------------------------------------------------------------------
# the palette, the kick and the shortlist
# ---------------------------------------------------------------------------


def _active_item(record, slot):
    for item in record.active_items:
        if item.slot == slot:
            return item
    return None


def _kick_sample(repository, analysis_version, palette, record):
    """The validated kick of the request's palette, or the 409 it earns."""

    rows = repository.list_retrieval_rows(analysis_version, sample_ids=(palette.kick_id,))
    if not rows:
        raise ApiError("kick_unavailable",
                       details={"sample_id": palette.kick_id, "reason": "absent"})
    row = rows[0]
    if row.analysis_state != "current":
        raise ApiError("kick_unavailable", details={"sample_id": palette.kick_id,
                                                    "reason": _non_current_reason(repository, row)})
    item = _active_item(record, "kick")
    refusal = _kick_refusal(row, slot_role_mismatch=bool(item is not None
                                                         and item.slot_role_mismatch))
    if refusal is not None:
        raise ApiError("kick_unavailable", details={"sample_id": palette.kick_id,
                                                    "reason": refusal})
    return row.sample


def _kick_refusal(row, *, slot_role_mismatch):
    """The one kick-side code #11's admission would refuse this stored kick for.

    #11's filter_candidates raises one generic invalid_kick for a kick that fails
    its role, availability or core-analysis gates, so the specific code the
    response reports is named here from the same stored facts, in #11's own
    documented check order and using #11's own vocabulary. The candidate-side
    filter is not duplicated: this only names the kick the request cannot proceed
    through.
    """

    if slot_role_mismatch:
        return "wrong_role"
    availability = _AVAILABILITY.get(row.file_status, Availability.UNKNOWN)
    if availability is not Availability.AVAILABLE:
        return _AVAILABILITY_REASONS[availability]
    return _core_refusal(row.sample)


def _core_refusal(sample):
    """#11's first core-analysis kick code, in #11's own order, or None."""

    measures = {item.name: item for item in sample.features.measurements}
    peak, rms = measures["peak"], measures["rms"]
    if sample.audio.frame_count == 0:
        return "empty_audio"
    if peak.value == 0:
        return "silent_audio"
    if peak.value is None:
        return "unusable_analysis"
    if (rms.value is not None and rms.value > peak.value
            and not math.isclose(rms.value, peak.value,
                                 rel_tol=compatibility.CORE_RELATIVE_ROUNDOFF, abs_tol=0)):
        return "inconsistent_analysis"
    return None


def _non_current_reason(repository, row):
    """#21's stored analysis state of one row, in #21's own vocabulary.

    #25's retrieval read decides current, stale or absent; when nothing is stored,
    #21's detail read adds the queue half of the state so a queued or failed kick
    reports pending or failed rather than a blanket absent. Both are #21's own
    reads, and neither is reached by a run that gets a usable kick.
    """

    if row.analysis_state == "stale":
        return "stale"
    detail = repository.sample_detail(content_identity(row.content_sha256))
    if detail is not None and detail.analysis_state in ("stale", "pending", "failed"):
        return detail.analysis_state
    return "absent"


def _shortlist_samples(repository, analysis_version, shortlist, selected_bass_id, record):
    """The shortlist's stored samples in shortlist order, plus the selected bass.

    One keyed read serves the whole shortlist whatever its size, so the statement
    count does not grow with it. The palette's own selected bass is added to the
    same read when it is not already a shortlist member, because the contract
    requires it in samples; a selection whose sample is not a current bass cannot
    be assembled and is refused with the palette's own incomplete code.
    """

    wanted = list(shortlist)
    extra = selected_bass_id is not None and selected_bass_id not in wanted
    if extra:
        wanted.append(selected_bass_id)
    if not wanted:
        return (), None, {}
    rows = repository.list_retrieval_rows(analysis_version, sample_ids=tuple(wanted))
    by_id = {row.sample_id: row for row in rows}
    fingerprints = {row.sample_id: row.content_sha256 for row in rows}
    samples = tuple(by_id[sample_id].sample for sample_id in shortlist
                    if sample_id in by_id and by_id[sample_id].sample is not None)
    selected = None
    if extra:
        row = by_id.get(selected_bass_id)
        if row is None or row.sample is None or row.sample.role not in ("bass", "sub-bass"):
            raise ApiError("palette_incomplete", details={"palette_id": record.palette_id})
        selected = row.sample
    return samples, selected, fingerprints


def _require_same_palette(repository, request, record, content_hash):
    """Refuse a run whose palette changed or vanished while it was working."""

    current = repository.load_palette(record.palette_id)
    if current is None:
        raise ApiError("revision_conflict", details={"expected_revision": request.revision,
                                                     "current_revision": None,
                                                     "phase": "after_ranking"})
    if current.revision != record.revision or palette_hash(current) != content_hash:
        raise ApiError("revision_conflict", details={"expected_revision": request.revision,
                                                     "current_revision": current.revision,
                                                     "phase": "after_ranking"})


# ---------------------------------------------------------------------------
# cache first, then live
# ---------------------------------------------------------------------------


@dataclass
class _Question:
    """One built question's evidence slot, resolved at most once."""

    candidate_id: str
    dimension: str
    question: object
    judgment: object | None = None
    source: str | None = None
    reason: str | None = None
    outcome: object | None = None


class _Stage:
    """The Jev stage of one run: #26's cache first, #14 for the misses.

    The stage owns every question built for the shortlist, the evidence each one
    resolved to, the count of cache reads that failed, and the #14 runs whose
    model versions and decisions are recorded only once the run is known to be
    publishable.
    """

    def __init__(self, connection, *, collaborators, palette, content_hash, kick, samples,
                 fingerprints, analysis_version):
        self.connection = connection
        self.collaborators = collaborators
        self.content_hash = content_hash
        self.analysis_version = analysis_version
        self._fingerprints = fingerprints
        self._pin = None
        self._pin_resolved = False
        self.interface_name, self.source = _transport_identity(collaborators)
        self.lookup, self.store = _cache_operations(collaborators)
        self.questions = []
        for sample in samples:
            for dimension in ranking.HYBRID_WEIGHTED_DIMENSIONS:
                self.questions.append(_Question(
                    candidate_id=sample.sample_id, dimension=dimension,
                    question=jev_questions.build_question(dimension, kick, sample,
                                                          song=palette.song)))
        self.cache_errors = 0
        self.runs = []

    def resolve(self, *, cancelled):
        """Serve every question the cache can, then send the misses in batches."""

        misses = []
        for entry in self.questions:
            if type(entry.question) is jev_questions.UnavailableQuestion:
                entry.reason = entry.question.code
                continue
            if self._serve(entry):
                continue
            misses.append(entry)
        self._send(misses, cancelled=cancelled)

    def _serve(self, entry):
        """Serve one question from #26's cache; False means it is a miss."""

        if not self._has_pin():
            return False
        key = self._key(entry, self._pin.model_version)
        try:
            found = self.lookup(self.connection, key, question=entry.question)
        except _CACHE_FAILURES:
            self.cache_errors += 1
            return False
        if found is not None and found.status == "hit" and found.judgment is not None:
            entry.judgment = found.judgment
            entry.source = "cache"
            return True
        return False

    def _has_pin(self):
        if not self._pin_resolved:
            self._pin_resolved = True
            try:
                self._pin = decision_cache.pinned_model_version(
                    self.connection, interface_name=self.interface_name, source=self.source,
                    adapter_version=jev_adapter.ADAPTER_VERSION,
                    prompt_version=jev_questions.PROMPT_VERSION)
            except _CACHE_FAILURES:
                self._pin = None
                self.cache_errors += 1
        return self._pin is not None

    def _key(self, entry, model_version):
        return decision_cache.judgment_key(
            decision_cache.JudgmentIdentity(
                source=self.source, interface_name=self.interface_name,
                adapter_version=jev_adapter.ADAPTER_VERSION,
                prompt_version=jev_questions.PROMPT_VERSION, model_version=model_version,
                palette_hash=self.content_hash, palette_hash_version=PALETTE_HASH_VERSION,
                candidate_id=entry.candidate_id,
                candidate_content_fingerprint=self._fingerprints[entry.candidate_id],
                candidate_analysis_version=self.analysis_version),
            entry.question)

    def _send(self, misses, *, cancelled):
        """Send the misses to #14 in bounded batches, in shortlist order.

        A batch never exceeds #14's own bound, and a batch that would start after
        the run was cancelled is answered by #14's own not-attempted path without
        a send, so no transport call happens after the abort and every unanswered
        dimension carries #14's code rather than a judgment.
        """

        if not misses:
            return
        requests = [jev_adapter.JevScoringRequest(
            request_id=entry.candidate_id + "|" + entry.dimension, question=entry.question)
            for entry in misses]
        bound = jev_adapter.MAX_BATCH_SIZE
        for start in range(0, len(requests), bound):
            entries = misses[start:start + bound]
            batch = requests[start:start + bound]
            when = cancelled if not cancelled() else _always_cancelled
            run = jev_adapter.score_questions(
                batch, transport=self.collaborators.transport, config=self.collaborators.config,
                credentials=self.collaborators.credentials,
                environ=_environment(self.collaborators), cancelled=when,
                monotonic=self.collaborators.monotonic)
            self.runs.append(run)
            self._apply(entries, run)

    def _apply(self, entries, run):
        """Record one #14 run's outcomes on their evidence slots."""

        by_request = {entry.candidate_id + "|" + entry.dimension: entry for entry in entries}
        for outcome in run.outcomes:
            entry = by_request.get(outcome.request_id)
            if entry is None:
                continue
            if outcome.state in _JUDGED_STATES and outcome.judgment is not None:
                entry.judgment = outcome.judgment
                entry.source = run.source
                entry.outcome = outcome
            else:
                entry.reason = outcome.code

    def evidence(self):
        """#15's evidence mapping: five entries per shortlisted candidate."""

        by_candidate = {}
        for entry in self.questions:
            if entry.judgment is not None:
                item = ranking.HybridEvidence(dimension=entry.dimension, judgment=entry.judgment)
            else:
                item = ranking.HybridEvidence(dimension=entry.dimension,
                                              unavailable_reason=entry.reason or "not_attempted")
            by_candidate.setdefault(entry.candidate_id, []).append(item)
        return {candidate_id: tuple(items) for candidate_id, items in by_candidate.items()}

    def sources(self):
        """The per-dimension origin of every candidate: a transport source or a hit."""

        return [(entry.candidate_id, entry.source if entry.judgment is not None else "unavailable")
                for entry in self.questions]

    def commit(self):
        """Record the observed model versions, then store this run's decisions.

        Both are optimizations: a refused or failed write leaves every existing
        entry unchanged and the response and its status valid. A decision's key
        carries the judgment's own model version, so a version the pin did not
        name is stored under the version it actually was.
        """

        for run in self.runs:
            try:
                decision_cache.record_model_version(self.connection, run)
            except _CACHE_FAILURES:
                pass
        for entry in self.questions:
            if entry.outcome is None:
                continue
            try:
                key = self._key(entry, entry.judgment.model_version)
                self.store(self.connection, key, outcome=entry.outcome)
            except _CACHE_FAILURES:
                pass


def _transport_identity(collaborators):
    """The interface name and source #26's key and #14's run record share."""

    transport = collaborators.transport
    if transport is None:
        return jev_adapter.HttpJevTransport.interface_name, jev_adapter.HttpJevTransport.source
    return transport.interface_name, transport.source


def _cache_operations(collaborators):
    """#26's two landed operations, or the caller's own pair of callables."""

    cache = collaborators.cache
    if cache is None:
        return decision_cache.lookup_judgment, decision_cache.store_judgment
    return cache.lookup, cache.store


def _open_connection(app):
    """One short-lived handler-side connection for this request.

    #27's single database thread runs one statement sequence at a time, so a
    whole recommendation -- including the Jev stage, which waits on a transport --
    would hold it and serialize even the health route behind the run. This route
    therefore opens its own connection for its own thread, exactly as #23's
    runner thread does, and closes it in the same request. A database that cannot
    be opened is #27's database_unavailable.
    """

    try:
        return open_database(app.database.path)
    except (LibraryError, sqlite3.Error, OSError):
        raise ApiError("database_unavailable") from None


def _environment(collaborators):
    return os.environ if collaborators.environ is None else collaborators.environ


def _always_cancelled():
    return True


# ---------------------------------------------------------------------------
# the assembled result
# ---------------------------------------------------------------------------


def _assemble(request, *, palette, content_hash, kick, stored, hybrid, shortlist_samples, selected,
              analysis_version, stage):
    """The operation's records: the projected batch's inputs and the run block."""

    ranked_by_id = {candidate.sample_id: candidate for candidate in stored.result.ranked}
    counts = RecommendationCounts(
        eligible=len(stored.filter_result.eligible_ids),
        excluded=len(stored.filter_result.excluded),
        shortlisted=stored.result.shortlist_size_returned, scored=len(hybrid.ranked),
        unscored=len(hybrid.unscored), requested=request.limit,
        returned=min(request.limit, len(hybrid.ranked)))
    results = tuple(_result(record, ranked_by_id) for record in hybrid.ranked[:counts.returned])
    alternatives = tuple(hybrid.alternatives)
    ranked_ids = {record.candidate_id for record in hybrid.ranked}
    run = RecommendationRun(
        run_id=run_id_for(palette, request, palette_hash_value=content_hash, kick=kick,
                          retrieval=stored.result, analysis_version=analysis_version),
        palette_hash=content_hash, analysis_version=analysis_version,
        policy_version=stored.filter_result.policy_version, filters=request.filters,
        retrieval=stored.result, duplicate_candidates=stored.duplicate_candidates,
        skipped_stale_analysis=stored.skipped_stale_analysis,
        skipped_absent_analysis=stored.skipped_absent_analysis,
        ranking_version=hybrid.ranking_version, weight_table_id=hybrid.weight_table_id,
        jev_status=hybrid.jev_status, prompt_version=jev_questions.PROMPT_VERSION,
        adapter_version=jev_adapter.ADAPTER_VERSION, model_versions=_model_versions(hybrid.ranked),
        counts=counts, exclusions=_exclusions(stored.filter_result.excluded),
        evidence=_tally(stage.sources(), stage.cache_errors, ranked_ids))
    return RecommendationResult(run=run, palette=palette,
                                samples=_supplied(palette, kick, shortlist_samples, selected,
                                                  results, alternatives),
                                results=results, alternatives=alternatives,
                                ranking_version=hybrid.ranking_version, mode=hybrid.mode)


def _supplied(palette, kick, shortlist_samples, selected, results, alternatives):
    """The selected kick plus every sample referenced by a result or an alternative.

    The palette's own selected bass is supplied whenever it is set, and the kick
    is never a candidate, so a currently selected bass can appear among the
    results: a documented limit of this pair-only operation.
    """

    wanted = {record.candidate_id for record in results} | set(alternatives)
    if palette.selected_bass_id is not None:
        wanted.add(palette.selected_bass_id)
    supplied = [kick]
    supplied.extend(sample for sample in shortlist_samples if sample.sample_id in wanted)
    if selected is not None and selected.sample_id not in {sample.sample_id for sample in supplied}:
        supplied.append(selected)
    return tuple(supplied)


def _result(record, ranked_by_id):
    """One contract RankedCandidate from #15's record and #25's ordering."""

    retrieved = ranked_by_id.get(record.candidate_id)
    return RankedCandidate(
        candidate_id=record.candidate_id, analysis_version=record.analysis_version, rank=record.rank,
        compatibility=record.compatibility, confidence=record.confidence,
        similarity=None if retrieved is None else retrieved.similarity,
        similarity_unavailable_reason=None if retrieved is None
        else retrieved.similarity_unavailable_reason,
        dsp_dimensions=record.dsp_dimensions, jev_judgments=record.jev_judgments,
        reasons=record.reasons, warnings=record.warnings)


def _model_versions(ranked):
    """Every model version the returned judgments carry, sorted and unique."""

    versions = {judgment.model_version for record in ranked for judgment in record.jev_judgments}
    return tuple(sorted(versions))


def _exclusions(excluded):
    """#11's per-code tally over the excluded candidates, one record per code, sorted."""

    tally = {}
    for entry in excluded:
        for reason in entry.reasons:
            tally[reason.code] = tally.get(reason.code, 0) + 1
    return tuple((code, tally[code]) for code in sorted(tally))


def _tally(sources, cache_errors, ranked_ids):
    """The evidence counters over the scored candidates' weighted dimensions."""

    counts = {"interface": 0, "double": 0, "unavailable": 0, "cache": 0}
    for candidate_id, source in sources:
        if candidate_id not in ranked_ids:
            continue
        counts[source if source in counts else "unavailable"] += 1
    return RecommendationEvidence(interface=counts["interface"], double=counts["double"],
                                  unavailable=counts["unavailable"], cache=counts["cache"],
                                  cache_errors=cache_errors)


def _project(value):
    """One contract record as JSON: every field, and no local_path."""

    if isinstance(value, AudioMetadata):
        return {name: getattr(value, name) for name in AUDIO_FIELDS}
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _project(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_project(item) for item in value]
    return value
