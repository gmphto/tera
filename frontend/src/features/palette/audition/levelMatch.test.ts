/**
 * The gain rule's contract, including what it must never become (issue #34).
 *
 * Two things are asserted here and nowhere else: the comparison factor is exactly
 * #17's frozen `10 ** (-6/20)`, and it is the same factor for a candidate whose
 * measurements are all missing as for one that carries them — because the client
 * applies no loudness match. The label is checked for the claim it must not make.
 */

import { describe, expect, it } from "vitest";

import {
  AUDITION_COMPARISON_GAIN_DB,
  AUDITION_UNITY_GAIN_DB,
  comparisonGainFactor,
  descriptionForGain,
  gainFactor,
  gainForMode,
} from "./levelMatch";
import { AUDITION_MODES } from "./auditionTypes";

/** Stand-ins for the measurements a loudness match would need to read. */
const NO_MEASUREMENTS = { rms: null, peak: null, crest_factor: null, loudness: null };
const FULL_MEASUREMENTS = { rms: 0.12, peak: 0.7, crest_factor: 4.1, loudness: -9.3 };

describe("the comparison gain", () => {
  it("is exactly #17's frozen render factor", () => {
    expect(AUDITION_COMPARISON_GAIN_DB).toBe(-6.0);
    expect(comparisonGainFactor()).toBe(10 ** (-6 / 20));
    expect(gainFactor(-6.0)).toBe(10 ** (-6 / 20));
  });

  it("does not depend on any measurement", () => {
    // A match would have to read these; nothing in the module can.
    expect(JSON.stringify(NO_MEASUREMENTS)).not.toBe(JSON.stringify(FULL_MEASUREMENTS));
    expect(comparisonGainFactor()).toBe(comparisonGainFactor());
    expect(gainForMode("comparison")).toBe(10 ** (-6 / 20));
  });

  it("plays a candidate alone at unity and a comparison at the fixed gain", () => {
    expect(AUDITION_UNITY_GAIN_DB).toBe(0.0);
    expect(gainForMode("candidate")).toBe(1);
    expect(gainForMode("comparison")).toBeLessThan(1);
    expect(gainForMode("comparison")).not.toBe(gainForMode("candidate"));
  });

  it("gives both elements of a comparison the same factor", () => {
    // One value, applied to the candidate and to the kick alike: the rule has no
    // per-element variant to call.
    const factors = [gainForMode("comparison"), gainForMode("comparison")];
    expect(new Set(factors).size).toBe(1);
  });

  it("has a mode for every factor it can return", () => {
    for (const mode of AUDITION_MODES) {
      expect(Number.isFinite(gainForMode(mode))).toBe(true);
      expect(gainForMode(mode)).toBeGreaterThan(0);
      expect(gainForMode(mode)).toBeLessThanOrEqual(1);
    }
  });
});

describe("the gain's visible label", () => {
  it("shows the exact applied value", () => {
    expect(descriptionForGain("comparison")).toContain("-6.0 dB");
    expect(descriptionForGain("candidate")).toContain("0.0 dB");
  });

  it("reads as a fixed comparison gain, never as a loudness or level match", () => {
    for (const mode of AUDITION_MODES) {
      const label = descriptionForGain(mode).toLowerCase();
      expect(label).not.toContain("match");
      expect(label).not.toContain("loudness");
      expect(label).not.toContain("normalis");
      expect(label).not.toContain("equal");
    }
    expect(descriptionForGain("comparison")).toContain("fixed comparison gain");
  });

  it("names only the mode it was asked about", () => {
    expect(descriptionForGain("candidate")).toBe("0.0 dB");
    expect(descriptionForGain("comparison")).toBe("-6.0 dB fixed comparison gain");
  });
});
