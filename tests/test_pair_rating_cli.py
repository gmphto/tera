"""The scripted CLI dry run: labels, skips, correction, resume, refusals and export.

Every child process is driven with file-backed stdio because piped stdio is unavailable
under the confined test sandbox; cwd and PYTHONPATH point at the temporary workspace and
the repository root.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.evaluation import rating
from tests.test_pair_rating_session import (DATASET_VERSION, PACK_NAMES, REPOSITORY_ROOT, WAV_NAMES,
                                            build_workspace, pairs_document, tone, write_pool)
from tests.test_audio import wav, write


SAMPLE_IDS = ("sample-kick-01", "sample-kick-02", "sample-kick-03",
              "sample-bass-01", "sample-bass-02", "sample-bass-03")
PAIR_IDS = tuple(f"synth-pair-{number:02d}" for number in range(1, 7))
FILE_NAMES = ("dataset.json", "pairs.json", "session.json", "answers.jsonl", "export.json",
              "instructions.txt")
FAIL_FIRST_PLAYER = """
import pathlib, sys
counter = pathlib.Path(sys.argv[1])
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
print("player-stdout-canary")
print("player-stderr-canary", file=sys.stderr)
raise SystemExit(1 if count == 0 else 0)
"""
DELETE_FIRST_PLAYER = """
import pathlib, sys
counter = pathlib.Path(sys.argv[1])
victim = pathlib.Path(sys.argv[2])
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
if count == 0:
    victim.unlink()
