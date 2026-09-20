import { describe, expect, it } from "vitest";

import {
  ALTERNATIVES_LIMIT,
  CARD_STATES,
  CLIENT_RESULT_LIMIT,
  EXCLUSION_LABELS,
  LABEL_LENGTH,
  SLOW_RUN_NOTICE_MS,
  alternativeItems,
  cardLabel,
  cardModel,
  cardModels,
  deriveRunNotice,
  emptyKind,
  exclusionLabel,
  exclusionRows,
  formatPercent,
  isDegraded,
  isUncertain,
  unscoredCount,
} from "./cards";
import {
  ALL_EXCLUDED,
  DSP_ONLY,
  HYBRID_FIVE,
  NO_CANDIDATES,
  NULL_SIMILARITY,
  UNSCORED_TWO,
  candidate,
  candidateId,
  response,
} from "../api/fixtures";

const SELECTED = candidateId(2);

describe("the client's own values", () => {
  it("are the documented ones", () => {
    expect(CLIENT_RESULT_LIMIT).toBe(10);
    expect(SLOW_RUN_NOTICE_MS).toBe(10000);
    expect(LABEL_LENGTH).toBe(12);
    expect(ALTERNATIVES_LIMIT).toBe(3);
    expect([...CARD_STATES]).toEqual([
      "idle",
      "pending",
      "saved",
      "unrecorded",
      "error",
      "rejected",
    ]);
  });
});

describe("the card model", () => {
  it("has exactly the documented keys and no similarity", () => {
    const model = cardModel(candidate(1), null);
    expect(Object.keys(model).sort()).toEqual([
      "candidateId",
      "compatibility",
      "confidence",
      "jevPresent",
      "label",
      "rank",
      "reasons",
      "selectedBass",
      "uncertain",
      "warnings",
    ]);
    expect("similarity" in model).toBe(false);
  });

  it("is identical for a known similarity and its null twin", () => {
    const known = candidate(1, { similarity: 0.9, similarity_unavailable_reason: null });
    const unknown = candidate(1, {
      similarity: null,
      similarity_unavailable_reason: "insufficient_common_dimensions",
    });
    expect(cardModel(known, null)).toEqual(cardModel(unknown, null));
    expect(JSON.stringify(cardModel(known, null))).toBe(JSON.stringify(cardModel(unknown, null)));
  });

  it("labels a candidate with a fixed-length prefix of its id", () => {
    expect(cardLabel(candidateId(1))).toBe(`${candidateId(1).slice(0, LABEL_LENGTH)}…`);
    expect(cardLabel("sha256:short")).toBe("sha256:short");
    expect(JSON.stringify(cardModel(candidate(1), null))).not.toContain("\\\\");
  });

  it("marks the selected bass from the batch's own field", () => {
    expect(cardModel(candidate(2), SELECTED).selectedBass).toBe(true);
    expect(cardModel(candidate(3), SELECTED).selectedBass).toBe(false);
  });

  it("reports Jev presence from the judgments and nothing else", () => {
    expect(cardModel(candidate(1, { jev_judgments: [{ dimension: "frequency" }] }), null).jevPresent).toBe(true);
    expect(cardModel(candidate(1), null).jevPresent).toBe(false);
  });
});

describe("order", () => {
  it("keeps the order the API returned, in both directions", () => {
    const api = response({ batch: { results: [candidate(1), candidate(2), candidate(3)] } });
    expect(cardModels(api.recommendation).map((card) => card.rank)).toEqual([1, 2, 3]);

    const permuted = response({ batch: { results: [candidate(1), candidate(2), candidate(3)] } });
    const reversed = { ...permuted.recommendation, results: [...permuted.recommendation.results].reverse() };
    expect(cardModels(reversed).map((card) => card.rank)).toEqual([3, 2, 1]);
  });

  it("never sorts by a score", () => {
    const batch = response({
      batch: {
        results: [
          candidate(1, { compatibility: 0.2 }),
          candidate(2, { compatibility: 0.9 }),
        ],
      },
    }).recommendation;
    expect(cardModels(batch).map((card) => card.compatibility)).toEqual([0.2, 0.9]);
    expect(formatPercent(0.2)).toBe(20);
    expect(formatPercent(0.905)).toBe(91);
  });
});

