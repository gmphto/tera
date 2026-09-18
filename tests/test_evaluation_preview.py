"""Formal QA regression: file-extension boundaries must not admit previews."""

import pytest
from backend.evaluation import manifest as pool
from tests.test_evaluation_manifest import setup


@pytest.mark.parametrize("name", ["preview.wav", "demo.wav", "Misc.wav", "kick.preview.WAV",
                                  "kick-demo.wav", "kick_preview.wav", "preview/tone.wav"])
def test_marked_file_or_folder_excluded_before_decode(setup, monkeypatch, name):
    config, root, temp = setup
    path = root/"Kicks"/name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"marked sample must never be decoded")
    original = pool.decoded
    def decoded(candidate):
        assert candidate != path
        return original(candidate)
    monkeypatch.setattr(pool,"decoded",decoded)
    m = pool.construct(config)
    record = next(r for r in m["excluded"] if r["mapping"]["path"] == path.relative_to(root).as_posix())
    assert record["reason"] == "demo_preview_or_misc"
    assert record["sha256"] is None
    assert len(m["selected"]) == 2


@pytest.mark.parametrize("path", ["Kicks/democracy.wav", "Bass/miscellaneous.wav", "Kicks/previewless.wav"])
def test_marker_is_a_token_not_an_arbitrary_substring(path):
    assert not pool.restricted(path)
