/**
 * One row of the library (issue #31).
 *
 * File availability and analysis availability are two independent badges: a row
 * may be `missing` with a current analysis, and the row never collapses the two
 * into one error. Identity is the `sample_id` and nothing else.
 */

import type { AnalysisState, FileStatus, SampleListItem } from "../api/types";

const FILE_SENTENCE: Record<FileStatus, string> = {
  present: "The file is where the library last saw it.",
  missing: "The file is no longer at its stored path. Re-import the folder, or leave the row.",
  unknown: "The file has not been checked since it was imported.",
  unreadable: "The file exists but cannot be read. Check its permissions, then re-import.",
};

const ANALYSIS_SENTENCE: Record<AnalysisState, string> = {
  current: "The stored analysis is the current one.",
  stale: "The stored analysis is from an older version. Re-import to refresh it.",
  pending: "This sample is queued for analysis.",
  failed: "Analysis failed for this sample. Retry the import to try it again.",
  absent: "This sample has no stored analysis yet.",
};

export interface SampleRowProps {
  item: SampleListItem;
  selected: boolean;
  onSelect(sampleId: string): void;
}

export function SampleRow({ item, selected, onSelect }: SampleRowProps) {
  const fileUnavailable = item.file_status !== "present";
  const analysisUnavailable = item.analysis.state !== "current";
  return (
    <li>
      <button
        type="button"
        className="sample-row"
        data-testid="sample-row"
        data-sample-id={item.sample_id}
        data-role={item.role}
        data-file-status={item.file_status}
        data-analysis-state={item.analysis.state}
        data-selected={selected ? "true" : "false"}
        data-duration-ms={item.audio.duration_ms}
        data-sample-rate-hz={item.audio.sample_rate_hz}
        data-channels={item.audio.channels}
        aria-pressed={selected}
        aria-label={`${item.file_name}, ${item.role}, file ${item.file_status}, analysis ${item.analysis.state}`}
        onClick={() => onSelect(item.sample_id)}
      >
        <span className="sample-row__name">{item.file_name}</span>
        <span className="sample-row__role">{item.role}</span>
        <span
          className="badge"
          data-testid="sample-file-status"
          data-file-status={item.file_status}
        >
          {fileUnavailable ? FILE_SENTENCE[item.file_status] : "File present"}
        </span>
        {analysisUnavailable ? (
          <span
            className="badge"
            data-testid="sample-analysis-unavailable"
            data-analysis-state={item.analysis.state}
          >
            {ANALYSIS_SENTENCE[item.analysis.state]}
          </span>
        ) : null}
      </button>
      {item.file_status === "missing" ? (
        <span data-testid="sample-reimport" className="sample-row__action">
          Re-import the folder to restore this file.
        </span>
      ) : null}
    </li>
  );
}
