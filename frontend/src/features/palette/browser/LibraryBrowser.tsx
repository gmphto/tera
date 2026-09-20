/**
 * The library browser: one container over #30's store, api and status panel
 * (issue #31).
 *
 * It issues the six library and import routes and nothing else, keeps no
 * response of its own, and hands the selected `sample_id` on to later tasks.
 */

import { useEffect, useState } from "react";

import { useAppDispatch, useAppSelector } from "../../../app/hooks";
import { useServiceStatus } from "../../service/ServiceStatusPanel";
import { toLibraryError, type LibraryError } from "../api/errors";
import {
  useCancelImportMutation,
  useGetImportQuery,
  useGetSampleQuery,
  useGetSamplesQuery,
  useRetryImportMutation,
  useStartImportMutation,
} from "../api/libraryApi";
import { SEARCH_DEBOUNCE_MS, type PaletteRole, type RunState } from "../api/types";
import {
  importRoleChanged,
  importRunTracked,
  pagePopped,
  pagePushed,
  pageSizeChanged,
  pagingReset,
  rolesChanged,
  sampleSelected,
  searchCommitted,
  searchTextChanged,
  selectionKept,
  selectionRefused,
} from "../state/browserSlice";
import { importPollingInterval, libraryPollingInterval, selectQueryArgs } from "../state/selectors";
import { ImportPanel } from "./ImportPanel";
import { LibraryFilters } from "./LibraryFilters";
import { LibraryResults } from "./LibraryResults";
import { PageControls } from "./PageControls";
import { baseName, pickFolder, type PickerReason } from "./folderPicker";

