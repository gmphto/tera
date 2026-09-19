# Pair-rating workflow

One local terminal utility rates assigned kick+bass pairs under the frozen blind protocol in
`_docs/evaluation-protocol.md` (`PROTOCOL_VERSION = "tera-eval-protocol-v1"`). It plays one pair at
a time at one fixed gain, records one of the four labels with a separately recorded skip, keeps
every answer durable, resumes an interrupted session without losing or duplicating an answer and
exports records that satisfy the protocol's "Evidence #17 must record" contract.

The utility is for the evaluator and the operator running a session. It is not a comparison: it
never computes, stores, displays or exports a score, a rank or an arm identity, it imports neither
`backend.palette` nor `backend.intelligence`, and it makes no network call. Sampling, the split
manifest and evaluator assignment are #65; producer sessions are #18; the comparison report is #19.

## Inputs

Two private documents and one frozen public document feed a session.

* **The private dataset** is the #61 pool document: a `dataset_version`, a `sources` mapping whose
  entries name an absolute local `root`, and a `selected` list of records that carry `sample_id`,
  `role` (`kick` or `bass`) and a `mapping` (`source` plus a relative `path`). Audio is resolved
  read-only through `backend.evaluation.manifest.mapping_path` and is never modified.
* **The private pair list** is the #65 assignment: exactly `schema_version` (`"1.0"`),
  `dataset_version`, `split_manifest_digest` (the literal `sha256:` plus 64 lowercase hex characters),
  `sampler_seed`, `assignment_seed` and `pairs`. Each pair carries exactly `pair_id`,
  `kick_sample_id` and `bass_sample_id`; a `pair_id` is a nonblank ASCII string of at most 128
  characters matching `[A-Za-z0-9:_.-]+` and is unique in the file.
* **The protocol document** is `_docs/evaluation-protocol.md` at the repository root, overridable
  with `--protocol-doc`. `start` and `resume` read `PROTOCOL_VERSION`, `ORDER_SEED` and
  `MAX_RECOGNISED_RATE` from its frozen-constants table and refuse the session with
  `protocol_version_mismatch` (missing, unreadable or another version) or
  `protocol_constants_missing` (no pinned `ORDER_SEED` or no `MAX_RECOGNISED_RATE`). The contract is
  never guessed from an issue text and never narrowed to fit one.

`start` validates everything before it writes anything: session id and evaluator id shape, playback
backend and monitoring description, the protocol document, the dataset document, the pair-list
document, every `pair_id` being unique, every kick id resolving to a selected `kick` and every bass
id to a selected `bass`, both files existing as regular local WAV files that
`backend.audio.load_wav` decodes, and both elements sharing one sample rate. Every violation is
printed as one line `<code>: <pair_id>` on stderr — never an absolute audio path — and the command
exits 2 without creating a session directory.

## Where the files live

Every file the utility writes lives under `<cwd>/.local-evaluation/pair-rating/` (already ignored by
`.gitignore`), with sessions at `sessions/<session_id>/`. The private root is never overridable;
any destination outside it is refused with `outside_private_root` and exit 2.

| File | Content |
| --- | --- |
| `session.json` | The ten contract fields, plus only `schema_version`, `state` and the persisted `presentations` |
| `answers.jsonl` | One canonical JSON rating record per line, in answer order, including superseded records |
| `instructions.txt` | The exact evaluator instruction text taken from the protocol document |
| `playback/<presentation_index>.wav` | The current render only; it is deleted once that presentation has an effective answer |
| `export.json` | The export document written by `export` |
| `.lock` | The single-writer lock; a second writer on one session is refused with `session_locked` |

Audio, absolute paths, pack names, evaluator identities, session records, journals, exports and
renders stay on the device: they are never committed, attached to an issue or uploaded. The
utility itself prints no path, no sample id, no file name, no pack name, no dataset version and no
pair id on its interactive surface.

## Commands

```sh
python -m backend.evaluation.rating start --dataset <dataset.json> --pairs <pairs.json> \
    --evaluator-id <code> --monitoring "<what you are listening on>" [--session-id <id>] \
    [--playback-backend command|fake] [--player-command "<player> {audio}"] [--protocol-doc <path>]
python -m backend.evaluation.rating resume --session <session directory> --dataset <dataset.json> \
    [--playback-backend command|fake] [--player-command "<player> {audio}"] [--protocol-doc <path>]
python -m backend.evaluation.rating status  --session <session directory>
python -m backend.evaluation.rating export  --session <session directory> [--output <path>]
python -m backend.evaluation.rating validate --session <session directory>
```

`start` requires `--dataset`, `--pairs`, `--evaluator-id` and `--monitoring`. `resume` needs the
dataset document again because the session record deliberately holds no path; it never needs the
pair list, because the persisted presentation order already fixes every pair. `status`,
`export` and `validate` print their result as one canonical JSON line on stdout.

