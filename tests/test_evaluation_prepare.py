"""Synthetic compressed-container evidence, never counted in the private pool."""
import io
import shutil
import struct
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from backend.audio import load_wav_bytes
from backend.evaluation import prepare as recovery
from backend.evaluation import diagnostics
from backend.evaluation import manifest as pool
from tests.test_audio import chunk
from tests.test_evaluation_manifest import setup


def compressed(rate=44100, channels=1, tag=0x674F, frame_delta=0):
    count=rate//5
    t=np.arange(count)/rate
    x=np.column_stack([.7*np.sin(2*np.pi*(110+55*c)*t) for c in range(channels)])
    stream=io.BytesIO()
    sf.write(stream,x,rate,format="OGG",subtype="VORBIS")
    ogg=stream.getvalue()
    fmt=struct.pack("<HHIIHH",tag,channels,rate,16000,1,16)+struct.pack("<H",0)
    body=b"WAVE"+chunk(b"fmt ",fmt)+chunk(b"fact",struct.pack("<I",count+frame_delta))+chunk(b"data",ogg)
    return b"RIFF"+struct.pack("<I",len(body))+body,ogg


@pytest.fixture
def coherent_decoder(monkeypatch):
    # A deterministic external-decoder model for positive structural/round-trip
    # tests. Real independent FFmpeg/SoundFile evidence is recorded locally;
    # modern libvorbis fixtures otherwise expose a 128-frame disagreement.
    original=recovery.subprocess.run
    def run(command, **kwargs):
        if "pcm_f64le" not in command: return original(command,**kwargs)
        with sf.SoundFile(io.BytesIO(kwargs["input"])) as decoder:
            samples=decoder.read(dtype="float64",always_2d=True)
        return SimpleNamespace(returncode=0,stdout=samples.astype("<f8").tobytes(),stderr=b"")
    monkeypatch.setattr(recovery.subprocess,"run",run)


@pytest.mark.parametrize("rate,channels",[(44100,1),(48000,2)])
def test_complete_nested_ogg_roundtrip_native_samples(rate,channels,coherent_decoder):
    if not shutil.which("ffmpeg"): pytest.skip("Existing FFmpeg required for independent decoder integration")
    data,ogg=compressed(rate,channels)
    before=bytes(data)
    result,evidence=recovery.decode_verified(data)
    loaded=load_wav_bytes(result)
    assert loaded.sample_rate_hz==rate and loaded.channels==channels and loaded.frame_count==rate//5
    assert loaded.subtype=="DOUBLE" and evidence["canonical_sample_equality"]=="exact"
    assert recovery.decode_verified(data)[0]==result
    assert data==before
    assert evidence["framing"]["final_granule"]==rate//5


@pytest.mark.parametrize("fault",["checksum","truncated","missing_eos","trailing"])
def test_ogg_integrity_rejects_incomplete_or_changed_pages(fault):
    _,ogg=compressed()
    if fault=="checksum":
        changed=bytearray(ogg); changed[-1]^=1; ogg=bytes(changed)
    elif fault=="truncated": ogg=ogg[:-1]
    elif fault=="missing_eos":
        # Keep only the first complete page (valid checksum, no final EOS).
        end=27+ogg[26]+sum(ogg[27:27+ogg[26]])
        ogg=ogg[:end]
    else: ogg+=b"extra"
    with pytest.raises(ValueError): recovery.ogg_complete(ogg)


@pytest.mark.parametrize("fault",["fact","variant","padding","riff_size"])
def test_unsupported_or_inconsistent_sources_not_repaired(fault):
    data,_=compressed(tag=0x6750 if fault=="variant" else 0x674F,frame_delta=1 if fault=="fact" else 0)
    if fault=="padding":
        body=data[8:]+b"inst"+struct.pack("<I",7)+b"\0"*7
        data=b"RIFF"+struct.pack("<I",len(body))+body
    if fault=="riff_size": data=data[:-1]
    before=bytes(data)
    with pytest.raises(ValueError): recovery.decode_verified(data)
    assert data==before


def test_diagnostic_failure_does_not_admit_or_expose_payload(monkeypatch):
    data,_=compressed()
    monkeypatch.setattr(diagnostics,"invoke",lambda *a,**kw:{"exit":1,"stdout":"{}","stderr":"no decoder found private-path SECRET"})
    r=diagnostics.probe(data)
    assert not r["admitted"] and r["decoded_frames"] is None
    assert diagnostics.safe_outcome(r["automatic_decode"])=={"exit":1,"failure_categories":["no decoder found"]}
    assert "SECRET" not in str(diagnostics.safe_outcome(r["automatic_decode"]))
    assert diagnostics.inspect_riff(b"short")["errors"]==["not_riff_wave"]


def test_preparation_idempotence_source_preservation_and_collisions(setup,coherent_decoder):
    if not shutil.which("ffmpeg"): pytest.skip("Existing FFmpeg required for independent decoder integration")
    config,root,temp=setup
    data,_=compressed()
    source=root/"Kicks"/"compressed.wav"
    source.write_bytes(data)
    dataset=pool.construct(config)
    destination=temp/"prepared"
    first_config,first=recovery.prepare(dataset,destination)
    second_config,second=recovery.prepare(dataset,destination)
    assert first==second and first_config==second_config
    assert source.read_bytes()==data
    prepared=[r for r in first["records"] if r["status"]=="prepared"]
    assert len(prepared)==1 and prepared[0]["source_unchanged"]
    output=next(destination.glob("*.wav"))
    output.write_bytes(b"unrelated collision")
    _,third=recovery.prepare(dataset,destination)
    assert third["records"][0]["status"]=="excluded"
    assert output.read_bytes()==b"unrelated collision"
    with pytest.raises(ValueError): recovery.prepare(dataset,root/"unsafe")

def test_decoder_frame_disagreement_stays_excluded(monkeypatch):
    data,_=compressed()
    monkeypatch.setattr(recovery.subprocess,"run",lambda *a,**kw: SimpleNamespace(returncode=0,stdout=b"\\0"*8,stderr=b""))
    with pytest.raises(ValueError,match="frame count"):
        recovery.decode_verified(data)

