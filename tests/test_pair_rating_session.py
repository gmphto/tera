"""Session lifecycle, ordering, render, resume and validation for the pair-rating utility."""

import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend import audio
from backend.evaluation import playback
from backend.evaluation import rating
from backend.evaluation.manifest import canonical
from tests.test_audio import wav, write


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "pair-rating"
SAMPLE_RATE = 48000
KICK_TONES = {"kick-01": 60.0, "kick-02": 80.0, "kick-03": 70.0}
BASS_TONES = {"bass-01": 110.0, "bass-02": 140.0, "bass-03": 90.0}
DATASET_VERSION = "synthetic-fixture-pool-01"
PACK_NAMES = ("fixture-kick-pack", "fixture-bass-pack")
WAV_NAMES = tuple(name + ".wav" for name in (*KICK_TONES, *BASS_TONES))
SESSION_FIELDS = ("session_id", "evaluator_id", "state", "presentations", "answered", "skipped",
                  "recognised", "failed_playback", "next_presentation_index")


def tone(frequency, frames=4800, rate=SAMPLE_RATE, amplitude=0.5):
    time = np.arange(frames) / rate
    return np.column_stack([amplitude * np.sin(2 * np.pi * frequency * time)])


def write_pool(directory, rate=SAMPLE_RATE, skip=(), channels=1):
    for name, frequency in {**KICK_TONES, **BASS_TONES}.items():
        if name in skip:
            continue
        samples = tone(frequency, rate=rate)
        if channels == 2:
            samples = np.repeat(samples, 2, axis=1)
        write(directory, wav(samples, rate, "FLOAT"), name + ".wav")


def pairs_document():
    return json.loads((FIXTURES / "pairs.json").read_text(encoding="utf-8"))


def dataset_document(audio_directory):
    document = json.loads((FIXTURES / "dataset.template.json").read_text(encoding="utf-8"))
    document["sources"]["fixture_pool"]["root"] = str(audio_directory)
    return document


def sample_ids(document, role):
    return [record["sample_id"] for record in document["selected"] if record["role"] == role]


