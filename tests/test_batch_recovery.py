"""Additional failure-boundary checks for the durable batch protocol."""

import json
from pathlib import Path

import numpy as np
import pytest

from backend.analysis import batch
from tests.test_audio import wav
from tests.test_batch import library, read


def test_previous_success_is_verified_before_initial_checkpoint(library, monkeypatch):
    root, output, data = library
    assert batch.run(root, "bass", output) == 0
    (root / "z.wav").write_bytes(wav(np.zeros((48,1))))
    def interrupt(*args):
        durable = read(output)
        assert durable["entries"]["tone.WAV"]["status"] == "complete"
        assert durable["entries"]["tone.WAV"]["disposition"] == "reused"
        assert durable["entries"]["z.wav"]["status"] == "pending"
        raise KeyboardInterrupt
    monkeypatch.setattr(batch, "extract", interrupt)
    assert batch.run(root, "bass", output) == 130
    assert batch.counts(read(output))["completed"] == 1


def test_extractor_failure_does_not_stop_other_content(library, monkeypatch):
    root, output, data = library
    (root / "z.wav").write_bytes(wav(np.zeros((48,1))))
    original = batch.extract
    def extract(data, path, *args):
        if path.name == "tone.WAV":
            raise RuntimeError("synthetic failure")
        return original(data, path, *args)
    monkeypatch.setattr(batch, "extract", extract)
    assert batch.run(root, "bass", output) == 1
    assert batch.counts(read(output))["completed"] == 1
    assert read(output)["entries"]["z.wav"]["status"] == "complete"


def test_root_discovery_failure_leaves_previous_manifest(library, monkeypatch):
    root, output, data = library
    assert batch.run(root, "bass", output) == 0
    before = output.read_bytes()
    monkeypatch.setattr(batch.os, "scandir", lambda *args: (_ for _ in ()).throw(PermissionError("synthetic root denied")))
    assert batch.main([str(root), "--role", "bass", "--manifest", str(output)]) == 2
    assert output.read_bytes() == before


def test_concurrent_lock_and_stale_temporary_are_not_success(library):
    root, output, data = library
    stale = output.parent / (output.name + ".old.tmp")
    stale.write_text("incomplete artifact")
    with batch.manifest_lock(output):
        with pytest.raises(batch.BatchError, match="locked"):
            batch.run(root, "kick", output)
        assert not output.exists()
    assert batch.run(root, "kick", output) == 0
    assert read(output)["entries"]["tone.WAV"]["disposition"] == "analyzed"
    assert stale.read_text() == "incomplete artifact"


def test_duplicate_path_json_rejected_before_reuse(library):
    root, output, data = library
    assert batch.run(root, "kick", output) == 0
    m = read(output)
    entry = json.dumps(m["entries"]["tone.WAV"])
    original = json.dumps(m)
    replacement = '{"tone.WAV":' + entry + ',"tone.WAV":' + entry + '}'
    text = original.replace(json.dumps(m["entries"]), replacement)
    output.write_text(text, encoding="utf-8")
    assert batch.main([str(root), "--role", "kick", "--manifest", str(output)]) == 2
    assert output.read_text(encoding="utf-8") == text


def test_interruption_during_outcome_checkpoint_does_not_claim_uncommitted_work(library, monkeypatch):
    root, output, data = library
    original = batch.os.replace
    calls = 0
    def interrupt(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(source, destination)
    monkeypatch.setattr(batch.os, "replace", interrupt)
    assert batch.run(root, "kick", output) == 130
    m = read(output)
    assert m["state"] == "interrupted"
    assert m["entries"]["tone.WAV"]["status"] == "pending"
    assert batch.counts(m)["completed"] == 0