Exit codes: `0` success or a valid session, `1` a complete session that recorded a failed
playback (and `validate` reporting an invalid record set), `2` a refusal or unreadable
input, `3` paused by `q` or by end of input, `130` interrupted by Ctrl-C.

## The record contract

The private session file carries the ten contract fields plus only `schema_version`, `state`
(`in_progress` or `complete`) and the persisted `presentations`. The export's `session` and
`ratings` blocks carry the contract fields and nothing else, in journal order, so re-running
`export` on an unchanged session writes byte-identical bytes.

### Session fields

| Field | Type | Allowed values |
| --- | --- | --- |
| `session_id` | string | `^[a-z0-9][a-z0-9_-]{0,63}$`; defaults to `uuid4().hex` |
| `evaluator_id` | string | `^[a-z0-9][a-z0-9-]{0,31}$`; an anonymous code, never a name, `@`, whitespace or a path separator |
| `protocol_version` | string | `tera-eval-protocol-v1` |
| `dataset_version` | string | Nonblank; copied from the dataset document |
| `split_manifest_digest` | string | `sha256:` plus 64 lowercase hex characters, copied verbatim from the pair list |
| `order_seed` | string | `tera-eval-order-16-v1`, read from the protocol document |
| `playback_gain_db` | number | `-6.0`, the one fixed gain of the whole session |
| `monitoring_description` | string | Nonblank, at most 200 characters; must begin with `dry-run` when the playback backend is `fake` |
| `started_at` | string | ISO-8601 with the local UTC offset and at least millisecond resolution, for example `2026-09-19T12:34:56.789+10:00` |
| `finished_at` | string or null | Null until every presentation has an effective answer |

### Rating record fields

| Field | Type | Allowed values |
| --- | --- | --- |
| `pair_id` | string | The assigned pair, `[A-Za-z0-9:_.-]{1,128}` |
| `kick_sample_id` | string | The pair's kick sample id |
| `bass_sample_id` | string | The pair's bass sample id |
| `presentation_index` | integer | The 1-based position in the persisted order |
| `rating` | string or null | `poor`, `acceptable`, `good`, `excellent`, or null |
| `skip` | boolean | True for a recorded skip |
| `skip_reason` | string or null | `failed-playback`, `cannot-judge`, `recognised`, `other`, or null |
| `recognised` | boolean | True when the presentation was flagged |
| `playback_completed` | boolean | True only after a playback that exited 0 within the timeout |
| `responded_at` | string | ISO-8601 with the local UTC offset and at least millisecond resolution |
| `correction_of` | integer or null | Null, or the `presentation_index` the record supersedes |

Invariants: `rating != null` implies `skip == false` and `playback_completed == true`;
`skip == true` implies `rating == null` and a non-null `skip_reason`; `skip == false`
implies `skip_reason == null`; a `recognised` skip reason implies `recognised == true`. A
correction repeats the same `presentation_index` and sets `correction_of` to it; the effective
answer for a presentation is its last record in file order, no record is ever rewritten or deleted
and a superseded record stays in the journal and in the export.

The close summary and `status` report counts only: `session_id`, `evaluator_id`, `state`,
`presentations`, `answered`, `skipped` (every effective skip, including a failed playback),
`recognised`, `failed_playback` (effective answers whose playback did not complete) and
`next_presentation_index` (null when every presentation is answered). The close summary adds
`recognised_rate` and `recognised_rate_exceeded`, which is true when the flagged share exceeds
the protocol's `MAX_RECOGNISED_RATE` (0.20). Reporting that number is where this utility stops:
excluding the session and re-running it belongs to #18 and #19.

## Presentation order

`order_seed` is read from the protocol's frozen-constants table and recorded verbatim; no flag
can change it. The order is derived deterministically from that seed, the `session_id` and the
sorted pair ids: every pair id is hashed with SHA-256 over `order_seed + "\n" + session_id + "\n" +
pair_id` and the pairs are ordered by that digest, ties broken by `pair_id` ascending. The
result is a permutation of the assignment with no repeat and no omission.

One repair pass then keeps two consecutive presentations from sharing a kick:
whenever two consecutive presentations share a `kick_sample_id`, the pair at the later position
trades places with the first later, then the first earlier, presentation whose kick differs, and the
swap is kept only when it strictly reduces the number of shared-kick adjacencies. When the
assignment makes separation impossible — for example when every assigned pair uses one kick — no
swap reduces the count, and the two stay adjacent. The resolved order is persisted in the session
file, `resume` reads it instead of recomputing it, and the same inputs always produce the same
persisted order.

## Playback

