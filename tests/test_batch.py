import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from backend import audio
from backend.analysis import batch
from backend.contracts import MEASURES, Sample
from tests.test_audio import wav


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    data = wav((.3*np.sin(2*np.pi*110*np.arange(19200)/48000))[:, None])
    (root / "tone.WAV").write_bytes(data)
    return root, tmp_path / "manifest.json", data


def read(output):
    return batch.read_manifest(output)


def test_fresh_duplicate_recursive_complete_contract_and_source_preservation(library, capsys, monkeypatch):
    root, output, data = library
    (root / "nested").mkdir()
    (root / "nested" / "copy.wav").write_bytes(data)
    (root / "ignore.txt").write_text("not audio")
    original = batch.extract
    calls = []
    def counted(*args):
        calls.append(args[1])
        return original(*args)
    monkeypatch.setattr(batch, "extract", counted)
    assert batch.run(root, "sub-bass", output) == 0
    m = read(output)
    assert list(m["entries"]) == ["nested/copy.wav", "tone.WAV"]
    assert len(calls) == 1
    for relative, e in m["entries"].items():
        assert (root / relative).read_bytes() == data
        assert e["fingerprint"] == hashlib.sha256(data).hexdigest()
        s = Sample.from_json(Sample.from_dict(e["result"]).to_json())
        assert s.sample_id == "sha256:" + hashlib.sha256(data).hexdigest()
        assert s.audio.local_path == str(root / relative) and s.role == "sub-bass"
        assert s.audio.frame_count == 19200 and s.audio.sample_rate_hz == 48000
        assert len(s.features.measurements) == len(MEASURES)
        measured = {x.name: x for x in s.features.measurements}
        assert measured["fundamental"].value == pytest.approx(110, abs=.1)
        assert measured["rms"].value == pytest.approx(.3/np.sqrt(2), abs=1e-5)
        assert measured["tempo"].unavailable_reason == measured["stereo_width"].unavailable_reason == "not_implemented"
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last == {"state": "complete", "discovered": 2, "total": 2, "completed": 2,
                    "failed": 0, "remaining": 0, "analyzed": 1, "reused": 1}
    assert sorted(p.name for p in output.parent.iterdir()) == ["manifest.json", "synthetic"]


def test_unchanged_reuse_and_rename_no_extraction(library, monkeypatch):
    root, output, data = library
    assert batch.run(root, "bass", output) == 0
    old_id = read(output)["entries"]["tone.WAV"]["sample_id"]
    def forbidden(*args):
        pytest.fail("unchanged readable content must not be extracted")
    monkeypatch.setattr(batch, "extract", forbidden)
    assert batch.run(root, "bass", output) == 0
    (root / "tone.WAV").rename(root / "renamed.wav")
    assert batch.run(root, "bass", output) == 0
    entry = read(output)["entries"]["renamed.wav"]
    assert entry["disposition"] == "reused" and entry["sample_id"] == old_id
    assert entry["result"]["audio"]["local_path"] == str(root / "renamed.wav")


def test_same_size_mtime_edit_and_version_invalidate(library, monkeypatch):
    root, output, data = library
    assert batch.run(root, "kick", output) == 0
    first = read(output)
    path = root / "tone.WAV"
    info = path.stat()
    changed = wav((.2*np.sin(2*np.pi*220*np.arange(19200)/48000))[:, None])
    assert len(changed) == len(data)
    path.write_bytes(changed)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert batch.run(root, "kick", output) == 0
    second = read(output)
    assert second["entries"]["tone.WAV"]["disposition"] == "analyzed"
    assert second["entries"]["tone.WAV"]["sample_id"] != first["entries"]["tone.WAV"]["sample_id"]
    monkeypatch.setattr(batch.harmony, "ANALYSIS_VERSION", "new-policy")
    assert batch.run(root, "kick", output) == 0
    third = read(output)
    assert third["analysis_digest"] != second["analysis_digest"]
    assert third["entries"]["tone.WAV"]["disposition"] == "analyzed"