describe("uncertainty", () => {
  const table: Array<{ name: string; confidence: number; warnings: string[]; uncertain: boolean }> = [
    { name: "a middling confidence with no warning is not uncertain", confidence: 0.55, warnings: [], uncertain: false },
    { name: "a high confidence with the Jev warning is uncertain", confidence: 0.95, warnings: ["low_jev_confidence: only two dimensions"], uncertain: true },
    { name: "low coverage is uncertain", confidence: 0.9, warnings: ["low_coverage: shortlist of one"], uncertain: true },
    { name: "an unrelated warning is not uncertainty", confidence: 0.4, warnings: ["tempo_locked"], uncertain: false },
  ];

  for (const row of table) {
    it(row.name, () => {
      expect(isUncertain(candidate(1, { confidence: row.confidence, warnings: row.warnings }))).toBe(
        row.uncertain,
      );
      expect(cardModel(candidate(1, { confidence: row.confidence, warnings: row.warnings }), null).uncertain).toBe(row.uncertain);
    });
  }
});

describe("alternatives", () => {
  it("resolves the returned ids in result rank order", () => {
    expect(alternativeItems(HYBRID_FIVE.recommendation)).toEqual([
      { candidateId: candidateId(2), rank: 2, compatibility: candidate(2).compatibility, confidence: 0.8 },
      { candidateId: candidateId(3), rank: 3, compatibility: candidate(3).compatibility, confidence: 0.8 },
    ]);
  });

  it("is empty when the batch carries none", () => {
    expect(alternativeItems(DSP_ONLY.recommendation)).toEqual([]);
  });

  it("lists no more than the limit even if the payload carries more", () => {
    const batch = response({
      batch: {
        results: [candidate(1), candidate(2), candidate(3), candidate(4), candidate(5)],
        alternatives: [candidateId(2), candidateId(3), candidateId(4), candidateId(5)],
      },
    }).recommendation;
    expect(alternativeItems(batch)).toHaveLength(ALTERNATIVES_LIMIT);
  });
});

describe("exclusions", () => {
  it("preserves the returned codes and counts, with a label each", () => {
    expect(exclusionRows(ALL_EXCLUDED.run)).toEqual([
      { code: "file_missing", count: 1, label: "File missing" },
      { code: "inconsistent_analysis", count: 1, label: "Inconsistent analysis" },
      { code: "silent_audio", count: 1, label: "Silent audio" },
    ]);
  });

  it("labels every landed code and shows an unknown one verbatim", () => {
    for (const code of [
      "empty_audio",
      "silent_audio",
      "unusable_analysis",
      "inconsistent_analysis",
      "selected_kick",
      "wrong_role",
      "file_missing",
      "file_unreadable",
      "availability_unknown",
      "tempo_mismatch",
      "key_mismatch",
    ]) {
      expect(EXCLUSION_LABELS[code], code).toBeTruthy();
      expect(exclusionLabel(code)).not.toBe(code);
    }
    expect(exclusionLabel("a_code_from_the_future")).toBe("a_code_from_the_future");
  });
});

describe("the three empty kinds", () => {
  it("are told apart by the counts alone", () => {
    expect(emptyKind(NO_CANDIDATES.run)).toBe("no_candidates");
    expect(emptyKind(ALL_EXCLUDED.run)).toBe("all_excluded");
    const unscored = response({
      batch: { results: [], alternatives: [] },
      run: { counts: { ...UNSCORED_TWO.run.counts, eligible: 2, scored: 0, unscored: 2 } },
    });
    expect(emptyKind(unscored.run)).toBe("all_unscored");
  });

  it("is null when there are results", () => {
    expect(emptyKind(DSP_ONLY.run)).toBeNull();
    expect(emptyKind(UNSCORED_TWO.run)).toBeNull();
  });
});

describe("the degraded flag and the unscored count", () => {
  it("follows the returned mode and Jev status", () => {
    expect(isDegraded(DSP_ONLY.recommendation, DSP_ONLY.run)).toBe(true);
    expect(isDegraded(HYBRID_FIVE.recommendation, HYBRID_FIVE.run)).toBe(false);
    const partial = response({ run: { ranking: { ...HYBRID_FIVE.run.ranking, jev_status: "jev_partial" } } });
    expect(isDegraded(partial.recommendation, partial.run)).toBe(true);
  });

  it("counts the unscorable candidates without listing one", () => {
    expect(unscoredCount(UNSCORED_TWO.run)).toBe(2);
    expect(UNSCORED_TWO.recommendation.results).toHaveLength(5);
  });
});

describe("the slow-run notice", () => {
  it("turns on exactly at the boundary", () => {
    expect(deriveRunNotice(9999)).toBe(false);
    expect(deriveRunNotice(10000)).toBe(true);
    expect(deriveRunNotice(0)).toBe(false);
  });
});
