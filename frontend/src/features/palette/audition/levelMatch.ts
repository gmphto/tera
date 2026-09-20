/**
 * The audition's one gain rule (issue #34).
 *
 * A comparison plays the candidate and the selected kick at the same fixed gain,
 * `AUDITION_COMPARISON_GAIN_DB`. The value is a constant and is never derived
 * from a measurement: a candidate whose `rms`, `peak`, `crest_factor` and
 * `loudness` are all missing gets exactly the same factor as one that carries
 * them, so nothing here reads a measurement at all.
 *
 * It is the same gain #17's frozen render rule applies to the summed pair
 * (`PLAYBACK_GAIN_DB = -6.0` in `backend/evaluation/playback.py`, documented in
 * `_docs/pair-rating-workflow.md`), which is why the number is quoted rather than
 * chosen here. That is also why the visible label says what it is — a fixed
 * comparison gain — and never claims a loudness or a level match: the client
 * applies no normalisation, limiting, fading or resampling, and no per-element
 * difference.
 */

import type { AuditionMode } from "./auditionTypes";

/** Alone, a candidate plays as the file is. */
export const AUDITION_UNITY_GAIN_DB = 0.0;

/** Both elements of a comparison play at this one gain, from #17's render rule. */
export const AUDITION_COMPARISON_GAIN_DB = -6.0;

/** The linear factor of one decibel value: `10 ** (dB / 20)`. */
export function gainFactor(decibels: number): number {
  return 10 ** (decibels / 20);
}

/** The factor a comparison applies to both of its elements. */
export function comparisonGainFactor(): number {
  return gainFactor(AUDITION_COMPARISON_GAIN_DB);
}

/** The factor one mode applies: the fixed comparison gain, or unity. */
export function gainForMode(mode: AuditionMode): number {
  return mode === "comparison"
    ? comparisonGainFactor()
    : gainFactor(AUDITION_UNITY_GAIN_DB);
}

/**
 * The exact applied value, as the control shows it.
 *
 * A label that reads as a gain rather than as a result: it names the number and
 * says what kind of gain it is, so "matched", "normalised" and "loudness" have no
 * place in it and the producer can see that both elements got the same value.
 */
export function descriptionForGain(mode: AuditionMode): string {
  const decibels = (
    mode === "comparison" ? AUDITION_COMPARISON_GAIN_DB : AUDITION_UNITY_GAIN_DB
  ).toFixed(1);
  return mode === "comparison"
    ? `${decibels} dB fixed comparison gain`
    : `${decibels} dB`;
}