@pytest.mark.parametrize("group,key", [(None,"batch"),(None,"reader"),(None,"contract"),
    ("extractors","loudness"),("extractors","spectral"),("extractors","transient"),("extractors","harmony"),
    ("runtime","python"),("runtime","numpy"),("runtime","scipy"),("runtime","soundfile"),("runtime","libsndfile"),
    ("configuration","snapshot")])
def test_descriptor_constituents_participate_in_digest(group, key):
    descriptor = batch.analysis_descriptor()
    changed = copy.deepcopy(descriptor)
    target = changed[group] if group else changed
    target[key] += "-changed"
    assert batch.digest(changed) != batch.digest(descriptor)
    assert batch.digest(dict(reversed(list(descriptor.items())))) == batch.digest(descriptor)


def test_errors_retry_without_losing_success(library, monkeypatch):
    root, output, data = library
    (root / "broken.wav").write_bytes(b"broken")
    assert batch.run(root, "bass", output) == 1
    m = read(output)
    assert m["entries"]["broken.wav"]["error"]["stage"] == "decode"
    assert m["entries"]["broken.wav"]["result"] is None
    assert m["entries"]["broken.wav"]["fingerprint"] == hashlib.sha256(b"broken").hexdigest()
    (root / "broken.wav").write_bytes(data)
    monkeypatch.setattr(batch, "extract", lambda *args: pytest.fail("successful duplicate must be reusable"))
    assert batch.run(root, "bass", output) == 0
    assert batch.counts(read(output))["reused"] == 2


