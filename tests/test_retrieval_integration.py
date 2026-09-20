"""Storage-facing retrieval tests over temporary synthetic databases (issue #25).

Every database is a temporary file created with #21's `open_database` and
migrate chain, every stored sample is the synthetic contract fixture or a
`dataclasses.replace` copy of it, and every content hash is the SHA-256 of a
synthetic label. No real library path, sample name, content fingerprint or audio
byte appears in this file or in any file it writes, and no test opens or decodes
an audio file.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from backend.analysis import batch
from backend.contracts import RecommendationBatch, Sample
from backend.evaluation import comparison, rating
from backend.library import retrieval as stored_retrieval
from backend.library.errors import DuplicateContent
from backend.library.repository import LibraryRepository
from backend.library.schema import open_database
from backend.palette import compatibility
from backend.palette.compatibility import Availability, FilterInputError
from backend.palette.retrieval import (REPRESENTATION_VERSION, RETRIEVAL_DIMENSIONS,
                                       RETRIEVAL_POLICY_VERSION, RetrievalPolicy,
                                       fit_normalization, normalization_id)
from backend.palette import retrieval as pure_retrieval

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"
DOCUMENT = Path(__file__).resolve().parent.parent / "_docs" / "feature-retrieval.md"
SYNTHETIC_DIGEST = "sha256:" + "0" * 64


def content_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def fixture_samples():
    return RecommendationBatch.from_json(
        (FIXTURES / "hybrid.json").read_text(encoding="utf-8")).samples


def silent_sample():
    return Sample.from_json((FIXTURES / "silent-sample.json").read_text(encoding="utf-8"))


def with_values(sample: Sample, values: dict) -> Sample:
    """A copy of one fixture sample with the named measurements replaced."""

    measurements = []
    for item in sample.features.measurements:
        if item.name not in values:
            measurements.append(item)
            continue
        value = values[item.name]
        if value is None:
            measurements.append(replace(item, value=None, confidence=None,
                                        unavailable_reason="not_measured"))
            continue
        confidence = item.confidence
        if item.name in ("fundamental", "tempo") and confidence is None:
            confidence = 0.9
        measurements.append(replace(item, value=value, confidence=confidence,
                                   unavailable_reason=None))
    return replace(sample, features=replace(sample.features, measurements=tuple(measurements)))


def variant(base: Sample, sample_id: str, *, role=None, values=None) -> Sample:
    return replace(with_values(base, values or {}), sample_id=sample_id,
                   role=role or base.role,
                   audio=replace(base.audio,
                                 local_path="C:/tera-fixtures/" + sample_id + ".wav"))


def store(repository: LibraryRepository, sample: Sample, *, file_status="present",
          label=None) -> None:
    repository.import_sample(sample, content_sha256=content_hash(label or sample.sample_id),
                             file_status=file_status)


@pytest.fixture
def library(tmp_path):
    connection = open_database(tmp_path / "library.sqlite3")
    repository = LibraryRepository(connection)
    try:
        yield repository
    finally:
        connection.close()


def seed_wide(repository: LibraryRepository, count=60):
    """One kick and `count` basses whose only varying dimension is loudness.

    Every other dimension is constant across the row set, so exactly one
    dimension is active and the order is by the kick's loudness distance.
    Returns the samples keyed by sample id and the analysis version.
    """

    version = repository.register_analysis_version(batch.analysis_descriptor())
    kick, bass = fixture_samples()
    kick = replace(kick, analysis_version=version)
    bass = replace(bass, analysis_version=version)
    store(repository, kick)
    samples = {"kick-001": kick}
    for index in range(1, count + 1):
        sample_id = f"bass-{index:03d}"
        samples[sample_id] = variant(bass, sample_id,
                                     values={"loudness": -20.0 - index})
        store(repository, samples[sample_id])
    return samples, version


def statement_count(connection, action) -> int:
    seen = []

    def record(statement):
        seen.append(statement)

    connection.set_trace_callback(record)
    try:
        action()
    finally:
        connection.set_trace_callback(None)
    return len(seen)


def test_filter_admission_happens_first_and_only_eligible_ids_are_ranked(library):
    samples, version = seed_wide(library, count=4)
    silent = variant(silent_sample(), "bass-silent")
    silent = replace(silent, analysis_version=version)
    store(library, silent, label="bass-silent")
    library.mark_file_status("bass-002", "missing")
    result = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    eligible = set(result.filter_result.eligible_ids)
    excluded = {entry.sample_id: tuple(reason.code for reason in entry.reasons)
                for entry in result.filter_result.excluded}
    assert result.filter_result.kick_id == "kick-001"
    assert result.filter_result.policy_version == compatibility.POLICY_VERSION
    assert {sample_id for sample_id, _version in result.filter_result.analysis_versions} == {
        "bass-001", "bass-002", "bass-003", "bass-004", "bass-silent"}
    assert excluded["bass-silent"] == ("silent_audio",)
    assert excluded["bass-002"] == ("file_missing",)
    assert eligible == {"bass-001", "bass-003", "bass-004"}
    assert {item.sample_id for item in result.result.ranked} == eligible
    assert set(result.result.shortlist) == eligible
    assert result.result.limit_reason == "eligible_exhausted"
    # The population is every current candidate-role row, excluded or not.
    assert result.population_count == 5
    assert result.duplicate_candidates == 0
    assert result.skipped_stale_analysis == 0 and result.skipped_absent_analysis == 0
    for excluded_id in excluded:
        assert excluded_id not in {item.sample_id for item in result.result.ranked}
        assert excluded_id not in result.result.shortlist


def test_availability_comes_from_storage_and_can_be_injected(library):
    samples, version = seed_wide(library, count=3)
    library.mark_file_status("bass-002", "unknown")
    result = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    codes = {entry.sample_id: tuple(reason.code for reason in entry.reasons)
             for entry in result.filter_result.excluded}
    assert codes["bass-002"] == ("availability_unknown",)
    every_id = set(result.filter_result.analysis_versions)
    injected = {sample_id: Availability.AVAILABLE for sample_id, _version in every_id}
    injected["kick-001"] = Availability.AVAILABLE
    allowed = stored_retrieval.retrieve_shortlist(
        library.connection, "kick-001", policy=RetrievalPolicy(50), availability=injected)
    assert set(allowed.filter_result.eligible_ids) == {"bass-001", "bass-002", "bass-003"}
    incomplete = dict(injected)
    incomplete.pop("bass-003")
    with pytest.raises(FilterInputError) as refusal:
        stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                            policy=RetrievalPolicy(50),
                                            availability=incomplete)
    assert refusal.value.code == "invalid_availability"
    with pytest.raises(FilterInputError) as unknown_id:
        stored_retrieval.retrieve_shortlist(
            library.connection, "kick-001", policy=RetrievalPolicy(50),
            availability={**injected, "bass-999": Availability.AVAILABLE})
    assert unknown_id.value.code == "invalid_availability"


def test_the_population_is_the_whole_library_and_never_the_querys_own_set(library):
    samples, version = seed_wide(library, count=3)
    first = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                policy=RetrievalPolicy(50))
    extra = variant(samples["bass-001"], "bass-004", values={"loudness": -40.0})
    store(library, extra)
    second = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    assert second.population_count == 4
    assert second.result.normalization_id != first.result.normalization_id
    library.mark_file_status("bass-003", "missing")
    third = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                policy=RetrievalPolicy(50))
    assert third.result.normalization_id == second.result.normalization_id
    another_kick = variant(samples["kick-001"], "kick-002", role="kick")
    store(library, another_kick)
    fourth = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    assert fourth.result.normalization_id == second.result.normalization_id
    assert fourth.population_count == 4
    library.insert_path_record("C:/tera-fixtures/bass-005.wav", role="bass",
                               content_sha256=content_hash("bass-005-pending"),
                               sample_rate_hz=48000, channels=1, frame_count=24000,
                               duration_ms=500.0, sample_id="bass-005")
    fifth = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                policy=RetrievalPolicy(50))
    assert fifth.result.normalization_id == second.result.normalization_id
    assert fifth.skipped_absent_analysis == 1


def test_stale_and_absent_rows_are_counted_and_never_scored(library, tmp_path):
    samples, version = seed_wide(library, count=2)
    base = samples["bass-001"]
    stale_version = library.register_analysis_version({"synthetic": "stale-descriptor"})
    assert stale_version != version
    stale = replace(base, sample_id="bass-stale", analysis_version=stale_version,
                    audio=replace(base.audio, local_path="C:/tera-fixtures/bass-stale.wav"))
    library.import_sample(stale, content_sha256=content_hash("bass-stale"),
                          file_status="present")
    library.insert_path_record("C:/tera-fixtures/bass-absent.wav", role="bass",
                               content_sha256=content_hash("bass-absent"),
                               sample_rate_hz=48000, channels=1, frame_count=24000,
                               duration_ms=500.0, sample_id="bass-absent")
    result = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    assert result.skipped_stale_analysis == 1 and result.skipped_absent_analysis == 1
    assert {"bass-stale", "bass-absent"}.isdisjoint(
        {item.sample_id for item in result.result.ranked})
    assert {"bass-stale", "bass-absent"}.isdisjoint(set(result.result.shortlist))
    # The same current rows without the stale or absent one: the population
    # digest covers the current row identities, so this is the comparison the
    # criterion asks for.
    alone_connection = open_database(tmp_path / "clean.sqlite3")
    try:
        seed_wide(LibraryRepository(alone_connection), count=2)
        single = stored_retrieval.retrieve_shortlist(alone_connection, "kick-001",
                                                     policy=RetrievalPolicy(50))
        assert single.skipped_stale_analysis == 0 and single.skipped_absent_analysis == 0
        assert single.result.normalization_id == result.result.normalization_id
        assert single.result.shortlist == result.result.shortlist
    finally:
        alone_connection.close()


def test_duplicate_identity_rows_collapse_to_one_candidate(library, monkeypatch):
    samples, version = seed_wide(library, count=2)
    real = LibraryRepository.list_retrieval_rows

    def duplicated(self, analysis_version, *, sample_ids=None, roles=None):
        rows = real(self, analysis_version, sample_ids=sample_ids, roles=roles)
        first = rows[0]
        second = replace(first, path="C:/tera-fixtures/A/dup.wav",
                         path_key=os.path.normcase("C:/tera-fixtures/A/dup.wav"))
        first = replace(first, path="C:/tera-fixtures/Z/dup.wav",
                        path_key=os.path.normcase("C:/tera-fixtures/Z/dup.wav"))
        return (second, first) + rows[1:]

    monkeypatch.setattr(LibraryRepository, "list_retrieval_rows", duplicated)
    result = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    assert result.duplicate_candidates == 1
    assert result.representative_paths == ((samples["bass-001"].sample_id,
                                            "C:/tera-fixtures/A/dup.wav"),)
    assert len(result.result.ranked) == 2
    assert len(set(result.result.shortlist)) == len(result.result.shortlist)


def test_the_storage_layer_refuses_the_duplicate_rows_this_collapse_would_merge(library):
    """The landed schema stores one row per content identity, so #25's collapse
    criterion cannot be reached through the repository: the same bytes at a
    second path raise duplicate_content instead of writing a second row."""

    repository = library
    kick, bass = fixture_samples()
    version = repository.register_analysis_version(batch.analysis_descriptor())
    sample = replace(bass, analysis_version=version)
    store(repository, sample, label="same-bytes")
    second = variant(sample, "bass-copy")
    with pytest.raises(DuplicateContent):
        store(repository, second, label="same-bytes")
    records = repository.list_path_records()
    assert len(records) == 1
    assert records[0].sample_id == sample.sample_id
    assert records[0].content_sha256 == content_hash("same-bytes")


def test_the_statement_count_does_not_grow_with_the_candidate_count(tmp_path):
    def build(count):
        directory = tmp_path / f"library-{count}"
        directory.mkdir()
        connection = open_database(directory / "library.sqlite3")
        repository = LibraryRepository(connection)
        seed_wide(repository, count=count)
        return connection

    counts = {}
    for size in (60, 120):
        connection = build(size)
        try:
            counts[size] = statement_count(
                connection,
                lambda: stored_retrieval.retrieve_shortlist(
                    connection, "kick-001", policy=RetrievalPolicy(100)))
        finally:
            connection.close()
    assert counts[60] == counts[120], counts


def test_retrieval_never_opens_stats_or_decodes_audio(library, monkeypatch):
    samples, version = seed_wide(library, count=3)

    def forbidden(*args, **kwargs):
        raise AssertionError("retrieval must not read audio")

    from backend import audio
    from backend.analysis import harmony, loudness, spectral, transient
    monkeypatch.setattr(audio, "load_wav", forbidden)
    monkeypatch.setattr(audio, "load_wav_bytes", forbidden)
    for module in (loudness, spectral, transient, harmony):
        for name in dir(module):
            if name.startswith("measure_"):
                monkeypatch.setattr(module, name, forbidden)
    result = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                 policy=RetrievalPolicy(50))
    assert result.result.shortlist
    assert set(result.result.shortlist) <= set(result.filter_result.eligible_ids)


def build_library(directory, count=60):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "library.sqlite3"
    connection = open_database(path)
    seed_wide(LibraryRepository(connection), count=count)
    return connection, path


def pair(pair_id, bass_id, kick_id="kick-001"):
    return {"pair_id": pair_id, "kick_sample_id": kick_id, "bass_sample_id": bass_id}


def pair_list_document(pairs):
    return {"schema_version": rating.SCHEMA_VERSION, "dataset_version": "synthetic-pool-01",
            "split_manifest_digest": SYNTHETIC_DIGEST, "sampler_seed": "tera-eval-pairs-16-v1",
            "assignment_seed": "tera-eval-assignment-16-v1", "pairs": list(pairs)}


SESSION_VALUES = {"protocol_version": "tera-eval-protocol-v1",
                  "dataset_version": "synthetic-pool-01",
                  "split_manifest_digest": SYNTHETIC_DIGEST,
                  "order_seed": "tera-eval-order-16-v1", "playback_gain_db": -6.0,
                  "monitoring_description": "synthetic monitoring session",
                  "started_at": "2026-01-01T00:00:00.000+00:00",
                  "finished_at": "2026-01-01T00:10:00.000+00:00"}


def rating_record(index, pair_record, label, *, skip=False):
    return {"pair_id": pair_record["pair_id"],
            "kick_sample_id": pair_record["kick_sample_id"],
            "bass_sample_id": pair_record["bass_sample_id"], "presentation_index": index,
            "rating": None if skip else label, "skip": skip,
            "skip_reason": "cannot-judge" if skip else None, "recognised": False,
            "playback_completed": True, "responded_at": "2026-01-01T00:05:00.000+00:00",
            "correction_of": None}


def export_document(session_id, evaluator_id, pairs, labels, skips=None):
    skips = skips or [False] * len(pairs)
    ratings = [rating_record(index, record, label, skip=skip)
               for index, (record, label, skip) in enumerate(zip(pairs, labels, skips), start=1)]
    return {"schema_version": rating.SCHEMA_VERSION,
            "session": {"session_id": session_id, "evaluator_id": evaluator_id, **SESSION_VALUES},
            "ratings": ratings}


def write_json(path, document):
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


METRIC_PAIRS = (pair("pair-01", "bass-001"), pair("pair-02", "bass-002"),
                pair("pair-03", "bass-003"), pair("pair-04", "bass-012"),
                pair("pair-05", "bass-055"), pair("pair-06", "bass-999"))
METRIC_LABELS = ("good", "good", "good", "good", "poor", "good")
METRIC_EXPORTS = (("session-01", "evaluator-01"), ("session-02", "evaluator-02"))


def metric_exports(labels=METRIC_LABELS):
    return [export_document(session, evaluator, METRIC_PAIRS, labels)
            for session, evaluator in METRIC_EXPORTS]


def test_reference_metrics_and_miss_classification_are_hand_computed(library):
    samples, version = seed_wide(library, count=60)
    library.mark_file_status("bass-002", "missing")
    reference = stored_retrieval.reference_for(pair_list_document(METRIC_PAIRS),
                                               metric_exports())
    assert reference.dataset_version == "synthetic-pool-01"
    assert reference.split_manifest_digest == SYNTHETIC_DIGEST
    assert reference.sampler_seed == "tera-eval-pairs-16-v1"
    assert reference.assignment_seed == "tera-eval-assignment-16-v1"
    assert reference.positive_labels == ("good", "excellent")
    assert reference.min_ratings_per_pair == comparison.MIN_RATINGS_PER_PAIR
    assert reference.positive_labels == tuple(
        label for label in rating.LABELS if label in ("good", "excellent"))
    assert {comparison.LABEL_VALUES[label] for label in reference.positive_labels} == {2.0, 3.0}
    assert len(reference.queries) == 1
    query = reference.queries[0]
    assert query.query_kick_id == "kick-001"
    assert list(query.declared) == ["bass-001", "bass-002", "bass-003", "bass-012",
                                    "bass-055", "bass-999"]
    # The reference is built without the library, so an unavailable positive is
    # still declared; recall_metrics intersects it with the stored rows.
    assert list(query.positive) == ["bass-001", "bass-002", "bass-003", "bass-012", "bass-999"]
    retrieval = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                    policy=RetrievalPolicy(50))
    metrics = stored_retrieval.recall_metrics(reference, {"kick-001": retrieval})
    row = metrics.queries[0]
    assert (row.declared_count, row.available_count, row.positive_count) == (6, 5, 4)
    assert (row.retrieved_declared, row.retrieved_positive) == (3, 3)
    assert row.recall == pytest.approx(3 / 4)
    assert row.not_evaluable_reason is None
    misses = {miss.sample_id: (miss.classification, miss.codes) for miss in row.misses}
    assert misses == {"bass-002": ("filter_excluded", ("file_missing",)),
                      "bass-055": ("cut", ()),
                      "bass-999": ("missing_from_library", ())}
    assert metrics.totals.queries == 1 and metrics.totals.measurable_queries == 1
    assert metrics.totals.queries_not_evaluable == 0
    assert metrics.totals.declared_total == 6 and metrics.totals.available_total == 5
    assert metrics.totals.positive_total == 4
    assert metrics.totals.recall_at_size == pytest.approx(3 / 4)
    assert metrics.totals.coverage_at_size == pytest.approx(3 / 5)
    curve = {point.size: (point.recall, point.coverage) for point in metrics.curve}
    assert set(curve) == {5, 10, 20, 50}
    assert curve[5][0] == pytest.approx(2 / 4) and curve[5][1] == pytest.approx(2 / 5)
    assert curve[10] == curve[5]
    assert curve[20][0] == pytest.approx(3 / 4) and curve[20][1] == pytest.approx(3 / 5)
    assert curve[50] == curve[20]
    # A skipped presentation and a label outside #17's vocabulary never count.
    skipped = stored_retrieval.reference_for(
        pair_list_document(METRIC_PAIRS),
        [export_document("session-01", "evaluator-01", METRIC_PAIRS, METRIC_LABELS,
                         skips=[False, False, True, False, False, False]),
         export_document("session-02", "evaluator-02", METRIC_PAIRS, METRIC_LABELS)])
    # One skipped rating leaves pair-03 with a single evaluator, below
    # MIN_RATINGS_PER_PAIR, so it is no longer a positive reference.
    assert list(skipped.queries[0].positive) == ["bass-001", "bass-002", "bass-012", "bass-999"]
    alone = stored_retrieval.reference_for(
        pair_list_document(METRIC_PAIRS),
        [export_document("session-01", "evaluator-01", METRIC_PAIRS, METRIC_LABELS)])
    assert alone.queries[0].positive == ()


def test_a_query_with_no_record_and_a_query_with_no_positive_are_not_evaluable(library):
    samples, version = seed_wide(library, count=3)
    pairs = (pair("pair-01", "bass-001"), pair("pair-02", "bass-999", kick_id="kick-999"))
    exports = [export_document(session, evaluator, METRIC_PAIRS, ("poor",) * len(METRIC_PAIRS))
               for session, evaluator in METRIC_EXPORTS]
    reference = stored_retrieval.reference_for(pair_list_document(pairs), exports)
    assert reference.queries[0].positive == ()
    assert [query.query_kick_id for query in reference.queries] == ["kick-001", "kick-999"]
    retrieval = stored_retrieval.retrieve_shortlist(library.connection, "kick-001",
                                                    policy=RetrievalPolicy(50))
    metrics = stored_retrieval.recall_metrics(reference, {"kick-001": retrieval})
    reasons = {row.query_kick_id: row.not_evaluable_reason for row in metrics.queries}
    assert reasons == {"kick-001": "no_positive_references", "kick-999": "query_kick_missing"}
    assert metrics.totals.queries_not_evaluable == 2 and metrics.totals.measurable_queries == 0
    assert all(row.recall is None for row in metrics.queries)
    with pytest.raises(stored_retrieval.RetrievalReportError) as refusal:
        stored_retrieval.recall_metrics(reference, "not-a-retrieval")
    assert refusal.value.code == "invalid_retrieval"


def test_the_reference_is_arm_independent_and_non_circular(library, monkeypatch):
    samples, version = seed_wide(library, count=3)
    pairs = (pair("pair-01", "bass-001"), pair("pair-02", "bass-002"))

    def forbidden(*args, **kwargs):
        raise AssertionError("the reference must not run retrieval")

    monkeypatch.setattr(stored_retrieval, "select_shortlist", forbidden)
    monkeypatch.setattr(stored_retrieval, "retrieve_shortlist", forbidden)
    reference = stored_retrieval.reference_for(pair_list_document(pairs), metric_exports())
    assert [query.query_kick_id for query in reference.queries] == ["kick-001"]
    assert list(reference.queries[0].declared) == ["bass-001", "bass-002"]
    assert list(reference.queries[0].positive) == ["bass-001", "bass-002"]
    assert reference.pair_list_digest.startswith("sha256:")
    assert reference.export_digests and len(reference.export_digests) == 2


_AUDIT_HOOK = []
_OPENED_FILES = []


def test_the_report_path_reads_only_the_named_pair_list_and_exports(library, tmp_path):
    samples, version = seed_wide(library, count=3)
    pairs = (pair("pair-01", "bass-001"),)
    path = write_json(tmp_path / "pairs.json", pair_list_document(pairs))
    exports = [export_document(session, evaluator, pairs, ("good",))
               for session, evaluator in METRIC_EXPORTS]
    export_paths = [write_json(tmp_path / f"export-{number}.json", document)
                    for number, document in enumerate(exports, start=1)]
    if not _AUDIT_HOOK:
        def record(event, args):
            if event == "open":
                _OPENED_FILES.append(str(args[0]))

        sys.addaudithook(record)
        _AUDIT_HOOK.append(True)
    del _OPENED_FILES[:]
    document = stored_retrieval.recall_report(library.connection, pair_list_path=path,
                                              export_paths=export_paths, size=50)
    assert document["state"] == "measured"
    allowed = {str(path)} | {str(item) for item in export_paths}
    opened = set(_OPENED_FILES)
    assert allowed <= opened, sorted(allowed - opened)
    assert opened <= allowed | {name for name in opened if name.endswith("library.sqlite3")}


def test_the_report_fields_are_exactly_the_declared_ones(library, tmp_path):
    samples, version = seed_wide(library, count=4)
    pairs = (pair("pair-01", "bass-001"),)
    path = write_json(tmp_path / "pairs.json", pair_list_document(pairs))
    exports = [export_document(session, evaluator, pairs, ("good",))
               for session, evaluator in METRIC_EXPORTS]
    export_paths = [write_json(tmp_path / f"export-{number}.json", document)
                    for number, document in enumerate(exports, start=1)]
    document = stored_retrieval.recall_report(library.connection, pair_list_path=path,
                                              export_paths=export_paths, size=50)
    assert set(document) == {"retrieval_schema", "retrieval_version", "representation_version",
                             "normalization_id", "analysis_version", "shortlist_size", "policy",
                             "population_count", "duplicate_candidates",
                             "skipped_stale_analysis", "skipped_absent_analysis", "reference",
                             "queries", "curve", "totals", "state", "blocked_reason"}
    assert document["retrieval_schema"] == stored_retrieval.RETRIEVAL_SCHEMA == "1.0"
    assert document["retrieval_version"] == RETRIEVAL_POLICY_VERSION
    assert document["representation_version"] == REPRESENTATION_VERSION
    assert document["shortlist_size"] == 50 and document["policy"] == {"shortlist_size": 50}
    assert len(document["normalization_id"]) == 64 and len(document["analysis_version"]) == 64
    assert set(document["reference"]) == {"pair_list_digest", "dataset_version",
                                          "split_manifest_digest", "sampler_seed",
                                          "assignment_seed", "export_digests", "positive_labels",
                                          "min_ratings_per_pair"}
    assert document["reference"]["pair_list_digest"] == "sha256:" + hashlib.sha256(
        path.read_bytes()).hexdigest()
    assert document["reference"]["export_digests"] == sorted(
        "sha256:" + hashlib.sha256(item.read_bytes()).hexdigest() for item in export_paths)
    assert set(document["queries"][0]) == {"query_kick_id", "declared_count", "available_count",
                                            "positive_count", "retrieved_declared",
                                            "retrieved_positive", "recall",
                                            "not_evaluable_reason", "misses"}
    assert set(document["totals"]) == {"queries", "measurable_queries", "queries_not_evaluable",
                                        "declared_total", "available_total", "positive_total",
                                        "retrieved_declared_total", "retrieved_positive_total",
                                        "recall_at_size", "coverage_at_size"}
    assert [point["size"] for point in document["curve"]] == [5, 10, 20, 50]
    assert document["state"] == "measured" and document["blocked_reason"] is None
    text = batch.canonical(document)
    for forbidden in ("C:", ".wav", "\\\\", "2026-01-01", "tera-fixtures"):
        assert forbidden not in text, forbidden


def test_the_recall_command_writes_a_private_report_and_returns_the_exit_codes(tmp_path,
                                                                                   capsys):
    connection, database = build_library(tmp_path / "library", count=4)
    connection.close()
    directory = tmp_path / "private"
    directory.mkdir()
    pairs = (pair("pair-01", "bass-001"), pair("pair-02", "bass-002"))
    pair_path = write_json(directory / "pairs.json", pair_list_document(pairs))
    exports = [export_document(session, evaluator, pairs, ("good", "good"))
               for session, evaluator in METRIC_EXPORTS]
    export_paths = [write_json(directory / f"export-{number}.json", document)
                    for number, document in enumerate(exports, start=1)]
    output = directory / "recall-report.json"
    command = ["recall", "--database", str(database), "--pair-list", str(pair_path),
               "--export", str(export_paths[0]), "--export", str(export_paths[1]),
               "--size", "50", "--output", str(output)]
    assert stored_retrieval.main(command) == 0
    first = output.read_bytes()
    document = json.loads(first)
    assert document["state"] == "measured"
    assert document["queries"][0]["positive_count"] == 2
    assert document["queries"][0]["retrieved_positive"] == 2
    assert document["queries"][0]["recall"] == 1.0
    assert stored_retrieval.main(command) == 0
    assert output.read_bytes() == first
    unavailable = write_json(
        directory / "unavailable.json",
        pair_list_document((pair("pair-03", "bass-999", kick_id="kick-999"),)))
    assert stored_retrieval.main(["recall", "--database", str(database),
                                  "--pair-list", str(unavailable), "--size", "50",
                                  "--output", str(output)]) == 1
    assert json.loads(output.read_bytes())["queries"][0]["not_evaluable_reason"] \
        == "query_kick_missing"
    text_file = directory / "not-a-database.sqlite3"
    text_file.write_text("not a database")
    malformed = write_json(directory / "malformed.json", {"schema_version": "1.0"})
    refused = (
        ["recall", "--database", str(database), "--size", "49", "--output", str(output)],
        ["recall", "--database", str(database), "--size", "1001", "--output", str(output)],
        ["recall", "--database", str(database), "--size", "many", "--output", str(output)],
        ["recall", "--database", str(database), "--pair-list", str(malformed), "--size", "50",
         "--output", str(output)],
        ["recall", "--database", str(text_file), "--size", "50", "--output", str(output)],
        ["recall", "--database", str(database), "--size", "50",
         "--output", str(directory / "report.txt")],
        ["recall", "--database", str(database), "--pair-list", str(pair_path),
         "--export", str(pair_path), "--size", "50", "--output", str(pair_path)],
    )
    for command_line in refused:
        assert stored_retrieval.main(command_line) == 2, command_line
    assert json.loads(pair_path.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    # argparse-level refusals are one code plus one path-free line, exit 2,
    # and `main` returns the code instead of raising over a usage table.
    capsys.readouterr()  # the refusals above already asserted; read each below on its own
    for invalid_command in (["recall", "--database", str(database), "--output", str(output),
                             "--not-a-flag"],
                            ["recall", "--output", str(output)],
                            []):
        assert stored_retrieval.main(invalid_command) == 2, invalid_command
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.rstrip("\n").splitlines()
        assert len(lines) == 1, lines
        assert lines[0].startswith("invalid_arguments: ")
        assert len(lines[0]) > len("invalid_arguments: ")
        assert "usage" not in lines[0].lower()
        assert str(database) not in lines[0]
    original = stored_retrieval._write_report

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    stored_retrieval._write_report = interrupted
    try:
        assert stored_retrieval.main(command) == 130
    finally:
        stored_retrieval._write_report = original


def test_the_insufficient_evidence_paths_are_documented_blockers(library, tmp_path):
    samples, version = seed_wide(library, count=3)
    pairs = (pair("pair-01", "bass-001"),)
    exports = [export_document(session, evaluator, pairs, ("good",))
               for session, evaluator in METRIC_EXPORTS]
    absent = stored_retrieval.recall_report(library.connection, pair_list_path=None,
                                            export_paths=(), size=50)
    assert absent["state"] == "insufficient_evidence"
    assert absent["blocked_reason"] == "pair_list_absent"
    assert absent["queries"] == [] and absent["curve"] == []
    assert absent["totals"]["recall_at_size"] is None
    assert absent["population_count"] == 3 and absent["reference"]["pair_list_digest"] is None
    missing = stored_retrieval.recall_report(library.connection,
                                             pair_list_path=tmp_path / "absent.json",
                                             export_paths=(), size=50)
    assert missing["blocked_reason"] == "pair_list_absent"
    path = write_json(tmp_path / "pairs.json", pair_list_document(pairs))
    no_sessions = stored_retrieval.recall_report(library.connection, pair_list_path=path,
                                                 export_paths=(), size=50)
    assert no_sessions["blocked_reason"] == "no_sessions"
    assert no_sessions["queries"][0]["not_evaluable_reason"] == "no_positive_references"
    assert no_sessions["queries"][0]["available_count"] == 1
    assert no_sessions["totals"]["recall_at_size"] is None
    negative = [export_document(session, evaluator, pairs, ("poor",))
                for session, evaluator in METRIC_EXPORTS]
    no_positive = stored_retrieval.recall_report(library.connection, pair_list_path=path,
                                                 export_paths=negative, size=50)
    assert no_positive["blocked_reason"] == "no_positive_references"
    assert no_positive["totals"]["positive_total"] == 0
    assert no_positive["totals"]["recall_at_size"] is None
    measured = stored_retrieval.recall_report(library.connection, pair_list_path=path,
                                              export_paths=exports, size=50)
    assert measured["state"] == "measured" and measured["blocked_reason"] is None
    assert measured["totals"]["recall_at_size"] == 1.0


def test_the_empty_one_and_small_libraries_are_valid_results_and_exit_zero(tmp_path):
    for count, expected in ((0, "no_candidates"), (1, "eligible_exhausted"),
                            (3, "eligible_exhausted")):
        directory = tmp_path / f"small-{count}"
        connection, database = build_library(directory, count=count)
        try:
            result = stored_retrieval.retrieve_shortlist(connection, "kick-001",
                                                         policy=RetrievalPolicy(100))
            assert result.population_count == count
            assert result.result.limit_reason == expected
            assert result.result.shortlist_size_returned == count
            if count == 0:
                assert result.result.ranked == () and result.result.shortlist == ()
                assert len(result.result.normalization_id) == 64
                document = stored_retrieval.recall_report(connection, pair_list_path=None,
                                                          export_paths=(), size=100)
            else:
                pairs = (pair("pair-01", "bass-001"),)
                path = write_json(directory / "pairs.json", pair_list_document(pairs))
                exports = [export_document(session, evaluator, pairs, ("good",))
                           for session, evaluator in METRIC_EXPORTS]
                for number, item in enumerate(exports, start=1):
                    write_json(directory / f"export-{number:02d}.json", item)
                document = stored_retrieval.recall_report(connection, pair_list_path=path,
                                                          export_paths=exports, size=100)
                assert document["state"] == "measured"
                # The pair list declares one reference, so at most one is
                # available however many candidates the library holds.
                assert document["queries"][0]["available_count"] == 1
                assert document["population_count"] == count
                assert document["queries"][0]["retrieved_positive"] == 1
                assert document["queries"][0]["recall"] == 1.0
        finally:
            connection.close()
        assert document["population_count"] == count
        output = directory / "recall-report.json"
        command = ["recall", "--database", str(database), "--size", "100",
                   "--output", str(output)]
        if count:
            command[3:3] = ["--pair-list", str(directory / "pairs.json"),
                            "--export", str(directory / "export-01.json"),
                            "--export", str(directory / "export-02.json")]
        assert stored_retrieval.main(command) == 0
        written = json.loads(output.read_bytes())
        assert written["population_count"] == count
        assert written["state"] == document["state"]


def test_the_same_rows_in_another_insertion_order_reproduce_the_normalization(tmp_path):
    kick, bass = fixture_samples()
    records, ids, shortlists = [], [], []
    for number, order in enumerate((list(range(1, 61)), list(reversed(range(1, 61))))):
        directory = tmp_path / f"order-{number}"
        directory.mkdir()
        connection = open_database(directory / "library.sqlite3")
        repository = LibraryRepository(connection)
        version = repository.register_analysis_version(batch.analysis_descriptor())
        stored_kick = replace(with_values(kick, {"loudness": -20.0}), analysis_version=version)
        repository.import_sample(stored_kick, content_sha256=content_hash("kick-001"),
                                 file_status="present")
        for index in order:
            sample_id = f"bass-{index:03d}"
            sample = replace(with_values(bass, {"loudness": -20.0 - index}),
                             sample_id=sample_id, analysis_version=version,
                             audio=replace(bass.audio,
                                           local_path=f"C:/tera-fixtures/{sample_id}.wav"))
            repository.import_sample(sample, content_sha256=content_hash(sample_id),
                                     file_status="present")
        rows = repository.list_retrieval_rows(version, roles=("bass", "sub-bass"))
        record = fit_normalization(tuple(row.sample for row in rows),
                                   analysis_version=version)
        result = stored_retrieval.retrieve_shortlist(connection, "kick-001",
                                                     policy=RetrievalPolicy(50))
        records.append(batch.canonical(record.to_dict()))
        ids.append(normalization_id(record))
        shortlists.append(result.result.shortlist)
        assert result.result.normalization_id == ids[-1]
        connection.close()
    assert records[0] == records[1]
    assert ids[0] == ids[1]
    assert shortlists[0] == shortlists[1]
    assert len(shortlists[0]) == 50


def test_neither_new_module_imports_intelligence_or_ranking(library):
    for module in (pure_retrieval, stored_retrieval):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        for forbidden in ("backend.intelligence", "backend.palette.ranking"):
            assert forbidden not in names, module.__name__
        assert not any(name.startswith("backend.intelligence.") for name in names)
    source = Path(stored_retrieval.__file__).read_text(encoding="utf-8")
    for sql in ("SELECT ", "INSERT ", "UPDATE ", "CREATE TABLE", "CREATE INDEX"):
        assert sql not in source, sql
    assert "list_retrieval_rows" in source


def test_the_document_names_every_read_constant_reason_code_metric_and_exit_code():
    text = DOCUMENT.read_text(encoding="utf-8")
    for literal in ("RETRIEVAL_SCHEMA", "RECALL_AT_SIZES", "RECALL_POSITIVE_LABELS",
                    "REPORT_STATES", "REPORT_BLOCKED_REASONS", "NOT_EVALUABLE_REASONS",
                    "MISS_CLASSIFICATIONS", "REPORT_ERROR_CODES", "list_retrieval_rows",
                    "RetrievalRow", "retrieve_shortlist", "reference_for", "recall_metrics",
                    "recall_report", "StoredRetrieval", "RecallReference", "RecallQuery",
                    "RecallQueryRow", "RecallCurvePoint", "RecallTotals", "RecallMetrics",
                    "RecallMiss", "filter_candidates", "FilterResult", "Availability",
                    "write_private", "load_pair_list", "ratings_by_pair", "pair_aggregates",
                    "MIN_RATINGS_PER_PAIR", "LABEL_VALUES", "RECALL_AT_SIZES",
                    "normalization_id", "population_digest"):
        assert literal in text, literal
    for value in (REPRESENTATION_VERSION, RETRIEVAL_POLICY_VERSION, "1.0", "measured",
                  "insufficient_evidence", "pair_list_absent", "no_sessions",
                  "no_positive_references", "query_kick_missing", "no_available_candidates",
                  "cut", "filter_excluded", "missing_from_library", "coverage_at_size",
                  "recall_at_size", "recall_at_size", "exit 130", "exit 2", "exit 1", "exit 0",
                  "0.50", "1e9", "1e-9", "0.80", "sha256:", "atomic replace"):
        assert value in text, value
    for dimension in RETRIEVAL_DIMENSIONS:
        assert f"`{dimension}`" in text, dimension

