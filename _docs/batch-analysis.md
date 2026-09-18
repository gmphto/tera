# Resumable local batch analysis

From the repository root:

```text
uv run python -m backend.analysis.batch FOLDER --role bass --manifest OUTPUT.json
```

Role is required and must be `kick`, `bass`, or `sub-bass`; it applies to every
file in this run. Names never determine role. FOLDER must be a local directory;
the manifest parent must already exist. The command recursively discovers regular
files with case-insensitive `.wav` suffixes, sorts POSIX-style relative paths
lexically, and fixes that list for the run. Later additions wait for a new run;
deleted historical entries are dropped on a fresh scan. An empty folder succeeds.
Symlinks and Windows junction/reparse entries are skipped; roots/destinations with
linked ancestors, UNC paths and mapped network drives are rejected. Root scan
failure stops the command; nested scan/stat failures are visible discovery errors.

Output must end in `.json`. Existing outputs must be valid manifests for the same
canonical root and explicit role. Unrelated files and malformed manifests are
never overwritten. Output symlinks, junctions and multiply hardlinked files are
rejected, preventing aliases of source audio. Destination checks repeat before
atomic replacement. Source audio is opened read-only; no sidecars, telemetry or
network services are used. Paths, errors and manifests contain private local data
and must remain on the device.

## Manifest schema 1.0

All fields below are required; unknown fields, duplicate JSON keys (including
paths), nonfinite numbers and unsupported schemas fail validation before reuse.

| Top-level field | Meaning |
| --- | --- |
| `manifest_schema` | Literal `1.0` |
| `root` | Canonical absolute local input directory |
| `role` | Explicit role for this manifest |
| `analysis_descriptor` | Batch/reader/contract/extractor/configuration/runtime policies below |
| `analysis_digest` | Lowercase 64-character SHA-256 of canonical descriptor |
| `state` | `running`, `complete`, or `interrupted` |
| `entries` | Object keyed by normalized relative WAV path, no absolute/traversal/backslash paths |
| `discovery_errors` | Array of `{path, error}` records for failed nested enumeration/stat operations |

Each entry has exactly `status`, `fingerprint`, `sample_id`, `result`, `error`,
and `disposition`:

| Status | Fingerprint / sample ID | Result | Error | Disposition |
| --- | --- | --- | --- | --- |
| `pending` | null / null | null | null | null |
| `complete` | SHA-256 / `sha256:` plus SHA-256 | Full validated schema-1.0 Sample | null | `analyzed` or `reused` |
| `error` | Hash and ID when snapshot was readable, otherwise both null | null | Structured error | `analyzed` |

Error objects contain nonblank `stage`, `code`, `message`. Stages are `read`,
`decode`, `extract`, or (for discovery records only) `discovery`. Stable read codes
are `not_found`, `access_denied`, `not_file`, `source_changed`, and `io_error`;
decode codes come from AudioErrorCode; unexpected extractor failures use
`extractor_failure`. Discovery uses `enumeration_failed` or `entry_unavailable`.
Messages explain the local failure without pretending a failed file is a Sample.

Completed Samples contain the original path, explicit role, native reader
metadata, all loudness/spectral/transient measurements, harmony F0 and independent
key, and the combined digest as `analysis_version`. Every contract measurement
appears once; tempo/stereo width are null with `not_implemented`. Existing
extractor uncertainty reasons remain intact. Sample IDs depend solely on full
source bytes, so rename/duplicates preserve identity but have separate path
entries. Roles never migrate between manifests through reuse.

## Versioning, byte identity and reuse

Canonical JSON uses sorted keys, ASCII escaping, no NaN/Infinity and separators
`,` / `:` with no whitespace; SHA-256 hashes its UTF-8 bytes. The descriptor stores
`batch`, `reader`, `contract`, all four named extractor versions, `configuration`
(extractor defaults, unimplemented measures, immutable snapshot/native channel/
explicit role policies), and `runtime` versions of Python, NumPy, SciPy, SoundFile
and bundled libsndfile. Every constituent participates in the digest. Thresholds
remain the versioned extractor defaults documented in their respective docs;
changing them requires the owning extractor version to change. Change batch or
reader version for corresponding policy changes, contract version for wire
changes, and add explicit configuration values for future configurable behavior.
Never change semantics while retaining their identifying version.

