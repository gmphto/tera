# Audition controls (#34)

A producer hears one ranked candidate alone, or that candidate together with the
selected kick, from the local file already on the device. The service is not in
the playback path at all: it serves no audio byte and no path, so the client
holds a content identity and a basename and the Tauri shell resolves them inside
a folder the producer registered.

## Files

| file | what it owns |
| --- | --- |
| `auditionTypes.ts` | the modes, the five statuses, the thirteen error codes, the four record states, `AuditionTrack` and `AuditionOutcomeRef` |
| `levelMatch.ts` | the one gain rule |
| `auditionKeys.ts` | the four bindings and `commandForAuditionKey` |
| `auditionSource.ts` | the shell boundary, the closed error mapping with its sentences and states, and the client's half of the root and file-name rules |
| `auditionPlayer.ts` | the two elements, the request sequence, the release |
| `auditionOutcome.ts` | the ten-key `auditioned` body and the one-id-per-start recorder |
| `auditionSlice.ts` | what the control renders, and `auditionOutcomeFor` |
| `AuditionControls.tsx` | the wiring, the readouts and the keyboard |
| `../src-tauri/src/audition.rs` | the root registry, the file resolution and the byte reader |

## The three shell commands

Registered in `main.rs`'s existing `invoke_handler`, with the registry created
once by `.manage(AuditionRegistry::load(paths::data_dir().ok()))`.

| command | arguments | returns |
| --- | --- | --- |
| `register_audition_root` | `path` | `{rootId, rootLabel}` |
| `forget_audition_root` | `rootId` | nothing |
| `read_audition_source` | `sampleId`, `fileName`, `rootId?` | the file's bytes as a raw IPC payload, so `invoke` resolves to an `ArrayBuffer` |

`AUDITION_ROOTS_SCHEMA_VERSION = 1`, `AUDITION_ROOT_LIMIT = 16`,
`SUPPORTED_AUDITION_EXTENSIONS = ("wav",)` and `MAX_AUDITION_BYTES = 67_108_864`
are the shell's constants, and `auditionSource.ts` mirrors the last three.

`register_audition_root` applies #22's whole root rule — `validate_root` in
`backend/library/scanner.py` and `local_path` in `backend/analysis/batch.py`: an
absolute path, no UNC prefix, no `://`, no NUL, an existing directory, not a
mapped network drive (`GetDriveTypeW(anchor) != DRIVE_REMOTE`, declared locally
rather than added as a crate), and no symlink, junction or other reparse point in
the path or any ancestor. It then stores the canonical form and returns the
folder's own name as the label: it never returns or logs the path.

The registry is `{"schema_version": 1, "roots": [{"root_id", "canonical_path"}]}`
in `audition-roots.json` in #30's data directory, written to a sibling temporary
file and renamed over the original. It is reloaded with entries whose canonical
path is no longer a directory pruned, and a missing, unreadable or malformed file
is treated as an empty registry. Nothing else is ever written.

`read_audition_source` accepts `fileName` only as one path component ending in
`.wav` (case-insensitive) with no separator, drive letter, `..` or control
character; resolves it inside the registry (`rootId` first, then the others in
registration order); requires a regular readable file; refuses a file larger than
the cap; opens it **read-only**; hashes the bytes with SHA-256 (implemented in
`audition.rs`, because the module adds no crate and `std` has no digest, and
pinned against the published `""`, `"abc"` and 1000-`a` vectors); and returns them
only when the digest equals `sampleId`.

## The closed error vocabulary

Every code has exactly one sentence and one UI state. `audition-error` carries
the code in `data-code`.

| code | sentence | state |
| --- | --- | --- |
| `shell_unavailable` | This window cannot read local files, so nothing can be auditioned here. | blocked |
| `no_registered_root` | No library folder is registered for playback. Pick the folder you imported. | blocked |
| `root_invalid` | That folder cannot be used for playback. Pick the one you imported. | blocked |
| `root_limit_reached` | Too many library folders are registered. Remove one and try again. | blocked |
| `invalid_file_name` | The stored file name cannot be resolved to a file. | blocked |
| `missing_file` | This file is no longer where the library found it. Re-import its folder. | blocked |
| `unreadable_file` | This file could not be read. | blocked |
| `unsupported_extension` | Only .wav files can be auditioned. | blocked |
| `too_large` | This file is too large to audition. | blocked |
| `content_mismatch` | This file's bytes no longer match the analysed sample. Re-import its folder. | blocked |
| `playback_blocked` | This window refused to start playback. Press play again. | blocked |
| `decode_failed` | This file could not be decoded for playback. | stopped |
| `not_recorded` | It played, but the audition event was not recorded. | not-recorded |

## The two modes and the one gain

