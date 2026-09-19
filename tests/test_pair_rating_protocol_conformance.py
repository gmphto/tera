"""The utility proves it implements the frozen #16 protocol, and fails closed when it cannot.

The field names below are extracted from _docs/evaluation-protocol.md by this file, not from
the utility, so the assertions fail if either side drifts.
"""

import json
import re
from pathlib import Path

import pytest

from backend.evaluation import rating
from tests.test_pair_rating_session import build_workspace, run, session_directory, start_arguments


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = REPOSITORY_ROOT / "_docs" / "evaluation-protocol.md"
MARK = "\N{GRAVE ACCENT}"
FOUR_LABELS = ("poor", "acceptable", "good", "excellent")
FOUR_SKIP_REASONS = ("failed-playback", "cannot-judge", "recognised", "other")
PROSE_FIELD_NAMES = {"split-manifest digest": "split_manifest_digest",
                     "monitoring description": "monitoring_description"}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return build_workspace(tmp_path)


def protocol_text():
    return PROTOCOL.read_text(encoding="utf-8")


def section(text, heading):
    lines, collecting = [], False
    for line in text.splitlines():
        if line.strip().startswith("#"):
            if collecting:
                break
            collecting = line.strip() == heading
            continue
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def frozen_constant(text, name):
    for line in section(text, "### Frozen constants").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == MARK + name + MARK:
            return cells[1].strip(MARK)
    return None


def protocol_labels(text):
    names = []
    for line in section(text, "## Rating scale and anchors").splitlines():
        match = re.match(r"\|\s*" + MARK + r"([a-z-]+)" + MARK + r"\s*\|", line)
        if match:
            names.append(match.group(1))
    return tuple(names)


def protocol_skip_reasons(text):
    clause = section(text, "## Rating scale and anchors").split(
        "a reason from the fixed set", 1)[1].split("It is never a rating", 1)[0]
    return tuple(re.findall(MARK + r"([a-z-]+)" + MARK, clause))


def evidence_field_names(text):
    body = section(text, "## Evidence #17 must record")
    session_part = body.split("Session record fields:", 1)[1].split("Rating record fields:", 1)[0]
    rating_part = body.split("Rating record fields:", 1)[1].split("Rules:", 1)[0]
    session = set(re.findall(MARK + r"([a-z_]+)" + MARK, session_part))
    for phrase, name in PROSE_FIELD_NAMES.items():
        if phrase in session_part:
            session.add(name)
    return session, set(re.findall(MARK + r"([a-z_]+)" + MARK, rating_part))


def test_the_protocol_document_exists_and_declares_the_supported_version():
    assert PROTOCOL.is_file()
    assert frozen_constant(protocol_text(), "PROTOCOL_VERSION") == "tera-eval-protocol-v1"
    assert 'PROTOCOL_VERSION = "tera-eval-protocol-v1"' in protocol_text()
    assert rating.SUPPORTED_PROTOCOL_VERSION == "tera-eval-protocol-v1"


def test_the_module_constants_equal_the_protocol_values():
    text = protocol_text()
    assert rating.SUPPORTED_PROTOCOL_VERSION == frozen_constant(text, "PROTOCOL_VERSION")
    assert tuple(rating.LABELS) == protocol_labels(text) == FOUR_LABELS
    assert tuple(rating.SKIP_REASONS) == protocol_skip_reasons(text) == FOUR_SKIP_REASONS
    assert rating.ORDER_SEED == frozen_constant(text, "ORDER_SEED") == "tera-eval-order-16-v1"
    assert rating.MAX_RECOGNISED_RATE == float(frozen_constant(text, "MAX_RECOGNISED_RATE")) == 0.20


def test_the_evidence_section_names_are_the_export_schema(workspace):
    session_names, rating_names = evidence_field_names(protocol_text())
    assert session_names == set(rating.SESSION_FIELDS)
    assert rating_names == set(rating.RATING_FIELDS)
    assert run(start_arguments(workspace), "1\n2\nq\n") == 3
    directory = session_directory(workspace, "session-one")
    assert rating.main(["export", "--session", str(directory)]) == 0
    exported = json.loads((directory / "export.json").read_text(encoding="utf-8"))
    assert set(exported) == {"schema_version", "session", "ratings"}
    assert set(exported["session"]) == session_names
    assert all(set(record) == rating_names for record in exported["ratings"])


def test_the_loaded_protocol_carries_the_document_values_and_the_instruction_text():
    protocol = rating.load_protocol(PROTOCOL)
    text = protocol_text()
    assert protocol.version == frozen_constant(text, "PROTOCOL_VERSION")
    assert protocol.order_seed == frozen_constant(text, "ORDER_SEED")
    assert protocol.max_recognised_rate == float(frozen_constant(text, "MAX_RECOGNISED_RATE"))
    assert protocol.instructions.startswith("**What you are doing.**")
    assert protocol.instructions.endswith("without repeating a completed answer.")


def test_start_fails_closed_without_a_usable_protocol_document(workspace, capsys):
    absent = workspace.tmp / "absent-protocol.md"
    assert run(start_arguments(workspace) + ["--protocol-doc", str(absent)], "q\n") == 2
    assert "protocol_version_mismatch" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()
    other = workspace.tmp / "other-version.md"
    other.write_text(protocol_text().replace("tera-eval-protocol-v1", "tera-eval-protocol-v2"),
                     encoding="utf-8")
    assert run(start_arguments(workspace) + ["--protocol-doc", str(other)], "q\n") == 2
    assert "protocol_version_mismatch" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()


def test_resume_fails_closed_without_a_usable_protocol_document(workspace, capsys):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    arguments = ["resume", "--session", str(session_directory(workspace, "session-one")),
                 "--dataset", str(workspace.dataset), "--playback-backend", "fake",
                 "--protocol-doc", str(workspace.tmp / "absent-protocol.md")]
    assert run(arguments, "2\n") == 2
    assert "protocol_version_mismatch" in capsys.readouterr().err


def test_a_document_without_order_seed_or_with_another_order_seed_fails_closed(workspace, capsys):
    stripped = [line for line in protocol_text().splitlines()
                if not line.startswith("| " + MARK + "ORDER_SEED" + MARK + " |")]
    without = workspace.tmp / "without-order-seed.md"
    without.write_text("\n".join(stripped) + "\n", encoding="utf-8")
    assert run(start_arguments(workspace) + ["--protocol-doc", str(without)], "q\n") == 2
    assert "protocol_constants_missing" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()

    replaced = workspace.tmp / "other-order-seed.md"
    replaced.write_text(protocol_text().replace(MARK + "tera-eval-order-16-v1" + MARK,
                                                MARK + "synthetic-order-seed" + MARK), encoding="utf-8")
    assert rating.protocol_constants(replaced.read_text(encoding="utf-8"))["ORDER_SEED"] \
        == "synthetic-order-seed"
    with pytest.raises(rating.RatingError):
        rating.load_protocol(replaced)
    assert run(start_arguments(workspace) + ["--protocol-doc", str(replaced)], "q\n") == 2
    assert "protocol_constants_missing" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()