def test_extractor_failure_isolated_and_retried(library, monkeypatch):
    root, output, data = library
    original = batch.extract
    monkeypatch.setattr(batch, "extract", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic extractor failure")))
    assert batch.run(root, "kick", output) == 1
    e = read(output)["entries"]["tone.WAV"]
    assert e["error"]["code"] == "extractor_failure" and e["result"] is None
    monkeypatch.setattr(batch, "extract", original)
    assert batch.run(root, "kick", output) == 0
    assert read(output)["entries"]["tone.WAV"]["disposition"] == "analyzed"


def test_disappearing_file_and_later_addition(library, monkeypatch):
    root, output, data = library
    original = batch.discover
    def changed(root):
        found = original(root)
        (root / "tone.WAV").unlink()
        (root / "later.wav").write_bytes(data)
        return found
    monkeypatch.setattr(batch, "discover", changed)
    assert batch.run(root, "kick", output) == 1
    m = read(output)
    assert set(m["entries"]) == {"tone.WAV"}
    assert m["entries"]["tone.WAV"]["error"]["code"] == "not_found"
    monkeypatch.setattr(batch, "discover", original)
    assert batch.run(root, "kick", output) == 0
    assert set(read(output)["entries"]) == {"later.wav"}


def test_unreadable_and_nested_discovery_errors_visible(library, monkeypatch):
    root, output, data = library
    (root / "denied").mkdir()
    original_scan, original_snapshot = batch.os.scandir, batch.snapshot
    def scan(path):
        if Path(path).name == "denied":
            raise PermissionError("synthetic unreadable directory")
        return original_scan(path)
    monkeypatch.setattr(batch.os, "scandir", scan)
    monkeypatch.setattr(batch, "snapshot", lambda path: (_ for _ in ()).throw(PermissionError("synthetic unreadable file")))
    assert batch.run(root, "kick", output) == 1
    m = read(output)
    assert batch.counts(m)["failed"] == 2
    assert m["discovery_errors"][0]["error"]["code"] == "enumeration_failed"
    assert m["entries"]["tone.WAV"]["error"]["code"] == "access_denied"
    monkeypatch.setattr(batch, "snapshot", original_snapshot)


def test_snapshot_bound_to_decode_even_if_source_changes_after_read(library, monkeypatch):
    root, output, data = library
    original = batch.extract
    def extract(snapshot, *args):
        (root / "tone.WAV").write_bytes(b"replacement after snapshot")
        assert snapshot == data
        return original(snapshot, *args)
    monkeypatch.setattr(batch, "extract", extract)
    assert batch.run(root, "bass", output) == 0
    e = read(output)["entries"]["tone.WAV"]
    assert e["fingerprint"] == hashlib.sha256(data).hexdigest()
    assert e["result"]["audio"]["frame_count"] == 19200


def test_changed_during_read_is_error(library, monkeypatch):
    root, output, data = library
    original = batch.os.fstat
    calls = 0
    def fstat(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "tone.WAV").write_bytes(data + b"changed")
        return original(fd)
    monkeypatch.setattr(batch.os, "fstat", fstat)
    assert batch.run(root, "bass", output) == 1
    e = read(output)["entries"]["tone.WAV"]
    assert e["error"]["code"] == "source_changed"
    assert e["fingerprint"] is None and e["result"] is None


def test_interrupt_after_success_preserves_pending_and_resumes(library, monkeypatch):
    root, output, data = library
    (root / "z.wav").write_bytes(wav(np.zeros((100,1))))
    original = batch.extract
    calls = []
    def interrupt(data, path, *args):
        calls.append(path.name)
        if path.name == "z.wav":
            raise KeyboardInterrupt
        return original(data, path, *args)
    monkeypatch.setattr(batch, "extract", interrupt)
    assert batch.run(root, "kick", output) == 130
    m = read(output)
    assert m["state"] == "interrupted"
    assert m["entries"]["tone.WAV"]["status"] == "complete"
    assert m["entries"]["z.wav"]["status"] == "pending"
    assert not Path(str(output)+".lock").exists()
    monkeypatch.setattr(batch, "extract", original)
    assert batch.run(root, "kick", output) == 0
    assert read(output)["entries"]["tone.WAV"]["disposition"] == "reused"


@pytest.mark.parametrize("after", [False, True])
def test_interrupted_atomic_replace_leaves_valid_old_or_new(library, monkeypatch, after):
    root, output, data = library
    assert batch.run(root, "kick", output) == 0
    original = read(output)
    updated = copy.deepcopy(original)
    updated["state"] = "interrupted"
    replace = batch.os.replace
    def interrupt(source, destination):
        if after:
            replace(source, destination)
        raise KeyboardInterrupt
    monkeypatch.setattr(batch.os, "replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        batch.checkpoint(output, updated)
    assert read(output) == (updated if after else original)
    assert not list(output.parent.glob("*.tmp"))


def test_initial_checkpoint_before_extract_and_storage_failure_stops(library, monkeypatch):
    root, output, data = library
    original = batch.extract
    def extract(*args):
        m = read(output)
        assert m["state"] == "running" and m["entries"]["tone.WAV"]["status"] == "pending"
        return original(*args)
    monkeypatch.setattr(batch, "extract", extract)
    assert batch.run(root, "kick", output) == 0
    before = output.read_bytes()
    monkeypatch.setattr(batch.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("synthetic storage failure")))
    assert batch.main([str(root), "--role", "kick", "--manifest", str(output)]) == 2
    assert output.read_bytes() == before


@pytest.mark.parametrize("mutation", ["schema", "fields", "digest", "sample", "sample_path", "missing_measure",
                                      "pending_result", "path_traversal", "status", "null_error"])
def test_malformed_manifest_rejected_unchanged(library, mutation):
    root, output, data = library
    assert batch.run(root, "kick", output) == 0
    m = read(output)
    e = m["entries"]["tone.WAV"]
    if mutation == "schema": m["manifest_schema"] = "2.0"
    elif mutation == "fields": m["extra"] = True
    elif mutation == "digest": m["analysis_digest"] = "a"*64
    elif mutation == "sample": e["result"]["role"] = "bass"
    elif mutation == "sample_path": e["result"]["audio"]["local_path"] = str(root / "other.wav")
    elif mutation == "missing_measure": e["result"]["features"]["measurements"].pop()
    elif mutation == "pending_result": e["status"] = "pending"
    elif mutation == "path_traversal": m["entries"]["../tone.WAV"] = m["entries"].pop("tone.WAV")
    elif mutation == "status": e["status"] = "pretend"
    elif mutation == "null_error": e.update(status="error", result=None)
    output.write_text(json.dumps(m), encoding="utf-8")
    before = output.read_bytes()
    assert batch.main([str(root), "--role", "kick", "--manifest", str(output)]) == 2
    assert output.read_bytes() == before


@pytest.mark.parametrize("text", ['{"manifest_schema":"1.0","manifest_schema":"1.0"}', '{"x":NaN}', 'not a manifest'])
def test_unrelated_duplicate_or_nonfinite_json_not_overwritten(library, text):
    root, output, data = library
    output.write_text(text)
    assert batch.main([str(root), "--role", "kick", "--manifest", str(output)]) == 2
    assert output.read_text() == text


def test_wrong_role_root_and_lock_require_explicit_recovery(library):
    root, output, data = library
    assert batch.run(root, "kick", output) == 0
    before = output.read_bytes()
    other = root.parent / "other"
    other.mkdir()
    for folder, role in [(root,"bass"), (other,"kick")]:
        with pytest.raises(batch.BatchError, match="Different root/role"):
            batch.run(folder, role, output)
        assert output.read_bytes() == before
    lock = Path(str(output)+".lock")
    lock.write_text("stale-or-live")
    with pytest.raises(batch.BatchError, match="locked"):
        batch.run(root, "kick", output)
    assert lock.read_text() == "stale-or-live" and output.read_bytes() == before


def test_destination_audio_hardlink_and_symlink_protection(library):
    root, output, data = library
    with pytest.raises(batch.BatchError):
        batch.run(root, "kick", root / "tone.WAV")
    os.link(root / "tone.WAV", output)
    with pytest.raises(batch.BatchError, match="alias"):
        batch.run(root, "kick", output)
    assert (root / "tone.WAV").read_bytes() == data
    output.unlink()
    try:
        output.symlink_to(root / "tone.WAV")
    except OSError:
        pytest.skip("symbolic link creation unavailable")
    with pytest.raises(batch.BatchError, match="links/junctions"):
        batch.run(root, "kick", output)
    assert (root / "tone.WAV").read_bytes() == data


def test_discovery_skips_symlinks(library):
    root, output, data = library
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
        (root / "alias.wav").symlink_to(root / "tone.WAV")
    except OSError:
        pytest.skip("symbolic link creation unavailable")
    assert batch.discover(root) == (["tone.WAV"], [])


def test_cli_empty_and_invalid_inputs(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    output = tmp_path / "empty.json"
    command = [sys.executable, "-m", "backend.analysis.batch", str(root), "--role", "kick", "--manifest", str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read(output)["entries"] == {}
    assert json.loads(result.stdout.splitlines()[-1])["total"] == 0
    for folder, role, target in [(root/"missing","kick",output), (root,"drums",output),
                                  (root,"kick",tmp_path/"missing"/"out.json"), ("//server/share","kick",output)]:
        result = subprocess.run([sys.executable,"-m","backend.analysis.batch",str(folder),"--role",role,
                                 "--manifest",str(target)], capture_output=True, text=True)
        assert result.returncode == 2


def test_cli_fresh_and_resume(library):
    root, output, data = library
    command = [sys.executable,"-m","backend.analysis.batch",str(root),"--role","bass","--manifest",str(output)]
    first = subprocess.run(command, capture_output=True, text=True)
    second = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout.splitlines()[-1])["analyzed"] == 1
    assert json.loads(second.stdout.splitlines()[-1])["reused"] == 1


def test_byte_reader_equivalence_and_rejects_mutable_input(library):
    root, output, data = library
    np.testing.assert_array_equal(audio.load_wav(root/"tone.WAV").samples, audio.load_wav_bytes(data).samples)
    with pytest.raises(audio.AudioReadError):
        audio.load_wav_bytes(bytearray(data))