def build_workspace(base):
    """Synthetic audio plus the two private documents inside one candidate cwd."""
    audio_directory = base / "audio"
    audio_directory.mkdir(parents=True, exist_ok=True)
    write_pool(audio_directory)
    dataset = dataset_document(audio_directory)
    dataset_path = base / "dataset.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    pairs_path = base / "pairs.json"
    pairs_path.write_text(json.dumps(pairs_document()), encoding="utf-8")
    return SimpleNamespace(tmp=base, audio=audio_directory, dataset=dataset_path, pairs=pairs_path,
                           root=base / ".local-evaluation" / "pair-rating",
                           dataset_document=dataset, pairs_document=pairs_document())


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A private cwd holding synthetic audio, the two private documents and the private root."""
    monkeypatch.chdir(tmp_path)
    return build_workspace(tmp_path)


def session_directory(workspace, session_id):
    return workspace.root / "sessions" / session_id


def session_record(workspace, session_id):
    return json.loads((session_directory(workspace, session_id) / "session.json").read_text(encoding="utf-8"))


def journal_records(workspace, session_id):
    text = (session_directory(workspace, session_id) / "answers.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def start_arguments(workspace, session_id="session-one", evaluator="evaluator-one", backend="fake",
                    monitoring="dry-run synthetic fixture", player=None, pairs=None):
    arguments = ["start", "--dataset", str(workspace.dataset),
                 "--pairs", str(pairs if pairs is not None else workspace.pairs),
                 "--evaluator-id", evaluator, "--monitoring", monitoring,
                 "--session-id", session_id, "--playback-backend", backend]
    if player is not None:
        arguments += ["--player-command", player]
    return arguments


def resume_arguments(workspace, session_id, backend="fake", player=None, dataset=None):
    arguments = ["resume", "--session", str(session_directory(workspace, session_id)),
                 "--dataset", str(dataset if dataset is not None else workspace.dataset),
                 "--playback-backend", backend]
    if player is not None:
        arguments += ["--player-command", player]
    return arguments


def run(arguments, script=""):
    return rating.main(arguments, stdin=io.StringIO(script))


def player_command(code):
    import sys
    return "'" + sys.executable + "' -c \"" + code + "\" {audio}"


# --- order, render and playback backends -----------------------------------

def test_presentation_order_is_reproducible_and_a_permutation(workspace):
    pairs = workspace.pairs_document["pairs"]
    first = rating.presentation_order(pairs, rating.ORDER_SEED, "session-one")
    second = rating.presentation_order(pairs, rating.ORDER_SEED, "session-one")
    assert [pair["pair_id"] for pair in first] == [pair["pair_id"] for pair in second]
    assert sorted(pair["pair_id"] for pair in first) == sorted(pair["pair_id"] for pair in pairs)
    assert len({pair["pair_id"] for pair in first}) == len(pairs)


def test_order_repair_separates_shared_kicks_whenever_a_swap_exists():
    pairs = [{"pair_id": "pair-1", "kick_sample_id": "kick-a", "bass_sample_id": "bass-1"},
             {"pair_id": "pair-2", "kick_sample_id": "kick-a", "bass_sample_id": "bass-2"},
             {"pair_id": "pair-3", "kick_sample_id": "kick-b", "bass_sample_id": "bass-3"}]
    for number in range(60):
        kicks = [pair["kick_sample_id"] for pair in
                 rating.presentation_order(pairs, rating.ORDER_SEED, f"repair-{number}")]
        assert sum(first == second for first, second in zip(kicks, kicks[1:])) == 0


def seeded_order(pairs, session_id):
    """The documented pre-repair order, recomputed without calling the module."""
    ordered = sorted(pairs, key=lambda pair: pair["pair_id"])
    keys = {pair["pair_id"]: hashlib.sha256(
        (rating.ORDER_SEED + "\n" + session_id + "\n" + pair["pair_id"]).encode("utf-8")).hexdigest()
        for pair in ordered}
    return sorted(ordered, key=lambda pair: (keys[pair["pair_id"]], pair["pair_id"]))


def test_order_repair_leaves_an_unavoidable_shared_kick_adjacent():
    pairs = [{"pair_id": "pair-1", "kick_sample_id": "kick-a", "bass_sample_id": "bass-1"},
             {"pair_id": "pair-2", "kick_sample_id": "kick-a", "bass_sample_id": "bass-2"},
             {"pair_id": "pair-3", "kick_sample_id": "kick-a", "bass_sample_id": "bass-3"}]
    order = rating.presentation_order(pairs, rating.ORDER_SEED, "adjacent")
    assert [pair["kick_sample_id"] for pair in order] == ["kick-a", "kick-a", "kick-a"]
    assert [pair["pair_id"] for pair in order] == \
        [pair["pair_id"] for pair in seeded_order(pairs, "adjacent")]


def test_render_rule_is_alignment_padding_gain_and_a_lossless_float_wav(tmp_path):
    kick_path, bass_path, destination = tmp_path / "kick.wav", tmp_path / "bass.wav", tmp_path / "render.wav"
    kick = tone(60.0, frames=100)
    bass = np.repeat(tone(110.0, frames=60), 2, axis=1)
    write(tmp_path, wav(kick, SAMPLE_RATE, "FLOAT"), "kick.wav")
    write(tmp_path, wav(bass, SAMPLE_RATE, "FLOAT"), "bass.wav")
    before = (kick_path.read_bytes(), bass_path.read_bytes())
    kick = audio.load_wav(kick_path).samples
    bass = audio.load_wav(bass_path).samples
    playback.render_pair(kick_path, bass_path, destination)
    expected = np.zeros((100, 2), dtype=np.float64)
    expected[:] += np.repeat(kick, 2, axis=1)
    expected[:60] += bass
    expected *= 10 ** (playback.PLAYBACK_GAIN_DB / 20)
    loaded = audio.load_wav(destination)
    assert (loaded.sample_rate_hz, loaded.channels, loaded.frame_count) == (SAMPLE_RATE, 2, 100)
    assert loaded.subtype == playback.RENDER_SUBTYPE
    np.testing.assert_array_equal(loaded.samples, expected)
    assert (kick_path.read_bytes(), bass_path.read_bytes()) == before
    assert playback.PLAYBACK_GAIN_DB == -6.0


def test_render_refuses_two_sample_rates_and_keeps_mono_mono(tmp_path):
    kick_path, bass_path, destination = tmp_path / "kick.wav", tmp_path / "bass.wav", tmp_path / "render.wav"
    write(tmp_path, wav(tone(60.0, frames=10), SAMPLE_RATE, "FLOAT"), "kick.wav")
    bass_path = write(tmp_path, wav(tone(110.0, frames=10), 44100, "FLOAT"), "bass.wav")
    with pytest.raises(playback.PlaybackError):
        playback.render_pair(kick_path, bass_path, destination)
    write(tmp_path, wav(tone(110.0, frames=10), SAMPLE_RATE, "FLOAT"), "bass.wav")
    playback.render_pair(kick_path, bass_path, destination)
    assert audio.load_wav(destination).channels == 1


def test_command_backend_reports_completion_only_on_a_zero_exit(tmp_path):
    render = write(tmp_path, wav(tone(60.0, frames=10), SAMPLE_RATE, "FLOAT"), "render.wav")
    missing = "'" + str(tmp_path / "absent-player.exe") + "' {audio}"
    assert playback.play("command", render, player_command("import sys; sys.exit(0)")) is True
    assert playback.play("command", render, player_command("import sys; sys.exit(3)")) is False
    assert playback.play("command", render, missing) is False
    assert playback.play("fake", render, None) is True


def test_command_backend_reports_a_timeout_as_incomplete(tmp_path, monkeypatch):
    render = write(tmp_path, wav(tone(60.0, frames=10), SAMPLE_RATE, "FLOAT"), "render.wav")
    monkeypatch.setattr(playback, "PLAYBACK_TIMEOUT_SECONDS", 0.5)
    assert playback.play("command", render, player_command("import time; time.sleep(10)")) is False


def test_fake_backend_needs_a_dry_run_monitoring_description(workspace, capsys):
    code = run(start_arguments(workspace, monitoring="listening on monitors"), "q\n")
    assert code == 2
    assert "invalid_monitoring" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()
    assert run(start_arguments(workspace, monitoring=""), "q\n") == 2
    assert not session_directory(workspace, "session-one").exists()


def test_command_backend_needs_the_audio_placeholder(workspace, capsys):
    code = run(start_arguments(workspace, backend="command", monitoring="studio monitors",
                               player="play --file"), "q\n")
    assert code == 2
    assert "invalid_player_command" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()


def test_invalid_identifiers_are_refused(workspace, capsys):
    assert run(start_arguments(workspace, evaluator="producer@example.com"), "q\n") == 2
    assert "invalid_evaluator_id" in capsys.readouterr().err
    assert run(start_arguments(workspace, evaluator="Producer One"), "q\n") == 2
    assert "invalid_evaluator_id" in capsys.readouterr().err
    assert run(start_arguments(workspace, evaluator="producer/one"), "q\n") == 2
    assert "invalid_evaluator_id" in capsys.readouterr().err
    assert run(start_arguments(workspace, session_id="Bad/Session"), "q\n") == 2
    assert "invalid_session_id" in capsys.readouterr().err
    assert not (workspace.root / "sessions").exists()


# --- session lifecycle, durability and resume ------------------------------

def test_start_persists_the_contract_fields_and_the_order(workspace):
    assert run(start_arguments(workspace), "q\n") == 3
    document = session_record(workspace, "session-one")
    assert set(document) == set(rating.SESSION_DOCUMENT_FIELDS.split())
    assert document["protocol_version"] == rating.SUPPORTED_PROTOCOL_VERSION
    assert document["order_seed"] == rating.ORDER_SEED
    assert document["playback_gain_db"] == playback.PLAYBACK_GAIN_DB == -6.0
    assert document["dataset_version"] == DATASET_VERSION
    assert document["split_manifest_digest"] == workspace.pairs_document["split_manifest_digest"]
    assert document["monitoring_description"] == "dry-run synthetic fixture"
    assert document["state"] == "in_progress" and document["finished_at"] is None
    assert rating.TIMESTAMP_PATTERN.fullmatch(document["started_at"])
    expected = rating.presentation_order(workspace.pairs_document["pairs"], rating.ORDER_SEED, "session-one")
    assert [item["pair_id"] for item in document["presentations"]] == [item["pair_id"] for item in expected]
    assert [item["presentation_index"] for item in document["presentations"]] == [1, 2, 3, 4, 5, 6]
    assert journal_records(workspace, "session-one") == []
    session = session_directory(workspace, "session-one")
    assert (session / "instructions.txt").read_text(encoding="utf-8").startswith("**What you are doing.**")
    assert (session / "playback").is_dir()


def test_two_sessions_with_the_same_inputs_persist_the_same_order(workspace):
    first = build_workspace(workspace.tmp / "one")
    second = build_workspace(workspace.tmp / "two")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(first.tmp)
        assert run(start_arguments(first), "q\n") == 3
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(second.tmp)
        assert run(start_arguments(second), "q\n") == 3
    assert session_record(first, "session-one")["presentations"] == \
        session_record(second, "session-one")["presentations"]


def test_all_four_labels_and_two_skips_and_the_close_summary(workspace, capsys):
    assert run(start_arguments(workspace), "1\nk\n2\ns cannot-judge\ns other\n3\n4\n") == 0
    records = journal_records(workspace, "session-one")
    assert [record["rating"] for record in records] == ["poor", "acceptable", None, None, "good", "excellent"]
    assert [record["skip_reason"] for record in records] == [None, None, "cannot-judge", "other", None, None]
    assert records[1]["recognised"] is True and records[0]["recognised"] is False
    for record in records:
        assert set(record) == set(rating.RATING_FIELDS)
        if record["rating"] is not None:
            assert record["skip"] is False and record["playback_completed"] is True
        if record["skip"]:
            assert record["rating"] is None and record["skip_reason"] in rating.SKIP_REASONS
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary == {"session_id": "session-one", "evaluator_id": "evaluator-one", "state": "complete",
                       "presentations": 6, "answered": 6, "skipped": 2, "recognised": 1,
                       "failed_playback": 0, "next_presentation_index": None,
                       "recognised_rate": 1 / 6, "recognised_rate_exceeded": False}
    document = session_record(workspace, "session-one")
    assert document["state"] == "complete" and rating.TIMESTAMP_PATTERN.fullmatch(document["finished_at"])


def test_skip_reason_not_applicable_after_a_completed_playback(workspace, capsys):
    assert run(start_arguments(workspace), "s failed-playback\ns cannot-judge\nq\n") == 3
    assert "skip_reason_not_applicable" in capsys.readouterr().out
    records = journal_records(workspace, "session-one")
    assert [record["skip_reason"] for record in records] == ["cannot-judge"]


def test_label_before_an_incomplete_playback_is_refused_and_the_skip_is_recorded(workspace, capsys, monkeypatch):
    calls = {"count": 0}
    def scripted(backend, render_path, player_command=None):
        calls["count"] += 1
        return calls["count"] != 2
    monkeypatch.setattr(playback, "play", scripted)
    assert run(start_arguments(workspace), "r\n3\ns failed-playback\n") == 3
    output = capsys.readouterr().out
    assert output.count("playback_incomplete") == 2
    records = journal_records(workspace, "session-one")
    assert len(records) == 1
    assert (records[0]["rating"], records[0]["skip"], records[0]["skip_reason"],
            records[0]["playback_completed"]) == (None, True, "failed-playback", False)


def test_playback_failure_is_recorded_and_the_session_continues(workspace, capsys, monkeypatch):
    calls = {"count": 0}
    def scripted(backend, render_path, player_command=None):
        calls["count"] += 1
        return calls["count"] != 1
    monkeypatch.setattr(playback, "play", scripted)
    assert run(start_arguments(workspace), "1\n2\n3\n1\n2\n") == 1
    records = journal_records(workspace, "session-one")
    assert len(records) == 6
    assert [record["presentation_index"] for record in records] == [1, 2, 3, 4, 5, 6]
    assert (records[0]["rating"], records[0]["skip"], records[0]["skip_reason"],
            records[0]["playback_completed"]) == (None, True, "failed-playback", False)
    document = session_record(workspace, "session-one")
    assert document["state"] == "complete" and document["finished_at"] is not None
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["state"] == "complete" and summary["failed_playback"] == 1


def test_resume_continues_at_the_first_unanswered_presentation(workspace, capsys):
    assert run(start_arguments(workspace), "1\n2\nq\n") == 3
    assert [record["presentation_index"] for record in journal_records(workspace, "session-one")] == [1, 2]
    assert session_record(workspace, "session-one")["finished_at"] is None
    assert run(resume_arguments(workspace, "session-one"), "3\n4\n1\n2\n") == 0
    records = journal_records(workspace, "session-one")
    assert [record["presentation_index"] for record in records] == [1, 2, 3, 4, 5, 6]
    assert len(records) == 6
    assert "answered 6 of 6" in capsys.readouterr().out
    assert run(resume_arguments(workspace, "session-one"), "") == 0
    assert len(journal_records(workspace, "session-one")) == 6


def test_ctrl_c_leaves_the_session_resumable(workspace, capsys):
    class Interrupting(io.StringIO):
        def readline(self):
            raise KeyboardInterrupt
    assert rating.main(start_arguments(workspace), stdin=Interrupting("")) == 130
    document = session_record(workspace, "session-one")
    assert document["state"] == "in_progress" and document["finished_at"] is None
    assert journal_records(workspace, "session-one") == []
    assert not (session_directory(workspace, "session-one") / ".lock").exists()


def test_correction_keeps_the_audit_trail(workspace, capsys):
    assert run(start_arguments(workspace), "1\n2\nc 1\n4\n3\n4\n1\n2\n") == 0
    records = journal_records(workspace, "session-one")
    assert len(records) == 7
    assert [record["presentation_index"] for record in records] == [1, 2, 1, 3, 4, 5, 6]
    assert [record["correction_of"] for record in records] == [None, None, 1, None, None, None, None]
    assert records[0]["rating"] == "poor" and records[2]["rating"] == "excellent"
    assert rating.effective_answers(records)[1] is records[2]
    assert rating.main(["status", "--session", str(session_directory(workspace, "session-one"))]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["answered"] == 6
    session = session_directory(workspace, "session-one")
    assert rating.main(["export", "--session", str(session)]) == 0
    assert len(json.loads((session / "export.json").read_text(encoding="utf-8"))["ratings"]) == 7
    assert rating.main(["validate", "--session", str(session)]) == 0


def test_evaluator_pair_already_rated_names_the_earlier_session(workspace, capsys):
    assert run(start_arguments(workspace, "session-one"), "1\nq\n") == 3
    answered = session_record(workspace, "session-one")["presentations"][0]["pair_id"]
    session = session_directory(workspace, "session-one")
    before = {path.name: path.read_bytes() for path in session.iterdir() if path.is_file()}
    assert run(start_arguments(workspace, "session-two"), "q\n") == 2
    error = capsys.readouterr().err
    assert "evaluator_pair_already_rated" in error
    assert "session-one" in error and answered in error
    assert not session_directory(workspace, "session-two").exists()
    after = {path.name: path.read_bytes() for path in session.iterdir() if path.is_file()}
    assert before == after


def test_a_second_session_over_disjoint_pairs_is_allowed(workspace):
    assert run(start_arguments(workspace, "session-one"), "1\nq\n") == 3
    answered = session_record(workspace, "session-one")["presentations"][0]["pair_id"]
    remaining = [pair for pair in workspace.pairs_document["pairs"] if pair["pair_id"] != answered]
    disjoint = dict(workspace.pairs_document, pairs=remaining)
    pairs_path = workspace.tmp / "pairs-disjoint.json"
    pairs_path.write_text(json.dumps(disjoint), encoding="utf-8")
    assert run(start_arguments(workspace, "session-two", pairs=pairs_path), "1\n2\n3\n4\n1\n") == 0
    assert len(journal_records(workspace, "session-two")) == 5
    assert session_record(workspace, "session-two")["evaluator_id"] == "evaluator-one"
    assert answered not in {item["pair_id"] for item in session_record(workspace, "session-two")["presentations"]}
    session = session_directory(workspace, "session-two")
    assert rating.main(["export", "--session", str(session)]) == 0
    assert len(json.loads((session / "export.json").read_text(encoding="utf-8"))["ratings"]) == 5


def test_session_locked_refuses_a_second_writer(workspace, capsys):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    (session_directory(workspace, "session-one") / ".lock").write_text("held\n", encoding="ascii")
    assert run(resume_arguments(workspace, "session-one"), "2\n") == 2
    assert "session_locked" in capsys.readouterr().err
    assert len(journal_records(workspace, "session-one")) == 1


def test_storage_failure_keeps_the_previous_journal_and_the_answer(workspace, capsys, monkeypatch):
    real = rating._atomic_replace
    def failing(source, destination):
        if str(destination).endswith(rating.JOURNAL_FILE) and Path(destination).exists():
            raise OSError("simulated")
        return real(source, destination)
    monkeypatch.setattr(rating, "_atomic_replace", failing)
    assert run(start_arguments(workspace), "1\n2\n") == 2
    assert "storage_failure" in capsys.readouterr().err
    session = session_directory(workspace, "session-one")
    assert (session / "answers.jsonl").read_text(encoding="utf-8") == ""
    assert session_record(workspace, "session-one")["finished_at"] is None
    monkeypatch.setattr(rating, "_atomic_replace", real)
    assert run(resume_arguments(workspace, "session-one"), "1\n2\n3\n4\n1\n2\n") == 0
    assert len(journal_records(workspace, "session-one")) == 6


def test_outside_private_root_is_refused(workspace, capsys):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    outside = workspace.tmp / "outside.json"
    assert rating.main(["status", "--session", str(workspace.tmp / "elsewhere")]) == 2
    assert "outside_private_root" in capsys.readouterr().err
    assert rating.main(["export", "--session", str(session_directory(workspace, "session-one")),
                        "--output", str(outside)]) == 2
    assert "outside_private_root" in capsys.readouterr().err
    assert not outside.exists()


def test_preflight_reports_every_violation_together(workspace, capsys):
    pairs = workspace.pairs_document
    pairs["pairs"][1]["kick_sample_id"] = "sample-kick-02"     # synth-pair-02: 44100 bass
    pairs["pairs"][2]["kick_sample_id"] = "sample-kick-99"     # synth-pair-03: unknown sample id
    pairs["pairs"][3]["kick_sample_id"] = "sample-bass-02"     # synth-pair-04: wrong role
    pairs["pairs"].append(dict(pairs["pairs"][0]))             # duplicate pair id
    pairs_path = workspace.tmp / "pairs-broken.json"
    pairs_path.write_text(json.dumps(pairs), encoding="utf-8")
    write(workspace.audio, wav(tone(140.0, rate=44100), 44100, "FLOAT"), "bass-02.wav")
    (workspace.audio / "kick-01.wav").unlink()                 # synth-pair-01: unreadable
    arguments = ["start", "--dataset", str(workspace.dataset), "--pairs", str(pairs_path),
                 "--evaluator-id", "evaluator-one", "--monitoring", "dry-run synthetic fixture",
                 "--session-id", "session-one", "--playback-backend", "fake"]
    assert run(arguments, "q\n") == 2
    error = capsys.readouterr().err
    assert "audio_unreadable: synth-pair-01" in error
    assert "sample_rate_mismatch: synth-pair-02" in error
    assert "unknown_sample_id: synth-pair-03" in error
    assert "role_mismatch: synth-pair-04" in error
    assert "duplicate_pair: synth-pair-01" in error
    assert str(workspace.audio) not in error and ".wav" not in error
    assert not session_directory(workspace, "session-one").exists()


def test_documents_that_do_not_match_the_schema_are_refused(workspace, capsys):
    pairs = workspace.pairs_document
    pairs["unexpected_field"] = True
    extra = workspace.tmp / "pairs-extra-field.json"
    extra.write_text(json.dumps(pairs), encoding="utf-8")
    assert run(start_arguments(workspace, pairs=extra), "q\n") == 2
    assert "schema_mismatch: pair_list" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()

    dataset = workspace.dataset_document
    dataset.pop("dataset_version")
    other = workspace.tmp / "dataset-no-version.json"
    other.write_text(json.dumps(dataset), encoding="utf-8")
    arguments = start_arguments(workspace)
    arguments[arguments.index(str(workspace.dataset))] = str(other)
    assert run(arguments, "q\n") == 2
    assert "schema_mismatch: dataset.dataset_version" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()


def test_a_pair_list_with_a_bad_split_manifest_digest_is_refused(workspace, capsys):
    pairs = workspace.pairs_document
    pairs["split_manifest_digest"] = "sha256:not-a-digest"
    pairs_path = workspace.tmp / "pairs-bad-digest.json"
    pairs_path.write_text(json.dumps(pairs), encoding="utf-8")
    assert run(start_arguments(workspace, pairs=pairs_path), "q\n") == 2
    error = capsys.readouterr().err
    assert "split_manifest_digest_invalid: pair_list.split_manifest_digest" in error
    assert str(workspace.audio) not in error
    assert not session_directory(workspace, "session-one").exists()


def test_status_reports_counts_only(workspace, capsys):
    assert run(start_arguments(workspace), "1\nk\n2\ns recognised\nq\n") == 3
    session = session_directory(workspace, "session-one")
    assert rating.main(["status", "--session", str(session)]) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(summary) == {"session_id", "evaluator_id", "state", "presentations", "answered",
                            "skipped", "recognised", "failed_playback", "next_presentation_index"}
    assert (summary["presentations"], summary["answered"], summary["skipped"], summary["recognised"],
            summary["failed_playback"], summary["next_presentation_index"], summary["state"]) == \
        (6, 3, 1, 2, 0, 4, "in_progress")


def test_export_is_deterministic_and_private(workspace, capsys):
    assert run(start_arguments(workspace), "1\n2\nq\n") == 3
    session = session_directory(workspace, "session-one")
    assert rating.main(["export", "--session", str(session)]) == 0
    first = (session / "export.json").read_bytes()
    assert rating.main(["export", "--session", str(session)]) == 0
    assert (session / "export.json").read_bytes() == first
    exported = json.loads(first)
    assert set(exported) == {"schema_version", "session", "ratings"}
    assert set(exported["session"]) == set(rating.SESSION_FIELDS)
    assert exported["session"]["finished_at"] is None
    assert all(set(record) == set(rating.RATING_FIELDS) for record in exported["ratings"])
    text = first.decode("utf-8")
    for canary in (str(workspace.tmp), str(workspace.audio), ".wav", "dataset.json", "pairs.json",
                   "sampler_seed", "assignment_seed", "fixture-kick-pack", "fixture-bass-pack"):
        assert canary not in text
    assert rating.main(["validate", "--session", str(session)]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"valid": True, "violations": []}


def test_validate_names_missing_renamed_extra_and_correction_violations(workspace, capsys):
    assert run(start_arguments(workspace), "1\n2\nq\n") == 3
    session = session_directory(workspace, "session-one")

    def violations():
        capsys.readouterr()
        code = rating.main(["validate", "--session", str(session)])
        return code, json.loads(capsys.readouterr().out.strip())

    code, report = violations()
    assert code == 1 and report["valid"] is False
    assert [item["code"] for item in report["violations"]] == ["export_missing"]

    assert rating.main(["export", "--session", str(session)]) == 0
    code, report = violations()
    assert code == 0 and report == {"valid": True, "violations": []}

    document = json.loads((session / "session.json").read_text(encoding="utf-8"))
    document["monitoring"] = document.pop("monitoring_description")
    (session / "session.json").write_text(json.dumps(document), encoding="utf-8")
    code, report = violations()
    assert code == 1
    assert report["violations"][0]["code"] == "invalid_field_set"
    document["monitoring_description"] = document.pop("monitoring")
    (session / "session.json").write_text(json.dumps(document), encoding="utf-8")

    records = journal_records(workspace, "session-one")
    with (session / "answers.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(canonical(dict(records[0], score=1)) + "\n")
    code, report = violations()
    assert code == 1
    assert "invalid_field_set" in {item["code"] for item in report["violations"]}

    records = journal_records(workspace, "session-one")
    with (session / "answers.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(canonical(dict(records[0], presentation_index=6, correction_of=6,
                                    pair_id="synth-pair-01")) + "\n")
    code, report = violations()
    assert code == 1
    codes = {item["code"] for item in report["violations"]}
    assert "unknown_correction_target" in codes and "presentation_mismatch" in codes

    records = journal_records(workspace, "session-one")
    with (session / "answers.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(canonical(dict(records[0], responded_at=rating.timestamp())) + "\n")
    code, report = violations()
    assert "duplicate_presentation" in {item["code"] for item in report["violations"]}

    (session / "export.json").unlink()
    code, report = violations()
    assert "export_missing" in {item["code"] for item in report["violations"]}


def test_validate_reports_unreadable_input(workspace, capsys):
    assert rating.main(["validate", "--session", str(session_directory(workspace, "absent"))]) == 2
    assert "session_unreadable" in capsys.readouterr().err


def test_the_two_modules_never_import_the_ranking_packages():
    source = ""
    for name in ("rating.py", "playback.py"):
        source += (REPOSITORY_ROOT / "backend" / "evaluation" / name).read_text(encoding="utf-8")
    assert "backend.palette" not in source
    assert "backend.intelligence" not in source


# --- inputs, persisted order and the recognition gate ----------------------

def test_a_dataset_version_mismatch_is_refused(workspace, capsys):
    dataset = workspace.dataset_document
    dataset["dataset_version"] = "synthetic-fixture-pool-02"
    other = workspace.tmp / "dataset-other.json"
    other.write_text(json.dumps(dataset), encoding="utf-8")
    arguments = start_arguments(workspace)
    arguments[arguments.index(str(workspace.dataset))] = str(other)
    assert run(arguments, "q\n") == 2
    assert "dataset_version_mismatch" in capsys.readouterr().err
    assert not session_directory(workspace, "session-one").exists()
    assert run(start_arguments(workspace), "1\nq\n") == 3
    assert run(resume_arguments(workspace, "session-one", dataset=other), "2\n") == 2
    assert "dataset_version_mismatch" in capsys.readouterr().err
    assert len(journal_records(workspace, "session-one")) == 1


def test_resume_reads_the_persisted_order_instead_of_recomputing_it(workspace):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    session = session_directory(workspace, "session-one")
    document = session_record(workspace, "session-one")
    document["presentations"][1], document["presentations"][2] = \
        document["presentations"][2], document["presentations"][1]
    for position, presentation in enumerate(document["presentations"], start=1):
        presentation["presentation_index"] = position
    (session / "session.json").write_text(json.dumps(document), encoding="utf-8")
    stored = session_record(workspace, "session-one")
    recomputed = rating.presentation_order(workspace.pairs_document["pairs"], rating.ORDER_SEED,
                                           "session-one")
    assert [item["pair_id"] for item in stored["presentations"][1:3]] != \
        [item["pair_id"] for item in recomputed[1:3]]
    assert run(resume_arguments(workspace, "session-one"), "2\nq\n") == 3
    written = journal_records(workspace, "session-one")
    assert [record["pair_id"] for record in written] == \
        [presentation["pair_id"] for presentation in stored["presentations"][:2]]


def test_recognised_rate_exceeded_is_reported(workspace, capsys):
    assert run(start_arguments(workspace), "s recognised\ns recognised\n1\n2\n1\n2\n") == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (summary["presentations"], summary["recognised"]) == (6, 2)
    assert summary["recognised_rate"] == 2 / 6
    assert summary["recognised_rate_exceeded"] is True
    assert summary["skipped"] == 2

def test_a_dry_run_session_cannot_be_resumed_as_a_listening_session(workspace, capsys):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    player = player_command("import sys; sys.exit(0)")
    arguments = resume_arguments(workspace, "session-one", backend="command", player=player)
    assert run(arguments, "2\n") == 2
    assert "invalid_monitoring" in capsys.readouterr().err
    assert len(journal_records(workspace, "session-one")) == 1


def test_export_accepts_an_output_inside_the_private_root(workspace, capsys):
    assert run(start_arguments(workspace), "1\nq\n") == 3
    exports = workspace.root / "exports"
    exports.mkdir(parents=True)
    destination = exports / "one.json"
    arguments = ["export", "--session", str(session_directory(workspace, "session-one")),
                 "--output", str(destination)]
    assert rating.main(arguments) == 0
    assert set(json.loads(destination.read_text(encoding="utf-8"))) == \
        {"schema_version", "session", "ratings"}
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ratings"] == 1

