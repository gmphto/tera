"""Bounded read-only compressed-WAV diagnosis; never prepares or repairs audio."""

import argparse
import hashlib
import json
import struct
import subprocess

from backend.analysis.batch import canonical, snapshot
from backend.evaluation.manifest import mapping_path, read_json, validate_schema, write_private

VERSION = "compressed-wav-diagnostic-v1"
EXPECTED = {("kick", 0x674F, "encoding"), ("kick", 0x6750, "encoding"),
            ("bass", 0x674F, "encoding"), ("bass", 0x674F, "bounds")}


def inspect_riff(data):
    result = {"bytes":len(data), "riff":False, "declared_bytes":None, "format":None,
              "declared_frames":None, "chunks":[], "errors":[], "ogg_capture_in_data":False}
    if len(data)<12 or data[:4]!=b"RIFF" or data[8:12]!=b"WAVE":
        result["errors"].append("not_riff_wave")
        return result
    result["riff"] = True
    result["declared_bytes"] = struct.unpack_from("<I",data,4)[0]+8
    if result["declared_bytes"] != len(data): result["errors"].append("riff_size_mismatch")
    offset=12
    while offset<len(data):
        if offset+8>len(data):
            result["errors"].append("truncated_chunk_header")
            break
        name,size=struct.unpack_from("<4sI",data,offset)
        start,end=offset+8,offset+8+size
        # Only predefined structural labels are emitted, never arbitrary payload bytes.
        label={b"fmt ":"fmt",b"fact":"fact",b"data":"data",b"inst":"inst",b"smpl":"smpl",b"LIST":"LIST"}.get(name,"other")
        result["chunks"].append({"kind":label,"size":size,"start":start,"end":end,
                                 "payload_in_bounds":end<=len(data),"padding_in_bounds":end+size%2<=len(data)})
        if end>len(data):
            result["errors"].append("truncated_chunk_payload")
            break
        if name==b"fmt " and size>=16:
            values=struct.unpack_from("<HHIIHH",data,start)
            result["format"]=dict(zip(("tag","channels","rate","byte_rate","alignment","bits"),values))
        if name==b"fact" and size>=4: result["declared_frames"]=struct.unpack_from("<I",data,start)[0]
        if name==b"data": result["ogg_capture_in_data"]=data[start:end].startswith(b"OggS")
        if end+size%2>len(data):
            result["errors"].append("missing_chunk_padding")
            break
        offset=end+size%2
    return result


def invoke(command, data=None):
    try:
        result=subprocess.run(command,input=data,capture_output=True,timeout=60)
        return {"exit":result.returncode,"stdout":result.stdout.decode("utf-8",errors="replace"),
                "stderr":result.stderr.decode("utf-8",errors="replace")}
    except subprocess.TimeoutExpired:
        return {"exit":None,"stdout":"","stderr":"diagnostic_timeout"}


def safe_outcome(result):
    labels=("no decoder found","invalid argument","extradata","header","error opening output","diagnostic_timeout")
    return {"exit":result["exit"],"failure_categories":[s for s in labels if s in result["stderr"].lower()]}


def probe(data, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    structural=inspect_riff(data)
    recognized=invoke([ffprobe,"-v","error","-show_streams","-show_format","-of","json","pipe:0"],data)
    try:
        streams=json.loads(recognized["stdout"]).get("streams",[])
    except ValueError:
        streams=[]
    first=streams[0] if streams else {}
    common=[ffmpeg,"-nostdin","-v","error","-xerror","-err_detect","explode"]
    output=["-i","pipe:0","-map","0:a:0","-f","null","-"]
    automatic=invoke(common+output,data)
    # Standard documented decoder-selection option only: no byte/header edits.
    # This bounded second probe does not assert that ACM packet framing is supported.
    explicit=invoke(common+["-c:a","vorbis"]+output,data)
    return {"structure":structural,"recognized_codec":first.get("codec_name","unknown"),
            "recognized_rate":first.get("sample_rate"),"recognized_channels":first.get("channels"),
            "probe":recognized,"automatic_decode":automatic,"explicit_vorbis_decode":explicit,
            "decoded_frames":None,"admitted":False,
            "admission_reason":"diagnostic_only_no_validated_canonical_route"}


def diagnose(dataset, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    validate_schema(dataset)
    selected={}
    for entry in dataset["excluded"]:
        if entry["role"] not in ("kick","bass") or not entry["reason"].startswith("read_validation_failed:"):
            continue
        path=mapping_path(dataset["sources"],entry["mapping"])
        data=snapshot(path)
        structure=inspect_riff(data)
        tag=(structure["format"] or {}).get("tag")
        category="bounds" if "padding" in entry["reason"] else "encoding"
        group=(entry["role"],tag,category)
        if group not in EXPECTED or group in selected: continue
        before=hashlib.sha256(data).hexdigest()
        report=probe(data,ffmpeg,ffprobe)
        report.update(group=list(group),source_mapping=entry["mapping"],source_sha256=before,
                      source_unchanged=hashlib.sha256(snapshot(path)).hexdigest()==before)
        selected[group]=report
        if set(selected)==EXPECTED: break
    version=invoke([ffmpeg,"-version"])
    return {"report_schema":"1.0","dataset_version":dataset["dataset_version"],"diagnostic_version":VERSION,
            "tool_version":version["stdout"].splitlines()[0] if version["stdout"] else "unavailable",
            "missing_groups":[list(g) for g in sorted(EXPECTED-set(selected))],
            "representatives":[selected[g] for g in sorted(selected)],"recovered":0}


def sanitized(report):
    return {"diagnostic_version":VERSION,"missing_groups":report["missing_groups"],"recovered":0,
            "representatives":[{"group":r["group"],"structure":r["structure"],
              "recognized_codec":r["recognized_codec"],"recognized_rate":r["recognized_rate"],
              "recognized_channels":r["recognized_channels"],"source_unchanged":r["source_unchanged"],
              "automatic_decode":safe_outcome(r["automatic_decode"]),
              "explicit_vorbis_decode":safe_outcome(r["explicit_vorbis_decode"]),
              "decoded_frames":r["decoded_frames"],"admitted":False} for r in report["representatives"]]}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset")
    parser.add_argument("--report",required=True)
    parser.add_argument("--ffmpeg",default="ffmpeg")
    parser.add_argument("--ffprobe",default="ffprobe")
    args=parser.parse_args(argv)
    dataset=read_json(args.dataset)
    report=diagnose(dataset,args.ffmpeg,args.ffprobe)
    write_private(args.report,report,dataset["sources"],(args.dataset,))
    print(canonical(sanitized(report)))
    return 1  # Diagnostics alone never certify recovery.


if __name__=="__main__":
    raise SystemExit(main())
