"""Stored project, palette and palette-item records (issue #24).

A palette is the producer's selection: one active kick, at most one active bass,
and the optional song context (tempo, key, genre) with every field in one of
three distinct states. This module holds the records those rows are read into
and the two pure helpers over them -- :func:`palette_hash` and
:meth:`PaletteRecord.to_context` -- so the content identity of a palette and
its contract assembly are decided without touching a database.

Only the standard library, `backend.contracts`, `backend.analysis.batch`'s
identity helpers and the one coded failure `to_context` raises are imported
here: the module never imports `sqlite3`, never opens a file and never reaches
the network. Reading and writing the rows is `backend.library.repository`'s
job.

Design rules:

- `MVP_SLOTS` is the slot vocabulary and `SLOT_ROLES` the roles each slot
  accepts; the schema's CHECK and the repository's write validation are both
  generated from them, so the three cannot drift. Later roles and later slots
  are new literals and a new migration, never a new item shape.
- `palette_hash` covers content, not history: active items and the context
  values and reasons, and nothing else. `revision`, timestamps, `item_id`
  values and removed items are deliberately outside it, so a no-op or a
  remove-then-re-add that restores the same selection keeps the hash while the
  revision advances.
- `to_context` maps an unset field to `SONG_CONTEXT_ABSENT_REASON` and an
  explicitly unknown field to its stored reason, so no field is ever silently
  defaulted to a value or to certainty.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from backend.analysis.batch import digest
from backend.contracts import PaletteContext, SongContext
from backend.library.errors import PaletteIncomplete


MVP_SLOTS = ("kick", "bass")

# slot -> the roles a stored sample may have for that slot to be selectable.
# The sets are disjoint, so one sample can never be active in two slots.
SLOT_ROLES = {"kick": ("kick",), "bass": ("bass", "sub-bass")}

PALETTE_HASH_VERSION = "palette-hash-v1"

# The reason a palette with no stored context reports: the same literal
# `backend.intelligence.questions.SONG_CONTEXT_ABSENT` uses for an absent song
# context, so the two cannot drift (a test asserts the equality).
SONG_CONTEXT_ABSENT_REASON = "song_context_absent"

CONTEXT_STATES = ("known", "unknown", "unset")


@dataclass(frozen=True)
class ProjectRecord:
    """One stored project and the palette it owns (MVP: exactly one).

    `palette_id` is the project's single palette, or None for a project row
    whose palette row is absent -- a state `create_project` never leaves and
    this module's API cannot reach.
    """

    project_id: str
    name: str
    created_at: str
    updated_at: str
    palette_id: str | None


@dataclass(frozen=True)
class PaletteContextState:
    """The stored state of each song-context field: `known`, `unknown` or `unset`.

    `known` means the value columns are set; `unknown` means they are NULL and
    the field's unavailable reason is stored; `unset` means every column for the
    field is NULL, which is how a palette is created.
    """

    tempo: str
    key: str
    genre: str


@dataclass(frozen=True)
class PaletteItemRecord:
    """One stored selection of a sample for a slot.

    `role` is the role the item is read as: the stored selected role while the
    sample row keeps it, and the sample's current role when issue #72 has
    changed it since -- with `slot_role_mismatch` set when that role is not in
    `SLOT_ROLES[slot]`. `sample_state` is the referenced row's file status
    (`present`, `missing`, `unknown`) or `removed` when no `samples` row
    exists any more (issue #71's pruning); `sample_error_code` carries the
    analysis-queue error code stored for that row, or None.
    """

    item_id: str
    slot: str
    sample_id: str
    role: str
    added_revision: int
    added_at: str
    removed_revision: int | None
    removed_at: str | None
    sample_state: str
    sample_error_code: str | None
    slot_role_mismatch: bool


@dataclass(frozen=True)
class PaletteRecord:
    """One stored palette with its context, its active items and its history.

    `song` is the stored context exactly as the columns hold it (an unset field
    carries no reason); `to_context()` is the contract assembly that fills the
    absent reason in. `active_items` are ordered by slot, `removed_items` by
    the revision that removed them.
    """

    palette_id: str
    project_id: str
    name: str
    revision: int
    song: SongContext
    context_state: PaletteContextState
    active_items: tuple
    removed_items: tuple

    def to_context(self) -> PaletteContext:
        """This palette as a validated `backend.contracts.PaletteContext`.

        Raises `palette_incomplete` when no kick is active: a palette without a
        kick is a valid stored state, and inventing a kick id or an empty string
        for it would put an unmeasured selection into a contract.
        """

        kick = _active(self, "kick")
        if kick is None:
            raise PaletteIncomplete(
                f"palette {self.palette_id} has no active kick; select one before assembling "
                "a context.")
        bass = _active(self, "bass")
        return PaletteContext(
            palette_id=self.palette_id,
            revision=self.revision,
            kick_id=kick.sample_id,
            selected_bass_id=None if bass is None else bass.sample_id,
            song=_context_song(self.song, self.context_state))


@dataclass(frozen=True)
class PaletteMutation:
    """What one palette-mutating repository call did.

    `revision` is the palette's revision after the call: unchanged for a no-op,
    the new value for a committed change. `active_item` is the item a
    `set_palette_item` call left active (None for a removal or a context
    change) and `previous_item` is the item a replacement or a removal took
    out of the slot (None when the slot was empty). Both are None for a context
    change, which touches no item.
    """

    palette_id: str
    revision: int
    changed: bool
    active_item: PaletteItemRecord | None
    previous_item: PaletteItemRecord | None


def _active(record: PaletteRecord, slot: str) -> PaletteItemRecord | None:
    for item in record.active_items:
        if item.slot == slot:
            return item
    return None


def _context_song(song: SongContext, state: PaletteContextState) -> SongContext:
    """The contract song context, with every unset field reporting the absent reason."""

    if state.tempo == "unset":
        song = replace(song, tempo=replace(song.tempo, unavailable_reason=SONG_CONTEXT_ABSENT_REASON))
    if state.key == "unset":
        song = replace(song, key=replace(song.key, unavailable_reason=SONG_CONTEXT_ABSENT_REASON))
    if state.genre == "unset":
        song = replace(song, genre=None, genre_unavailable_reason=SONG_CONTEXT_ABSENT_REASON)
    return song


def palette_hash(record: PaletteRecord) -> str:
    """The SHA-256 content identity of one palette, as 64 lowercase hex.

    The hashed payload is `batch.canonical`'s JSON of

    - `palette_hash_version`: `PALETTE_HASH_VERSION`,
    - `palette_id`: the palette's id,
    - `items`: the active items sorted by slot, each as its slot, sample id and
      read role,
    - `context`: tempo value and confidence, key tonic, mode and confidence,
      genre, and every stored unavailable reason,

    digested with `batch.digest` (SHA-256 over the canonical bytes). `revision`,
    timestamps, `item_id` values, removed items and the referenced samples'
    current availability are outside it: the hash identifies what the palette
    selects, not how it got there. The function reads only the record and never
    raises for a palette whose samples are missing, unknown or pruned.
    """

    payload = {
        "palette_hash_version": PALETTE_HASH_VERSION,
        "palette_id": record.palette_id,
        "items": [
            {"slot": item.slot, "sample_id": item.sample_id, "role": item.role}
            for item in sorted(record.active_items, key=lambda item: (item.slot, item.sample_id))
        ],
        "context": {
            "tempo": {
                "value": record.song.tempo.value,
                "confidence": record.song.tempo.confidence,
                "unavailable_reason": record.song.tempo.unavailable_reason,
            },
            "key": {
                "tonic": record.song.key.tonic,
                "mode": record.song.key.mode,
                "confidence": record.song.key.confidence,
                "unavailable_reason": record.song.key.unavailable_reason,
            },
            "genre": {
                "value": record.song.genre,
                "unavailable_reason": record.song.genre_unavailable_reason,
            },
        },
    }
    return digest(payload)

