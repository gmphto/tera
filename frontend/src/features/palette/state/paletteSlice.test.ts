import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it } from "vitest";

import type { PaletteWire } from "../api/paletteApi";
import {
  paletteActionIssued,
  paletteConflicted,
  paletteDraftChanged,
  paletteDraftCleared,
  paletteLoaded,
  paletteReducer,
  paletteResponseApplied,
  paletteSaveFailed,
  selectContextDraft,
  selectKick,
  selectKickState,
  selectPalettePending,
  selectPaletteRevision,
} from "./paletteSlice";
import { fromPaletteContext } from "./contextDraft";

function palette(overrides: Partial<PaletteWire> = {}): PaletteWire {
  return {
    palette_id: "palette-001",
    project: { project_id: "project-001", name: "Track A" },
    name: "Main",
    revision: 3,
    context: { tempo: { state: "unset" }, key: { state: "unset" }, genre: { state: "unset" } },
    items: { kick: null, bass: null },
    ...overrides,
  };
}

function kickItem(sampleId: string, sampleState = "present") {
  return {
    slot: "kick",
    sample_id: sampleId,
    role: "kick",
    added_revision: 3,
    sample_state: sampleState,
    sample_error_code: null,
    slot_role_mismatch: false,
  };
}

function storeWith() {
  return configureStore({ reducer: { palette: paletteReducer } });
}

let store = storeWith();

beforeEach(() => {
  store = storeWith();
});

const applied = (wire: PaletteWire, issuedEpoch: number) => ({
  paletteId: wire.palette_id,
  revision: wire.revision,
  issuedEpoch,
  palette: wire,
});

describe("loading", () => {
  it("applies a palette with a kick and known context verbatim", () => {
    const wire = palette({
      items: { kick: kickItem("sha256:aaaa"), bass: null },
      context: {
        tempo: { state: "known", bpm: 140 },
        key: { state: "known", tonic: "C", mode: "major" },
        genre: { state: "unknown", reason: "producer_marked_unknown" },
      },
    });
    store.dispatch(paletteLoaded(wire));
    const state = store.getState();
    expect(selectKick(state)?.sample_id).toBe("sha256:aaaa");
    expect(selectPaletteRevision(state)).toBe(3);
    expect(selectKickState(state)).toBe("present");
    expect(selectContextDraft(state).values).toEqual(fromPaletteContext(wire.context));
  });

  it("loads a palette with no kick as none and dispatches no action", () => {
    store.dispatch(paletteLoaded(palette()));
    expect(selectKick(store.getState())).toBeNull();
    expect(selectKickState(store.getState())).toBe("none");
    expect(selectPalettePending(store.getState())).toBeNull();
    expect(store.getState().palette.epoch).toBe(0);
  });

  it("reproduces the same projection from a fresh store", () => {
    const wire = palette({ items: { kick: kickItem("sha256:aaaa", "missing"), bass: null } });
    const first = storeWith();
    const second = storeWith();
    first.dispatch(paletteLoaded(wire));
    second.dispatch(paletteLoaded(wire));
    expect(second.getState().palette.items).toEqual(first.getState().palette.items);
    expect(selectKickState(second.getState())).toBe("missing");
  });
});

