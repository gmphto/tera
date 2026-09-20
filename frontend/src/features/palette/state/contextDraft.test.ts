import { describe, expect, it } from "vitest";

import {
  CLIENT_UNKNOWN_REASON,
  MAX_CONTEXT_TEXT_LENGTH,
  draftIsSavable,
  fromPaletteContext,
  parseGenre,
  parseKey,
  parseTempo,
  toContextRequest,
  type ContextDraft,
} from "./contextDraft";

function draft(tempo: ContextDraft["tempo"], key: ContextDraft["key"], genre: ContextDraft["genre"]): ContextDraft {
  return { tempo, key, genre };
}

const KNOWN = (value: string) => ({ state: "known" as const, value });
const UNSET = { state: "unset" as const, value: "" };
const UNKNOWN = { state: "unknown" as const, value: "" };

describe("parseTempo", () => {
  it("parses a whole and a decimal bpm", () => {
    expect(parseTempo("140")).toEqual({ ok: true, value: 140 });
    expect(parseTempo(" 174.5 ")).toEqual({ ok: true, value: 174.5 });
  });

  it("refuses every input the route refuses", () => {
    for (const text of ["", "0", "-1", "abc", "Infinity", "1.5.5", "NaN", "1e3"]) {
      const parsed = parseTempo(text);
      expect(parsed.ok, text).toBe(false);
      expect(parsed.ok ? null : parsed.field).toBe("tempo.bpm");
    }
  });
});

describe("parseKey", () => {
  it("accepts the contract literals", () => {
    expect(parseKey("C major")).toEqual({ ok: true, value: { tonic: "C", mode: "major" } });
    expect(parseKey("F# minor")).toEqual({ ok: true, value: { tonic: "F#", mode: "minor" } });
    expect(parseKey("  A#   major ")).toEqual({ ok: true, value: { tonic: "A#", mode: "major" } });
  });

  it("refuses anything outside them", () => {
    for (const text of ["H major", "C dorian", "C", "major C", "", "C major extra"]) {
      const parsed = parseKey(text);
      expect(parsed.ok, text).toBe(false);
      expect(parsed.ok ? null : parsed.field).toBe("key");
    }
  });
});

describe("parseGenre", () => {
  it("trims a value and treats a blank one as cleared", () => {
    expect(parseGenre("  grime ")).toEqual({ ok: true, value: "grime" });
    expect(parseGenre("   ")).toEqual({ ok: true, value: null });
  });

  it("refuses a text past the bound the route enforces", () => {
    const parsed = parseGenre("c".repeat(MAX_CONTEXT_TEXT_LENGTH + 1));
    expect(parsed.ok).toBe(false);
    expect(parsed.ok ? null : parsed.field).toBe("genre");
  });
});

describe("toContextRequest", () => {
  it("serializes each state exactly as the route documents it", () => {
    const request = toContextRequest(
      draft(KNOWN("140"), KNOWN("C major"), { state: "unknown", value: "" }),
    );
    expect(request).toEqual({
      ok: true,
      value: {
        tempo: { state: "known", bpm: 140 },
        key: { state: "known", tonic: "C", mode: "major" },
        genre: { state: "unknown", reason: CLIENT_UNKNOWN_REASON },
      },
    });
  });

  it("sends unset for a cleared field and refuses a known one that does not parse", () => {
    expect(toContextRequest(draft(UNSET, UNSET, UNSET))).toEqual({
      ok: true,
      value: { tempo: { state: "unset" }, key: { state: "unset" }, genre: { state: "unset" } },
    });
    const refused = toContextRequest(draft(KNOWN("abc"), UNSET, UNSET));
    expect(refused.ok).toBe(false);
    expect(refused.ok ? null : refused.field).toBe("tempo.bpm");
  });

  it("treats a blank genre the producer typed as unset", () => {
    const request = toContextRequest(draft(UNSET, UNSET, KNOWN("   ")));
    expect(request).toEqual({
      ok: true,
      value: { tempo: { state: "unset" }, key: { state: "unset" }, genre: { state: "unset" } },
    });
  });

  it("never sends a value for a field marked unknown", () => {
    const request = toContextRequest(draft(UNKNOWN, UNKNOWN, UNKNOWN));
    expect(request.ok).toBe(true);
    const value = request.ok ? request.value : null;
    expect(value?.tempo).toEqual({ state: "unknown", reason: CLIENT_UNKNOWN_REASON });
    expect(JSON.stringify(value)).not.toContain("bpm");
  });
});

describe("fromPaletteContext", () => {
  it("round-trips known, unknown and unset", () => {
    const stored = {
      tempo: { state: "known" as const, bpm: 140 },
      key: { state: "known" as const, tonic: "C", mode: "major" },
      genre: { state: "unknown" as const, reason: "producer_marked_unknown" },
    };
    const rebuilt = fromPaletteContext(stored);
    expect(rebuilt.tempo).toEqual({ state: "known", value: "140" });
    expect(rebuilt.key).toEqual({ state: "known", value: "C major" });
    expect(rebuilt.genre).toEqual({ state: "unknown", value: "" });

    const back = toContextRequest(rebuilt);
    expect(back.ok).toBe(true);
    expect(back.ok ? back.value.tempo : null).toEqual({ state: "known", bpm: 140 });
    expect(back.ok ? back.value.genre : null).toEqual({
      state: "unknown",
      reason: CLIENT_UNKNOWN_REASON,
    });
  });

  it("keeps an unset field unset and does not invent a value", () => {
    const rebuilt = fromPaletteContext({
      tempo: { state: "unset" },
      key: { state: "unset" },
      genre: { state: "unset" },
    });
    expect(rebuilt).toEqual({ tempo: UNSET, key: UNSET, genre: UNSET });
    expect(draftIsSavable(rebuilt)).toBe(true);
  });
});

describe("draftIsSavable", () => {
  it("refuses a known field that does not parse and allows an unknown or unset one", () => {
    expect(draftIsSavable(draft(KNOWN("140"), UNSET, UNSET))).toBe(true);
    expect(draftIsSavable(draft(KNOWN("0"), UNSET, UNSET))).toBe(false);
    expect(draftIsSavable(draft(UNSET, KNOWN("H major"), UNSET))).toBe(false);
    expect(draftIsSavable(draft(UNKNOWN, UNKNOWN, UNSET))).toBe(true);
  });
});
