# Private feasibility pool, schema 1.0

The collected pool is a **documented shortfall, not an evaluation-ready 100/100
dataset**: 81 unique readable kicks and 3 bass samples. Missing coverage is 19
kicks and 97 basses. No human ratings, audition judgments, or perceptual diversity
claims were fabricated. This bounded collection task permits an honest shortfall;
later evaluation needs additional verified material or safe format preparation.

Actual configuration, library mappings, pack/sample names, content IDs, dataset
and detailed validation records live only in `.local-evaluation/`, which is
gitignored. No source audio was copied, modified, rendered or uploaded. The
committed example and tests are synthetic and do not contribute to these counts.

## Reproduce locally

```text
uv run python -m backend.evaluation.manifest build .local-evaluation/sources.json --output .local-evaluation/dataset.json --report .local-evaluation/validation.json
uv run python -m backend.evaluation.manifest validate .local-evaluation/dataset.json --report .local-evaluation/validation.json
```

For another evaluator, copy the structure in `evaluation-sources.example.json`
to a private ignored directory, map their own absolute local roots, and replace
the example category/evidence fields with verified source evidence. The example
is deliberately `synthetic_fixture`, which can never be admitted as real coverage.
Do not relabel generated audio as factory material. Real source configuration is
an evidence declaration requiring review, not an automatic certificate of truth.

Sources are ordered by integer priority, then source ID; files use normalized
relative POSIX paths, then SHA-256 as a final deterministic tie-breaker. This run
prioritizes the current factory installation, actual downloaded Sounds, then the
older installation. The older copy verifies duplicates rather than inflating
coverage. One representative of each eligible full-byte content group is selected
up to 100 per role; excess unique eligible records become reserves. Duplicate
content with conflicting roles is excluded entirely. Equal hashes across names,
roots and installations retain duplicate mapping evidence and a shared ID. Byte
deduplication does not establish perceptual independence of re-encoded sounds or
related instrument multisamples.

Build reads confirmed-role WAV candidates using the strict reader and immutable
snapshot hashing. Presets/non-WAV files are ignored, never rendered. Symbolic
directory links/junctions are not followed. Offline/recall-on-access cloud files
are excluded without downloading/hydrating them. Reader failures keep actionable
local reasons and never count as readable coverage. Filename-only matches remain
pending without being decoded/admitted. Other unrelated WAVs are excluded from
the role pool. Source files are read-only; outputs must be JSON files outside all
source roots and cannot alias a source, configuration or input dataset. Writes
use a flushed, fsynced temporary file and atomic replacement. Existing unrelated
outputs are rejected. This is a local collection tool, not the resumable feature
analysis command from issue #9.

Exit 0 means schema/content validation succeeded, even if there is a documented
shortfall; exit 1 means revalidation/scan failures; exit 2 means command/schema/
storage failure. `summary` records the declared collection ledger; validation
also reports `verified_selected` and `verified_shortfall` based on current reads.
A stale/missing selected mapping loses verified coverage immediately. Rebuild to
replace stale records and deterministically select newly available reserves.

## Role and provenance policy

Exact source-relative category-to-role rules and their evidence are in the
private source configuration. This run admits only explicit manufacturer kick
categories as kick and explicit bass instrument/bass guitar categories as bass.
It does not infer bass from bass-drum filenames. Manufacturer categories resolve
the role only within their explicitly configured subtree. Each rule has a stable
policy ID, exact folder path, role, optional subtype and evidence statement.
Overlapping contradictory rules produce pending `conflicting_role_evidence`.
An exact reviewed per-file mapping is supported, but none was invented for this
run. Such mappings must record the actual review/source evidence; they are not
automatically auditions. Confirmed sub-bass maps to bass with subtype `sub-bass`.

Unconfirmed names containing kick/bass/808/sub are merely discovery hints. An
ambiguous 808, mixed-role loop or bass-drum name is not enough to admit material.
Downloaded pack metadata inspected locally identified packs but did not supply
per-file instrument roles, so those filename candidates remain pending. Demo,
preview and Misc-marked material and unresolved provenance are ineligible.
No downloaded file was assumed to prove an account purchase.

The local-use basis is `user_authorized_local_evaluation_only`, reflecting the
user's authorization to analyze their available libraries locally. It is separate
from redistribution rights. Source categories distinguish `installed_factory`,
`downloaded_sounds`, `demo_preview`, `unresolved`, and `synthetic_fixture`.
Existing local pack evidence stays at its original location and is referenced in
private provenance statements; no account license certificate was asserted.