describe("the epoch and the guard", () => {
  it("increments the epoch for every user action and records it as pending", () => {
    store.dispatch(paletteActionIssued("kick"));
    expect(store.getState().palette.epoch).toBe(1);
    expect(selectPalettePending(store.getState())).toEqual({ epoch: 1, action: "kick" });
    store.dispatch(paletteActionIssued("context"));
    expect(store.getState().palette.epoch).toBe(2);
  });

  it("applies an answer from the newest action and clears pending", () => {
    store.dispatch(paletteLoaded(palette({ revision: 3 })));
    const wire = palette({ revision: 4, items: { kick: kickItem("sha256:bbbb"), bass: null } });
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteResponseApplied(applied(wire, 1)));
    expect(selectKick(store.getState())?.sample_id).toBe("sha256:bbbb");
    expect(selectPalettePending(store.getState())).toBeNull();
    expect(store.getState().palette.droppedResponses).toBe(0);
  });

  it("drops an answer for a lower revision and counts it", () => {
    store.dispatch(paletteLoaded(palette({ revision: 5, items: { kick: kickItem("sha256:aaaa"), bass: null } })));
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteResponseApplied(applied(palette({ revision: 4, items: { kick: kickItem("sha256:cccc"), bass: null } }), 1)));
    expect(selectKick(store.getState())?.sample_id).toBe("sha256:aaaa");
    expect(selectPaletteRevision(store.getState())).toBe(5);
    expect(store.getState().palette.droppedResponses).toBe(1);
  });

  it("drops kick A's answer after kick B was requested, even at a higher revision", () => {
    store.dispatch(paletteLoaded(palette({ revision: 3 })));
    // Kick A was issued under epoch 1; kick B under epoch 2.
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteResponseApplied(applied(palette({ revision: 9, items: { kick: kickItem("sha256:aaaa"), bass: null } }), 1)));
    expect(selectKick(store.getState())).toBeNull();
    expect(store.getState().palette.droppedResponses).toBe(1);
  });

  it("keeps exactly one item after two answers for the same slot", () => {
    store.dispatch(paletteLoaded(palette()));
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteResponseApplied(applied(palette({ revision: 4, items: { kick: kickItem("sha256:aaaa"), bass: null } }), 1)));
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteResponseApplied(applied(palette({ revision: 5, items: { kick: kickItem("sha256:bbbb"), bass: null } }), 2)));
    const items = store.getState().palette.items;
    expect(items.kick?.sample_id).toBe("sha256:bbbb");
    expect(Object.keys(items)).toEqual(["kick", "bass"]);
  });
});

describe("the context draft", () => {
  it("keeps an unsaved draft and the stored context apart after a failed save", () => {
    store.dispatch(paletteLoaded(palette()));
    const draft = fromPaletteContext({
      tempo: { state: "known", bpm: 140 },
      key: { state: "unset" },
      genre: { state: "unset" },
    });
    store.dispatch(paletteDraftChanged(draft));
    store.dispatch(paletteSaveFailed({ code: "invalid_context", field: "key" }));
    const state = store.getState();
    expect(selectContextDraft(state).values).toEqual(draft);
    expect(selectContextDraft(state).dirty).toBe(true);
    expect(selectContextDraft(state).saveState).toBe("error");
    expect(selectContextDraft(state).errorField).toBe("key");
    // The stored context is untouched: nothing was saved.
    expect(state.palette.context).toEqual(palette().context);
  });

  it("clears the draft only on a successful save", () => {
    store.dispatch(paletteLoaded(palette()));
    store.dispatch(paletteDraftChanged(fromPaletteContext({
      tempo: { state: "known", bpm: 140 },
      key: { state: "unset" },
      genre: { state: "unset" },
    })));
    store.dispatch(paletteDraftCleared());
    expect(selectContextDraft(store.getState()).dirty).toBe(false);
  });
});

describe("a kick that cannot be used", () => {
  it("keeps its state and offers the reselect action", () => {
    for (const state of ["missing", "unknown", "removed"]) {
      const local = storeWith();
      local.dispatch(paletteLoaded(palette({ items: { kick: kickItem("sha256:aaaa", state), bass: null } })));
      expect(selectKickState(local.getState())).toBe(state);
      expect(selectKick(local.getState())?.sample_id).toBe("sha256:aaaa");
    }
  });

  it("reports a re-roled kick as role-mismatch", () => {
    store.dispatch(paletteLoaded(palette({
      items: {
        kick: { ...kickItem("sha256:aaaa", "present"), slot_role_mismatch: true, role: "snare" },
        bass: null,
      },
    })));
    expect(selectKickState(store.getState())).toBe("role-mismatch");
  });
});

describe("a conflict", () => {
  it("is recorded with its attempt count and clears pending", () => {
    store.dispatch(paletteActionIssued("kick"));
    store.dispatch(paletteConflicted(1));
    expect(store.getState().palette.conflict).toEqual({ attempts: 1 });
    expect(selectPalettePending(store.getState())).toBeNull();
  });
});
