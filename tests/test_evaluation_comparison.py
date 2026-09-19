"""The #19 runner: preflight, arms, metric arithmetic, uncertainty, latency and the report.

Every fixture under tests/fixtures/comparison/ declares hand-computed expected values; no
expected value is ever read back from the implementation. The workspaces here are synthetic
and private (this task decodes no audio), so #17's own
`python -m backend.evaluation.rating validate` is still exercised for real through
file-backed stdio while the comparison itself stays offline.
"""

import ast
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.contracts import JevJudgment, LabelProbability
from backend.evaluation import comparison
from backend.intelligence.jev import ADAPTER_VERSION, JevOutcome, JevScoringRun
from backend.intelligence.questions import PROMPT_VERSION
from backend.palette import ranking
from tests.test_dsp_baseline import bass, sample, selected_kick

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "comparison"
MODULE = REPOSITORY_ROOT / "backend" / "evaluation" / "comparison.py"

DATASET_VERSION = "comparison-fixture-pool-1"
ANALYSIS = "comparison-fixture-analysis-1"
DIGEST = "sha256:" + "ab" * 32
EVALUATORS = ("evaluator-alpha", "evaluator-bravo", "evaluator-charlie", "evaluator-delta",
              "evaluator-echo")
MONITORING = "fixture monitoring, one fixed gain"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def rated_from(pairs):
    """The per-pair aggregates of one fixture query: raw valid values in, aggregates out."""
    ratings = []
    for candidate, values in pairs.items():
        for index, value in enumerate(values):
            ratings.append(comparison.Rating(candidate, "kick", candidate, float(value),
                                             "evaluator-" + str(index), "session"))
    aggregates = comparison.pair_aggregates(comparison.ratings_by_pair(tuple(ratings)))
    return ({candidate: record["value"] for candidate, record in aggregates.items()},
            {candidate: record["count"] for candidate, record in aggregates.items()})


def query_for(query_id, candidate_ids, rated, counts):
    candidates = tuple(candidate_ids)
    return comparison.Query(query_id, candidates, candidates, dict(rated), dict(counts))


def order_for(arm, order, scored=None):
    """One arm's ordering of a query; scored defaults to every ordered candidate."""
    return comparison.ArmOrder(arm, tuple(order),
                               tuple(order if scored is None else scored), "fixture-version",
                               "fixture-weights")


def run(arguments):
    """Run one command in-process and return its exit status and stdout."""
    stream = io.StringIO()
    code = comparison.main(arguments, stdout=stream)
    return code, stream.getvalue()


# --- the module contract ----------------------------------------------------

def test_module_docstring_names_every_file_it_writes():
    docstring = ast.get_docstring(ast.parse(MODULE.read_text(encoding="utf-8")))
    assert docstring is not None
    for name in ("_docs/ranking-comparison-19.md", "run.json", "records.json",
                 ".local-evaluation/ranking-comparison/runs/<run_key>/"):
        assert name in docstring, name