Image-Line's [copyright guidance](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/app_copyright.htm)
distinguishes included production content from restricted demo-project/Misc and
streamed preview content. Its [downloaded Sounds guidance](https://support.image-line.com/action/knowledgebase?ans=718)
describes production use after downloading and excludes promotional demos and
undownloaded previews. These sources inform provenance handling; they do not
authorize distributing this evaluation pool's sample files. No audio or private
manifest should be attached to issues or committed.

## JSON contracts and identity

Configuration requires exactly `schema_version` (`1.0`) and `sources`. Each
source requires `root`, nonnegative integer `priority`, `kind`,
`provenance_evidence`, `local_use_basis`, `role_rules`, and `reviewed_files`.
Rules/mappings have exactly `id`, `path`, `role` (`kick`/`bass`), `subtype`
(null or bass-only `sub-bass`), and nonblank `evidence`. Rules apply to descendants
of an exact folder; reviewed mappings match one exact relative file. Source roots
are private machine mappings and may be relocated without changing identity.

Dataset top-level fields are exactly:

- `schema_version`, `dataset_version`, `sources`.
- `policy`: `selection_version`, `provenance_version`, `target_per_role` (100).
- `ignored_non_wav` and `scan_errors` (local source/message records).
- `selected`, `reserves`, `duplicates`, `pending`, `excluded`: ordered record arrays.

Every record requires these fields, including explicit nulls where unavailable:

| Field | Meaning |
| --- | --- |
| `mapping` | `{source, path}`; source ID and normalized relative WAV path |
| `sample_id`, `sha256` | `sha256:` plus full-byte hash / lowercase 64-hex hash; both null if not decoded successfully |
| `role` | Confirmed kick/bass or null; must agree with configured evidence |
| `candidate_roles` | Filename discovery hints; never role proof |
| `pack` | Source-relative first directory, or `root` for top-level material |
| `role_evidence` | `{method, rule_id, subtype}` or null; method trusted_category/reviewed_mapping |
| `source_kind` | Source category above |
| `provenance_kind` | `real_library_sample` or `synthetic_fixture` |
| `local_use_basis` | Explicit local authorization token above |
| `metadata` | Decoded rate, channels, frame count, duration seconds, subtype, or null |
| `reason` | null for eligible representatives; duplicate/exclusion/pending reason otherwise |

Selected/reserve/duplicate records require real eligible provenance, confirmed
role, decoded metadata and hash. Selected/reserve IDs cannot repeat; duplicate
records intentionally share their deterministic representative's content ID.
Pending/excluded reasons are required. Unknown schemas/policies, duplicate JSON
keys/local mappings, invalid IDs, roles or metadata, unknown fields, mismatched
evidence and nondeterministic/tampered selected membership are rejected.

Dataset identity is SHA-256 over sorted-key, ASCII-escaped canonical JSON with
`,`/`:` separators and finite values only. It includes selected IDs/content,
roles, source/pack provenance, evidence/subtype, local-use basis, source priorities
and role-policy definitions, plus selection/provenance-policy versions. Absolute
root mappings are excluded. Changing only roots preserves identity; changing
content, membership, role or included policy changes it. Change the relevant
policy version when selection/provenance behavior changes. This integrity digest
detects accidental tampering; it is not a digital signature or proof that a
maliciously rewritten provenance claim is true.

Validation recomputes hashes and strict decoded metadata for all selected,
reserve and duplicate mappings. It checks membership against the complete stored
ledger and canonical version. It does not rescan new files during validation;
`build` rescans sources and records a new collection. Failed reads are detailed
privately and removed from `verified_selected` coverage.

## Actual aggregate evidence

The local build and repeat validation both completed successfully. Across the
three sources the scan recorded 6,728 WAV mappings and 4,142 ignored non-WAV files;
zero enumeration errors. Two installations contain duplicate material. Totals:
84 selected unique records, 84 readable duplicate mappings, zero reserves,
439 pending mappings and 6,121 excluded mappings (mostly outside the role pool).

| Measure | Kick | Bass |
| --- | ---: | ---: |
| Discovered role candidates | 668 | 302 |
| Role-confirmed mappings | 276 | 250 |
| Strictly readable mappings | 162 | 6 |
| Readable duplicate mappings | 81 | 3 |
| Excluded role candidates | 114 | 246 |
| Pending role candidates | 392 | 50 |
| Selected unique readable | **81** | **3** |
| Shortfall to 100 | **19** | **97** |

Hint-based candidate counts can overlap roles (for example an ambiguous
bass-drum name); they are not additive coverage. Bass exclusions include 244
confirmed-role decoder failures plus two unconfirmed excluded candidates.

Confirmed-role rejection diagnostics, including both installations:

| Role | Strict rejection | RIFF format tag | Header bits | Mappings |
| --- | --- | --- | ---: | ---: |
| Kick | Unsupported encoding | 0x674F | 16 | 96 |
| Kick | Unsupported encoding | 0x6750 | 16 | 18 |
| Bass | Unsupported encoding | 0x674F | 16 | 164 |
| Bass | Truncated chunk payload/padding | 0x674F | 16 | 80 |

A bounded direct SoundFile probe of one representative from each group also
failed opening with a malformed format-chunk error. No approved-stack successful
decoding/conversion was demonstrated. These facts do not establish that the
content is truly corrupt, nor that padding repair is safe. The strict reader was
not weakened. Next collection need: investigate these containers and a safe,
separate preparation path, or obtain additional authorized, role-confirmed
readable PCM/float sources, then rebuild/revalidate. Until coverage is resolved,
the 97-bass shortfall blocks the intended balanced evaluation.

Offline tests use generated fixtures only, including explicitly ineligible
synthetic provenance and simulations of external source claims. They exercise
schema/role/provenance errors, duplicate IDs/content/conflicts, deterministic
selection/reserves, machine root remapping, missing/unreadable/invalid/stale
sources, cloud placeholders, output aliases and the CLI. Focused validation:
23 passed. No synthetic fixture was included in the actual pool.

```text
uv run pytest tests/test_evaluation_manifest.py --basetemp .pytest_cache/pool-focused
uv run pytest --basetemp .pytest_cache/pool-final
```
