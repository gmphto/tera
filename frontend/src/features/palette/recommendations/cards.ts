/**
 * The card model and every derivation the panel needs (issue #33).
 *
 * Pure functions of one parsed response, so the ordering, uncertainty,
 * alternatives, exclusion and empty-kind rules are covered in a test rather than
 * in a component. Nothing here re-derives a compatibility, a confidence, a mode,
 * a ranking version or an alternative: the values are the payload's values.
 *
 * A card model carries no `similarity`: retrieval similarity is never presented
 * as a score or a reason to pick, so a result with a known similarity and its
 * twin with `null` produce identical models.
 */

import type { BatchWire, RankedCandidateWire, RunWire } from "../api/batch";

/** The client's own request and display values, none of which scores anything. */
export const CLIENT_RESULT_LIMIT = 10;
export const SLOW_RUN_NOTICE_MS = 10000;
export const LABEL_LENGTH = 12;

/** #28's own budget, quoted in the slow-run copy; the client adds no timeout. */
export const RECOMMENDATION_BUDGET_SECONDS = 25.0;

/** #15's bound on how many alternatives a batch may carry. */
export const ALTERNATIVES_LIMIT = 3;

/** The states one card's action can be in. */
export const CARD_STATES = [
  "idle",
  "pending",
  "saved",
  "unrecorded",
  "error",
  "rejected",
] as const;
export type CardState = (typeof CARD_STATES)[number];

/** The two warning prefixes #28 documents as its uncertainty rule. */
export const UNCERTAIN_WARNING_PREFIXES = ["low_coverage", "low_jev_confidence"] as const;

/** One human label per landed exclusion code, with a verbatim fallback. */
export const EXCLUSION_LABELS: Record<string, string> = {
  empty_audio: "Empty audio",
  silent_audio: "Silent audio",
  unusable_analysis: "Unusable analysis",
  inconsistent_analysis: "Inconsistent analysis",
  selected_kick: "This is the selected kick",
  wrong_role: "Not a bass",
  file_missing: "File missing",
  file_unreadable: "File unreadable",
  availability_unknown: "Availability unknown",
  tempo_mismatch: "Tempo mismatch",
  key_mismatch: "Key mismatch",
};

/** An unknown code is shown verbatim rather than hidden or renamed. */
export function exclusionLabel(code: string): string {
  return EXCLUSION_LABELS[code] ?? code;
}

export interface CardModel {
  rank: number;
  candidateId: string;
  label: string;
  compatibility: number;
  confidence: number;
  uncertain: boolean;
  jevPresent: boolean;
  selectedBass: boolean;
  reasons: string[];
  warnings: string[];
}

/** A fixed-length prefix of the candidate id, never a path or the full id. */
export function cardLabel(candidateId: string): string {
  return candidateId.length <= LABEL_LENGTH
    ? candidateId
    : `${candidateId.slice(0, LABEL_LENGTH)}…`;
}

/**
 * #28's own warning rule, applied verbatim: uncertain exactly when a warning
 * begins with one of the two documented prefixes. The client applies no numeric
 * confidence threshold of its own.
 */
export function isUncertain(result: RankedCandidateWire): boolean {
  return result.warnings.some((warning) =>
    UNCERTAIN_WARNING_PREFIXES.some((prefix) => warning.startsWith(prefix)),
  );
}

export function cardModel(result: RankedCandidateWire, selectedBassId: string | null): CardModel {
  return {
    rank: result.rank,
    candidateId: result.candidate_id,
    label: cardLabel(result.candidate_id),
    compatibility: result.compatibility,
    confidence: result.confidence,
    uncertain: isUncertain(result),
    jevPresent: result.jev_judgments.length > 0,
    selectedBass: result.candidate_id === selectedBassId,
    reasons: [...result.reasons],
    warnings: [...result.warnings],
  };
}

/** The cards of one batch, in the order the API returned them. */
export function cardModels(batch: BatchWire): CardModel[] {
  return batch.results.map((result) => cardModel(result, batch.palette.selected_bass_id));
}

/** The one documented percentage rule, used for display only. */
export function formatPercent(value: number): number {
  return Math.round(value * 100);
}

export interface AlternativeItem {
  candidateId: string;
  rank: number;
  compatibility: number;
  confidence: number;
}

/**
 * The batch's own alternatives, resolved to their results in result rank order.
 *
 * The client invents none, derives none from confidence or rank, and lists no
 * more than `ALTERNATIVES_LIMIT`.
 */
export function alternativeItems(batch: BatchWire): AlternativeItem[] {
  const byId = new Map(batch.results.map((result) => [result.candidate_id, result]));
  return batch.alternatives
    .map((id) => byId.get(id))
    .filter((result): result is RankedCandidateWire => result !== undefined)
    .sort((left, right) => left.rank - right.rank)
    .slice(0, ALTERNATIVES_LIMIT)
    .map((result) => ({
      candidateId: result.candidate_id,
      rank: result.rank,
      compatibility: result.compatibility,
      confidence: result.confidence,
    }));
}

export interface ExclusionRow {
  code: string;
  count: number;
  label: string;
}

/** The tally, in the order the service returned it (code-sorted). */
export function exclusionRows(run: RunWire): ExclusionRow[] {
  return run.exclusions.map((record) => ({
    code: record.code,
    count: record.count,
    label: exclusionLabel(record.code),
  }));
}

/** Why a 200 carried no results. All three are results, never errors. */
export type EmptyKind = "no_candidates" | "all_excluded" | "all_unscored";

export function emptyKind(run: RunWire): EmptyKind | null {
  const { eligible, excluded, scored, unscored } = run.counts;
  if (eligible === 0 && excluded === 0) {
    return "no_candidates";
  }
  if (eligible === 0 && excluded > 0) {
    return "all_excluded";
  }
  if (eligible > 0 && scored === 0 && unscored > 0) {
    return "all_unscored";
  }
  return null;
}

/** Whether the run is degraded, from the returned mode and Jev status. */
export function isDegraded(batch: BatchWire, run: RunWire): boolean {
  return batch.mode !== "hybrid" || run.ranking.jev_status !== "jev_present";
}

/** The slow-run notice, at the documented boundary. */
export function deriveRunNotice(elapsedMs: number): boolean {
  return elapsedMs >= SLOW_RUN_NOTICE_MS;
}

/** How many candidates could not be scored, which are never rendered as cards. */
export function unscoredCount(run: RunWire): number {
  return run.counts.unscored;
}