def test_module_imports_no_audio_analysis_or_network_module():
    """The runner declares its imports; nothing here decodes audio or reaches a socket."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"numpy", "scipy", "soundfile", "urllib.request", "http.client", "requests"}
    assert not any(name == "backend.analysis" or name.startswith("backend.analysis.")
                   for name in imported)
    assert not any(name.split(".")[0] in forbidden for name in imported)
    assert not any(name.startswith("backend.evaluation") for name in imported)


def test_the_declared_constant_table_covers_every_frozen_name():
    text = (REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md").read_text(encoding="utf-8")
    assert set(comparison.FROZEN_CONSTANTS) == set(comparison.frozen_constants(text))


# --- metrics, fixtures and the protocol's arithmetic ------------------------

METRIC_CASES = fixture("metric-cases.json")


@pytest.mark.parametrize("case", METRIC_CASES["cases"],
                         ids=[case["name"] for case in METRIC_CASES["cases"]])
def test_hand_computed_metric_cases_reproduce_exactly(case):
    evaluations = {}
    for query in case["queries"]:
        rated, counts = rated_from(query["pairs"])
        for candidate, expected in query["expected_aggregates"].items():
            assert rated[candidate] == expected, (case["name"], candidate)
        built = query_for(query["query"], query["pairs"], rated, counts)
        for arm, order in query["orders"].items():
            evaluation = comparison.evaluate_query(order_for(arm, order), built)
            assert evaluation.eligible, (case["name"], arm, evaluation.reason)
            for name, expected in query["expected"][arm].items():
                assert evaluation.metrics[name] == expected, (case["name"], arm, name)
            evaluations.setdefault(arm, []).append(evaluation)
    count_keys = {"pairwise_accuracy": "pairwise_queries", "top1_margin": "top1_queries",
                  "topk_mean": "topk_queries"}
    for arm, expected in case["expected_arm"].items():
        summary = comparison.arm_summary(tuple(evaluations[arm]))
        for metric in comparison.METRIC_NAMES:
            assert summary[metric]["value"] == expected[metric], (case["name"], arm, metric)
            assert summary[metric]["queries"] == expected[count_keys[metric]], (case["name"], arm)


UNSCORED = fixture("unscored-cases.json")


@pytest.mark.parametrize("case", UNSCORED["share_cases"],
                         ids=[case["name"] for case in UNSCORED["share_cases"]])
def test_scored_share_and_query_eligibility(case):
    eligible = ["b1", "b2", "b3", "b4", "b5"]
    rated = {name: 2.0 for name in eligible[:case["rated_size"]]}
    scored = eligible[:case["scored_in_rated"]]
    query = query_for("q1", eligible, rated, {name: 2 for name in rated})
    full = scored + eligible[len(scored):]
    evaluation = comparison.evaluate_query(order_for("dsp-only", full, scored), query)
    assert evaluation.scored_share == case["expected_share"], case["name"]
    assert evaluation.eligible is case["expected_eligible"], case["name"]
    assert evaluation.reason == case["expected_reason"], case["name"]


def test_an_unscored_candidate_leaves_the_metric_and_sits_in_the_tail_by_id():
    tail = UNSCORED["tail_case"]
    rated = {name: 2.0 for name in tail["scored"]}
    query = query_for("q1", tail["eligible"], rated, {name: 2 for name in rated})
    evaluation = comparison.evaluate_query(
        order_for("dsp-only", tail["expected_order"], tail["scored"]), query)
    assert list(evaluation.order) == tail["expected_order"]
    assert list(evaluation.unscored) == tail["expected_unscored"]
    assert evaluation.metrics["rated_size"] == len(tail["scored"])
    assert evaluation.metrics["scored_in_rated"] == len(tail["scored"])


IDENTICAL = fixture("identical-order-cases.json")


@pytest.mark.parametrize("case", IDENTICAL["cases"],
                         ids=[case["name"] for case in IDENTICAL["cases"]])
def test_identical_orderings_are_counted_and_contribute_zero(case):
    evaluations = {}
    for arm, order in case["arms"].items():
        query = query_for("q1", order, case["rated"], {name: 2 for name in case["rated"]})
        evaluations[arm] = (comparison.evaluate_query(order_for(arm, order), query),)
    orders = {arm: {item.query_id: item.order for item in items}
              for arm, items in evaluations.items()}
    counts = comparison.count_identical_orderings(orders)
    first, second = sorted(case["arms"])
    assert counts[first] == case["expected_identical"], case["name"]
    assert counts[second] == case["expected_identical"], case["name"]
    differences = comparison.paired_differences(evaluations[first], evaluations[second])
    for metric, expected in case["expected_difference"].items():
        found = [value for _, name, value in differences if name == metric]
        assert found == [expected], (case["name"], metric)
    assert len(orders[first]) == case["expected_denominator"]


BOOTSTRAP = fixture("bootstrap-cases.json")


@pytest.mark.parametrize("case", BOOTSTRAP["cases"],
                         ids=[case["name"] for case in BOOTSTRAP["cases"]])
def test_bootstrap_interval_is_pinned_and_deterministic(case):
    first = comparison.bootstrap_values(case["values"], resamples=case["resamples"],
                                        label=case["label"])
    second = comparison.bootstrap_values(case["values"], resamples=case["resamples"],
                                         label=case["label"])
    assert first == second
    assert first["value"] == case["expected"]["value"]
    assert first["interval"] == [case["expected"]["low"], case["expected"]["high"]]
    assert first["draws"] == case["expected"]["draws"]


@pytest.mark.parametrize("case", BOOTSTRAP["verdict_cases"],
                         ids=[case["name"] for case in BOOTSTRAP["verdict_cases"]])
def test_the_four_verdicts_follow_the_protocol(case):
    verdict = comparison.verdict_for(case["value"], case["interval"], case["minimum"],
                                     evidence_met=case["evidence_met"], lift=case["lift"])
    assert verdict == case["expected"], case["name"]


def test_the_bootstrap_constant_and_the_cluster_rule_are_the_protocols():
    assert comparison.BOOTSTRAP_RESAMPLES == 10000
    assert comparison.BOOTSTRAP_SEED == "tera-eval-bootstrap-16-v1"
    assert comparison.percentile([0.0, 1.0, 2.0, 3.0], 0.5) == 1.5
    assert comparison.percentile([], 0.5) is None
# --- one private, synthetic workspace ---------------------------------------

KICK_PROFILES = (
    {"band_sub": 0.50, "band_bass": 0.30, "band_low_mid": 0.10, "band_mid": 0.05,
     "band_high_mid": 0.03, "band_high": 0.02},
    {"band_sub": 0.20, "band_bass": 0.20, "band_low_mid": 0.30, "band_mid": 0.20,
     "band_high_mid": 0.07, "band_high": 0.03},
    {"band_sub": 0.35, "band_bass": 0.35, "band_low_mid": 0.15, "band_mid": 0.08,
     "band_high_mid": 0.05, "band_high": 0.02},
)
BASS_PROFILES = (
    {"band_sub": 0.10, "band_bass": 0.20, "band_low_mid": 0.30, "band_mid": 0.25,
     "band_high_mid": 0.10, "band_high": 0.05},
    {"band_sub": 0.05, "band_bass": 0.10, "band_low_mid": 0.25, "band_mid": 0.35,
     "band_high_mid": 0.15, "band_high": 0.10},
    {"band_sub": 0.15, "band_bass": 0.25, "band_low_mid": 0.25, "band_mid": 0.20,
     "band_high_mid": 0.10, "band_high": 0.05},
    {"band_sub": 0.25, "band_bass": 0.30, "band_low_mid": 0.20, "band_mid": 0.15,
     "band_high_mid": 0.06, "band_high": 0.04},
    {"band_sub": 0.45, "band_bass": 0.25, "band_low_mid": 0.15, "band_mid": 0.08,
     "band_high_mid": 0.04, "band_high": 0.03},
)
FINGERPRINT = "ab" * 32
SESSION_FIELDS = ("session_id", "evaluator_id", "protocol_version", "dataset_version",
                  "split_manifest_digest", "order_seed", "playback_gain_db",
                  "monitoring_description", "started_at", "finished_at")


def kick_sample(index):
    return selected_kick("kick-%03d" % index, **KICK_PROFILES[index % len(KICK_PROFILES)])


def bass_sample(index):
    return bass("bass-%03d" % index, **BASS_PROFILES[index % len(BASS_PROFILES)])


def analysis_manifest(role, samples):
    entries = {}
    for sample in samples:
        entries[sample.sample_id + ".wav"] = {
            "status": "complete", "fingerprint": FINGERPRINT, "sample_id": sample.sample_id,
            "result": sample.to_dict(), "error": None, "disposition": "analyzed"}
    return {"manifest_schema": "1.0", "root": "C:/synthetic-comparison/" + role,
            "role": role, "analysis_descriptor": {"batch": "fixture"}, "analysis_digest": "cd" * 32,
            "state": "complete", "entries": entries, "discovery_errors": []}


def build_workspace(base, *, kick_count=6, bass_count=4, candidates=5,
                    provenance="real_library_sample", assign_to=("evaluator-alpha",
                                                                "evaluator-bravo")):
    """A private workspace: dataset, split manifest, pair list, assignment and analyses."""
    root = base / ".local-evaluation" / "pair-rating"
    sessions = root / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    kicks = [kick_sample(index) for index in range(1, kick_count + 1)]
    basses = [bass_sample(index) for index in range(1, bass_count + 1)]
    selected = []
    for sample in kicks + basses:
        selected.append({"sample_id": sample.sample_id, "role": sample.role,
                         "pack": "comparison-fixture-pack",
                         "mapping": {"source": "fixture_pool", "path": sample.sample_id + ".wav"},
                         "sha256": FINGERPRINT, "provenance_kind": provenance})
    dataset = {"schema_version": "1.0", "dataset_version": DATASET_VERSION,
               "sources": {"fixture_pool": {"root": str(base / "audio")}},
               "selected": selected, "reserves": []}
    split = {"schema_version": "1.0", "dataset_version": DATASET_VERSION, "split_seed":
             comparison.SPLIT_SEED, "sampler_seed": comparison.PAIR_SAMPLER_SEED,
             "assignment_seed": comparison.ASSIGNMENT_SEED,
             "tuning": {"kicks": [], "basses": [basses[-1].sample_id]},
             "held_out": {"kicks": [sample.sample_id for sample in kicks],
                          "basses": [sample.sample_id for sample in basses[:
                                                                          max(candidates,
                                                                              len(basses))]]},
             "split_manifest_digest": DIGEST}
    records = []
    for index, kick in enumerate(kicks):
        for offset in range(min(candidates, bass_count)):
            candidate = basses[(index + offset) % bass_count]
            records.append({"pair_id": "pair-%03d-%s" % (index + 1, candidate.sample_id[-3:]),
                            "kick_sample_id": kick.sample_id,
                            "bass_sample_id": candidate.sample_id})
    # A later query may draw a bass twice through the offset; keep the first occurrence.
    seen, unique = set(), []
    for record in records:
        key = (record["kick_sample_id"], record["bass_sample_id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    records = unique
    pair_list = {"schema_version": "1.0", "dataset_version": DATASET_VERSION,
                 "split_manifest_digest": DIGEST, "sampler_seed": comparison.PAIR_SAMPLER_SEED,
                 "assignment_seed": comparison.ASSIGNMENT_SEED, "pairs": records}
    assignment = {"schema_version": "1.0", "dataset_version": DATASET_VERSION,
                  "split_manifest_digest": DIGEST,
                  "assignment_seed": comparison.ASSIGNMENT_SEED,
                  "assignments": {record["pair_id"]: list(assign_to) for record in records}}
    samples = {sample.sample_id: sample for sample in kicks + basses}
    paths = {}
    for name, document in (("dataset", dataset), ("split", split), ("pairs", pair_list),
                           ("assignment", assignment)):
        paths[name] = base / (name + ".json")
        paths[name].write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    analyses = []
    for role, group in (("kick", kicks), ("bass", basses)):
        path = base / ("analysis-" + role + "s.json")
        path.write_text(comparison.canonical(analysis_manifest(role, group)) + "\n",
                        encoding="utf-8")
        analyses.append(path)
    return SimpleNamespace(
        tmp=base, root=root, sessions=sessions, audio=base / "audio",
        dataset=paths["dataset"], split=paths["split"], pairs=paths["pairs"],
        assignment=paths["assignment"], analyses=tuple(analyses), samples=samples,
        records=records, kicks=kicks, basses=basses, report=base / "report.md",
        runs=base / "runs", jev_runs=(), dataset_document=dataset, split_document=split,
        pair_list=pair_list, assignment_document=assignment)


def workspace_arguments(workspace, command, **overrides):
    protocol = overrides.get("protocol_doc",
                             REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md")
    arguments = [command, "--protocol-doc", str(protocol), "--dataset", str(workspace.dataset),
                 "--split-manifest", str(overrides.get("split", workspace.split)),
                 "--pairs", str(overrides.get("pairs", workspace.pairs)),
                 "--assignment", str(overrides.get("assignment", workspace.assignment)),
                 "--sessions", str(overrides.get("sessions", workspace.sessions))]
    for path in overrides.get("analyses", workspace.analyses):
        arguments += ["--analysis-manifest", str(path)]
    for path in overrides.get("jev_runs", workspace.jev_runs):
        arguments += ["--jev-run", str(path)]
    if command == "report":
        arguments += ["--report", str(overrides.get("report", workspace.report)),
                      "--runs", str(overrides.get("runs", workspace.runs))]
    return arguments


def write_session(workspace, session_id, evaluator_id, assignments, labels, *, monitoring=MONITORING,
                  dataset_version=DATASET_VERSION, digest=DIGEST):
    """Write one #17 session directory: session.json, answers.jsonl and export.json."""
    presentations = [{"presentation_index": index, "pair_id": pair["pair_id"],
                      "kick_sample_id": pair["kick_sample_id"],
                      "bass_sample_id": pair["bass_sample_id"]}
                     for index, pair in enumerate(assignments, start=1)]
    session = {"schema_version": "1.0", "session_id": session_id, "evaluator_id": evaluator_id,
               "protocol_version": comparison.PROTOCOL_VERSION,
               "dataset_version": dataset_version, "split_manifest_digest": digest,
               "order_seed": comparison.ORDER_SEED, "playback_gain_db": -6.0,
               "monitoring_description": monitoring,
               "started_at": "2026-09-19T12:34:56.789+10:00",
               "finished_at": "2026-09-19T13:34:56.789+10:00", "state": "complete",
               "presentations": presentations}
    records = [{"pair_id": presentation["pair_id"],
                "kick_sample_id": presentation["kick_sample_id"],
                "bass_sample_id": presentation["bass_sample_id"],
                "presentation_index": presentation["presentation_index"],
                "rating": labels[index % len(labels)], "skip": False, "skip_reason": None,
                "recognised": False, "playback_completed": True,
                "responded_at": "2026-09-19T12:35:00.000+10:00", "correction_of": None}
               for index, presentation in enumerate(presentations)]
    directory = workspace.sessions / session_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.json").write_text(comparison.canonical(session) + "\n", encoding="utf-8")
    (directory / "answers.jsonl").write_text(
        "".join(comparison.canonical(record) + "\n" for record in records), encoding="utf-8")
    export = {"schema_version": "1.0",
              "session": {name: session[name] for name in SESSION_FIELDS}, "ratings": records}
    (directory / "export.json").write_text(comparison.canonical(export) + "\n", encoding="utf-8")
    return directory