`candidate` plays one element at unity. `comparison` plays the candidate and the
selected kick, both from `currentTime = 0`, started in one action — and sample
accurate alignment is **not** claimed anywhere, by any label or accessible name.

Both elements of a comparison get the same fixed
`AUDITION_COMPARISON_GAIN_DB = -6.0`, applied as `volume = 10 ** (-6/20)`. That
number is quoted from #17's frozen render rule (`PLAYBACK_GAIN_DB = -6.0` in
`backend/evaluation/playback.py`, `_docs/pair-rating-workflow.md`), not chosen
here. It is a constant and is never derived from a measurement: a candidate whose
`rms`, `peak`, `crest_factor` and `loudness` are all missing gets exactly the
factor a populated one gets, and no per-element difference, normalisation,
limiting, fading or resampling is applied. The label therefore reads
`-6.0 dB fixed comparison gain` (or `0.0 dB` alone) and claims no loudness or
level match.

## The keyboard

Attached to the `audition-controls` element only, never to `document` or
`window`. Mapped by `event.code`, so the layout's letter never matters.

| key | command |
| --- | --- |
| `Space` | play or stop this candidate |
| `C` | compare with the selected kick, from the start |
| `R` | replay from the start |
| `Esc` | stop and release |

Ignored, without `preventDefault`: a held key (`event.repeat`), any key whose
target is an `input`, `textarea`, `select` or contenteditable element, a `Space`
on one of the control's own buttons (the button activates natively, so handling
the key too would toggle twice), and every unbound key.

## Elements

`audition-controls` (focusable, `role="group"`), `audition-play`,
`audition-stop`, `audition-compare` (`aria-pressed`), `audition-status` (with
`data-status`), `audition-gain`, `audition-latency` (with `data-latency-ms`),
`audition-record-state` (with `data-record-state`), `audition-retry-record`
(only while an event is unrecorded), `audition-error` (with `data-code`) and
`audition-legend`. Every button has an accessible name; when the shell is absent
or a refusal is visible, play and compare are disabled and reference the error
element with `aria-describedby`.

`audition-latency` is measured from the activating press to the element's
`playing` event with `performance.now()`. No threshold is claimed here:
`MAX_AUDITION_START_P95_MS = 250` belongs to #37/#39.

## Names this task reused, and two edits outside its list

Both endpoints #34's criterion asked for already existed, so `app/api.ts` is
byte-unchanged:

| the criterion says | landed | where |
| --- | --- | --- |
| a `getSample` read | `useGetSampleQuery` (#31) | `features/palette/api/libraryApi.ts` |
| a `recordOutcome` mutation | `useRecordOutcomeMutation` (#33) | `features/palette/api/recommendationsApi.ts` |

Two edits fall outside the criterion's named list, and both are load-bearing:

1. **`features/palette/browser/LibraryBrowser.tsx`** — one import and one call.
   The criterion requires the frontend to register the folder the producer
   picked, and the only place that path exists is the picker's own call
   (`LibraryBrowser.onPick`); #34's file list cannot reach it any other way, and
   without it no root is ever registered and every audition is
   `no_registered_root`. A refusal there is swallowed on purpose: an import that
   worked is not undone by playback failing to resolve it, and the producer sees
   the real reason on the first press.
2. **`api/recommendationsApi.ts`** — `OUTCOME_EVENT_TYPES` widened from #33's two
   to the four #29 accepts. #33 declared only the events it sends and noted that
   `auditioned` was #34's; the union is the route's vocabulary, so #34 sends its
   own event without a second body builder or a cast.

Also edited, as the criterion allows: `app/store.ts` (one reducer),
`RecommendationCard.tsx` (one import, one `<AuditionControls/>` element) and
`recommendations/render.test.ts` (its store now carries the api slice and the
audition reducer, because the cards it renders now contain the controls).

## What has been run, and what has not

Run: `npx tsc --noEmit` clean; `npx vitest run` 329 tests passing (11 files
touched or added by this task; 66 new cases across the five named test files);
`cargo check --manifest-path frontend/src-tauri/Cargo.toml` clean with
`Cargo.toml` and `Cargo.lock` byte-unchanged; `cargo test --bins` 11 passing,
including the three new audition cases and the SHA-256 vectors. No Python file
changed, so `uv run pytest` was not re-run for this task.

**Not run: the manual scenario.** No window was opened, so there is no recorded
state table, no measured start latency, no `Get-FileHash` before/after, no
observed comparison start delta and no `GET /outcomes?event_type=auditioned`
count. `npm run tauri dev` opens a window and spawns its own service, and the
criterion's scenario needs a producer pressing keys while the service runs. The
turn that finishes #34 must record this as an open check rather than claim it —
and note that #175 (a scanned library's sample id is not the content identity)
means the palette cannot hold a kick yet, so a live run would stop before any
card exists to audition.
