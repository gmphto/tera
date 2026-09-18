"""Strict source-preserving nested-Ogg preparation; no repair or header patching."""

import argparse
import copy
import hashlib
import io
import os
from pathlib import Path
import struct
import subprocess

import numpy as np
import soundfile as sf

from backend.audio import load_wav_bytes
from backend.analysis.batch import canonical, local_path, snapshot
from backend.evaluation.diagnostics import inspect_riff
from backend.evaluation.manifest import (LOCAL_USE, construct, mapping_path, read_json, require,
                                         validate, validate_schema, write_private)

VERSION="nested-ogg-674f-double-v1"
_CRC=[]
for _i in range(256):
    _r=_i<<24
    for _ in range(8): _r=((_r<<1)^0x04C11DB7 if _r&0x80000000 else _r<<1)&0xFFFFFFFF
    _CRC.append(_r)


def ogg_complete(payload):
    """Validate one complete Ogg logical stream, page CRC/sequence/lacing/EOS."""
    offset=0
    serial=None
    number=0
    continued=False
    final=None
    while offset<len(payload):
        require(offset+27<=len(payload) and payload[offset:offset+4]==b"OggS", "Invalid Ogg page boundary.")
        header=payload[offset:offset+27]
        version,flags=header[4],header[5]
        require(version==0 and not flags&~7, "Unsupported Ogg page version/flags.")
        granule,stream,sequence,checksum=struct.unpack_from("<QIII",header,6)
        require(sequence==number, "Missing/reordered Ogg page.")
        if serial is None:
            serial=stream
            require(bool(flags&2) and not flags&1, "Ogg stream must start with BOS.")
        else:
            require(stream==serial and not flags&2, "Chained/multiplexed Ogg is not admitted.")
        require(bool(flags&1)==continued, "Ogg packet continuation mismatch.")
        count=header[26]
        require(offset+27+count<=len(payload), "Truncated Ogg segment table.")
        segments=payload[offset+27:offset+27+count]
        end=offset+27+count+sum(segments)
        require(end<=len(payload), "Truncated Ogg page payload.")
        page=bytearray(payload[offset:end])
        page[22:26]=b"\0"*4
        crc=0
        for byte in page: crc=((crc<<8)&0xFFFFFFFF)^_CRC[((crc>>24)^byte)&255]
        require(crc==checksum, "Ogg checksum mismatch.")
        continued=bool(segments and segments[-1]==255)
        if flags&4:
            require(end==len(payload) and not continued and granule!=0xFFFFFFFFFFFFFFFF, "Invalid Ogg EOS/trailing bytes.")
            final=granule
        offset=end
        number+=1
    require(final is not None and number>0, "Missing complete Ogg EOS.")
    return {"pages":number,"final_granule":final}


def decode_verified(data, ffmpeg="ffmpeg"):
    structure=inspect_riff(data)
    require(not structure["errors"], "Original RIFF bounds invalid; no repair admitted.")
    fmt=structure["format"]
    require(fmt is not None and fmt["tag"]==0x674F, "Variant lacks a passing representative route.")
    require(fmt["channels"] in (1,2) and fmt["rate"]>0, "Invalid declared layout.")
    chunks=[c for c in structure["chunks"] if c["kind"]=="data"]
    require(len(chunks)==1 and sum(c["kind"]=="fmt" for c in structure["chunks"])==1
            and sum(c["kind"]=="fact" for c in structure["chunks"])==1, "Ambiguous compressed container.")
    chunk=chunks[0]
    payload=data[chunk["start"]:chunk["end"]]
    framing=ogg_complete(payload)
    require(framing["final_granule"]==structure["declared_frames"] and framing["final_granule"]>0,
            "Ogg final granule disagrees with declared frames.")
    command=[ffmpeg,"-nostdin","-v","error","-xerror","-err_detect","explode","-f","ogg","-i","pipe:0",
             "-map","0:a:0","-c:a","pcm_f64le","-f","f64le","pipe:1"]
    decoded=subprocess.run(command,input=payload,capture_output=True,timeout=60)
    require(decoded.returncode==0 and not decoded.stderr, "Complete FFmpeg decode failed or reported errors.")
    require(len(decoded.stdout)==structure["declared_frames"]*fmt["channels"]*8, "Decoded frame count disagrees with source.")
    samples=np.frombuffer(decoded.stdout,dtype="<f8").reshape(-1,fmt["channels"])
    require(np.isfinite(samples).all(), "Nonfinite decoded samples.")
    with sf.SoundFile(io.BytesIO(payload)) as independent:
        require(independent.format=="OGG" and independent.subtype=="VORBIS"
                and independent.samplerate==fmt["rate"] and independent.channels==fmt["channels"]
                and independent.frames==len(samples), "Independent decoder metadata mismatch.")
        check=independent.read(dtype="float64",always_2d=True)
    require(check.shape==samples.shape and np.isfinite(check).all()
            and np.allclose(check,samples,rtol=1e-5,atol=1e-6), "Independent decoder sample disagreement.")
    maximum=float(np.max(np.abs(check-samples)))
    pcm=samples.astype("<f8",copy=False).tobytes()
    require(len(pcm)+36<=0xFFFFFFFF, "Canonical RIFF size exceeds supported bounds.")
    header=struct.pack("<HHIIHH",3,fmt["channels"],fmt["rate"],fmt["rate"]*fmt["channels"]*8,fmt["channels"]*8,64)
    body=b"WAVE"+b"fmt "+struct.pack("<I",16)+header+b"data"+struct.pack("<I",len(pcm))+pcm
    canonical_bytes=b"RIFF"+struct.pack("<I",len(body))+body
    loaded=load_wav_bytes(canonical_bytes)
    require(loaded.sample_rate_hz==fmt["rate"] and loaded.channels==fmt["channels"]
            and loaded.frame_count==len(samples) and np.array_equal(loaded.samples,samples), "Canonical copy changed decoded samples.")
    return canonical_bytes,{"framing":framing,"sample_rate_hz":fmt["rate"],"channels":fmt["channels"],
                            "frames":len(samples),"duration_seconds":len(samples)/fmt["rate"],
                            "subtype":"DOUBLE","canonical_sample_equality":"exact",
                            "independent_max_absolute_error":maximum,"independent_rtol":1e-5,"independent_atol":1e-6}