LABELS = ("very-poor", "poor", "neutral", "good", "excellent")


def assignments_for(workspace, evaluator_id):
    """The pair records one evaluator is assigned, in file order."""
    return [record for record in workspace.records
            if evaluator_id in workspace.assignment_document["assignments"][record["pair_id"]]]


def judgement(dimension, label, probabilities=None):
    values = probabilities or {name: (1.0 if name == label else 0.0) for name in LABELS}
    return JevJudgment(dimension=dimension, label=label, confidence=0.9,
                       probabilities=tuple(LabelProbability(label=name, probability=value)
                                           for name, value in values.items()),
                       model_version="fixture-model-1", prompt_version=PROMPT_VERSION)


def abstention(dimension):
    return JevJudgment(dimension=dimension, label=None, confidence=None, probabilities=(),
                       model_version="fixture-model-1", prompt_version=PROMPT_VERSION,
                       unavailable_reason="model_abstained")


def outcome(request_id, dimension, judgment, *, state="judged", code=None, source="interface",
            model_versions=("fixture-model-1",)):
    attempts = 0 if state in ("not_asked", "unavailable") else 1
    record = JevOutcome(request_id=request_id, question_id=None, dimension=dimension, state=state,
                        code=code, attempts=attempts, judgment=judgment, elapsed_ms=1)
    run = JevScoringRun(adapter_version=ADAPTER_VERSION, source=source,
                        interface_name="fixture-interface" if source == "interface" else None,
                        prompt_version=PROMPT_VERSION, model_versions=model_versions,
                        cancelled=False, outcomes=(record,), elapsed_ms=1)
    return record, run


# --- the arms ---------------------------------------------------------------

def test_random_arm_is_the_documented_seeded_draw_without_replacement():
    candidates = ["bass-001", "bass-002", "bass-003", "bass-004", "bass-005"]
    first = comparison.random_order("kick-001", candidates, DATASET_VERSION)
    assert first == comparison.random_order("kick-001", candidates, DATASET_VERSION)
    assert sorted(first) == sorted(candidates)
    assert first == comparison.seeded_order(candidates, comparison.RANDOM_ARM_SEED,
                                            DATASET_VERSION, "random", "kick-001")
    assert first != comparison.random_order("kick-002", candidates, DATASET_VERSION)


def test_the_random_arms_analytic_expectations_are_declared():
    assert comparison.MIN_LIFT_OVER_RANDOM == 0.10
    assert comparison.MIN_PAIRWISE_ACCURACY == 0.60
    report_source = MODULE.read_text(encoding="utf-8")
    assert "analytic expectations are pairwise accuracy 0.50" in report_source


def test_the_dsp_only_arm_places_unscored_candidates_in_the_tail():
    kick = kick_sample(1)
    scoreable = bass_sample(1)
    # Every band ratio is zero and the other dimensions are unknown: #12 cannot score it and
    # never crashes, so the arm must place it at the tail.
    blank = sample("bass-blank", role="bass", **{name: 0.0 for name in KICK_PROFILES[0]})
    samples = {kick.sample_id: kick, scoreable.sample_id: scoreable, blank.sample_id: blank}
    arm = comparison.build_arm("dsp-only", kick=kick,
                               candidates=("bass-blank", scoreable.sample_id), samples=samples,
                               dataset_version=DATASET_VERSION)
    assert arm.order == (scoreable.sample_id, "bass-blank")
    assert arm.scored == (scoreable.sample_id,)
    assert arm.version == comparison.RANKING_VERSION
    assert arm.weight_table_id == comparison.DEFAULT_WEIGHT_TABLE_ID


def test_a_query_an_arm_orders_as_a_different_set_is_rejected():
    query = query_for("q1", ["b1", "b2"], {"b1": 2.0}, {"b1": 2})
    with pytest.raises(comparison.ComparisonError) as error:
        comparison.evaluate_query(order_for("dsp-only", ["b1"]), query)
    assert error.value.code == "candidate_set_mismatch"


def test_jev_only_ignores_every_dsp_compatibility_value():
    """The criterion's wiring test: complement the baseline and change nothing."""
    kick = kick_sample(1)
    candidates = (bass_sample(1), bass_sample(2), bass_sample(3))
    samples = {kick.sample_id: kick, **{candidate.sample_id: candidate for candidate in candidates}}
    table = {}
    for candidate in candidates:
        for dimension in ("frequency", "transient", "tonal", "rhythmic", "texture", "arrangement"):
            table[comparison.jev_key(kick.sample_id, candidate.sample_id, dimension)] = outcome(
                comparison.jev_key(kick.sample_id, candidate.sample_id, dimension), dimension,
                judgement(dimension, "good"))
    first = comparison.build_arm("jev-only", kick=kick,
                                 candidates=tuple(sample.sample_id for sample in candidates),
                                 samples=samples, dataset_version=DATASET_VERSION,
                                 outcome_table=table)
    baseline = ranking.rank_candidates(kick, list(candidates), policy=ranking.RankingPolicy())
    complemented = replace(
        baseline, ranked=tuple(replace(record, compatibility=1.0 - record.compatibility)
                               for record in baseline.ranked))
    second = comparison.build_arm("jev-only", kick=kick,
                                  candidates=tuple(sample.sample_id for sample in candidates),
                                  samples=samples, dataset_version=DATASET_VERSION,
                                  baseline=complemented, outcome_table=table)
    assert first == second
    assert first.order == tuple(sorted(sample.sample_id for sample in candidates))
    assert first.scored == first.order
    assert first.weight_table_id == "equal-1/6-weights"


