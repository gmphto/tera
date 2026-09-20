/**
 * The shell half of an audition (issue #34).
 *
 * #27 serves no audio byte and no path, so the client holds `{sampleId,
 * fileName}` and the Tauri shell resolves it against a folder the producer
 * registered. This module is the only place that talks to that shell, and it is
 * a factory rather than a module-level binding so a test injects `invoke` and no
 * test needs a window: the app passes Tauri's `invoke`, a test passes a double.
 *
 * Every failure — the shell's own refusal, the client's pre-checks and a
 * malformed answer — becomes one of the closed `AuditionErrorCode` values, and
 * each of those has exactly one client-facing sentence and one UI state here.
 */

import {
  AUDITION_ERROR_CODES,
  type AuditionErrorCode,
  type AuditionTrack,
} from "./auditionTypes";

/** The shell's registry contract; the Rust side owns the same numbers. */
export const AUDITION_ROOTS_SCHEMA_VERSION = 1;
export const AUDITION_ROOT_LIMIT = 16;
export const SUPPORTED_AUDITION_EXTENSIONS = ["wav"] as const;
export const MAX_AUDITION_BYTES = 67_108_864;

/** Just enough of Tauri's `invoke` to be replaceable by a double. */
export interface AuditionShell {
  invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
}

export interface AuditionRoot {
  rootId: string;
  rootLabel: string;
}

export interface LoadAuditionRequest {
  track: AuditionTrack;
  /** The status the library read reported; anything but `present` is missing. */
  fileStatus?: string | null;
  /** The root to try first; the shell falls back to the others in order. */
  rootId?: string | null;
}

/** What one refusal means to the producer, and the state it puts the UI in. */
const SENTENCES: Record<AuditionErrorCode, { sentence: string; state: "blocked" | "stopped" | "not-recorded" }> = {
  shell_unavailable: {
    sentence: "This window cannot read local files, so nothing can be auditioned here.",
    state: "blocked",
  },
  no_registered_root: {
    sentence: "No library folder is registered for playback. Pick the folder you imported.",
    state: "blocked",
  },
  root_invalid: {
    sentence: "That folder cannot be used for playback. Pick the one you imported.",
    state: "blocked",
  },
  root_limit_reached: {
    sentence: "Too many library folders are registered. Remove one and try again.",
    state: "blocked",
  },
  invalid_file_name: {
    sentence: "The stored file name cannot be resolved to a file.",
    state: "blocked",
  },
  missing_file: {
    sentence: "This file is no longer where the library found it. Re-import its folder.",
    state: "blocked",
  },
  unreadable_file: {
    sentence: "This file could not be read.",
    state: "blocked",
  },
  unsupported_extension: {
    sentence: "Only .wav files can be auditioned.",
    state: "blocked",
  },
  too_large: {
    sentence: "This file is too large to audition.",
    state: "blocked",
  },
  content_mismatch: {
    sentence: "This file's bytes no longer match the analysed sample. Re-import its folder.",
    state: "blocked",
  },
  playback_blocked: {
    sentence: "This window refused to start playback. Press play again.",
    state: "blocked",
  },
  decode_failed: {
    sentence: "This file could not be decoded for playback.",
    state: "stopped",
  },
  not_recorded: {
    sentence: "It played, but the audition event was not recorded.",
    state: "not-recorded",
  },
};

/** The one sentence a code is shown with. */
export function auditionErrorSentence(code: AuditionErrorCode): string {
  return SENTENCES[code].sentence;
}

/** The one state a code leaves the control in. */
export function auditionErrorState(
  code: AuditionErrorCode,
): "blocked" | "stopped" | "not-recorded" {
  return SENTENCES[code].state;
}

export class AuditionSourceError extends Error {
  readonly code: AuditionErrorCode;

  constructor(code: AuditionErrorCode) {
    super(auditionErrorSentence(code));
    this.name = "AuditionSourceError";
    this.code = code;
  }
}

/**
 * The client's own half of the shell's file-name rule.
 *
 * The shell refuses the same names, but checking here means a producer sees
 * "only .wav files" rather than a generic refusal, and the rule is pinned by a
 * test instead of living only in Rust. A name is one path component: no
 * separator, no drive letter, no `..`, no control character.
 */