raise SystemExit(0)
"""
QUIET_PLAYER = """
import sys
print("player-stdout-canary")
print("player-stderr-canary", file=sys.stderr)
raise SystemExit(0)
"""


@pytest.fixture
def workspace(tmp_path):
    return build_workspace(tmp_path)


def session_path(workspace, session_id):
    return workspace.root / "sessions" / session_id


def cli(workspace, arguments, script="", name="run"):
    """Run the CLI in the workspace with file-backed stdin, stdout and stderr."""
    source = workspace.tmp / (name + "-stdin.txt")
    source.write_text(script, encoding="utf-8")
    output, error = workspace.tmp / (name + "-stdout.txt"), workspace.tmp / (name + "-stderr.txt")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(workspace.tmp), str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")])
    command = [sys.executable, "-m", "backend.evaluation.rating"] + [str(item) for item in arguments]
    with source.open(encoding="utf-8") as stdin, output.open("w", encoding="utf-8") as stdout, \
            error.open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                   cwd=str(workspace.tmp), env=environment, timeout=600)
    return completed.returncode, output.read_text(encoding="utf-8"), error.read_text(encoding="utf-8")


def start(workspace, session_id="cli-one", evaluator="cli-evaluator", backend="fake",
          monitoring="dry-run synthetic fixture", player=None, pairs=None, dataset=None):
    arguments = ["start", "--dataset", str(dataset if dataset is not None else workspace.dataset),
                 "--pairs", str(pairs if pairs is not None else workspace.pairs),
                 "--evaluator-id", evaluator, "--monitoring", monitoring,
                 "--session-id", session_id, "--playback-backend", backend]
    if player is not None:
        arguments += ["--player-command", player]
    return arguments


def resume(workspace, session_id, backend="fake", player=None):
    arguments = ["resume", "--session", str(session_path(workspace, session_id)),
                 "--dataset", str(workspace.dataset), "--playback-backend", backend]
    if player is not None:
        arguments += ["--player-command", player]
    return arguments


def player_command(workspace, body, name, extra=()):
    path = workspace.tmp / name
    path.write_text(body, encoding="utf-8")
    parts = ["'" + sys.executable + "'", "'" + str(path) + "'"]
    parts += ["'" + str(item) + "'" for item in extra]
    parts.append("{audio}")
    return " ".join(parts)


def records(workspace, session_id):
    text = (session_path(workspace, session_id) / "answers.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def last_line(text):
    return json.loads(text.strip().splitlines()[-1])


def assert_no_canaries(workspace, *transcripts, pair_ids=True):
    canaries = (*SAMPLE_IDS, *WAV_NAMES, *PACK_NAMES, DATASET_VERSION, *FILE_NAMES,
                str(workspace.tmp), str(workspace.audio), str(workspace.root), "player-stdout-canary",
                "player-stderr-canary")
    if pair_ids:
        canaries += PAIR_IDS
    for canary in canaries:
        for transcript in transcripts:
            assert canary not in transcript, canary


# --- the scripted dry run --------------------------------------------------

def test_help_lists_exactly_the_five_subcommands(workspace):
    code, out, err = cli(workspace, ["--help"], "", "help")
    assert code == 0, err
    assert "{start,resume,status,export,validate}" in out
    assert err == ""


def test_scripted_dry_run_labels_flag_correction_pause_resume_export_and_validate(workspace):
    transcripts = []
    code, out, err = cli(workspace, start(workspace), "1\nk\n2\n3\n4\n?\nc 2\n1\nq\n", "start")
    transcripts += [out, err]
    assert code == 3, err
    assert "answered 4 of 6" in out
    assert out.count("**What you are doing.**") == 2     # shown before the first presentation and by ?
    session = session_path(workspace, "cli-one")
    assert session.is_relative_to(workspace.tmp)
    written = records(workspace, "cli-one")
    assert [record["presentation_index"] for record in written] == [1, 2, 3, 4, 2]
    assert [record["rating"] for record in written] == ["poor", "acceptable", "good", "excellent", "poor"]
    assert written[1]["recognised"] is True and written[1]["skip"] is False
    assert written[2]["recognised"] is False
    assert written[4]["correction_of"] == 2 and written[0]["correction_of"] is None

    code, out, err = cli(workspace, resume(workspace, "cli-one"), "3\n4\n", "resume")
    transcripts += [out, err]
    assert code == 0, err
    written = records(workspace, "cli-one")
    assert [record["presentation_index"] for record in written] == [1, 2, 3, 4, 2, 5, 6]
    assert {record["rating"] for record in written} == {"poor", "acceptable", "good", "excellent"}
    summary = last_line(out)
    # The correction supersedes the flagged answer, so the effective counts stay clean.
    assert (summary["state"], summary["answered"], summary["skipped"], summary["recognised"],
            summary["failed_playback"], summary["next_presentation_index"]) == \
        ("complete", 6, 0, 0, 0, None)
    assert summary["recognised_rate_exceeded"] is False
    assert not any((session / "playback").iterdir())

    code, out, err = cli(workspace, ["status", "--session", str(session)], "", "status")
    transcripts += [out, err]
    assert code == 0
    assert set(last_line(out)) == {"session_id", "evaluator_id", "state", "presentations", "answered",
                                   "skipped", "recognised", "failed_playback", "next_presentation_index"}

    code, out, err = cli(workspace, ["export", "--session", str(session)], "", "export")
    transcripts += [out, err]
    assert code == 0, err
    first = (session / "export.json").read_bytes()
    code, out, err = cli(workspace, ["export", "--session", str(session)], "", "export-again")
    transcripts += [out, err]
    assert code == 0 and (session / "export.json").read_bytes() == first
    exported = json.loads(first)
    assert set(exported) == {"schema_version", "session", "ratings"}
    assert [record["correction_of"] for record in exported["ratings"]][4] == 2

    code, out, err = cli(workspace, ["validate", "--session", str(session)], "", "validate")
    transcripts += [out, err]
    assert code == 0 and last_line(out) == {"valid": True, "violations": []}
    assert_no_canaries(workspace, *transcripts)


def test_scripted_dry_run_records_all_four_skip_reasons_and_exits_one(workspace):
    player = player_command(workspace, FAIL_FIRST_PLAYER, "fail-first.py",
                            extra=(workspace.tmp / "fail-first.marker",))
    code, out, err = cli(workspace, start(workspace, "cli-skips", evaluator="cli-skips-evaluator",
                                          backend="command", monitoring="studio monitors", player=player),
                         "s cannot-judge\ns recognised\ns other\n1\n2\n", "skips")
    assert code == 1, err
    written = records(workspace, "cli-skips")
    assert [record["skip_reason"] for record in written] == \
        ["failed-playback", "cannot-judge", "recognised", "other", None, None]
    assert [record["rating"] for record in written] == [None, None, None, None, "poor", "acceptable"]
    assert (written[0]["skip"], written[0]["playback_completed"]) == (True, False)
    assert [record["playback_completed"] for record in written[1:]] == [True] * 5
    assert written[2]["recognised"] is True
    summary = last_line(out)
    assert (summary["state"], summary["failed_playback"], summary["skipped"], summary["recognised"]) == \
        ("complete", 1, 4, 1)
    assert_no_canaries(workspace, out, err)


def test_command_backend_records_a_completed_playback_and_discards_the_player(workspace):
    player = player_command(workspace, QUIET_PLAYER, "quiet.py")
    code, out, err = cli(workspace, start(workspace, "cli-command", evaluator="cli-command-evaluator",
                                          backend="command", monitoring="studio monitors", player=player),
                         "1\nq\n", "command")
    assert code == 3, err
    first = records(workspace, "cli-command")[0]
    assert (first["rating"], first["skip"], first["playback_completed"]) == ("poor", False, True)
    assert_no_canaries(workspace, out, err)


def test_file_removed_mid_session_is_recorded_as_failed_playback(workspace):
    subset = dict(workspace.pairs_document, pairs=workspace.pairs_document["pairs"][:3])
    pairs_path = workspace.tmp / "pairs-three.json"
    pairs_path.write_text(json.dumps(subset), encoding="utf-8")
    order = rating.presentation_order(subset["pairs"], rating.ORDER_SEED, "cli-removed")
    chosen = next(record for record in workspace.dataset_document["selected"]
                  if record["sample_id"] == order[1]["kick_sample_id"])
    victim = workspace.audio / chosen["mapping"]["path"]
    player = player_command(workspace, DELETE_FIRST_PLAYER, "deleter.py",
                            extra=(workspace.tmp / "deleter.marker", victim))
    code, out, err = cli(workspace, start(workspace, "cli-removed", evaluator="cli-removed-evaluator",
                                          backend="command", monitoring="studio monitors",
                                          player=player, pairs=pairs_path), "1\n2\n", "removed")
    assert code == 1, err
    assert not victim.exists()
    written = records(workspace, "cli-removed")
    assert [record["presentation_index"] for record in written] == [1, 2, 3]
    assert (written[1]["rating"], written[1]["skip"], written[1]["skip_reason"],
            written[1]["playback_completed"]) == (None, True, "failed-playback", False)
    summary = last_line(out)
    assert (summary["state"], summary["failed_playback"], summary["answered"]) == ("complete", 1, 3)
    assert not any((session_path(workspace, "cli-removed") / "playback").iterdir())


@pytest.mark.parametrize("case,expected", [
    ("missing", "audio_unreadable: synth-pair-03"),
    ("corrupt", "audio_unreadable: synth-pair-02"),
    ("duplicate", "duplicate_pair_id: synth-pair-01"),
    ("rate-mismatch", "sample_rate_mismatch: synth-pair-03"),
])
def test_preflight_refusals_exit_two_without_records(workspace, case, expected):
    pairs_path = workspace.pairs
    if case == "missing":
        (workspace.audio / "kick-03.wav").unlink()
    elif case == "corrupt":
        write(workspace.audio, b"RIFF this is not a decodable wave file", "bass-02.wav")
    elif case == "duplicate":
        document = workspace.pairs_document
        document["pairs"].append(dict(document["pairs"][0]))
        pairs_path = workspace.tmp / "pairs-duplicate.json"
        pairs_path.write_text(json.dumps(document), encoding="utf-8")
    else:
        write(workspace.audio, wav(tone(90.0, rate=44100), 44100, "FLOAT"), "bass-03.wav")
    code, out, err = cli(workspace, start(workspace, "cli-refused", pairs=pairs_path), "q\n", case)
    assert code == 2
    assert expected in err
    assert not session_path(workspace, "cli-refused").exists()
    assert not (workspace.root / "sessions").exists()
    assert_no_canaries(workspace, out, err, pair_ids=False)

