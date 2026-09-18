"""Local resumable WAV analysis. See _docs/batch-analysis.md for wire/recovery policy."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys
import tempfile

import numpy
import scipy
import soundfile

from backend import audio
from backend.analysis import harmony, loudness, spectral, transient
from backend.contracts import AudioFeatures, AudioMetadata, MEASURES, Measurement, Sample, SCHEMA_VERSION


MANIFEST_SCHEMA = "1.0"
BATCH_VERSION = "batch-snapshot-v1"
ROLES = ("kick", "bass", "sub-bass")


class BatchError(ValueError):
    """Command, manifest or checkpoint failure (exit 2)."""


class SourceError(OSError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def analysis_descriptor():
    return {
        "batch": BATCH_VERSION, "reader": audio.READER_VERSION, "contract": SCHEMA_VERSION,
        "extractors": {"loudness": loudness.ANALYSIS_VERSION, "spectral": spectral.ANALYSIS_VERSION,
                       "transient": transient.ANALYSIS_VERSION, "harmony": harmony.ANALYSIS_VERSION},
        "configuration": {"extractor_defaults": True, "unimplemented": ["stereo_width", "tempo"],
                          "snapshot": "immutable-bytes", "channels": "native", "role": "explicit"},
        "runtime": {"python": platform.python_version(), "numpy": numpy.__version__,
                    "scipy": scipy.__version__, "soundfile": soundfile.__version__,
                    "libsndfile": soundfile.__libsndfile_version__},
    }


def _require(condition, message):
    if not condition:
        raise BatchError(message)


def _fields(value, names):
    _require(type(value) is dict and set(value) == set(names.split()), "Malformed manifest fields.")


def _relative(value, directory=False):
    _require(type(value) is str and bool(value) and "\\" not in value and ":" not in value
             and "\0" not in value, "Invalid relative manifest path.")
    if directory and value == ".":
        return
    p = PurePosixPath(value)
    _require(not p.is_absolute() and p.as_posix() == value
             and all(part not in (".", "..") for part in value.split("/")), "Invalid relative manifest path.")


def _hex(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _error_record(value, stages):
    _fields(value, "stage code message")
    _require(value["stage"] in stages and all(type(value[k]) is str and value[k].strip()
             for k in ("stage", "code", "message")), "Malformed error record.")


def validate_manifest(m):
    _fields(m, "manifest_schema root role analysis_descriptor analysis_digest state entries discovery_errors")
    _require(m["manifest_schema"] == MANIFEST_SCHEMA, "Incompatible manifest schema.")
    _require(type(m["root"]) is str and Path(m["root"]).is_absolute()
             and not m["root"].startswith(("\\\\", "//")), "Invalid manifest root.")
    _require(m["role"] in ROLES and m["state"] in ("running", "complete", "interrupted"), "Invalid manifest role/state.")
    d = m["analysis_descriptor"]
    _fields(d, "batch reader contract extractors configuration runtime")
    _fields(d["extractors"], "loudness spectral transient harmony")
    _fields(d["configuration"], "extractor_defaults unimplemented snapshot channels role")
    _fields(d["runtime"], "python numpy scipy soundfile libsndfile")
    _require(all(type(v) is str and v for v in [d["batch"], d["reader"], d["contract"],
             *d["extractors"].values(), *d["runtime"].values()]), "Malformed analysis versions.")
    _require(type(d["configuration"]["extractor_defaults"]) is bool
             and d["configuration"]["unimplemented"] == ["stereo_width", "tempo"]
             and all(type(d["configuration"][k]) is str and d["configuration"][k]
                     for k in ("snapshot", "channels", "role")), "Malformed configuration.")
    _require(_hex(m["analysis_digest"]) and digest(d) == m["analysis_digest"], "Analysis digest disagrees with descriptor.")
    _require(type(m["entries"]) is dict and type(m["discovery_errors"]) is list, "Malformed manifest collections.")
    names = set()
    for path, entry in m["entries"].items():
        _relative(path)
        _require(PurePosixPath(path).suffix.lower() == ".wav", "Manifest entries must name WAV files.")
        key = os.path.normcase(path)
        _require(key not in names, "Duplicate normalized manifest path.")
        names.add(key)
        _fields(entry, "status fingerprint sample_id result error disposition")
        status, fingerprint = entry["status"], entry["fingerprint"]
        _require(status in ("pending", "complete", "error"), "Invalid entry status.")
        _require(fingerprint is None or _hex(fingerprint), "Invalid fingerprint.")
        _require(entry["sample_id"] == ("sha256:" + fingerprint if fingerprint else None), "Sample ID disagrees with content.")
        if status == "complete":
            _require(fingerprint is not None and entry["error"] is None
                     and entry["disposition"] in ("analyzed", "reused"), "Malformed completed entry.")
            try:
                sample = Sample.from_dict(entry["result"])
            except (TypeError, ValueError) as error:
                raise BatchError("Invalid completed Sample.") from error
            _require(sample.to_dict() == entry["result"], "Sample fields must be explicit.")
            _require(sample.sample_id == entry["sample_id"] and sample.role == m["role"]
                     and sample.analysis_version == m["analysis_digest"]
                     and sample.audio.local_path == str(Path(m["root"]) / Path(path)), "Sample identity/path/policy mismatch.")
        elif status == "pending":
            _require(all(entry[k] is None for k in ("fingerprint", "sample_id", "result", "error", "disposition")), "Malformed pending entry.")
        else:
            _require(entry["result"] is None and entry["disposition"] == "analyzed", "Malformed failed entry.")
            _error_record(entry["error"], ("read", "decode", "extract"))
    for error in m["discovery_errors"]:
        _fields(error, "path error")
        _relative(error["path"], directory=True)
        _error_record(error["error"], ("discovery",))
    _require(m["state"] != "complete" or all(e["status"] != "pending" for e in m["entries"].values()),
             "A completed run cannot contain pending work.")
    return m


def read_manifest(path):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate JSON key/path.")
            result[key] = value
        return result
    def constant(value):
        raise BatchError("Nonfinite JSON value.")
    try:
        return validate_manifest(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=constant))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise BatchError(f"Cannot use existing manifest: {error}") from error


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def local_path(value):
    raw = os.fspath(value)
    _require(isinstance(raw, str) and raw and "\0" not in raw and "://" not in raw
             and not raw.startswith(("\\\\", "//")), "Expected a local filesystem path.")
    path = Path(os.path.abspath(raw))
    if os.name == "nt":
        import ctypes
        _require(ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 4, "Network drives are not supported.")
    for ancestor in (*reversed(path.parents), path):
        try:
            _require(not _linked(ancestor.lstat()), "Symbolic links/junctions in root or destination are not supported.")
        except FileNotFoundError:
            pass
    return path.resolve()


def _destination(path):
    _require(path.suffix.lower() == ".json", "Manifest destination must end in .json.")
    _require(path.parent.is_dir(), "Manifest parent must already be a directory.")
    # Recheck immediately before each replacement, including late-created aliases.
    _require(local_path(path) == path, "Manifest destination changed.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Manifest must be an unlinked regular file, not an audio alias.")


@contextmanager
def manifest_lock(path):
    lock = Path(str(path) + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise BatchError(f"Manifest is locked: {lock.name}. Confirm no writer is active before removing a stale lock.") from error
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock.unlink()


def checkpoint(path, manifest):
    validate_manifest(manifest)
    _destination(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _destination(path)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as error:
        raise BatchError(f"Manifest checkpoint failed: {error}") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _failure(stage, code, message):
    return {"stage": stage, "code": str(code), "message": str(message) or str(code)}


def discover(root):
    found, failures = [], []
    def visit(folder):
        try:
            with os.scandir(folder) as stream:
                children = sorted(stream, key=lambda item: item.name)
        except OSError as error:
            if folder == root:
                raise BatchError(f"Cannot enumerate input root: {error}") from error
            failures.append({"path": folder.relative_to(root).as_posix(),
                             "error": _failure("discovery", "enumeration_failed", error)})
            return
        for child in children:
            p = Path(child.path)
            try:
                info = child.stat(follow_symlinks=False)
                if _linked(info):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    visit(p)
                elif stat.S_ISREG(info.st_mode) and p.suffix.lower() == ".wav":
                    found.append(p.relative_to(root).as_posix())
            except OSError as error:
                failures.append({"path": p.relative_to(root).as_posix(),
                                 "error": _failure("discovery", "entry_unavailable", error)})
    visit(root)
    return sorted(found), failures


def snapshot(path):
    # No link traversal even if a discovered entry was swapped before reading.
    try:
        _require(local_path(path) == path, "Source path changed.")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SourceError("not_file", "Source is no longer a regular file.")
            data = stream.read()
            after = os.fstat(stream.fileno())
        current = path.stat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        # Windows path stat and handle fstat can disagree on creation/ctime.
        # Compare ctime only between handle observations, never across APIs.
        if (signature(before) != signature(after) or before.st_ctime_ns != after.st_ctime_ns
                or signature(after) != signature(current) or len(data) != after.st_size):
            raise SourceError("source_changed", "Source changed while its byte snapshot was read.")
        return data
    except BatchError as error:
        raise SourceError("source_changed", str(error)) from error


def extract(data, path, role, fingerprint, version):
    loaded = audio.load_wav_bytes(data)
    measured = (loudness.measure_loudness(loaded).measurements
                + spectral.measure_spectral(loaded).measurements
                + transient.measure_transient(loaded).measurements)
    tonal = harmony.measure_harmony(loaded)
    measurements = measured + (tonal.fundamental,) + tuple(
        Measurement(name=name, unit=MEASURES[name][0], value=None, unavailable_reason="not_implemented")
        for name in ("stereo_width", "tempo"))
    return Sample(sample_id="sha256:" + fingerprint, role=role,
                  audio=AudioMetadata(local_path=str(path), channels=loaded.channels,
                                      sample_rate_hz=loaded.sample_rate_hz, frame_count=loaded.frame_count,
                                      duration_ms=loaded.duration_seconds * 1000),
                  features=AudioFeatures(measurements=measurements, key=tonal.key), analysis_version=version).to_dict()


def _pending():
    return dict(status="pending", fingerprint=None, sample_id=None, result=None, error=None, disposition=None)


def counts(m):
    values = list(m["entries"].values())
    errors = len(m["discovery_errors"])
    return {"discovered": len(values), "total": len(values) + errors,
            "completed": sum(e["status"] == "complete" for e in values),
            "failed": sum(e["status"] == "error" for e in values) + errors,
            "remaining": sum(e["status"] == "pending" for e in values),
            "analyzed": sum(e["status"] == "complete" and e["disposition"] == "analyzed" for e in values),
            "reused": sum(e["status"] == "complete" and e["disposition"] == "reused" for e in values)}


def progress(m):
    print(canonical({"state": m["state"], **counts(m)}), flush=True)


def run(folder, role, manifest_path):
    _require(role in ROLES, "Invalid explicit role.")
    root, output = local_path(folder), local_path(manifest_path)
    _require(root.is_dir(), "Input root must be an existing local directory.")
    _destination(output)
    with manifest_lock(output):
        old = read_manifest(output) if output.exists() else None
        if old is not None:
            _require(old["root"] == str(root) and old["role"] == role, "Different root/role requires a different manifest.")
        paths, errors = discover(root)
        descriptor = analysis_descriptor()
        version = digest(descriptor)
        current = dict(manifest_schema=MANIFEST_SCHEMA, root=str(root), role=role,
                       analysis_descriptor=descriptor, analysis_digest=version, state="running",
                       entries={p: _pending() for p in paths}, discovery_errors=errors)
        cache = {}
        if old is not None and old["analysis_digest"] == version:
            cache = {e["fingerprint"]: e["result"] for e in old["entries"].values() if e["status"] == "complete"}
        # Verify readable current content before preserving any previous success.
        # Interruption during this preparation leaves the previous manifest intact.
        if cache:
            for relative in paths:
                try:
                    fingerprint = hashlib.sha256(snapshot(root / relative)).hexdigest()
                except (OSError, BatchError):
                    continue  # A normal per-file retry below records a structured error.
                if fingerprint in cache:
                    sample = Sample.from_dict(cache[fingerprint]).to_dict()
                    sample["audio"]["local_path"] = str(root / relative)
                    current["entries"][relative] = dict(status="complete", fingerprint=fingerprint,
                        sample_id="sha256:" + fingerprint, result=sample, error=None, disposition="reused")
        checkpoint(output, current)
        progress(current)
        try:
            for relative in paths:
                if current["entries"][relative]["status"] == "complete":
                    continue
                path = root / relative
                entry, stage = _pending(), "read"
                try:
                    data = snapshot(path)
                    fingerprint = hashlib.sha256(data).hexdigest()
                    entry.update(fingerprint=fingerprint, sample_id="sha256:" + fingerprint)
                    if fingerprint in cache:
                        # Reconstruct distinct paths without sharing mutable wire dictionaries.
                        sample = Sample.from_dict(cache[fingerprint]).to_dict()
                        sample["audio"]["local_path"] = str(path)
                        disposition = "reused"
                    else:
                        stage = "extract"
                        sample = extract(data, path, role, fingerprint, version)
                        disposition = "analyzed"
                        cache[fingerprint] = sample
                    Sample.from_dict(sample)
                    entry.update(status="complete", result=sample, disposition=disposition)
                except audio.AudioReadError as error:
                    entry.update(status="error", disposition="analyzed", error=_failure("decode", error.code, error))
                except Exception as error:
                    code = getattr(error, "code", None) or ("not_found" if isinstance(error, FileNotFoundError)
                           else "access_denied" if isinstance(error, PermissionError)
                           else "io_error" if stage == "read" else "extractor_failure")
                    entry.update(status="error", disposition="analyzed", error=_failure(stage, code, error))
                current["entries"][relative] = entry
                checkpoint(output, current)
                progress(current)
            current["state"] = "complete"
            checkpoint(output, current)
            progress(current)
            return 1 if counts(current)["failed"] else 0
        except KeyboardInterrupt:
            # An interrupt can occur during replacement. Read only durable evidence;
            # never mark an in-memory, uncommitted extraction complete.
            durable = read_manifest(output)
            durable["state"] = "interrupted"
            checkpoint(output, durable)
            progress(durable)
            return 130


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.folder, args.role, args.manifest)
    except KeyboardInterrupt:
        print("Interrupted before a new checkpoint; resume the last valid manifest.", file=sys.stderr)
        return 130
    except (BatchError, OSError) as error:
        print(f"Batch command/storage failure: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