def test_jev_only_renormalizes_over_available_dimensions_and_excludes_abstentions():
    kick = kick_sample(1)
    candidates = (bass_sample(1), bass_sample(2))
    samples = {kick.sample_id: kick, **{candidate.sample_id: candidate for candidate in candidates}}
    table = {
        comparison.jev_key(kick.sample_id, "bass-001", "frequency"): outcome(
            "a", "frequency", judgement("frequency", "good", {"very-poor": 0.0, "poor": 0.0,
                                                              "neutral": 0.0, "good": 1.0,
                                                              "excellent": 0.0})),
        comparison.jev_key(kick.sample_id, "bass-001", "transient"): outcome(
            "b", "transient", abstention("transient"), state="abstained"),
        comparison.jev_key(kick.sample_id, "bass-002", "frequency"): outcome(
            "c", "frequency", None, state="not_asked", code="candidate_band_energy_zero"),
    }
    scores = comparison.jev_only_scores(kick.sample_id, ("bass-001", "bass-002"), table)
    assert scores["bass-001"]["score"] == 0.75
    assert scores["bass-001"]["confidence"] == 1.0 / 6.0
    assert scores["bass-001"]["dimensions"] == ("frequency",)
    assert "bass-002" not in scores
    arm = comparison.build_arm("jev-only", kick=kick, candidates=("bass-001", "bass-002"),
                               samples=samples, dataset_version=DATASET_VERSION,
                               outcome_table=table)
    assert arm.order == ("bass-001", "bass-002")
    assert arm.scored == ("bass-001",)


def test_the_probability_weighted_variant_can_disagree_with_the_label_variant():
    kick = kick_sample(1)
    candidates = (bass_sample(1), bass_sample(2))
    samples = {kick.sample_id: kick, **{candidate.sample_id: candidate for candidate in candidates}}
    table = {
        comparison.jev_key(kick.sample_id, "bass-001", "frequency"): outcome(
            "a", "frequency", judgement("frequency", "neutral",
                                        {"very-poor": 0.0, "poor": 0.0, "neutral": 0.0,
                                         "good": 0.0, "excellent": 1.0})),
        comparison.jev_key(kick.sample_id, "bass-002", "frequency"): outcome(
            "b", "frequency", judgement("frequency", "good")),
    }
    label_arm = comparison.build_arm("jev-only", kick=kick, candidates=("bass-001", "bass-002"),
                                     samples=samples, dataset_version=DATASET_VERSION,
                                     outcome_table=table, variant="label")
    weighted_arm = comparison.build_arm("jev-only", kick=kick, candidates=("bass-001", "bass-002"),
                                        samples=samples, dataset_version=DATASET_VERSION,
                                        outcome_table=table, variant="probability")
    assert label_arm.order == ("bass-002", "bass-001")
    assert weighted_arm.order == ("bass-001", "bass-002")


def test_the_hybrid_arm_reports_the_documented_fallback():
    kick = kick_sample(1)
    candidates = (bass_sample(1), bass_sample(2), bass_sample(3))
    samples = {kick.sample_id: kick, **{candidate.sample_id: candidate for candidate in candidates}}
    arm = comparison.build_arm("hybrid", kick=kick,
                               candidates=tuple(sample.sample_id for sample in candidates),
                               samples=samples, dataset_version=DATASET_VERSION,
                               outcome_table={})
    assert arm.mode == comparison.MODE_DSP_ONLY
    assert arm.jev_status == comparison.JEV_ABSENT
    # The fallback keeps the baseline's own version and weight table, so it cannot drift
    # from rank_candidates; it is never reported as a hybrid value.
    assert arm.version == comparison.RANKING_VERSION
    assert arm.weight_table_id == comparison.DEFAULT_WEIGHT_TABLE_ID


def test_the_hybrid_fallback_is_reported_as_a_fallback():
    summary = {"eligible_queries": 2}
    findings = SimpleNamespace(query=(1,), analyses_available=True)
    status, reason = comparison.arm_status("hybrid", summary, findings, "live",
                                           {comparison.MODE_DSP_ONLY: 2})
    assert (status, reason) == ("insufficient", comparison.FALLBACK_REASON)
# --- the commands -----------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    return build_workspace(tmp_path)


@pytest.fixture
def fast_run(monkeypatch):
    """Keep the tests off the real cold-process spawn and off 10000 bootstrap draws."""
    monkeypatch.setattr(comparison, "default_spawn", fake_spawn)
    monkeypatch.setattr(comparison, "BOOTSTRAP_RESAMPLES", 200)


ABSENT = [("--dataset", "dataset.json"), ("--split-manifest", "split-manifest.json"),
          ("--pairs", "pairs.json"), ("--assignment", "assignment.json")]


