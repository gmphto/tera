/**
 * The folder picker and its cancellation contract (issue #31).
 *
 * The wrapper never rethrows: a denied or missing dialog becomes a reason the
 * panel can render, so a plain browser session and a mis-configured window both
 * get a clean state instead of a crash. Dismissing the picker creates no import.
 */

import { isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

export const PICKER_REASONS = ["not_tauri", "permission_denied", "dialog_failed"] as const;
export type PickerReason = (typeof PICKER_REASONS)[number];

export type PickFolderResult =
  | { kind: "selected"; root: string }
  | { kind: "cancelled" }
  | { kind: "unavailable"; reason: PickerReason };

/** One actionable sentence per reason. */
export const PICKER_RECOVERY: Record<PickerReason, string> = {
  not_tauri:
    "The folder picker needs the desktop window. Start tera with npm run tauri dev rather than a browser tab.",
  permission_denied:
    "The window is missing the dialog:allow-open capability for its main window; add it and restart the shell.",
  dialog_failed: "The folder picker did not open. Retry, or restart the window.",
};

function reasonFor(error: unknown): PickerReason {
  const text = String(error).toLowerCase();
  if (
    text.includes("dialog:allow-open") ||
    text.includes("not allowed") ||
    text.includes("forbidden")
  ) {
    return "permission_denied";
  }
  return "dialog_failed";
}

export async function pickFolder(): Promise<PickFolderResult> {
  if (!isTauri()) {
    return { kind: "unavailable", reason: "not_tauri" };
  }
  try {
    const chosen = await open({
      directory: true,
      multiple: false,
      title: "Import a folder of kicks or basses",
    });
    if (chosen === null) {
      return { kind: "cancelled" };
    }
    if (typeof chosen === "string") {
      return { kind: "selected", root: chosen };
    }
    return { kind: "unavailable", reason: "dialog_failed" };
  } catch (error) {
    return { kind: "unavailable", reason: reasonFor(error) };
  }
}

/** The last path component, used only as transient visible text. */
export function baseName(root: string): string {
  const parts = root.split(/[\\/]/).filter((part) => part !== "");
  return parts.length === 0 ? root : parts[parts.length - 1];
}
