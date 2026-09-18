"""Offline simulations only: generated audio is never added to the actual pool.

The factory fixture simulates external provenance claims to test admission;
synthetic_fixture is the explicit production provenance for generated material.
All artifacts live under pytest temp paths, never .local-evaluation.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from backend.evaluation import manifest as pool
from tests.test_audio import wav


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "synthetic-simulation"
    (root/"Kicks").mkdir(parents=True)
    (root/"Bass").mkdir()
    (root/"Kicks"/"one.wav").write_bytes(wav(np.zeros((48,1))))
    (root/"Bass"/"two.wav").write_bytes(wav(np.ones((48,1))*.25))
    source = {"root":str(root), "priority":0, "kind":"installed_factory",
              "provenance_evidence":"SYNTHETIC TEST: simulated manufacturer folders; not actual evaluation material.",
              "local_use_basis":pool.LOCAL_USE, "reviewed_files":[], "role_rules":[
                  {"id":"kick-v1","path":"Kicks","role":"kick","subtype":None,"evidence":"SYNTHETIC TEST category"},
                  {"id":"bass-v1","path":"Bass","role":"bass","subtype":None,"evidence":"SYNTHETIC TEST category"}]}
    return {"schema_version":"1.0","sources":{"simulation":source}}, root, tmp_path


def test_selection_metadata_determinism_and_shortfall(setup):
    config, root, temp = setup
    m = pool.construct(config)
    assert pool.construct(config) == m
    assert [r["role"] for r in m["selected"]] == ["bass", "kick"]
    assert all(r["metadata"]["sample_rate_hz"] == 48000 for r in m["selected"])
    report = pool.validate(m)
    assert report["valid"] and report["verified_selected"] == {"kick":1,"bass":1}
    assert report["summary"]["status"] == "shortfall"
    assert report["verified_shortfall"] == {"kick":99,"bass":99}


def test_root_remapping_preserves_identity_and_content_change_changes_it(setup, tmp_path):
    config, root, temp = setup
    first = pool.construct(config)
    config2 = copy.deepcopy(config)
    moved = temp / "moved"
    root.rename(moved)
    config2["sources"]["simulation"]["root"] = str(moved)
    second = pool.construct(config2)
    assert first["dataset_version"] == second["dataset_version"]
    (moved/"Bass"/"two.wav").write_bytes(wav(np.ones((48,1))*.5))
    assert pool.construct(config2)["dataset_version"] != second["dataset_version"]


def test_duplicate_content_across_names_roots_and_conflicting_labels(setup):
    config, root, temp = setup
    data = (root/"Kicks"/"one.wav").read_bytes()
    (root/"Kicks"/"copy.wav").write_bytes(data)
    source2 = copy.deepcopy(config["sources"]["simulation"])
    source2["priority"] = 1
    config["sources"]["other-simulation"] = source2
    m = pool.construct(config)
    assert len(m["selected"]) == 2 and len(m["duplicates"]) == 4
    assert all(r["mapping"]["source"] == "simulation" for r in m["selected"])
    (root/"Bass"/"conflict.wav").write_bytes(data)
    m = pool.construct(config)
    assert len(m["selected"]) == 1
    assert sum(r["reason"] == "conflicting_content_labels" for r in m["excluded"]) == 6
    assert pool.validate(m)["valid"]


def test_filename_ambiguity_and_conflicting_rules_are_pending(setup):
    config, root, temp = setup
    for name in ("808.wav", "bass-drum.wav", "kick.wav", "sub.wav"):
        (root/name).write_bytes(wav(np.zeros((10,1))))
    m = pool.construct(config)
    assert len(m["pending"]) == 4 and len(m["selected"]) == 2
    config["sources"]["simulation"]["role_rules"].append(
        {"id":"conflicting-v1","path":"Kicks","role":"bass","subtype":None,"evidence":"simulated conflict"})
    m = pool.construct(config)
    assert any(r["reason"] == "conflicting_role_evidence" for r in m["pending"])


@pytest.mark.parametrize("kind",["demo_preview","unresolved","synthetic_fixture"])
def test_synthetic_demo_unresolved_never_count_as_real_pool(setup, kind):
    config, root, temp = setup
    config["sources"]["simulation"]["kind"] = kind
    m = pool.construct(config)
    assert m["selected"] == []
    assert pool.summary(m)["kick"]["shortfall"] == pool.summary(m)["bass"]["shortfall"] == 100
    assert all(r["reason"] == "ineligible_provenance" for r in m["excluded"])
    if kind == "synthetic_fixture":
        assert all(r["provenance_kind"] == "synthetic_fixture" for r in m["excluded"])


def test_explicit_reviewed_mapping_and_subtype(setup):
    config, root, temp = setup
    (root/"808.wav").write_bytes(wav(np.ones((24,1))*.1))
    config["sources"]["simulation"]["reviewed_files"] = [
        {"id":"test-review","path":"808.wav","role":"bass","subtype":"sub-bass",
         "evidence":"SYNTHETIC TEST reviewed mapping, not a fabricated production audition"}]
    m = pool.construct(config)
    entry = next(r for r in m["selected"] if r["mapping"]["path"] == "808.wav")
    assert entry["role_evidence"] == {"method":"reviewed_mapping","rule_id":"test-review","subtype":"sub-bass"}


def test_missing_unreadable_invalid_and_stale_do_not_validate(setup, monkeypatch):
    config, root, temp = setup
    m = pool.construct(config)
    (root/"Kicks"/"one.wav").unlink()
    report = pool.validate(m)
    assert not report["valid"] and report["verified_selected"]["kick"] == 0
    assert report["verified_shortfall"]["kick"] == 100
    (root/"Kicks"/"one.wav").write_bytes(b"broken")
    assert not pool.validate(m)["valid"]
    rebuilt = pool.construct(config)
    assert len(rebuilt["selected"]) == 1 and "read_validation_failed" in rebuilt["excluded"][0]["reason"]
    (root/"Kicks"/"one.wav").write_bytes(wav(np.ones((48,1))*.3))
    assert "stale_fingerprint" in pool.validate(m)["failures"][0]["error"]
    monkeypatch.setattr(pool, "snapshot", lambda path: (_ for _ in ()).throw(PermissionError("synthetic unreadable")))
    assert pool.construct(config)["selected"] == []


def test_source_change_and_cloud_placeholder_never_count(setup, monkeypatch):
    config, root, temp = setup
    monkeypatch.setattr(pool, "snapshot", lambda path: (_ for _ in ()).throw(OSError("source_changed")))
    assert pool.construct(config)["selected"] == []
    monkeypatch.undo()
    original = Path.stat
    class Offline:
        st_file_attributes = 0x1000
    def stat(path, *args, **kwargs):
        return Offline() if path.suffix == ".wav" else original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"stat",stat)
    monkeypatch.setattr(pool,"snapshot",lambda path: pytest.fail("must not hydrate a placeholder"))
    assert pool.construct(config)["selected"] == []


@pytest.mark.parametrize("mutation",["schema","role","provenance","duplicate_id","duplicate_path","membership","policy","fingerprint","extra"])
def test_schema_errors_and_tampering_rejected(setup, mutation):
    config, root, temp = setup
    m = pool.construct(config)
    if mutation == "schema": m["schema_version"] = "2"
    elif mutation == "role": m["selected"][0]["role"] = "kick"
    elif mutation == "provenance": m["selected"][0]["provenance_kind"] = "synthetic_fixture"
    elif mutation == "duplicate_id": m["selected"][1].update(sha256=m["selected"][0]["sha256"],sample_id=m["selected"][0]["sample_id"])
    elif mutation == "duplicate_path": m["selected"].append(copy.deepcopy(m["selected"][0]))
    elif mutation == "membership": m["reserves"].append(m["selected"].pop())
    elif mutation == "policy": m["policy"]["selection_version"] = "next"
    elif mutation == "fingerprint": m["selected"][0]["sha256"] = "not-a-hash"
    elif mutation == "extra": m["extra"] = True
    with pytest.raises(ValueError): pool.validate_schema(m)


def test_duplicate_json_keys_rejected(tmp_path):
    p = tmp_path/"duplicate.json"
    p.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
    with pytest.raises(pool.ManifestError,match="Duplicate"): pool.read_json(p)


def test_reserves_selection_does_not_depend_on_scan_order(setup, monkeypatch):
    config, root, temp = setup
    for i in range(101):
        (root/"Kicks"/f"sample-{i:03d}.wav").write_bytes(wav(np.ones((2,1))*(i+1)/1000))
    m = pool.construct(config)
    assert pool.summary(m)["kick"]["selected_unique"] == 100
    assert pool.summary(m)["kick"]["reserves"] == 2
    walk = os.walk
    monkeypatch.setattr(pool.os,"walk",lambda *a,**kw: reversed(list(walk(*a,**kw))))
    assert pool.construct(config)["dataset_version"] == m["dataset_version"]


def test_output_cannot_alias_audio_or_dataset(setup):
    config, root, temp = setup
    m = pool.construct(config)
    with pytest.raises(ValueError): pool.write_private(root/"report.json",m,m["sources"])
    output = temp/"alias.json"
    os.link(root/"Kicks"/"one.wav",output)
    with pytest.raises(ValueError): pool.write_private(output,m,m["sources"])
    output.unlink()
    pool.write_private(output,m,m["sources"])
    with pytest.raises(ValueError): pool.write_private(output,m,m["sources"],(output,))


def test_cli_build_validate_and_synthetic_shortfall(setup):
    config, root, temp = setup
    config["sources"]["simulation"]["kind"] = "synthetic_fixture"
    path, output, report = temp/"sources.json",temp/"dataset.json",temp/"report.json"
    path.write_text(json.dumps(config))
    prefix = [sys.executable,"-m","backend.evaluation.manifest"]
    result = subprocess.run(prefix+["build",str(path),"--output",str(output),"--report",str(report)],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    assert pool.read_json(report)["verified_selected"] == {"kick":0,"bass":0}
    result = subprocess.run(prefix+["validate",str(output),"--report",str(report)],capture_output=True,text=True)
    assert result.returncode == 0, result.stdout+result.stderr
