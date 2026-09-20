import { configureStore } from "@reduxjs/toolkit";
import { describe, expect, it, vi } from "vitest";

import { isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

import browserReducer, { importRunTracked } from "../state/browserSlice";
import { PICKER_RECOVERY, baseName, pickFolder } from "./folderPicker";

vi.mock("@tauri-apps/api/core", () => ({ isTauri: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn() }));

const FIXTURE_ROOT = "C:\\Samples\\Kicks";

function storeWith() {
  return configureStore({ reducer: { browser: browserReducer } });
}

describe("pickFolder", () => {
  it("answers not_tauri in a plain browser session without invoking the dialog", async () => {
    vi.mocked(isTauri).mockReturnValue(false);
    const result = await pickFolder();
    expect(result).toEqual({ kind: "unavailable", reason: "not_tauri" });
    expect(vi.mocked(open)).not.toHaveBeenCalled();
  });

  it("answers cancelled when the dialog is dismissed", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    vi.mocked(open).mockResolvedValue(null);
    expect(await pickFolder()).toEqual({ kind: "cancelled" });
  });

  it("answers selected with the chosen directory", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    vi.mocked(open).mockResolvedValue(FIXTURE_ROOT);
    const result = await pickFolder();
    expect(result).toEqual({ kind: "selected", root: FIXTURE_ROOT });
    expect(vi.mocked(open)).toHaveBeenCalledWith({
      directory: true,
      multiple: false,
      title: "Import a folder of kicks or basses",
    });
  });

  it("answers dialog_failed for a shape it does not expect", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    vi.mocked(open).mockResolvedValue(["a", "b"] as unknown as string);
    expect(await pickFolder()).toEqual({ kind: "unavailable", reason: "dialog_failed" });
  });

  it("maps a permission refusal and never rethrows", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    for (const message of [
      "dialog.open not allowed. Permissions associated with this command: dialog:allow-open",
      "forbidden",
      "not allowed",
    ]) {
      vi.mocked(open).mockRejectedValue(new Error(message));
      expect(await pickFolder()).toEqual({ kind: "unavailable", reason: "permission_denied" });
    }
  });

  it("maps any other throw to dialog_failed", async () => {
    vi.mocked(isTauri).mockReturnValue(true);
    vi.mocked(open).mockRejectedValue(new Error("the window closed"));
    expect(await pickFolder()).toEqual({ kind: "unavailable", reason: "dialog_failed" });
  });

  it("has one sentence per reason, naming the capability that was missing", () => {
    expect(PICKER_RECOVERY.permission_denied).toContain("dialog:allow-open");
    for (const sentence of Object.values(PICKER_RECOVERY)) {
      expect(sentence.length).toBeGreaterThan(0);
      expect(sentence.length).toBeLessThanOrEqual(200);
    }
  });
});

describe("the chosen path", () => {
  it("is reduced to its base name", () => {
    expect(baseName(FIXTURE_ROOT)).toBe("Kicks");
    expect(baseName("C:/Samples/Drums")).toBe("Drums");
    expect(baseName("Kicks")).toBe("Kicks");
  });

  it("never reaches the slice", () => {
    const store = storeWith();
    // The container tracks the run id and nothing else after a successful start.
    store.dispatch(importRunTracked("run-1e2ce2e140063b8fd2ab0cc64001488b"));
    const serialised = JSON.stringify(store.getState().browser);
    expect(serialised).not.toContain("Samples");
    expect(serialised).not.toContain("C:\\");
    expect(serialised).not.toContain("C:/");
  });
});