`backend/evaluation/playback.py` exposes `PLAYBACK_GAIN_DB = -6.0` and one render rule, identical
for every pair in every session:

1. Read both elements with `backend.audio.load_wav` (read-only; the source bytes are never
   modified).
2. Require one shared `sample_rate_hz`; a mismatch is a preflight refusal
   (`sample_rate_mismatch`), never a recorded skip.
3. Broadcast a mono element to the other element's channel count (1 or 2).
4. Align both at frame 0, zero-pad the shorter, sum them.
5. Multiply the sum by `10 ** (PLAYBACK_GAIN_DB / 20)` in float64.
6. Write the result as a lossless float (64-bit) WAV at that sample rate to
   `<session>/playback/<presentation_index>.wav`.

No normalisation, loudness matching, limiting, fading, resampling or per-element gain is applied,
and the gain is recorded in every session as `playback_gain_db`. A render is deleted once its
presentation has an effective answer, is never exported and never appears in a record.

Two backends exist. `command` requires `--player-command` with the `{audio}` placeholder, splits it
with `shlex.split` and runs it without a shell; `playback_completed` is true only when the
player exits 0 within `PLAYBACK_TIMEOUT_SECONDS = 60`, and a missing executable, a non-zero exit
or a timeout is incomplete. `fake` is deterministic, used by the tests, and requires a
`monitoring_description` beginning with `dry-run` so a synthetic run cannot masquerade as a
listening session. The player's stdout and stderr are consumed and discarded — never printed,
logged or written to a record — and the render path is never printed.

## The evaluator's screen

One presentation at a time, no identity of any kind, no feedback and no reveal after the session.
The accepted input grammar:

| Input | Effect |
| --- | --- |
| `poor`, `acceptable`, `good`, `excellent` (or `1` to `4`) | Records that label for the current presentation |
| `s <reason>` | Records a skip with `failed-playback`, `cannot-judge`, `recognised` or `other` |
| `r` | Replays the current pair under the normal playback rule |
| `k` | Toggles the recognition flag for the current presentation without answering |
| `c <presentation_index>` | Returns to an answered presentation, replays it and requires a fresh answer |
| `q` | Pauses: `finished_at` stays null and the session stays resumable (exit 3) |
| `?` | Prints the evaluator instructions again |

A single unknown word is read as a label attempt and refused with `invalid_label`; any other
unknown shape is refused with `unknown_command`; an unknown skip reason is refused with
`invalid_skip_reason`; an unknown or not-yet-answered presentation index is refused with
`unknown_presentation`. A label typed while the last playback did not complete is refused with
`playback_incomplete` and writes nothing; the evaluator may replay with `r` or record a skip.
`s failed-playback` is accepted only when the last playback attempt failed or did not complete
and is refused with `skip_reason_not_applicable` after a completed playback. Every refusal writes
nothing and re-prompts; after an accepted answer the utility prints one progress line with counts
only (`answered N of M`).

A presentation whose first playback attempt fails is recorded immediately — `rating = null`,
`skip = true`, `skip_reason = "failed-playback"`, `playback_completed = false` — and the
session continues with the next presentation, so a disappeared, unreadable or undecodable file
after preflight never becomes a label and never drops the pair from the record. A playback that
fails after a successful attempt (a failed `r` replay) stays on the same presentation in the
incomplete state until the evaluator replays successfully or records `s failed-playback`.

The instructed text is shown in full before the first presentation of a session, the same text is
kept in the session directory as `instructions.txt`, and `?` reprints it at any time.

## Refusal and validation codes

| Code | Meaning |
| --- | --- |
| `outside_private_root` | A session path or `--output` leaves `<cwd>/.local-evaluation/pair-rating/` |
| `invalid_session_id`, `invalid_evaluator_id` | The id does not match its pattern |
| `invalid_monitoring_description` | Blank, longer than 200 characters, or not a dry-run for the fake backend |
| `invalid_player_command` | The command backend has no player command with the `{audio}` placeholder |
| `protocol_version_mismatch` | The protocol document is missing, unreadable or declares another version |
| `protocol_constants_missing` | No pinned `ORDER_SEED`, no `MAX_RECOGNISED_RATE` or no instruction text |
| `schema_mismatch` | The dataset or pair-list document has an unexpected, missing or wrong-typed field |
| `dataset_version_mismatch` | The pair list and the dataset, or a resumed dataset, disagree on `dataset_version` |
| `duplicate_pair_id` | One `pair_id` appears twice |
| `sample_unresolved` | A kick or bass id does not resolve to a selected record of that role |
| `audio_unreadable` | Missing, not a regular local file, a cloud placeholder or undecodable audio |
| `sample_rate_mismatch` | The two elements of one pair do not share a sample rate |
| `session_exists` | A session with this id already exists under the private root |
| `session_locked` | Another writer holds the session lock |
| `evaluator_pair_already_rated` | This evaluator already answered one assigned pair in an earlier session (the code names that session and pair) |
| `session_unreadable`, `journal_unreadable`, `journal_invalid` | The stored session, journal or record set cannot be read |
| `storage_failure` | A private write failed; the previous complete journal is unchanged and the answer is not accepted |