export function inspectFileName(fileName: string): AuditionErrorCode | null {
  if (typeof fileName !== "string" || fileName.length === 0 || fileName.length > 255) {
    return "invalid_file_name";
  }
  if (fileName === "." || fileName === "..") {
    return "invalid_file_name";
  }
  for (const character of fileName) {
    const point = character.codePointAt(0) ?? 0;
    if (point < 0x20 || point === 0x7f) {
      return "invalid_file_name";
    }
    if (character === "/" || character === "\\" || character === ":") {
      return "invalid_file_name";
    }
  }
  const dot = fileName.lastIndexOf(".");
  const extension = dot === -1 ? "" : fileName.slice(dot + 1).toLowerCase();
  if (!(SUPPORTED_AUDITION_EXTENSIONS as readonly string[]).includes(extension)) {
    return "unsupported_extension";
  }
  return null;
}

/** The code a shell refusal carries, or `shell_unavailable` when it carries none. */
export function auditionCodeOf(error: unknown): AuditionErrorCode {
  const raw =
    typeof error === "string"
      ? error
      : typeof error === "object" && error !== null
        ? ((error as { code?: unknown }).code ?? (error as { message?: unknown }).message)
        : null;
  if (typeof raw === "string" && (AUDITION_ERROR_CODES as readonly string[]).includes(raw)) {
    return raw as AuditionErrorCode;
  }
  return "shell_unavailable";
}

function bytesOf(payload: unknown): ArrayBuffer | null {
  if (payload instanceof ArrayBuffer) {
    return payload;
  }
  if (ArrayBuffer.isView(payload)) {
    return payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength) as ArrayBuffer;
  }
  return null;
}

/**
 * The three shell operations, bound to one shell.
 *
 * Their arguments are the camelCase names Tauri 2 derives from the Rust
 * parameters, and `read_audition_source` answers with the file's bytes unchanged
 * as a raw IPC payload.
 */
export function createAuditionSource(shell: AuditionShell) {
  async function registerAuditionRoot(path: string): Promise<AuditionRoot> {
    if (typeof path !== "string" || path.trim().length === 0) {
      throw new AuditionSourceError("root_invalid");
    }
    let reply: unknown;
    try {
      reply = await shell.invoke("register_audition_root", { path });
    } catch (error) {
      throw new AuditionSourceError(auditionCodeOf(error));
    }
    const rootId = (reply as { rootId?: unknown } | null)?.rootId;
    const rootLabel = (reply as { rootLabel?: unknown } | null)?.rootLabel;
    if (typeof rootId !== "string" || typeof rootLabel !== "string") {
      throw new AuditionSourceError("root_invalid");
    }
    return { rootId, rootLabel };
  }

  async function releaseAuditionSource(rootId: string): Promise<void> {
    try {
      await shell.invoke("forget_audition_root", { rootId });
    } catch (error) {
      throw new AuditionSourceError(auditionCodeOf(error));
    }
  }

  /** The file's bytes, verified against the track's sample id by the shell. */
  async function loadAuditionSource(request: LoadAuditionRequest): Promise<ArrayBuffer> {
    const { track } = request;
    if (typeof request.fileStatus === "string" && request.fileStatus !== "present") {
      // The library already knows the file is gone; asking the shell would only
      // produce a worse message for the same fact.
      throw new AuditionSourceError("missing_file");
    }
    const nameCode = inspectFileName(track.fileName);
    if (nameCode !== null) {
      throw new AuditionSourceError(nameCode);
    }
    let payload: unknown;
    try {
      payload = await shell.invoke("read_audition_source", {
        sampleId: track.sampleId,
        fileName: track.fileName,
        ...(request.rootId == null ? {} : { rootId: request.rootId }),
      });
    } catch (error) {
      throw new AuditionSourceError(auditionCodeOf(error));
    }
    const bytes = bytesOf(payload);
    if (bytes === null) {
      throw new AuditionSourceError("unreadable_file");
    }
    return bytes;
  }

  return { registerAuditionRoot, loadAuditionSource, releaseAuditionSource };
}

export type AuditionSource = ReturnType<typeof createAuditionSource>;