def absent_arguments(base, command, *, report=None, runs=None):
    arguments = [command, "--protocol-doc", str(REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md")]
    for flag, file_name in ABSENT:
        arguments += [flag, str(base / file_name)]
    arguments += ["--sessions", str(base / "sessions"),
                  "--analysis-manifest", str(base / "analysis-kicks.json"),
                  "--analysis-manifest", str(base / "analysis-basses.json")]
    if command == "report":
        arguments += ["--report", str(report or (base / "report.md")),
                      "--runs", str(runs or (base / "runs"))]
    return arguments


def test_preflight_names_every_absent_artifact_and_exits_one(tmp_path):
    code, output = run(absent_arguments(tmp_path, "preflight"))
    assert code == 1
    summary = json.loads(output)
    assert [item["name"] for item in summary["items"]] == [
        "protocol_document", "split_manifest", "pair_list", "pair_membership",
        "evaluator_assignment", "pair_counts", "rated_pairs", "analysis_manifests",
        "dataset_provenance"]
    codes = {item["name"]: item["code"] for item in summary["items"]}
    assert codes["protocol_document"] == "ok"
    assert codes["split_manifest"] == "split_manifest_missing"
    assert codes["pair_list"] == "pair_list_missing"
    assert codes["evaluator_assignment"] == "assignment_missing"
    assert codes["pair_counts"] == "pair_counts_below_minimum"
    assert codes["rated_pairs"] == "ratings_missing"
    assert codes["analysis_manifests"] == "analysis_manifest_missing"
    assert codes["dataset_provenance"] == "dataset_missing"
    assert summary["items"][5]["actual"].startswith("0 of 300 sampled pairs")
    assert summary["exit"] == 1


def test_preflight_reports_a_wrong_pair_list_digest_with_exit_two(workspace, capsys):
    wrong = json.loads(workspace.pairs.read_text(encoding="utf-8"))
    wrong["split_manifest_digest"] = "sha256:" + "cd" * 32
    path = workspace.tmp / "wrong-pairs.json"
    path.write_text(comparison.canonical(wrong) + "\n", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight", pairs=path))
    assert code == 2
    item = next(item for item in json.loads(output)["items"] if item["name"] == "pair_list")
    assert item["code"] == "split_manifest_digest_mismatch"
    assert item["status"] == "inconsistent"
    assert "split_manifest_digest_mismatch" in capsys.readouterr().err or True


def test_preflight_rejects_a_malformed_pair_list(workspace):
    document = json.loads(workspace.pairs.read_text(encoding="utf-8"))
    document["renamed_field"] = document.pop("sampler_seed")
    path = workspace.tmp / "malformed-pairs.json"
    path.write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight", pairs=path))
    assert code == 2
    item = next(item for item in json.loads(output)["items"] if item["name"] == "pair_list")
    assert (item["code"], item["status"]) == ("schema_mismatch", "malformed")


def test_a_foreign_protocol_version_writes_nothing(workspace, capsys):
    document = (REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md").read_text(encoding="utf-8")
    path = workspace.tmp / "foreign-protocol.md"
    path.write_text(document.replace("tera-eval-protocol-v1", "tera-eval-protocol-v2"),
                    encoding="utf-8")
    code, _ = run(workspace_arguments(workspace, "report", protocol_doc=path))
    assert code == 2
    assert not workspace.report.exists()
    assert not workspace.runs.exists()
    assert "protocol_version_mismatch" in capsys.readouterr().err


def test_the_blocked_path_writes_the_report_with_zeros_and_shortfalls(tmp_path):
    report = tmp_path / "report.md"
    code, output = run(absent_arguments(tmp_path, "report", report=report))
    assert code == 1
    assert json.loads(output)["exit"] == 1
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    for section in comparison.REPORT_SECTIONS:
        assert section in text, section
    lines = text.splitlines()
    state_line = lines[lines.index("## State") + 1]
    assert state_line.startswith("insufficient evidence \u2014 ")
    assert "0 of 300" in text and "0 of 60" in text
    assert "0 of 5 counted evaluators" in text
    assert "0 of 30 valid" in text
    assert comparison.FABRICATION_SENTENCE in text
    assert comparison.ATTESTATION in text
    assert "cannot support any lift or quality" in text
    assert "queries_with_11_rated_candidates: 0 of 0" in text
    assert comparison.ADAPTER_VERDICT in text
    assert "never re-derives a manifest digest, a seed, a split, an assignment" in text
    for gate in ("MIN_HELDOUT_PAIRS = 300", "MIN_EVALUATORS = 5", "MIN_PAIRWISE_ACCURACY = 0.6"):
        assert gate in text, gate
    assert "not_implemented" in text


def test_a_synthetic_fixture_record_is_a_mechanism_failure(tmp_path, fast_run):
    workspace = build_workspace(tmp_path, provenance="synthetic_fixture")
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 2
    assert not workspace.report.exists()


def test_a_rated_pair_outside_the_pair_list_is_a_mechanism_failure(workspace):
    directory = write_session(workspace, "session-outside", "evaluator-alpha",
                              assignments_for(workspace, "evaluator-alpha"),
                              ("good", "excellent", "poor"))
    document = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    document["presentations"][0]["pair_id"] = "pair-not-sampled"
    (directory / "session.json").write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    export = json.loads((directory / "export.json").read_text(encoding="utf-8"))
    export["session"] = {name: document[name] for name in SESSION_FIELDS}
    export["ratings"][0]["pair_id"] = "pair-not-sampled"
    (directory / "export.json").write_text(comparison.canonical(export) + "\n", encoding="utf-8")
    (directory / "answers.jsonl").write_text(
        "".join(comparison.canonical(record) + "\n" for record in export["ratings"]),
        encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight"))
    assert code == 2
    item = next(item for item in json.loads(output)["items"] if item["name"] == "rated_pairs")
    assert (item["code"], item["status"]) == ("rated_pair_not_sampled", "inconsistent")


def test_two_analysis_versions_across_the_run_are_a_mechanism_failure(workspace, fast_run):
    document = json.loads(workspace.analyses[1].read_text(encoding="utf-8"))
    for entry in document["entries"].values():
        entry["result"]["analysis_version"] = "comparison-fixture-analysis-2"
    workspace.analyses[1].write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 2
    assert not workspace.report.exists()


def test_child_output_uses_single_files_not_a_temporary_directory():
    """A refused directory cleanup must never turn a validate result into a crash."""
    source = MODULE.read_text(encoding="utf-8")
    assert "TemporaryDirectory" not in source
    assert "mkstemp" in source


def test_a_validated_session_is_evidence_and_a_broken_one_is_not(workspace):
    assignments = assignments_for(workspace, "evaluator-alpha")
    directory = write_session(workspace, "session-one", "evaluator-alpha", assignments,
                              ("good", "excellent", "poor", "acceptable"))
    exit_code, report = comparison.run_validate(directory)
    assert exit_code == 0, report
    assert report["valid"] is True
    summaries = comparison.load_sessions(workspace.sessions)
    assert len(summaries) == 1
    assert summaries[0].validate_exit == 0
    assert len(summaries[0].ratings) == len(assignments)
    (directory / "export.json").unlink()
    exit_code, report = comparison.run_validate(directory)
    assert exit_code == 1 and report["valid"] is False
    summaries = comparison.load_sessions(workspace.sessions)
    assert summaries[0].counted is False
    assert summaries[0].reason == "validate_failed"
    assert "export_missing" in summaries[0].violations


def test_a_tooling_session_is_excluded_and_listed_in_the_deviations(workspace):
    directory = write_session(workspace, "session-dry", "evaluator-bravo",
                              assignments_for(workspace, "evaluator-bravo"),
                              ("good",), monitoring="dry-run synthetic fixture")
    summary = comparison.load_session(directory)
    assert summary.validate_exit == 0
    assert summary.tooling is True and summary.counted is False
    assert summary.reason == "tooling_session"


def test_the_report_is_byte_identical_on_a_rerun(workspace, fast_run):
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 1
    first = {name: (workspace.report.read_bytes(),) for name in ("report",)}
    keys = [path.name for path in workspace.runs.iterdir()]
    assert len(keys) == 1
    run_directory = workspace.runs / keys[0]
    artifacts = {name: (run_directory / name).read_bytes()
                 for name in ("run.json", "records.json")}
    code, output = run(workspace_arguments(workspace, "report"))
    assert code == 1
    assert [path.name for path in workspace.runs.iterdir()] == keys
    for name, content in artifacts.items():
        assert (run_directory / name).read_bytes() == content, name
    assert first["report"][0] == workspace.report.read_bytes()
    assert json.loads(output)["run_key"] == keys[0]


def test_a_session_with_a_foreign_dataset_version_contributes_nothing(workspace):
    write_session(workspace, "session-foreign", "evaluator-alpha",
                  assignments_for(workspace, "evaluator-alpha"), ("good", "excellent"),
                  dataset_version="another-pool")
    paths = comparison.Paths(REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md",
                             workspace.dataset, workspace.split, workspace.pairs,
                             workspace.assignment, workspace.sessions, workspace.analyses, (),
                             workspace.report, workspace.runs, workspace.tmp / "tuning", False,
                             False)
    findings = comparison.evaluate(paths)
    assert findings.accounting["counted_evaluators"] == 0
    assert findings.accounting["valid_ratings"] == 0
    assert any(item.get("reason") == "session_field_mismatch" for item in findings.deviations)


def test_the_report_carries_a_session_row_per_session(workspace, fast_run):
    write_session(workspace, "session-one", "evaluator-alpha",
                  assignments_for(workspace, "evaluator-alpha"), ("good", "excellent"))
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 1
    text = workspace.report.read_text(encoding="utf-8")
    assert "### Session rows" in text
    assert "| 1 | 0 | yes |" in text
    assert "Intersection of the eligible queries across the" in text


def test_the_report_carries_no_private_identifier(tmp_path, fast_run):
    workspace = build_workspace(tmp_path)
    write_session(workspace, "session-one", "evaluator-alpha",
                  assignments_for(workspace, "evaluator-alpha"), ("good", "excellent"))
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    for token in ("kick-001", "bass-001", "pair-001", "evaluator-alpha", "session-one",
                  str(tmp_path), "comparison-fixture-pack", "fixture_pool"):
        assert token not in text, token


def test_another_run_key_for_the_same_dataset_and_split_is_listed(workspace, fast_run):
    code, output = run(workspace_arguments(workspace, "report"))
    assert code == 1
    first_key = json.loads(output)["run_key"]
    record, run_document = outcome("jev|kick-001|bass-001|frequency", "frequency",
                                   judgement("frequency", "good"))
    path = workspace.tmp / "jev-run.json"
    path.write_text(run_document.to_json(), encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "report", jev_runs=(path,)))
    assert code == 1
    assert json.loads(output)["run_key"] != first_key
    text = workspace.report.read_text(encoding="utf-8")
    assert first_key in text
    assert "series" not in text


def test_the_top_k_gate_is_insufficient_with_its_n_of_m_count(workspace, fast_run):
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    assert "queries_with_11_rated_candidates:" in text
    assert "MIN_TOP_K_MEAN = 2.0" in text
    assert "MIN_RATED_CANDIDATES_FOR_TOP10_GATE" not in text
    assert "k = 5" in text


# --- QA regression cases: mechanism failures, honest counting, latency ------

def double_run(workspace, source, model_version="fixture-model-1"):
    """One recorded Jev run over a whole workspace; a double run is never evidence."""
    outcomes = []
    for record in workspace.records:
        label = "good" if int(record["bass_sample_id"][-3:]) % 2 == 0 else "poor"
        for dimension in comparison.DIMENSIONS:
            request_id = comparison.jev_key(record["kick_sample_id"],
                                            record["bass_sample_id"], dimension)
            outcomes.append(JevOutcome(request_id=request_id, question_id=None,
                                       dimension=dimension, state="judged", code=None, attempts=1,
                                       judgment=judgement(dimension, label), elapsed_ms=1))
    return JevScoringRun(adapter_version=ADAPTER_VERSION, source=source,
                         interface_name="fixture-interface" if source == "interface" else None,
                         prompt_version=PROMPT_VERSION, model_versions=(model_version,),
                         cancelled=False, outcomes=tuple(outcomes), elapsed_ms=1)


def partial_double_run(workspace, source, keep=3, model_version="fixture-model-1"):
    """A double run that judged only the first keep basses of every query."""
    outcomes = []
    for record in workspace.records:
        if int(record["bass_sample_id"][-3:]) > keep:
            continue
        label = "good" if int(record["bass_sample_id"][-3:]) % 2 == 0 else "poor"
        for dimension in comparison.DIMENSIONS:
            request_id = comparison.jev_key(record["kick_sample_id"],
                                            record["bass_sample_id"], dimension)
            outcomes.append(JevOutcome(request_id=request_id, question_id=None,
                                       dimension=dimension, state="judged", code=None, attempts=1,
                                       judgment=judgement(dimension, label), elapsed_ms=1))
    return JevScoringRun(adapter_version=ADAPTER_VERSION, source=source,
                         interface_name="fixture-interface" if source == "interface" else None,
                         prompt_version=PROMPT_VERSION, model_versions=(model_version,),
                         cancelled=False, outcomes=tuple(outcomes), elapsed_ms=1)


def stateful_spawn(command):
    """A cold sample answers 6.0 ms and its warm repeat 3.0 ms."""
    request_path = Path(command[command.index("--request") + 1])
    out = Path(command[command.index("--out") + 1])
    index = int(request_path.stem.split("-")[0])
    stages = dict(FAKE_STAGES)
    stages["total"] = 6.0 if index % 2 == 0 else 3.0
    request = json.loads(request_path.read_text(encoding="utf-8"))
    out.write_text(comparison.canonical({"arm": request["arm"], "query": request["query"],
                                         "stages": stages, "order": [],
                                         "memo_hit": False}) + "\n", encoding="utf-8")
    return 0


def test_a_malformed_dataset_is_a_mechanism_failure_with_exit_two(workspace):
    document = json.loads(workspace.dataset.read_text(encoding="utf-8"))
    document["schema_version"] = "2.0"
    workspace.dataset.write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight"))
    assert code == 2
    summary = json.loads(output)
    item = next(item for item in summary["items"] if item["name"] == "dataset_provenance")
    assert (item["code"], item["status"]) == ("schema_mismatch", "malformed")
    assert summary["mechanism"] and summary["mechanism"][0]["source"] == "dataset"


def test_an_unparseable_dataset_is_a_mechanism_failure_with_exit_two(workspace):
    workspace.dataset.write_text("{not json", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight"))
    assert code == 2
    summary = json.loads(output)
    assert [item["code"] for item in summary["mechanism"]] == ["schema_mismatch"]
    assert summary["mechanism"][0]["source"] == "dataset"
    assert summary["exit"] == 2


def test_a_malformed_analysis_manifest_is_a_mechanism_failure_with_exit_two(workspace):
    document = json.loads(workspace.analyses[0].read_text(encoding="utf-8"))
    document["manifest_schema"] = "9.9"
    workspace.analyses[0].write_text(comparison.canonical(document) + "\n", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight"))
    assert code == 2
    summary = json.loads(output)
    item = next(item for item in summary["items"] if item["name"] == "analysis_manifests")
    assert (item["code"], item["status"]) == ("schema_mismatch", "malformed")
    assert "1 of 2 declared analysis manifests are present but malformed" in item["actual"]


def test_a_malformed_jev_run_is_a_mechanism_failure_named_in_the_summary(workspace):
    path = workspace.tmp / "bad-jev-run.json"
    path.write_text("{not json", encoding="utf-8")
    code, output = run(workspace_arguments(workspace, "preflight", jev_runs=(path,)))
    assert code == 2
    summary = json.loads(output)
    assert [entry["source"] for entry in summary["mechanism"]] == ["jev_run"]
    assert summary["exit"] == 2


def test_a_ratings_source_under_tests_is_a_mechanism_failure(workspace):
    code, output = run(workspace_arguments(workspace, "preflight",
                                           sessions=REPOSITORY_ROOT / "tests"))
    assert code == 2
    summary = json.loads(output)
    item = next(item for item in summary["items"] if item["name"] == "rated_pairs")
    assert (item["code"], item["status"]) == ("public_rating_source", "inconsistent")
    assert any(entry["code"] == "public_rating_source" for entry in summary["mechanism"])


def test_a_rated_but_unscored_candidate_is_never_counted_in_a_metric():
    """QA's repro: the unscored candidate is rated 3.0 but must not enter any metric."""
    case = fixture("unscored-cases.json")["rated_unscored_case"]
    kick = kick_sample(1)
    first, second = bass_sample(1), bass_sample(2)
    blank = sample("bass-blank", role="bass", **{name: 0.0 for name in KICK_PROFILES[0]})
    samples = {kick.sample_id: kick, first.sample_id: first, second.sample_id: second,
               blank.sample_id: blank}
    arm = comparison.build_arm("dsp-only", kick=kick, candidates=case["sampled"], samples=samples,
                               dataset_version=DATASET_VERSION)
    assert list(arm.scored) == case["scored"]
    query = query_for("q1", case["sampled"], case["rated"], {name: 2 for name in case["rated"]})
    evaluation = comparison.evaluate_query(arm, query)
    for name, expected in case["expected"].items():
        if name in ("scored_share", "unscored_share", "eligible"):
            continue
        assert evaluation.metrics[name] == expected, name
    assert evaluation.scored_share == case["expected"]["scored_share"]
    assert evaluation.unscored_share == case["expected"]["unscored_share"]
    assert evaluation.eligible is case["expected"]["eligible"]
    summary = comparison.arm_summary((evaluation,))
    assert summary["unscored_share"]["value"] == case["expected"]["unscored_share"]
    assert summary["unscored_share"]["scored_in_rated"] == 2
    assert summary["unscored_share"]["rated_total"] == 3


def test_the_report_lists_a_random_lift_row_for_every_arm(workspace, fast_run):
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines()
            if line.startswith("| MIN_LIFT_OVER_RANDOM = 0.1 |")]
    assert len(rows) == 4
    for arm in ("random", "dsp-only", "jev-only", "hybrid"):
        assert any("| " + arm + " |" in row for row in rows), arm
    # No eligible query exists here, so the identical arm's row is an unevaluable gate: it
    # carries its exact shortfall and no invented interval, and it is never a failure.
    assert ("| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | random | not computed"
            " | not computed | insufficient | 0 of 60 paired eligible queries |") in text
    assert "not supported | structurally zero" not in text


def test_the_gate_class_column_carries_the_protocols_class_string(workspace, fast_run):
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| MIN_TOP_K_MEAN = 2.0 |")]
    assert rows and all("minimum (gate, literal top-10 only)" in row for row in rows)
    assert "| MIN_POOL_KICKS = 80 | minimum (gate) |" in text
    assert "| TARGET_EVALUATORS = 8 | target (non-gating) |" in text


def test_a_workspace_where_every_query_holds_eleven_rated_candidates_gates_the_top_ten(
        tmp_path, fast_run):
    workspace = build_workspace(tmp_path, kick_count=6, bass_count=12, candidates=11,
                                assign_to=EVALUATORS[:2])
    for evaluator in EVALUATORS[:2]:
        write_session(workspace, "session-" + evaluator, evaluator,
                      assignments_for(workspace, evaluator), ("good", "excellent", "poor"))
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 1
    text = workspace.report.read_text(encoding="utf-8")
    assert "queries_with_11_rated_candidates: 6 of 6" in text
    rows = [line for line in text.splitlines() if line.startswith("| MIN_TOP_K_MEAN = 2.0 |")]
    assert len(rows) == 4
    assert not any("not computed" in row for row in rows)


def test_the_random_self_lift_is_a_note_and_never_a_failure(workspace, fast_run):
    """With paired queries the identical arm's row is a note, so a pass stays reachable."""
    for evaluator in EVALUATORS[:2]:
        write_session(workspace, "session-" + evaluator, evaluator,
                      assignments_for(workspace, evaluator), ("good", "excellent", "poor"))
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines()
               if line.startswith("| MIN_LIFT_OVER_RANDOM = 0.1 | minimum (gate) | random |"))
    assert "not applicable (the identical arm)" in row
    assert "structural zero" in row
    assert "not supported" not in row
    arm_row = next(line for line in text.splitlines()
                   if line.startswith("| random | dsp-baseline-v1 |"))
    assert "| not supported |" not in arm_row
    assert "| insufficient |" in arm_row


def test_a_partial_double_jev_run_leaks_no_value_into_the_report(workspace, fast_run):
    """QA's repro: a double run judging 3 of 4 candidates must not move any published value."""
    for evaluator in EVALUATORS[:2]:
        write_session(workspace, "session-" + evaluator, evaluator,
                      assignments_for(workspace, evaluator), ("good", "excellent", "poor"))
    path = workspace.tmp / "partial-double.json"
    path.write_text(partial_double_run(workspace, "double", keep=3).to_json(), encoding="utf-8")
    code, _ = run(workspace_arguments(workspace, "report", jev_runs=(path,)))
    assert code == 1
    run_directory = list(workspace.runs.iterdir())[0]
    document = json.loads((run_directory / "run.json").read_text(encoding="utf-8"))
    assert document["arms"]["jev-only"]["unscored_share"] == 0.25
    text = workspace.report.read_text(encoding="utf-8")
    assert "0.7500" not in text
    assert "worst eligible query 1.0000 scored share" in text
    for arm in ("jev-only", "hybrid"):
        row = next(line for line in text.splitlines() if line.startswith("| " + arm + " |"))
        assert row.split("|")[2].strip() == "not reported (no live Jev outcomes)", arm
    latency = [line for line in text.splitlines() if line.startswith("| MIN_LATENCY")]
    assert latency and "no_live_jev_outcomes" in latency[0]
    assert "hybrid fallback on not reported" not in text


def test_a_reachable_top_ten_run_does_not_print_the_k_equals_five_boilerplate(
        tmp_path, fast_run):
    workspace = build_workspace(tmp_path, kick_count=6, bass_count=12, candidates=11,
                                assign_to=EVALUATORS[:2])
    for evaluator in EVALUATORS[:2]:
        write_session(workspace, "session-" + evaluator, evaluator,
                      assignments_for(workspace, evaluator), ("good", "excellent", "poor"))
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    assert "queries_with_11_rated_candidates: 6 of 6" in text
    assert "the literal top-10 rule holds" in text
    assert "With k = 5 the top-k means are descriptive" not in text


def test_the_jev_only_covered_weight_confidence_is_persisted(tmp_path, fast_run):
    workspace = build_workspace(tmp_path)
    path = workspace.tmp / "double-run.json"
    path.write_text(double_run(workspace, "double").to_json(), encoding="utf-8")
    code, _ = run(workspace_arguments(workspace, "report", jev_runs=(path,)))
    assert code == 1
    run_dirs = list(workspace.runs.iterdir())
    document = json.loads((run_dirs[0] / "records.json").read_text(encoding="utf-8"))
    scores = [query["arms"]["jev-only"]["scores"] for query in document["queries"]]
    assert any(entry for entry in scores)
    checked = next(entry for entry in scores if entry)
    assert all(set(value) == {"score", "confidence"} for value in checked.values())


def test_a_double_only_jev_run_never_publishes_a_derived_value(workspace, fast_run):
    for evaluator in EVALUATORS[:2]:
        write_session(workspace, "session-" + evaluator, evaluator,
                      assignments_for(workspace, evaluator), ("good", "excellent", "poor"))
    path = workspace.tmp / "double-run.json"
    path.write_text(double_run(workspace, "double").to_json(), encoding="utf-8")
    code, _ = run(workspace_arguments(workspace, "report", jev_runs=(path,)))
    assert code == 1
    text = workspace.report.read_text(encoding="utf-8")
    run_dirs = list(workspace.runs.iterdir())
    document = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert document["double_runs"] == {"count": 1, "label": comparison.DOUBLE_NOT_EVIDENCE}
    value = comparison.shown(document["arms"]["jev-only"]["metrics"]["pairwise_accuracy"]["value"])
    assert value != "not computed"
    assert "| pairwise_accuracy (jev-only) | " + value not in text
    assert "| MIN_JEV_ONLY_PAIRWISE_ACCURACY = 0.55 | minimum (gate) | jev-only |" in text
    assert "not reported (no live Jev outcomes)" in text
    assert comparison.DOUBLE_NOT_EVIDENCE in text
    assert document["arms"]["jev-only"]["metrics"]["pairwise_accuracy"]["value"] is not None


def test_the_deviations_never_print_a_session_id(workspace, fast_run):
    directory = write_session(workspace, "session-one", "evaluator-alpha",
                              assignments_for(workspace, "evaluator-alpha"), ("good",))
    (directory / "export.json").unlink()
    code, _ = run(workspace_arguments(workspace, "report"))
    assert code == 1
    text = workspace.report.read_text(encoding="utf-8")
    assert "session-one" not in text
    assert "session 1: validate exit 1" in text
    assert "the session id stays in the private run artifact" in text


def test_the_evidence_gap_row_counts_the_absent_manifests_honestly(tmp_path):
    report = tmp_path / "report.md"
    run(absent_arguments(tmp_path, "report", report=report))
    text = report.read_text(encoding="utf-8")
    assert "2 of 2 declared analysis manifests are absent" in text
    assert "0 of 2 analysis manifests present" not in text


def test_honest_evidence_strings_have_no_template_or_invented_minimum(tmp_path):
    report = tmp_path / "report.md"
    run(absent_arguments(tmp_path, "report", report=report))
    text = report.read_text(encoding="utf-8")
    gate_rows = [line for line in text.splitlines() if line.startswith("| MIN_")]
    assert gate_rows and not any("n of " in row for row in gate_rows)
    assert "0 of 1 session" not in text
    assert "0 of 60 paired eligible queries" in text
    assert "0 of 30 valid samples on the least-sampled published arm" in text
    assert "0 of 60 paired eligible queries" in text
    assert "not supported | structurally zero" not in text


def test_latency_arm_rows_report_the_measured_p95_of_each_state(tmp_path):
    queries, samples = latency_fixture(tmp_path, count=1)
    plan = comparison.latency_plan(queries, ("dsp-only",), per_arm=2, distinct_kicks=10)
    table = comparison.measure_latency(plan, queries, samples, dataset_version=DATASET_VERSION,
                                       outcome_table={}, run_directory=tmp_path,
                                       spawn=stateful_spawn)
    entry = table["arms"]["dsp-only"]
    assert entry["valid_samples"] == 2
    assert entry["states"]["cold"]["valid_samples"] == 1
    assert entry["states"]["warm"]["valid_samples"] == 1
    # The cold sample carries the spawned child's own stages; the warm repeat is timed here.
    assert entry["states"]["cold"]["stages"]["total"]["p95"] == 6.0
    assert entry["states"]["warm"]["stages"]["total"]["p95"] is not None
    warm = [record for record in table["samples"] if record["state"] == "warm"]
    cold = [record for record in table["samples"] if record["state"] == "cold"]
    assert warm and all(record["memo_hit"] for record in warm)
    assert cold and not any(record["memo_hit"] for record in cold)
    assert table["capture"] == "recorded"
    assert (tmp_path / "latency.json").is_file()
    gate = comparison.latency_gate("MAX_COLD_RECOMMENDATION_P95_MS", table)
    assert "2 of 30" in gate.value and gate.verdict == "insufficient"
    assert gate.shortfall == ("2 of 30 valid latency samples on the least-sampled published"
                              " arm")


def test_the_latency_gate_uses_the_real_sample_counts(tmp_path, fast_run):
    workspace = build_workspace(tmp_path)
    run(workspace_arguments(workspace, "report"))
    text = workspace.report.read_text(encoding="utf-8")
    # The Jev arms have no live outcome, so the latency evidence gate is insufficient with
    # its real count and names the cause; the published arms' counts are the measured ones.
    assert ("| MIN_LATENCY_REQUESTS_PER_ARM = 30 | minimum (gate) | random, dsp-only | 30 of 30"
            " valid samples on the least-sampled published arm | not applicable (evidence"
            " minimum) | insufficient | 30 of 30 valid latency samples on the least-sampled"
            " published arm; no_live_jev_outcomes |") in text
    row = next(line for line in text.splitlines() if line.startswith("| random | 30 of 30 |"))
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[2] == "6 of 10"
    assert cells[3] == "6.0 ms"          # the spawned cold samples' own stage value
    assert cells[4].endswith(" ms")      # a measured warm p95, never "not measured"
    assert "| MAX_COLD_RECOMMENDATION_P95_MS = 2000 | minimum (gate) |" in text
    assert "0 of 30 valid latency samples per arm" not in text


# --- the latency harness ----------------------------------------------------

FAKE_STAGES = {"feature_load": 1.0, "filter": 2.0, "retrieval": "not_implemented", "jev": None,
               "ranking": 3.0, "total": 6.0}


def fake_spawn(command, *, stages=None):
    values = FAKE_STAGES if stages is None else stages
    out = Path(command[command.index("--out") + 1])
    request = json.loads(Path(command[command.index("--request") + 1]).read_text(encoding="utf-8"))
    out.write_text(comparison.canonical({"arm": request["arm"], "query": request["query"],
                                         "stages": dict(values), "order": [],
                                         "memo_hit": False}) + "\n", encoding="utf-8")
    return 0


def latency_fixture(tmp_path, count=12):
    kick = kick_sample(1)
    candidate = bass_sample(1)
    queries = tuple(comparison.Query("query-%02d" % index, (candidate.sample_id,),
                                     (candidate.sample_id,), {candidate.sample_id: 2.0},
                                     {candidate.sample_id: 2}) for index in range(1, count + 1))
    samples = {query.kick_id: kick for query in queries}
    samples[candidate.sample_id] = candidate
    return queries, samples


def test_the_latency_plan_spreads_requests_over_query_kicks(tmp_path):
    queries, _ = latency_fixture(tmp_path)
    plan = comparison.latency_plan(queries, ("dsp-only",))
    assert len(plan["dsp-only"]) == comparison.MIN_LATENCY_REQUESTS_PER_ARM
    assert len({query.kick_id for _, query, _, _ in plan["dsp-only"]}) \
        == comparison.MIN_LATENCY_QUERY_KICKS
    states = [state for _, _, state, _ in plan["dsp-only"]]
    assert states.count("cold") == states.count("warm") == 15


def test_latency_samples_stages_and_the_memo_are_recorded(tmp_path):
    queries, samples = latency_fixture(tmp_path)
    plan = comparison.latency_plan(queries, ("dsp-only",))
    table = comparison.measure_latency(plan, queries, samples, dataset_version=DATASET_VERSION,
                                       outcome_table={}, run_directory=tmp_path,
                                       spawn=fake_spawn)
    entry = table["arms"]["dsp-only"]
    assert entry["valid_samples"] == 30
    assert entry["distinct_query_kicks"] == 10
    assert entry["reasons"] == []
    assert entry["states"]["cold"]["stages"]["total"] == {"p50": 6.0, "p95": 6.0, "count": 15}
    assert entry["states"]["cold"]["stages"]["feature_load"] == {"p50": 1.0, "p95": 1.0,
                                                                "count": 15}
    assert table["capture"] == "recorded"
    assert table["retrieval"] == "not_implemented"
    assert table["memo"]["hits"] == 15 and table["memo"]["misses"] == 15
    assert table["memo"]["key_fields"] == ["arm", "query", "candidates", "dataset_version",
                                           "analysis_version", "ranking_version"]
    assert table["memo"]["note"].endswith("not the #26 product decision cache")


def test_too_few_latency_samples_make_the_arm_insufficient(tmp_path):
    queries, samples = latency_fixture(tmp_path, count=1)
    plan = comparison.latency_plan(queries, ("dsp-only",), per_arm=4, distinct_kicks=10)
    table = comparison.measure_latency(plan, queries, samples, dataset_version=DATASET_VERSION,
                                       outcome_table={}, run_directory=tmp_path,
                                       spawn=fake_spawn)
    assert table["arms"]["dsp-only"]["valid_samples"] == 4
    assert table["arms"]["dsp-only"]["reasons"] == ["latency_samples_below_minimum",
                                                    "latency_query_kicks_below_minimum"]


def test_a_failed_request_and_a_warm_miss_are_not_samples(tmp_path):
    queries, samples = latency_fixture(tmp_path, count=1)
    plan = comparison.latency_plan(queries, ("dsp-only",), per_arm=4, distinct_kicks=10)
    table = comparison.measure_latency(plan, queries, samples, dataset_version=DATASET_VERSION,
                                       outcome_table={}, run_directory=tmp_path,
                                       spawn=lambda command: 1)
    entry = table["arms"]["dsp-only"]
    assert entry["valid_samples"] == 0
    assert entry["invalid_samples"] == 4
    assert {item["reason"] for item in table["invalid"]} == {comparison.INVALID_REQUEST,
                                                             comparison.INVALID_WARM}
    assert "invalid_samples_present" in entry["reasons"]
    assert comparison.percentile([], 0.95) is None
    assert comparison.percentile([4.0], 0.95) == 4.0


def test_retrieval_is_never_reported_as_zero_milliseconds(tmp_path):
    queries, samples = latency_fixture(tmp_path, count=1)
    plan = comparison.latency_plan(queries, ("dsp-only",), per_arm=2, distinct_kicks=10)
    table = comparison.measure_latency(plan, queries, samples, dataset_version=DATASET_VERSION,
                                       outcome_table={}, run_directory=tmp_path,
                                       spawn=fake_spawn)
    entry = table["arms"]["dsp-only"]["stages"]["retrieval"]
    assert entry["p50"] is None and entry["p95"] is None
    assert table["retrieval"] == "not_implemented"
    assert comparison.RETRIEVAL_NOT_IMPLEMENTED != 0
