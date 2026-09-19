"""Every frozen constant the #19 runner declares, checked against the protocol document.

This module parses _docs/evaluation-protocol.md itself: the frozen-constants table's name,
value and class, and the Thresholds table's gate classes. It never imports the runner's
numbers into its expectations, so a change to the document or to the runner fails here with
the constant named: the runner may not lower a threshold, rename a seed or move a value
between classes without this file failing.
"""

import ast
import io
from pathlib import Path

import pytest

from backend.evaluation import comparison

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md"
MODULE = REPOSITORY_ROOT / "backend" / "evaluation" / "comparison.py"

NAMED = ("MIN_PAIRWISE_ACCURACY", "TARGET_PAIRWISE_ACCURACY", "MIN_LIFT_OVER_RANDOM",
         "MIN_LIFT_OVER_DSP", "MIN_DSP_PAIRWISE_ACCURACY", "MIN_JEV_ONLY_PAIRWISE_ACCURACY",
         "MIN_TOP1_MARGIN", "MAX_COLD_RECOMMENDATION_P95_MS",
         "TARGET_COLD_RECOMMENDATION_P95_MS", "MAX_WARM_RECOMMENDATION_P95_MS",
         "TARGET_WARM_RECOMMENDATION_P95_MS", "MIN_LATENCY_REQUESTS_PER_ARM")
SEEDS = ("SPLIT_SEED", "PAIR_SAMPLER_SEED", "ASSIGNMENT_SEED", "ORDER_SEED", "RANDOM_ARM_SEED",
         "BOOTSTRAP_SEED")
SECTIONS = ("## State", "## Versions and digests", "## Eligibility accounting", "## Quality",
            "## Uncertainty", "## Agreement", "## Latency", "## Evidence gaps", "## Deviations",
            "## No-held-out-tuning attestation", "## Reproduction", "## Out of scope")


def protocol_text():
    return DOCUMENT.read_text(encoding="utf-8")


def protocol_table():
    return comparison.frozen_constants(protocol_text())


def threshold_classes():
    """The Thresholds table's own class column, by constant name."""
    classes = {}
    for cells in comparison.table_rows(protocol_text(), "## Thresholds"):
        if len(cells) < 4:
            continue
        name = cells[0].strip(comparison.MARK)
        if name.isupper():
            classes[name] = cells[3].strip().split()[0].lower()
    return classes


@pytest.mark.parametrize("name", sorted(comparison.FROZEN_CONSTANTS), ids=lambda name: name)
def test_every_declared_constant_matches_the_documents_name_value_and_class(name):
    declared_value, declared_class = comparison.FROZEN_CONSTANTS[name]
    parsed = protocol_table()
    assert name in parsed, name + " is declared by the runner and missing from the document"
    assert declared_value == parsed[name]["value"], name
    assert declared_class == parsed[name]["class"], name


def test_the_document_declares_no_constant_the_runner_ignores():
    declared = set(comparison.FROZEN_CONSTANTS)
    parsed = set(protocol_table())
    assert parsed - declared == set(), sorted(parsed - declared)


def test_the_protocol_version_is_the_frozen_one():
    assert comparison.PROTOCOL_VERSION == "tera-eval-protocol-v1"
    assert protocol_table()["PROTOCOL_VERSION"]["value"] == comparison.PROTOCOL_VERSION


def test_the_named_constants_and_the_seeds_are_declared_with_their_classes():
    parsed = protocol_table()
    for name in NAMED:
        assert name in comparison.FROZEN_CONSTANTS, name
        assert comparison.FROZEN_CONSTANTS[name][0] == parsed[name]["value"], name
        assert comparison.FROZEN_CONSTANTS[name][1] == parsed[name]["class"], name
        assert comparison.FROZEN_CONSTANTS[name][1] in ("minimum", "target"), name
    for name in SEEDS:
        value, kind = comparison.FROZEN_CONSTANTS[name]
        assert kind == "seed" and value == parsed[name]["value"], name
        assert value.startswith("tera-eval-"), name


def test_the_derived_pair_minimum_is_the_documented_arithmetic():
    parsed = protocol_table()
    assert parsed["MIN_HELDOUT_PAIRS"]["derivation"] == "60 x 5"
    assert (comparison.MIN_HELDOUT_PAIRS == comparison.MIN_HELDOUT_QUERIES
            * comparison.RATED_CANDIDATES_PER_QUERY == 300)


def test_minimum_constants_are_gates_and_targets_never_gate():
    classes = threshold_classes()
    assert classes["MIN_PAIRWISE_ACCURACY"] == "minimum"
    assert classes["TARGET_PAIRWISE_ACCURACY"] == "target"
    gated = {name for name, (value, kind) in comparison.FROZEN_CONSTANTS.items()
             if kind == "minimum"}
    assert (set(comparison.GATE_ARMS) | set(comparison.OUT_OF_SCOPE_GATES)
            | set(comparison.CONDITIONAL_GATES)) >= gated
    for name in comparison.GATE_ARMS:
        assert comparison.FROZEN_CONSTANTS[name][1] == "minimum", name
        assert comparison.GATE_ARMS[name], name
    for name in comparison.OUT_OF_SCOPE_GATES:
        assert comparison.FROZEN_CONSTANTS[name][1] in ("minimum", "target", "rule"), name


def test_the_report_contract_lists_the_sections_in_the_required_order():
    assert list(comparison.REPORT_SECTIONS) == list(SECTIONS)
    docstring = ast.get_docstring(ast.parse(MODULE.read_text(encoding="utf-8")))
    for phrase in ("exit 2, nothing written", "exit 1, report written", "exit 0",
                   "protocol_version_mismatch", "synthetic_fixture_record",
                   "pair_counts_below_minimum"):
        assert phrase in docstring, phrase


def test_a_foreign_protocol_version_is_refused_with_exit_two(tmp_path):
    path = tmp_path / "protocol-v2.md"
    path.write_text(protocol_text().replace("tera-eval-protocol-v1", "tera-eval-protocol-v2"),
                    encoding="utf-8")
    arguments = ["report", "--protocol-doc", str(path)]
    for flag in ("--dataset", "--split-manifest", "--pairs", "--assignment", "--sessions",
                 "--analysis-manifest", "--report", "--runs"):
        arguments += [flag, str(tmp_path / "absent")]
    stream = io.StringIO()
    assert comparison.main(arguments, stdout=stream) == 2
    assert not (tmp_path / "absent").exists()


def test_the_runner_compares_against_its_own_declared_constant_names():
    source = MODULE.read_text(encoding="utf-8")
    for name in ("MIN_PAIRWISE_ACCURACY", "MIN_LIFT_OVER_DSP", "MIN_LIFT_OVER_RANDOM",
                 "MIN_TOP1_MARGIN", "MIN_DSP_PAIRWISE_ACCURACY",
                 "MIN_JEV_ONLY_PAIRWISE_ACCURACY", "MAX_COLD_RECOMMENDATION_P95_MS",
                 "MAX_WARM_RECOMMENDATION_P95_MS"):
        assert ("\"" + name + "\"") in source, name
