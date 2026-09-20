import { describe, expect, it } from "vitest";

import {
  MAX_CONFLICT_RETRIES,
  acceptsPaletteResponse,
  isCurrentRevision,
  shouldRetryConflict,
} from "./revisionGuard";
import type { PaletteState } from "./paletteSlice";

function state(overrides: Partial<PaletteState> = {}): PaletteState {
  return {
    status: "ready",
    project: { project_id: "project-001", name: "Track A" },
    paletteId: "palette-001",
    revision: 4,
    epoch: 2,
    items: { kick: null, bass: null },
    context: null,
    draft: { values: null, dirty: false, saveState: "idle", errorField: null, errorCode: null },
    pending: null,
    conflict: null,
    lastError: null,
    droppedResponses: 0,
    ...overrides,
  };
}

describe("isCurrentRevision", () => {
  it("accepts the loaded palette at the same or a newer revision", () => {
    expect(isCurrentRevision(state(), "palette-001", 4)).toBe(true);
    expect(isCurrentRevision(state(), "palette-001", 5)).toBe(true);
  });

  it("refuses an older revision and another palette", () => {
    expect(isCurrentRevision(state(), "palette-001", 3)).toBe(false);
    expect(isCurrentRevision(state(), "palette-002", 5)).toBe(false);
  });
});

describe("acceptsPaletteResponse", () => {
  const table: Array<{ name: string; response: Parameters<typeof acceptsPaletteResponse>[1]; accepted: boolean }> = [
    { name: "the current action at the current revision", response: { paletteId: "palette-001", revision: 4, issuedEpoch: 2 }, accepted: true },
    { name: "the current action at a newer revision", response: { paletteId: "palette-001", revision: 5, issuedEpoch: 2 }, accepted: true },
    { name: "an older revision", response: { paletteId: "palette-001", revision: 3, issuedEpoch: 2 }, accepted: false },
    { name: "another palette", response: { paletteId: "palette-002", revision: 6, issuedEpoch: 2 }, accepted: false },
    { name: "a superseded action", response: { paletteId: "palette-001", revision: 6, issuedEpoch: 1 }, accepted: false },
  ];

  for (const item of table) {
    it(item.name, () => {
      expect(acceptsPaletteResponse(state(), item.response)).toBe(item.accepted);
    });
  }

  it("refuses the kick A response after kick B was requested, even at a higher revision", () => {
    // A: epoch 3 was issued for kick A; then B was issued, so the epoch is 4.
    const afterB = state({ epoch: 4, paletteId: "palette-001", revision: 4 });
    const answerForA = { paletteId: "palette-001", revision: 5, issuedEpoch: 3 };
    expect(acceptsPaletteResponse(afterB, answerForA)).toBe(false);
  });

  it("refuses a recommendation-shaped stale response by the same rule", () => {
    // #33's response carries the revision it was computed against.
    const loaded = state({ revision: 7, epoch: 5 });
    const stale = { paletteId: "palette-001", revision: 6, issuedEpoch: 5 };
    const current = { paletteId: "palette-001", revision: 7, issuedEpoch: 5 };
    expect(acceptsPaletteResponse(loaded, stale)).toBe(false);
    expect(acceptsPaletteResponse(loaded, current)).toBe(true);
  });
});

describe("shouldRetryConflict", () => {
  it("retries once for the newest action", () => {
    expect(MAX_CONFLICT_RETRIES).toBe(1);
    expect(shouldRetryConflict(state({ epoch: 3 }), 3, 1)).toBe(true);
  });

  it("drops a conflict for an action that is no longer newest", () => {
    expect(shouldRetryConflict(state({ epoch: 4 }), 3, 1)).toBe(false);
  });

  it("does not retry past the bound or after a conflict is already shown", () => {
    expect(shouldRetryConflict(state({ epoch: 3 }), 3, 2)).toBe(false);
    expect(shouldRetryConflict(state({ epoch: 3, conflict: { attempts: 1 } }), 3, 1)).toBe(false);
  });
});
