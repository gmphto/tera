/**
 * The context editor's draft and its three parsers (issue #32).
 *
 * A producer types a tempo, a key and a genre; each field is either a value they
 * declared, explicitly unknown, or unset. The parsers are pure, so the refused
 * inputs are covered in a node-environment test rather than in a component.
 */

import type { ContextFieldWire } from "../api/paletteApi";

export const CONTEXT_FIELDS = ["tempo", "key", "genre"] as const;
export type ContextField = (typeof CONTEXT_FIELDS)[number];

/** What the client sends for a field the producer marked unknown. */
export const CLIENT_UNKNOWN_REASON = "producer_marked_unknown";

/** The same bound the route enforces. */
export const MAX_CONTEXT_TEXT_LENGTH = 64;

/** The contract's key vocabulary, mirrored so the client refuses before it sends. */
export const TONICS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"] as const;
export const MODES = ["major", "minor"] as const;

export interface FieldDraft {
  state: "known" | "unknown" | "unset";
  /** What the input shows for a `known` field. */
  value: string;
}

export interface ContextDraft {
  tempo: FieldDraft;
  key: FieldDraft;
  genre: FieldDraft;
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; field: string };

/** A positive, finite number of beats per minute. */
export function parseTempo(text: string): Parsed<number> {
  const trimmed = text.trim();
  if (trimmed === "") {
    return { ok: false, field: "tempo.bpm" };
  }
  if (!/^\d+(\.\d+)?$/.test(trimmed)) {
    return { ok: false, field: "tempo.bpm" };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value <= 0) {
    return { ok: false, field: "tempo.bpm" };
  }
  return { ok: true, value };
}

/** `"C major"`, `"F# minor"` and nothing else. */
export function parseKey(text: string): Parsed<{ tonic: string; mode: string }> {
  const parts = text.trim().split(/\s+/).filter((part) => part !== "");
  if (parts.length !== 2) {
    return { ok: false, field: "key" };
  }
  const [tonic, mode] = parts;
  if (!(TONICS as readonly string[]).includes(tonic) || !(MODES as readonly string[]).includes(mode)) {
    return { ok: false, field: "key" };
  }
  return { ok: true, value: { tonic, mode } };
}

/** A nonblank genre inside the bound; a blank one means the producer cleared it. */
export function parseGenre(text: string): Parsed<string | null> {
  const trimmed = text.trim();
  if (trimmed === "") {
    return { ok: true, value: null };
  }
  if (trimmed.length > MAX_CONTEXT_TEXT_LENGTH) {
    return { ok: false, field: "genre" };
  }
  return { ok: true, value: trimmed };
}

function fieldWire(
  draft: FieldDraft,
  known: () => Parsed<Record<string, unknown> | null>,
): Parsed<ContextFieldWire> {
  if (draft.state === "unknown") {
    return { ok: true, value: { state: "unknown", reason: CLIENT_UNKNOWN_REASON } };
  }
  if (draft.state === "unset") {
    return { ok: true, value: { state: "unset" } };
  }
  const parsed = known();
  if (!parsed.ok) {
    return parsed;
  }
  // A null from the field's own parser is the producer clearing it: a blank
  // genre is `unset`, not a known empty genre.
  if (parsed.value === null) {
    return { ok: true, value: { state: "unset" } };
  }
  return { ok: true, value: { state: "known", ...parsed.value } };
}

export interface ContextRequest {
  tempo: ContextFieldWire;
  key: ContextFieldWire;
  genre: ContextFieldWire;
}

/** The three wire objects the route documents, or the first refused field. */
export function toContextRequest(draft: ContextDraft): Parsed<ContextRequest> {
  const tempo = fieldWire(draft.tempo, () => {
    const parsed = parseTempo(draft.tempo.value);
    return parsed.ok ? { ok: true, value: { bpm: parsed.value } } : parsed;
  });
  if (!tempo.ok) {
    return tempo;
  }
  const key = fieldWire(draft.key, () => {
    const parsed = parseKey(draft.key.value);
    return parsed.ok ? { ok: true, value: { tonic: parsed.value.tonic, mode: parsed.value.mode } } : parsed;
  });
  if (!key.ok) {
    return key;
  }
  const genre = fieldWire(draft.genre, () => {
    const parsed = parseGenre(draft.genre.value);
    return parsed.ok ? { ok: true, value: parsed.value === null ? null : { genre: parsed.value } } : parsed;
  });
  if (!genre.ok) {
    return genre;
  }
  return {
    ok: true,
    value: { tempo: tempo.value, key: key.value, genre: genre.value },
  };
}

function draftFrom(field: ContextFieldWire, known: (field: ContextFieldWire) => string): FieldDraft {
  if (field.state === "unknown") {
    return { state: "unknown", value: "" };
  }
  if (field.state === "unset") {
    return { state: "unset", value: "" };
  }
  return { state: "known", value: known(field) };
}

/** The stored context as an editable draft, round-tripping all three states. */
export function fromPaletteContext(context: {
  tempo: ContextFieldWire;
  key: ContextFieldWire;
  genre: ContextFieldWire;
}): ContextDraft {
  return {
    tempo: draftFrom(context.tempo, (field) =>
      field.state === "known" && field.bpm !== undefined ? String(field.bpm) : ""),
    key: draftFrom(context.key, (field) =>
      field.state === "known" && field.tonic && field.mode ? `${field.tonic} ${field.mode}` : ""),
    genre: draftFrom(context.genre, (field) =>
      field.state === "known" && field.genre !== undefined ? field.genre : ""),
  };
}

/** Whether every known field parses; a blank genre means unset, and is allowed. */
export function draftIsSavable(draft: ContextDraft): boolean {
  if (draft.tempo.state === "known" && !parseTempo(draft.tempo.value).ok) {
    return false;
  }
  if (draft.key.state === "known" && !parseKey(draft.key.value).ok) {
    return false;
  }
  if (draft.genre.state === "known") {
    const genre = parseGenre(draft.genre.value);
    if (!genre.ok) {
      return false;
    }
  }
  return true;
}

export function draftIsClean(draft: ContextDraft): boolean {
  return draft.tempo.state === "unset" && draft.key.state === "unset" && draft.genre.state === "unset";
}