Each source is read into one immutable byte snapshot. SHA-256 and
`load_wav_bytes` both consume those exact bytes; extraction never reopens the
path. File identity/size/mtime are checked against the open handle before/after
reading and the current path. Handle ctime is also compared before/after (Windows
handle/path APIs can disagree on ctime even for unchanged files). Detected changes
are errors. Even if a source is replaced after snapshot acquisition, its completed
record describes precisely the hashed snapshot; the next run hashes current bytes
again. Mtime/size never substitute for content hashes.

Reuse requires a valid successful Sample, matching root/role/digest, and freshly
read matching content. Pending/error entries are retried. Same-size edits with
restored mtime are reanalyzed. Successful duplicate content can share extraction,
but results are reconstructed with separate paths. Reuse and source reads still
occur locally. Processing retains one snapshot/decoded file at a time; this is a
full-file analysis rather than a streaming decoder.

## Checkpoints, progress and recovery

An exclusive `OUTPUT.json.lock` contains the writer PID. A second writer fails
clearly instead of racing. Before extraction, discovered pending work and all
verified reusable completions are checkpointed. Each success/error is checkpointed
separately: a uniquely named temporary file in the same directory is written,
flushed, fsynced and closed, then atomically replaces the manifest. On POSIX the
directory is also fsynced. A process termination around replacement leaves the
old or new complete JSON, not partially edited live JSON. Stale temporary files
are never evidence of completion. Power-loss durability ultimately depends on the
filesystem/storage guarantees; the Windows directory is not separately fsynced.

Progress emits JSON lines with state, `discovered` WAV count, `total` including
discovery errors, `completed`, `failed` including discovery errors, `remaining`,
and separate successful `analyzed`/`reused` counts. Failed analyses are counted in
`failed`, not successful `analyzed`. Output follows durable checkpoints; final
counts agree with the persisted manifest. `complete` means processing finished,
not that it was error-free; it cannot contain pending work.

| Exit | Meaning |
| --- | --- |
| 0 | Finished, no file/discovery errors (including empty folder) |
| 1 | Finished with one or more file/discovery errors |
| 2 | Invalid command/root/destination/manifest, active or stale lock, or storage failure |
| 130 | Interrupted; last durable checkpoint is resumable |

Ctrl-C during processing reloads durable state before marking it interrupted,
so uncommitted work is never reported complete. Interruption before the initial
checkpoint leaves the previous manifest intact (or no new manifest). A second
interrupt or storage failure while recording interruption can leave `running`;
that is still resumable. Graceful exit removes its own lock. Abrupt termination
can leave a lock: confirm that its PID/writer is no longer active, then remove
only that manifest's `.lock` and rerun the identical command. Optional stale
`OUTPUT.json.*.tmp` artifacts can be removed once no writer is active; never
promote them to the manifest. Do not delete/repair malformed manifests implicitly:
inspect or restore a known valid copy, or choose a new output filename. New root
or role requires a different manifest.

## Repeatable synthetic demonstration and validation

This separate fixture-generation command writes only synthetic test data under
the ignored pytest cache; it is not part of normal analysis:

```text
uv run python -c "from pathlib import Path; import numpy as np; from tests.test_audio import wav; p=Path('.pytest_cache/batch-demo'); p.mkdir(parents=True,exist_ok=True); data=wav((.3*np.sin(2*np.pi*110*np.arange(19200)/48000))[:,None]); (p/'tone.wav').write_bytes(data); (p/'duplicate.WAV').write_bytes(data)"
uv run python -m backend.analysis.batch .pytest_cache/batch-demo --role bass --manifest .pytest_cache/batch-demo.json
uv run python -m backend.analysis.batch .pytest_cache/batch-demo --role bass --manifest .pytest_cache/batch-demo.json
```

The fresh run reports one analyzed and one reused completion; the second reports
two reused and zero analyzed completions. Source bytes remain unchanged. Automated
offline coverage includes these subprocess commands, complete Sample round trips,
same-size/mtime edits, all descriptor constituents, duplicate/renamed content,
malformed manifests, read/decode/extractor/discovery errors and retries, concurrent
locks, source mutation, Ctrl-C/resume, and interruption before/after replacement.

```text
uv run pytest tests/test_batch.py tests/test_batch_recovery.py --basetemp .pytest_cache/batch-focused
uv run pytest --basetemp .pytest_cache/batch-final
```

Focused validation: 51 passed. No real-library audio or paths are part of this
evidence; dataset collection remains issue #10.