def prepare(dataset, output_directory, ffmpeg="ffmpeg"):
    validate_schema(dataset)
    destination=local_path(output_directory)
    for source in dataset["sources"].values():
        require(not destination.is_relative_to(local_path(source["root"])), "Preparation directory must be outside source roots.")
    destination.mkdir(parents=True,exist_ok=True)
    tool=subprocess.run([ffmpeg,"-version"],capture_output=True,text=True,check=True).stdout.splitlines()[0]
    attempts={}
    for entry in dataset["excluded"]:
        if entry["role"] not in ("kick","bass") or not entry["reason"].startswith("read_validation_failed:"): continue
        path=mapping_path(dataset["sources"],entry["mapping"])
        data=snapshot(path)
        fingerprint=hashlib.sha256(data).hexdigest()
        if fingerprint in attempts:
            attempts[fingerprint]["source_mappings"].append(entry["mapping"])
            require(attempts[fingerprint]["role"]==entry["role"], "Conflicting source roles: preparation halted.")
            continue
        record={"source_sha256":fingerprint,"source_mappings":[entry["mapping"]],"role":entry["role"],
                "source_kind":entry["source_kind"],"source_role_evidence":entry["role_evidence"],"pack":entry["pack"],
                "local_use_basis":LOCAL_USE,"status":"excluded",
                "output_path":None,"output_sha256":None,"validation":None,"reason":None,"source_unchanged":False}
        try:
            result,evidence=decode_verified(data,ffmpeg)
            output=destination/(fingerprint+"-"+VERSION+".wav")
            if output.exists():
                require(local_path(output)==output and output.stat().st_nlink==1 and output.read_bytes()==result, "Prepared output collision/alias.")
            else:
                # Exclusive creation cannot overwrite any existing source/other artifact.
                with output.open("xb") as f:
                    f.write(result)
                    f.flush()
                    os.fsync(f.fileno())
            require(hashlib.sha256(snapshot(path)).hexdigest()==fingerprint,"Original source changed during preparation.")
            record.update(status="prepared",output_path=str(output),output_sha256=hashlib.sha256(result).hexdigest(),validation=evidence)
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            record["reason"]=str(error)
        record["source_unchanged"]=hashlib.sha256(snapshot(path)).hexdigest()==fingerprint
        attempts[fingerprint]=record
    config={"schema_version":"1.0","sources":copy.deepcopy(dataset["sources"])}
    config["sources"]["prepared-verified"]={"root":str(destination),"priority":3,"kind":"installed_factory",
        "provenance_evidence":"Derived from source-preserved confirmed factory content; exact source relationships and decoder evidence in private preparation report.",
        "local_use_basis":LOCAL_USE,"role_rules":[],"reviewed_files":[
            {"id":VERSION+":"+r["source_sha256"],"path":Path(r["output_path"]).name,"role":r["role"],"subtype":r["source_role_evidence"]["subtype"],
             "evidence":"Inherited trusted manufacturer role; source SHA-256 "+r["source_sha256"]+"; complete independently verified nested Ogg decode."}
            for r in attempts.values() if r["status"]=="prepared"]}
    report={"dataset_version":dataset["dataset_version"],"preparation_version":VERSION,"tool":tool,
            "soundfile":sf.__version__,"libsndfile":sf.__libsndfile_version__,
            "decoder_settings":["-xerror","-err_detect","explode","-f","ogg","-map","0:a:0","-c:a","pcm_f64le","-f","f64le"],
            "gain_resample_trim_dither":"none","records":list(attempts.values())}
    return config,report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset")
    p.add_argument("--directory",required=True)
    p.add_argument("--report",required=True)
    p.add_argument("--output-dataset",required=True)
    p.add_argument("--validation-report",required=True)
    args=p.parse_args(argv)
    original=read_json(args.dataset)
    config,report=prepare(original,args.directory)
    write_private(args.report,report,original["sources"],(args.dataset,args.output_dataset,args.validation_report))
    derived=construct(config)
    validation=validate(derived)
    write_private(args.output_dataset,derived,derived["sources"],(args.dataset,args.report,args.validation_report))
    write_private(args.validation_report,validation,derived["sources"],(args.dataset,args.report,args.output_dataset))
    print(canonical({"prepared_sources":sum(r["status"]=="prepared" for r in report["records"]),
                     "excluded_sources":sum(r["status"]=="excluded" for r in report["records"]),
                     "all_sources_unchanged":all(r["source_unchanged"] for r in report["records"]),
                     "valid":validation["valid"],"summary":validation["summary"],
                     "verified_selected":validation["verified_selected"]}))
    return 0 if validation["valid"] else 1


if __name__=="__main__": raise SystemExit(main())
