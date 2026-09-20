import { configureStore } from "@reduxjs/toolkit";
import { beforeEach, describe, expect, it } from "vitest";

import { DEFAULT_PAGE_SIZE } from "../api/types";
import browserReducer, {
  importRoleChanged,
  importRunTracked,
  pagePopped,
  pagePushed,
  pageSizeChanged,
  pagingReset,
  rolesChanged,
  sampleSelected,
  searchCommitted,
  searchTextChanged,
  selectionRefused,
} from "./browserSlice";
import { canStartImport, selectQueryArgs } from "./selectors";

function storeWith() {
  return configureStore({ reducer: { browser: browserReducer } });
}

let store = storeWith();

beforeEach(() => {
  store = storeWith();
});

describe("the query arguments", () => {
  it("start from the documented defaults", () => {
    expect(store.getState().browser).toEqual({
      roles: [],
      text: "",
      committedText: "",
      limit: DEFAULT_PAGE_SIZE,
      cursorStack: [null],
      pageIndex: 0,
      selectedSampleId: null,
      selectionStale: false,
      importRole: "kick",
      importRunId: null,
    });
  });

  it("commit the typed text once and only when it changed", () => {
    store.dispatch(searchTextChanged("k"));
    store.dispatch(searchTextChanged("ki"));
    store.dispatch(searchCommitted("ki"));
    expect(store.getState().browser.committedText).toBe("ki");

    store.dispatch(pagePushed("cursor-a"));
    expect(store.getState().browser.pageIndex).toBe(1);
    // The same text committed again is not a new query: paging is untouched.
    store.dispatch(searchCommitted("ki"));
    expect(store.getState().browser.pageIndex).toBe(1);
    // A different text restarts the list.
    store.dispatch(searchCommitted("kick"));
    expect(store.getState().browser.pageIndex).toBe(0);
    expect(store.getState().browser.cursorStack).toEqual([null]);
  });

  it("commit roles and page size in the fixed role order and restart paging", () => {
    store.dispatch(pagePushed("cursor-a"));
    store.dispatch(rolesChanged(["sub-bass", "kick"]));
    expect(store.getState().browser.roles).toEqual(["kick", "sub-bass"]);
    expect(store.getState().browser.pageIndex).toBe(0);
    expect(store.getState().browser.cursorStack).toEqual([null]);

    store.dispatch(pagePushed("cursor-b"));
    store.dispatch(pageSizeChanged(100));
    expect(store.getState().browser.limit).toBe(100);
    expect(store.getState().browser.pageIndex).toBe(0);
  });

  it("are read back as one request argument object", () => {
    store.dispatch(rolesChanged(["bass"]));
    store.dispatch(searchTextChanged("kick"));
    store.dispatch(searchCommitted("kick"));
    store.dispatch(pagePushed("cursor-a"));
    expect(selectQueryArgs(store.getState())).toEqual({
      roles: ["bass"],
      text: "kick",
      limit: DEFAULT_PAGE_SIZE,
      cursor: "cursor-a",
    });
    store.dispatch(searchCommitted(""));
    expect(selectQueryArgs(store.getState()).text).toBeNull();
  });
});

describe("the cursor stack", () => {
  it("pushes the cursor the service returned and pops back to one already used", () => {
    store.dispatch(pagePushed("c1"));
    store.dispatch(pagePushed("c2"));
    expect(store.getState().browser.pageIndex).toBe(2);
    expect(selectQueryArgs(store.getState()).cursor).toBe("c2");
    store.dispatch(pagePopped());
    expect(selectQueryArgs(store.getState()).cursor).toBe("c1");
    store.dispatch(pagePopped());
    expect(selectQueryArgs(store.getState()).cursor).toBeNull();
    store.dispatch(pagePopped());
    expect(store.getState().browser.pageIndex).toBe(0);
  });

  it("is reset by the invalid-cursor recovery", () => {
    store.dispatch(pagePushed("c1"));
    store.dispatch(pagingReset());
    expect(store.getState().browser.cursorStack).toEqual([null]);
    expect(store.getState().browser.pageIndex).toBe(0);
  });

  it("drops any forward cursor when a new page is pushed from a popped one", () => {
    store.dispatch(pagePushed("c1"));
    store.dispatch(pagePushed("c2"));
    store.dispatch(pagePopped());
    store.dispatch(pagePushed("c3"));
    expect(store.getState().browser.cursorStack).toEqual([null, "c1", "c3"]);
  });
});

describe("the selection", () => {
  it("is one sample id, survives a page change and is cleared by a 404", () => {
    store.dispatch(sampleSelected("sha256:aaaa"));
    expect(store.getState().browser.selectedSampleId).toBe("sha256:aaaa");
    store.dispatch(pagePushed("c1"));
    store.dispatch(rolesChanged(["kick"]));
    expect(store.getState().browser.selectedSampleId).toBe("sha256:aaaa");
    store.dispatch(selectionRefused());
    expect(store.getState().browser.selectedSampleId).toBeNull();
    expect(store.getState().browser.selectionStale).toBe(true);
  });
});

describe("the import run", () => {
  const RUN = {
    analysis_version: "0".repeat(64),
    cancel_requested: false,
    counts: {
      pending: 0,
      running: 0,
      complete: 1,
      failed: 0,
      cancelled: 0,
      orphaned: 0,
      superseded: 0,
      analyzed: 1,
      reused: 0,
      remaining: 0,
    },
    current: null,
    failures: [],
    finished_at: null,
    phase: "analyzing" as const,
    role: "kick" as const,
    root_label: "Kicks",
    run_id: "run-1",
    scan: null,
    started_at: null,
    state: "running" as const,
  };

  it("adopts the run id from a 409 body and refuses a second start while it runs", () => {
    expect(canStartImport(null)).toBe(true);
    store.dispatch(importRunTracked("run-adopted"));
    expect(store.getState().browser.importRunId).toBe("run-adopted");
    expect(canStartImport(RUN)).toBe(false);
    expect(canStartImport({ ...RUN, state: "complete" })).toBe(true);
  });

  it("keeps the role the picker will use", () => {
    store.dispatch(importRoleChanged("bass"));
    expect(store.getState().browser.importRole).toBe("bass");
  });
});

describe("what the slice must never hold", () => {
  it("holds no page, no row and no file name", () => {
    store.dispatch(sampleSelected("sha256:aaaa"));
    store.dispatch(pagePushed("c1"));
    const keys = Object.keys(store.getState().browser);
    expect(keys).not.toContain("items");
    expect(keys).not.toContain("page");
    const serialised = JSON.stringify(store.getState().browser);
    for (const forbidden of ["file_name", "items", "next_cursor", "sample_rate_hz"]) {
      expect(serialised).not.toContain(forbidden);
    }
  });
});
