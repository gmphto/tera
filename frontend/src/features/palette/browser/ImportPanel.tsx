/**
 * The import controls and the run's progress (issue #31).
 *
 * Every name in an attribute is #27's. The visible labels sit beside them and
 * never replace them. The chosen absolute path is never stored, rendered as a
 * path or logged: only its base name appears, and only while a start is in
 * flight.
 */

import { recoveryFor, type LibraryError } from "../api/errors";
import { COUNT_LABELS, PALETTE_ROLES, type ImportRun, type PaletteRole } from "../api/types";
import {
  canCancel,
  canRetry,
  countsOf,
  discoveryErrors,
  isPartialFailure,
  progressModel,
  toFailureView,
} from "../state/selectors";
import { PICKER_RECOVERY, type PickerReason } from "./folderPicker";

const TERMINAL_SENTENCE: Record<string, string> = {
  complete: "The import finished.",
  cancelled: "The run was cancelled. Everything it committed stays in the library.",
  interrupted: "The run was interrupted. Retry resumes the work that is left.",
  failed: "The run failed. Retry starts a new run for the failed files.",
};

export interface ImportPanelProps {
  role: PaletteRole;
  run: ImportRun | null;
  pickerReason: PickerReason | null;
  startError: LibraryError | null;
  chosenLabel: string | null;
  retryNote: boolean;
  onRoleChange(role: PaletteRole): void;
  onPick(): void;
  onCancel(): void;
  onRetry(): void;
}

function FailureRows({ run }: { run: ImportRun }) {
  // `GET /imports/{run_id}` returns one record per *non-complete* item, not per
  // failed one: a queued file is listed there with `stage` and `code` null, and
  // #23's own docstring says those fields are null for an item that never
  // failed. Only a record that carries one of them is a failure.
  const analysis = run.failures
    .filter((failure) => failure.code !== null || failure.stage !== null)
    .map((failure) => ({
      origin: "analysis",
      view: toFailureView(failure),
    }));
  const scan = (run.scan?.files ?? [])
    .filter((file) => file.error_code !== null)
    .map((file) => ({
      origin: "scan",
      view: toFailureView({
        sample_id: file.sample_id,
        file_name: file.file_name,
        stage: file.stage,
        code: file.error_code,
        attempts: null,
      }),
    }));
  const rows = [...analysis, ...scan];
  if (rows.length === 0) {
    return null;
  }
  return (
    <ul className="import__failures" data-testid="import-failures">
      {rows.map((row) => (
        <li
          key={`${row.origin}-${row.view.sample_id}`}
          data-testid="import-failure"
          data-origin={row.origin}
          data-sample-id={row.view.sample_id}
          data-file-name={row.view.file_name}
          data-stage={row.view.stage ?? ""}
          data-code={row.view.code ?? ""}
          data-attempts={row.view.attempts ?? ""}
        >
          {row.view.file_name} — {row.view.stage ?? "unknown stage"} / {row.view.code ?? "unknown code"}
        </li>
      ))}
    </ul>
  );
}

