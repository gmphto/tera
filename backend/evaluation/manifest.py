"""Construct and revalidate private evaluation pools; see _docs/evaluation-pool.md."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from backend import audio
from backend.analysis.batch import canonical, digest, local_path, snapshot


SCHEMA = "1.0"
SELECTION = "source-priority-path-sha-v1"
PROVENANCE = "explicit-category-local-use-v1"
ROLES = ("kick", "bass")
KINDS = ("installed_factory", "downloaded_sounds", "demo_preview", "unresolved", "synthetic_fixture")
LOCAL_USE = "user_authorized_local_evaluation_only"
GROUPS = ("selected", "reserves", "duplicates", "pending", "excluded")


class ManifestError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ManifestError(message)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "Unexpected or missing schema fields.")


def text(value):
    require(type(value) is str and bool(value.strip()), "Expected nonblank text.")


def relative(value):
    text(value)
    require(not PurePosixPath(value).is_absolute() and "\\" not in value and ":" not in value
            and "\0" not in value and all(x not in ("", ".", "..") for x in value.split("/")), "Invalid relative mapping.")


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "Duplicate JSON key.")
            result[key] = value
        return result
    def constant(value):
        raise ManifestError("Nonfinite JSON value.")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (OSError, ValueError) as error:
        raise ManifestError(f"Cannot read local JSON: {error}") from error


def validate_sources(sources):
    require(type(sources) is dict and bool(sources), "Sources must be a nonempty mapping.")
    for source_id, source in sources.items():
        text(source_id)
        fields(source, "root priority kind provenance_evidence local_use_basis role_rules reviewed_files")
        text(source["root"])
        require(Path(source["root"]).is_absolute() and not source["root"].startswith(("//", "\\\\")), "Source root must be absolute/local.")
        require(type(source["priority"]) is int and source["priority"] >= 0, "Invalid priority.")
        require(source["kind"] in KINDS and source["local_use_basis"] == LOCAL_USE, "Invalid provenance/local-use basis.")
        text(source["provenance_evidence"])
        require(type(source["role_rules"]) is list and type(source["reviewed_files"]) is list, "Invalid role policy.")
        seen = set()
        for rule in source["role_rules"] + source["reviewed_files"]:
            fields(rule, "id path role subtype evidence")
            text(rule["id"])
            require(rule["id"] not in seen, "Duplicate role evidence ID.")
            seen.add(rule["id"])
            relative(rule["path"])
            require(rule["role"] in ROLES, "Invalid role.")
            require(rule["subtype"] is None or (rule["role"] == "bass" and rule["subtype"] == "sub-bass"), "Invalid role subtype.")
            text(rule["evidence"])


def validate_config(config):
    fields(config, "schema_version sources")
    require(config["schema_version"] == SCHEMA, "Unsupported configuration schema.")
    validate_sources(config["sources"])
    return config


def role_evidence(source, path):
    matches = [(r, "trusted_category") for r in source["role_rules"]
               if path.startswith(r["path"] + "/")]
    matches += [(r, "reviewed_mapping") for r in source["reviewed_files"] if path == r["path"]]
    roles = {r["role"] for r, _ in matches}
    if len(roles) > 1:
        return None, None, "conflicting_role_evidence"
    if not matches:
        return None, None, "unconfirmed_role"
    rule, method = sorted(matches, key=lambda item: item[0]["id"])[0]
    return rule["role"], {"method": method, "rule_id": rule["id"], "subtype": rule["subtype"]}, None


def hints(path):
    name = PurePosixPath(path).stem.lower()
    result = []
    if "kick" in name or re.search(r"bass[ _-]?drum", name): result.append("kick")
    if "bass" in name or "808" in name or re.search(r"(^|[ _-])sub([ _-]|$)", name): result.append("bass")
    return result


def mapping_path(sources, mapping):
    return Path(sources[mapping["source"]]["root"]) / mapping["path"]


def decoded(path):
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    if attributes & (0x1000 | 0x40000 | 0x400000):
        raise ManifestError("unavailable_cloud_placeholder: no hydration/download attempted")
    data = snapshot(path)
    fingerprint = hashlib.sha256(data).hexdigest()
    loaded = audio.load_wav_bytes(data)
    metadata = dict(sample_rate_hz=loaded.sample_rate_hz, channels=loaded.channels,
                    frame_count=loaded.frame_count, duration_seconds=loaded.duration_seconds, subtype=loaded.subtype)
    return fingerprint, metadata


def order(record, sources):
    m = record["mapping"]
    return (sources[m["source"]]["priority"], m["source"], m["path"], record["sha256"] or "")


def partition(records, sources, target=100):
    groups = {name: [] for name in GROUPS}
    eligible = defaultdict(list)
    for r in records:
        if r["reason"] is not None:
            category = "pending" if r["reason"] in ("unconfirmed_role", "conflicting_role_evidence") else "excluded"
            groups[category].append(r)
        else:
            require(r["provenance_kind"] == "real_library_sample", "Synthetic content cannot be eligible.")
            eligible[r["sha256"]].append(r)
    unique = []
    for fingerprint, duplicates in eligible.items():
        if len({r["role"] for r in duplicates}) != 1:
            for r in duplicates:
                r = dict(r, reason="conflicting_content_labels")
                groups["excluded"].append(r)
            continue
        duplicates.sort(key=lambda r: order(r, sources))
        unique.append(duplicates[0])
        for r in duplicates[1:]:
            groups["duplicates"].append(dict(r, reason="duplicate_content"))
    counts = Counter()
    for r in sorted(unique, key=lambda r: order(r, sources)):
        group = "selected" if counts[r["role"]] < target else "reserves"
        groups[group].append(r)
        counts[r["role"]] += 1
    return {g: sorted(items, key=lambda r: order(r, sources)) for g, items in groups.items()}


def dataset_version(manifest):
    membership = [{"sample_id": r["sample_id"], "sha256": r["sha256"], "role": r["role"],
                   "source": r["mapping"]["source"], "pack": r["pack"], "source_kind": r["source_kind"],
                   "provenance_kind": r["provenance_kind"], "role_evidence": r["role_evidence"],
                   "local_use_basis": r["local_use_basis"]} for r in manifest["selected"]]
    policies = {key: {"priority": value["priority"], "kind": value["kind"],
                      "role_rules": value["role_rules"], "reviewed_files": value["reviewed_files"]}
                for key, value in manifest["sources"].items()}
    return digest({"policy": manifest["policy"], "source_policies": policies,
                   "membership": sorted(membership, key=lambda r: r["sample_id"])})


def summary(m):
    all_records = [r for g in GROUPS for r in m[g]]
    result = {}
    for role in ROLES:
        belongs = lambda r: r["role"] == role or (r["role"] is None and role in r["candidate_roles"])
        selected = sum(r["role"] == role for r in m["selected"])
        result[role] = {"discovered_candidates": sum(belongs(r) for r in all_records),
            "role_confirmed": sum(r["role"] == role for r in all_records),
            "readable": sum(r["role"] == role and r["metadata"] is not None for r in all_records),
            "duplicates": sum(r["role"] == role for r in m["duplicates"]),
            "excluded": sum(belongs(r) for r in m["excluded"]),
            "pending": sum(belongs(r) for r in m["pending"]),
            "reserves": sum(r["role"] == role for r in m["reserves"]),
            "selected_unique": selected, "shortfall": max(0, m["policy"]["target_per_role"]-selected)}
    result["status"] = "target_met" if all(result[r]["shortfall"] == 0 for r in ROLES) else "shortfall"
    result["totals"] = {g: len(m[g]) for g in GROUPS}
    result["totals"].update(wav_files=len(all_records), ignored_non_wav=m["ignored_non_wav"], scan_errors=len(m["scan_errors"]))
    return result


def construct(config):
    validate_config(config)
    records, scan_errors, ignored = [], [], 0
    for source_id, source in sorted(config["sources"].items(), key=lambda item: (item[1]["priority"], item[0])):
        root = local_path(source["root"])
        require(root.is_dir(), "Missing source root.")
        def scan_error(error):
            scan_errors.append({"source": source_id, "message": str(error)})
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=scan_error):
            dirs[:] = sorted(d for d in dirs if not (Path(folder)/d).is_symlink() and not (Path(folder)/d).is_junction())
            for name in sorted(files):
                path = Path(folder)/name
                if path.suffix.lower() != ".wav":
                    ignored += 1
                    continue
                relative_path = path.relative_to(root).as_posix()
                role, evidence, reason = role_evidence(source, relative_path)
                candidates = hints(relative_path)
                r = dict(mapping={"source": source_id, "path": relative_path}, sample_id=None, sha256=None,
                         role=role, candidate_roles=candidates, pack=relative_path.split("/")[0] if "/" in relative_path else "root",
                         role_evidence=evidence, source_kind=source["kind"],
                         provenance_kind="synthetic_fixture" if source["kind"] == "synthetic_fixture" else "real_library_sample",
                         local_use_basis=source["local_use_basis"], metadata=None, reason=reason)
                if source["kind"] not in ("installed_factory", "downloaded_sounds"):
                    r["reason"] = "ineligible_provenance"
                elif any(re.search(r"(^|[ _-])(demo|preview|misc)([ _-]|$)", part, re.I) for part in PurePosixPath(relative_path).parts):
                    r["reason"] = "demo_preview_or_misc"
                elif role is None and reason == "unconfirmed_role" and not candidates:
                    r["reason"] = "outside_role_pool"
                elif role is not None and reason is None:
                    try:
                        fingerprint, metadata = decoded(path)
                        r.update(sha256=fingerprint, sample_id="sha256:"+fingerprint, metadata=metadata)
                    except (OSError, ValueError) as error:
                        r["reason"] = "read_validation_failed: " + str(error)
                records.append(r)
    m = dict(schema_version=SCHEMA, dataset_version="", sources=config["sources"],
             policy={"selection_version": SELECTION, "provenance_version": PROVENANCE, "target_per_role": 100},
             ignored_non_wav=ignored, scan_errors=scan_errors, **partition(records, config["sources"]))
    m["dataset_version"] = dataset_version(m)
    validate_schema(m)
    return m


def validate_schema(m):
    fields(m, "schema_version dataset_version sources policy ignored_non_wav scan_errors selected reserves duplicates pending excluded")
    require(m["schema_version"] == SCHEMA, "Unsupported dataset schema.")
    validate_sources(m["sources"])
    fields(m["policy"], "selection_version provenance_version target_per_role")
    require(m["policy"] == {"selection_version": SELECTION, "provenance_version": PROVENANCE, "target_per_role": 100}, "Unsupported dataset policy.")
    require(type(m["ignored_non_wav"]) is int and m["ignored_non_wav"] >= 0, "Invalid ignored-file count.")
    require(type(m["scan_errors"]) is list, "Invalid scan errors.")
    for error in m["scan_errors"]:
        fields(error, "source message")
        require(error["source"] in m["sources"], "Unknown error source.")
        text(error["message"])
    seen, ids = set(), set()
    records = []
    for group in GROUPS:
        require(type(m[group]) is list, "Expected record array.")
        for r in m[group]:
            fields(r, "mapping sample_id sha256 role candidate_roles pack role_evidence source_kind provenance_kind local_use_basis metadata reason")
            fields(r["mapping"], "source path")
            source_id, path = r["mapping"]["source"], r["mapping"]["path"]
            require(source_id in m["sources"], "Unknown source mapping.")
            relative(path)
            require(PurePosixPath(path).suffix.lower() == ".wav", "Only WAV mappings are accepted.")
            identity = (source_id, os.path.normcase(path))
            require(identity not in seen, "Duplicate local mapping.")
            seen.add(identity)
            source = m["sources"][source_id]
            role, evidence, reason = role_evidence(source, path)
            require(r["role"] == role and r["role_evidence"] == evidence, "Role evidence disagrees with source policy.")
            require(r["candidate_roles"] == hints(path), "Invalid discovery hints.")
            require(r["source_kind"] == source["kind"] and r["local_use_basis"] == LOCAL_USE, "Invalid provenance basis.")
            require(r["provenance_kind"] == ("synthetic_fixture" if source["kind"] == "synthetic_fixture" else "real_library_sample"), "Invalid provenance kind.")
            require(r["pack"] == (path.split("/")[0] if "/" in path else "root"), "Invalid pack mapping.")
            require(r["reason"] is None or type(r["reason"]) is str and bool(r["reason"]), "Invalid record reason.")
            fingerprint = r["sha256"]
            require(fingerprint is None or type(fingerprint) is str and re.fullmatch("[a-f0-9]{64}", fingerprint), "Invalid fingerprint.")
            require(r["sample_id"] == ("sha256:"+fingerprint if fingerprint else None), "Invalid content ID.")
            require((fingerprint is None) == (r["metadata"] is None), "Fingerprint/metadata must be paired.")
            if r["metadata"] is not None:
                meta = r["metadata"]
                fields(meta, "sample_rate_hz channels frame_count duration_seconds subtype")
                from backend.contracts import AudioMetadata
                AudioMetadata(local_path=str(mapping_path(m["sources"],r["mapping"])),
                              sample_rate_hz=meta["sample_rate_hz"], channels=meta["channels"],
                              frame_count=meta["frame_count"], duration_ms=meta["duration_seconds"]*1000)
                require(meta["frame_count"] > 0 and meta["subtype"] in ("PCM_U8","PCM_16","PCM_24","PCM_32","FLOAT","DOUBLE"), "Invalid decoded metadata.")
            if group in ("selected", "reserves", "duplicates"):
                require(role in ROLES and evidence is not None and fingerprint is not None
                        and source["kind"] in ("installed_factory", "downloaded_sounds")
                        and r["provenance_kind"] == "real_library_sample", "Ineligible sample admitted.")
            if group in ("selected", "reserves"):
                require(r["sample_id"] not in ids and r["reason"] is None, "Duplicate ID or ineligible selected record.")
                ids.add(r["sample_id"])
            if group == "duplicates": require(r["reason"] == "duplicate_content", "Invalid duplicate evidence.")
            if group in ("pending", "excluded"): require(r["reason"] is not None, "Excluded/pending reason required.")
            records.append(dict(r, reason=None) if r["reason"] in ("duplicate_content", "conflicting_content_labels") else r)
    require(partition(records, m["sources"]) == {g:m[g] for g in GROUPS}, "Tampered or nondeterministic selected membership.")
    require(m["dataset_version"] == dataset_version(m), "Dataset version disagrees with membership/policy.")
    return m


def validate(m):
    validate_schema(m)
    failures = []
    verified_selected = {role: 0 for role in ROLES}
    checked = 0
    for group in ("selected", "reserves", "duplicates"):
        for r in m[group]:
            checked += 1
            try:
                fingerprint, metadata = decoded(mapping_path(m["sources"], r["mapping"]))
                require(fingerprint == r["sha256"], "stale_fingerprint: source content changed")
                require(metadata == r["metadata"], "stale_metadata: decoded metadata changed")
                if group == "selected": verified_selected[r["role"]] += 1
            except (OSError, ValueError) as error:
                failures.append({"mapping": r["mapping"], "error": str(error)})
    return {"report_schema": SCHEMA, "dataset_version": m["dataset_version"],
            "valid": not failures and not m["scan_errors"], "checked": checked,
            "failures": failures, "summary": summary(m),
            "verified_selected": verified_selected,
            "verified_shortfall": {role: max(0,100-verified_selected[role]) for role in ROLES}}


def write_private(path, value, sources, protected=()):
    path = local_path(path)
    require(path.suffix.lower() == ".json" and path.parent.is_dir(), "Output needs existing local directory and .json suffix.")
    for source in sources.values():
        root = local_path(source["root"])
        require(not path.is_relative_to(root), "Outputs must be outside audio source roots.")
    for other in protected:
        other = local_path(other)
        require(path != other and not (path.exists() and other.exists() and os.path.samefile(path,other)), "Output aliases a protected input.")
    if path.exists():
        require(path.is_file() and path.stat().st_nlink == 1, "Output aliases another file.")
        previous = read_json(path)
        require(type(previous) is dict and ("dataset_version" in previous), "Refusing unrelated existing output.")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name+".", suffix=".tmp")
        with os.fdopen(fd,"w",encoding="utf-8") as f:
            f.write(canonical(value)+"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary is not None: Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("config")
    build.add_argument("--output",required=True)
    build.add_argument("--report",required=True)
    check = sub.add_parser("validate")
    check.add_argument("dataset")
    check.add_argument("--report",required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            m = construct(read_json(args.config))
            write_private(args.output,m,m["sources"],(args.config,args.report))
            protected = (args.config,args.output)
        else:
            m = read_json(args.dataset)
            protected = (args.dataset,)
        report = validate(m)
        write_private(args.report,report,m["sources"],protected)
        print(canonical({"valid":report["valid"], "verified_selected":report["verified_selected"],
                         "verified_shortfall":report["verified_shortfall"], **report["summary"]}))
        return 0 if report["valid"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Full exception stays local; never upload command output containing paths.
        print(f"Local manifest failure: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
