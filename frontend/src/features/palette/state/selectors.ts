/**
 * The pure view models of the library browser (issue #31).
 *
 * Everything here is a function of state and a response: the query arguments,
 * the progress model, the failure projection, the polling cadence, the
 * enablement rules and the empty-versus-no-matches decision.
 */

import { COUNT_KEYS, type ImportCounts, type ImportFailure, type ImportPhase, type ImportRun, type PaletteRole, type RunState, type SamplePage, type SampleQueryArgs } from "../api/types";
import { IMPORT_POLL_INTERVAL_MS, LIBRARY_REFRESH_INTERVAL_MS } from "../api/types";
import type { BrowserState } from "./browserSlice";

/** The arguments the list queries, taken from the slice and nowhere else. */
export function selectQueryArgs(state: { browser: BrowserState }): SampleQueryArgs {
  const browser = state.browser;
  return {
    roles: browser.roles,
    text: browser.committedText === "" ? null : browser.committedText,
    limit: browser.limit,
    cursor: browser.cursorStack[browser.pageIndex] ?? null,
  };
}

export interface ProgressModel {
  mode: "indeterminate" | "determinate";
  percent: number | null;
}

/**
 * Indeterminate while scanning, or while the denominator is zero; otherwise the
 * rounded share of the drain that is done, clamped to 0-100.
 */
export function progressModel(counts: ImportCounts, phase: ImportPhase): ProgressModel {
  const denominator = counts.analyzed + counts.remaining;
  if (phase === "scanning" || denominator === 0) {
    return { mode: "indeterminate", percent: null };
  }
  const percent = Math.min(100, Math.max(0, Math.round((100 * counts.analyzed) / denominator)));
  return { mode: "determinate", percent };
}

export interface FailureView {
  sample_id: string;
  file_name: string;
  stage: string | null;
  code: string | null;
  attempts: number | null;
}

/**
 * The five fields a failure row may render.
 *
 * #27 omits the human `message` because it may quote a local path, so this
 * projection is built from named fields only: a response that carries
 * `message`, `path` or `root` cannot leak one into a label.
 */
export function toFailureView(failure: ImportFailure): FailureView {
  return {
    sample_id: failure.sample_id,
    file_name: failure.file_name,
    stage: failure.stage,
    code: failure.code,
    attempts: failure.attempts,
  };
}

function isLive(state: RunState): boolean {
  return state === "running";
}

/** One status poll per second while the run is live, and none otherwise. */
export function importPollingInterval(state: RunState): number {
  return isLive(state) ? IMPORT_POLL_INTERVAL_MS : 0;
}

/** A list refresh every five seconds while a run is live, and none otherwise. */
export function libraryPollingInterval(state: RunState | null): number {
  return state !== null && isLive(state) ? LIBRARY_REFRESH_INTERVAL_MS : 0;
}

/** Cancel is offered only for a live run that has not been asked to stop. */
export function canCancel(run: ImportRun | null): boolean {
  return run !== null && isLive(run.state) && !run.cancel_requested;
}

/** Retry is offered only once a run has stopped with at least one failure. */
export function canRetry(run: ImportRun | null): boolean {
  return run !== null && !isLive(run.state) && run.counts.failed > 0;
}

/** While a run is live the picker and the role select are disabled. */
export function canStartImport(run: ImportRun | null): boolean {
  return run === null || !isLive(run.state);
}

/** A completed run with failures is a partial failure, not a failed run. */
export function isPartialFailure(run: ImportRun | null): boolean {
  return run !== null && run.state === "complete" && run.counts.failed > 0;
}

/** The neutral note when a retry finds nothing left to reset. */
export function retryFoundNothing(run: ImportRun | null): boolean {
  return run !== null && !isLive(run.state) && run.counts.failed === 0;
}

/** The ten counts, always all of them, in the documented order. */
export function countsOf(run: ImportRun | null): ImportCounts | null {
  if (run === null) {
    return null;
  }
  const counts = {} as ImportCounts;
  for (const key of COUNT_KEYS) {
    counts[key] = run.counts[key] ?? 0;
  }
  return counts;
}

/** The discovery-error count, or null when this process did not scan. */
export function discoveryErrors(run: ImportRun | null): number | null {
  return run?.scan?.counts?.discovery_errors ?? null;
}

/**
 * Which empty state a zero-item page is: the library itself is empty, or the
 * filters match nothing. They have different recovery actions, so they are
 * never the same element.
 */
export function emptyStateFor(
  page: SamplePage | undefined,
  roles: PaletteRole[],
  text: string,
): "empty" | "no-matches" | null {
  if (page === undefined || page.items.length > 0) {
    return null;
  }
  return roles.length === 0 && text === "" ? "empty" : "no-matches";
}

/** "Showing N", never a library total. */
export function pageSummary(page: SamplePage): string {
  const shown = page.page.count;
  return page.page.has_more ? `Showing ${shown}` : `Showing ${shown}, no more`;
}

/** Only `has_more` gates Next; a stale cursor beside it is ignored. */
export function canGoNext(page: SamplePage | undefined): boolean {
  return page !== undefined && page.page.has_more;
}

export function canGoPrevious(pageIndex: number): boolean {
  return pageIndex > 0;
}