function RunProgress({
  run,
  onCancel,
  onRetry,
  retryNote,
}: {
  run: ImportRun;
  onCancel(): void;
  onRetry(): void;
  retryNote: boolean;
}) {
  const counts = countsOf(run) ?? run.counts;
  const model = progressModel(counts, run.phase);
  const discovery = discoveryErrors(run);
  const partial = isPartialFailure(run);
  return (
    <section
      className="import__progress"
      data-testid="import-progress"
      data-run-id={run.run_id}
      data-state={run.state}
      data-phase={run.phase}
      role="status"
      aria-live="polite"
    >
      <p className="import__root">{run.root_label}</p>
      <dl className="import__counts">
        {(["pending", "running", "complete", "failed"] as const).map((key) => (
          <div key={key} className="import__count">
            <dt>{COUNT_LABELS[key]}</dt>
            <dd data-testid={`import-count-${key}`} data-count={counts[key]}>
              {counts[key]}
            </dd>
          </div>
        ))}
      </dl>
      <div
        className="progress"
        data-testid="import-progress-bar"
        data-mode={model.mode}
        {...(model.percent === null ? {} : { "data-percent": model.percent })}
      >
        <span className="progress__fill" style={{ width: `${model.percent ?? 0}%` }} />
      </div>
      {run.current === null ? null : (
        <p data-testid="import-current">
          {run.current.file_name} — attempt {run.current.attempts}
        </p>
      )}
      {run.scan === null ? (
        <p data-testid="import-scan-missing">
          This run did not scan in this session; the counts and failures below are what it stored.
        </p>
      ) : null}
      {discovery === null || discovery === 0 ? null : (
        <p data-testid="import-discovery-errors" data-count={discovery}>
          {discovery} folder entries could not be read. Re-import with the folder picker to try them again.
        </p>
      )}
      {partial ? (
        <p data-testid="import-partial" data-failed={counts.failed} data-total={counts.analyzed + counts.failed}>
          {counts.failed} of {counts.analyzed + counts.failed} files failed
        </p>
      ) : null}
      {run.state === "running" ? null : (
        <p data-testid="import-terminal" data-state={run.state}>
          {TERMINAL_SENTENCE[run.state] ?? "The run ended."}
        </p>
      )}
      <FailureRows run={run} />
      <div className="import__actions">
        <button
          type="button"
          data-testid="import-cancel"
          disabled={!canCancel(run)}
          onClick={onCancel}
        >
          Cancel import
        </button>
        {run.cancel_requested && run.state === "running" ? (
          <span data-testid="import-cancel-pending">
            Cancellation requested; the run stops at its next checkpoint.
          </span>
        ) : null}
        <button type="button" data-testid="import-retry" disabled={!canRetry(run)} onClick={onRetry}>
          Retry failed files
        </button>
        {retryNote ? (
          <span data-testid="import-retry-none">
            Nothing was left to retry: the failures were already resolved.
          </span>
        ) : null}
      </div>
    </section>
  );
}

export function ImportPanel({
  role,
  run,
  pickerReason,
  startError,
  chosenLabel,
  retryNote,
  onRoleChange,
  onPick,
  onCancel,
  onRetry,
}: ImportPanelProps) {
  const live = run !== null && run.state === "running";
  return (
    <section className="panel" data-testid="import-panel" aria-labelledby="import-heading">
      <h2 className="panel__heading" id="import-heading">
        Import
      </h2>
      <div className="import__controls" data-testid="import-controls">
        <label htmlFor="import-role-select">Role</label>
        <select
          id="import-role-select"
          data-testid="import-role"
          value={role}
          disabled={live}
          onChange={(event) => onRoleChange(event.target.value as PaletteRole)}
        >
          {PALETTE_ROLES.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <button type="button" data-testid="import-pick" disabled={live} onClick={onPick}>
          Import a folder
        </button>
        {chosenLabel === null ? null : <span className="import__chosen">{chosenLabel}</span>}
      </div>
      {pickerReason === null ? null : (
        <p
          className="panel__error"
          data-testid="import-picker-error"
          data-reason={pickerReason}
        >
          {PICKER_RECOVERY[pickerReason]}
        </p>
      )}
      {startError === null ? null : startError.kind === "api" ? (
        <p className="panel__error" data-testid="library-error" data-kind="api" data-code={startError.code}>
          {recoveryFor(startError)}
        </p>
      ) : (
        <p
          className="panel__error"
          data-testid="library-error"
          data-kind="transport"
          data-reason={startError.reason}
        >
          {recoveryFor(startError)}
        </p>
      )}
      {run === null ? null : (
        <RunProgress run={run} onCancel={onCancel} onRetry={onRetry} retryNote={retryNote} />
      )}
    </section>
  );
}