export function LibraryBrowser() {
  const dispatch = useAppDispatch();
  const browser = useAppSelector((state) => state.browser);
  const args = useAppSelector(selectQueryArgs);
  const { status } = useServiceStatus();
  const serviceReady = status.phase === "running" || status.phase === "degraded";

  const [pickerReason, setPickerReason] = useState<PickerReason | null>(null);
  const [chosenLabel, setChosenLabel] = useState<string | null>(null);
  const [startError, setStartError] = useState<LibraryError | null>(null);
  const [retryNote, setRetryNote] = useState(false);
  const [observedRunState, setObservedRunState] = useState<RunState>("running");

  // The debounce: one commit per burst, and the pending timer dies with the panel.
  useEffect(() => {
    const timer = setTimeout(() => dispatch(searchCommitted(browser.text)), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [browser.text, dispatch]);

  const runQuery = useGetImportQuery(browser.importRunId ?? "", {
    skip: browser.importRunId === null || !serviceReady,
    pollingInterval: importPollingInterval(observedRunState),
  });
  useEffect(() => {
    if (runQuery.data !== undefined) {
      setObservedRunState(runQuery.data.import.state);
    }
  }, [runQuery.data]);
  const run = browser.importRunId === null ? null : (runQuery.data?.import ?? null);

  const samplesQuery = useGetSamplesQuery(args, {
    skip: !serviceReady,
    pollingInterval: libraryPollingInterval(browser.importRunId === null ? null : observedRunState),
  });
  const listError = samplesQuery.isError ? toLibraryError(samplesQuery.error) : null;

  // A cursor the service refused sends the pager back to the first page instead
  // of retrying the same cursor.
  useEffect(() => {
    if (
      listError !== null &&
      listError.kind === "api" &&
      listError.code === "invalid_cursor" &&
      browser.pageIndex > 0
    ) {
      dispatch(pagingReset());
    }
  }, [listError, browser.pageIndex, dispatch]);

  const detailQuery = useGetSampleQuery(browser.selectedSampleId ?? "", {
    skip: browser.selectedSampleId === null || !serviceReady,
  });
  useEffect(() => {
    if (browser.selectedSampleId === null) {
      return;
    }
    if (detailQuery.isSuccess) {
      dispatch(selectionKept());
      return;
    }
    if (detailQuery.isError) {
      const failure = toLibraryError(detailQuery.error);
      if (failure.kind === "api" && failure.code === "unknown_sample") {
        dispatch(selectionRefused());
      }
    }
  }, [browser.selectedSampleId, detailQuery.isSuccess, detailQuery.isError, detailQuery.error, dispatch]);

  const [startImport] = useStartImportMutation();
  const [cancelImport] = useCancelImportMutation();
  const [retryImport] = useRetryImportMutation();

  const onPick = async () => {
    const picked = await pickFolder();
    if (picked.kind === "cancelled") {
      return;
    }
    if (picked.kind === "unavailable") {
      setPickerReason(picked.reason);
      return;
    }
    setPickerReason(null);
    setStartError(null);
    setRetryNote(false);
    // The absolute path lives in this call and nowhere else.
    setChosenLabel(baseName(picked.root));
    try {
      const answer = await startImport({ root: picked.root, role: browser.importRole }).unwrap();
      dispatch(importRunTracked(answer.import.run_id));
    } catch (error) {
      const failure = toLibraryError(error);
      const adopted =
        failure.kind === "api" && failure.code === "import_already_running"
          ? failure.details["run_id"]
          : undefined;
      if (typeof adopted === "string") {
        // The 409 body is the documented recovery path for a run started elsewhere.
        dispatch(importRunTracked(adopted));
      } else {
        setStartError(failure);
      }
    } finally {
      setChosenLabel(null);
    }
  };

  const onCancel = async () => {
    if (browser.importRunId === null) {
      return;
    }
    try {
      await cancelImport(browser.importRunId).unwrap();
    } catch (error) {
      const failure = toLibraryError(error);
      if (failure.kind === "api" && failure.code === "import_not_live") {
        // The run already ended; read its result once rather than retrying.
        await runQuery.refetch();
      }
    }
  };

  const onRetry = async () => {
    if (browser.importRunId === null) {
      return;
    }
    setRetryNote(false);
    try {
      const answer = await retryImport(browser.importRunId).unwrap();
      dispatch(importRunTracked(answer.import.run_id));
      setRetryNote(answer.import.retried === 0);
    } catch (error) {
      setStartError(toLibraryError(error));
    }
  };

  const onNext = () => {
    const page = samplesQuery.data;
    if (page !== undefined && page.page.has_more) {
      dispatch(pagePushed(page.page.next_cursor));
    }
  };

  const onClearFilters = () => {
    dispatch(rolesChanged([]));
    dispatch(searchTextChanged(""));
    dispatch(searchCommitted(""));
  };

  return (
    <section className="panel library" data-testid="library-browser" aria-labelledby="library-heading">
      <h2 className="panel__heading" id="library-heading">
        Library
      </h2>
      <ImportPanel
        role={browser.importRole}
        run={run}
        pickerReason={pickerReason}
        startError={startError}
        chosenLabel={chosenLabel}
        retryNote={retryNote}
        onRoleChange={(role: PaletteRole) => dispatch(importRoleChanged(role))}
        onPick={() => {
          void onPick();
        }}
        onCancel={() => {
          void onCancel();
        }}
        onRetry={() => {
          void onRetry();
        }}
      />
      <LibraryFilters
        text={browser.text}
        roles={browser.roles}
        limit={browser.limit}
        onText={(text: string) => dispatch(searchTextChanged(text))}
        onRoles={(roles: PaletteRole[]) => dispatch(rolesChanged(roles))}
        onLimit={(limit: number) => dispatch(pageSizeChanged(limit))}
      />
      <LibraryResults
        page={samplesQuery.data}
        args={args}
        serviceReady={serviceReady}
        isFetching={samplesQuery.isFetching}
        isFirstLoad={samplesQuery.isLoading}
        error={listError}
        staleSelection={browser.selectionStale}
        selectedSampleId={browser.selectedSampleId}
        onRetry={() => {
          void samplesQuery.refetch();
        }}
        onClearFilters={onClearFilters}
        onSelect={(sampleId: string) => dispatch(sampleSelected(sampleId))}
        onImport={() => {
          void onPick();
        }}
      />
      <PageControls
        pageIndex={browser.pageIndex}
        page={samplesQuery.data}
        onNext={onNext}
        onPrevious={() => dispatch(pagePopped())}
      />
    </section>
  );
}