`validate --session <dir>` re-reads the session file, the journal and the export, checks every
field list, type and invariant, the correction rules and the export's agreement with the journal,
and prints one canonical JSON line with `valid` and the named violations. It exits 0 valid, 1
invalid (with the named violations, including `invalid_field_set` for a missing, renamed or
extra field anywhere, `unknown_correction_target` and `duplicate_presentation`) and 2 for
unreadable input. It never repairs a record. Run `export` before `validate`: a missing
`export.json` is reported as `export_missing`.

## What is automated and what is manual

Automated by `tests/test_pair_rating_session.py`: the record contract and its invariants, the
seeded order and its adjacency repair, the render rule (alignment, padding, mono broadcast, the
fixed gain, lossless float output and untouched sources), the command backend (zero exit, non-zero
exit, missing executable, timeout), preflight refusal of every input class, the durable journal and
its failure path, pause and Ctrl-C resume, corrections, the cross-session duplicate guard, the
writer lock, the private-root guard, the export determinism and the named validation violations.

Automated by `tests/test_pair_rating_cli.py`: the five subcommands, the scripted dry run with all
four labels, all four skip reasons (a genuine non-zero player exit, a `recognised` skip and a
`k` flag on a rated presentation), a `c` correction with both records present, `q` then
`resume` with no lost or duplicated answer, preflight refusals for a missing file, a corrupt WAV,
a duplicate pair and a sample-rate mismatch (each exit 2 with no records), a file removed mid-session
recorded as `failed-playback`, a finished `export.json` that `validate` accepts, a transcript
free of every synthetic canary, and the player's output being discarded.

Automated by `tests/test_pair_rating_protocol_conformance.py`: the protocol document exists and
declares `PROTOCOL_VERSION = "tera-eval-protocol-v1"`; the module's supported version, four
labels, four skip reasons, `ORDER_SEED` and `MAX_RECOGNISED_RATE` equal the document's values;
the field names extracted from the protocol's `## Evidence #17 must record` section are exactly the
`session` and `ratings` field sets of the export schema; and `start` and `resume` fail
closed with `protocol_version_mismatch` or `protocol_constants_missing` and write nothing.

The synthetic fixtures under `tests/fixtures/pair-rating/` are a reduced #61-shaped dataset (its
`__AUDIO_ROOT__` placeholder is substituted with the test's temporary audio directory) and a
six-pair list with obviously synthetic ids; the tests write the WAVs with the `wav` helper of
`tests/test_audio.py`.

**Manual, on the development platform**: one real audible run through the `command` backend with
`ffplay`, and the listening judgement of the person assessing the pair. Everything else above is
automated.

### The real audible run

Run from a local working directory whose `.local-evaluation/` is disposable, with a two-pair
assignment:

```sh
python -m backend.evaluation.rating start \
    --dataset <dataset.json> --pairs <pairs.json> \
    --evaluator-id dev-check \
    --monitoring "development platform monitors at the fixed session gain" \
    --session-id audible-check \
    --playback-backend command \
    --player-command "ffplay -nodisp -autoexit -loglevel error {audio}"
```

Answer the first presentation, then `q` to pause. Observable result: the render plays as one
kick+bass pair on the platform's default output, the exit code is `3`, the first journal record
carries `playback_completed: true` (ffplay exited 0 inside the 60-second timeout) and the render
for that presentation is gone from `playback/`.

Development-platform observation on 2026-09-19 (Windows, ffplay `-nodisp -autoexit -loglevel error`):
the command ran to completion with no error output and exit 0, `playback_completed: true` was
recorded for the first presentation, and presentation 2's render remained for the next prompt. The
audible perception itself is the operator's to confirm; the utility cannot and does not judge it.

## Public-safety rules

* Committed content is code, tests, the synthetic fixtures, this document and the frozen protocol
  document only. No real sample id, local path, pack name, audio, evaluator identity or session
  record is ever committed, attached to an issue or uploaded.
* A session's records, journal, export, renders and instruction copy stay under
  `.local-evaluation/pair-rating/` on the device that produced them.
* The evaluator id is an anonymous code. It is recorded so that one evaluator cannot rate one pair
  twice and so that #18 can count evaluators; it is never a name or a contact.
* Nothing tells the evaluator where a pair came from, how it was chosen, whether a machine
  recommended it or how any system scored it — before, during or after the session.

