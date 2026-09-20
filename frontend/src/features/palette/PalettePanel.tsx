/**
 * The palette panel (issue #32).
 *
 * One container over #30's store and api: it reads the palette, lets a producer
 * select or replace the kick, edit the three context fields, and create the
 * first project. It decides nothing itself -- the rules live in the slice, the
 * revision guard and the draft module, and this file renders their state.
 */

import { useCallback, useEffect, useState } from "react";

import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { toLibraryError } from "./api/errors";
import {
  PROJECT_EXISTS,
  REVISION_CONFLICT,
  UNKNOWN_PROJECT,
  refusal,
  sendPaletteMutation,
  useCreateProjectMutation,
  useGetPaletteQuery,
  useSetPaletteContextMutation,
  useSetPaletteItemMutation,
} from "./api/paletteApi";
import { useGetSampleQuery } from "./api/libraryApi";
import { ContextEditor } from "./ContextEditor";
import { KickPicker } from "./KickPicker";
import { KickSlotControl } from "./KickSlotControl";
import {
  paletteActionIssued,
  paletteConflictCleared,
  paletteConflicted,
  paletteDraftChanged,
  paletteDraftCleared,
  paletteFailed,
  paletteLoaded,
  paletteSaveFailed,
  selectContextDraft,
  selectKick,
  selectKickState,
  selectPalette,
  selectPaletteConflict,
  selectPalettePending,
  selectPaletteRevision,
  selectPaletteStatus,
} from "./state/paletteSlice";
import {
  toContextRequest,
  type ContextDraft,
  type ContextField,
  type FieldDraft,
} from "./state/contextDraft";
import { shouldRetryConflict } from "./state/revisionGuard";

export function PalettePanel() {
  const dispatch = useAppDispatch();
  const origin = useAppSelector((state) => state.service.origin);
  const palette = useAppSelector(selectPalette);
  const status = useAppSelector(selectPaletteStatus);
  const revision = useAppSelector(selectPaletteRevision);
  const kick = useAppSelector(selectKick);
  const kickState = useAppSelector(selectKickState);
  const draft = useAppSelector(selectContextDraft);
  const pending = useAppSelector(selectPalettePending);
  const conflict = useAppSelector(selectPaletteConflict);
  const epoch = useAppSelector((state) => state.palette.epoch);

  const [pickerOpen, setPickerOpen] = useState(false);
  const [projectName, setProjectName] = useState("Track A");

  const ready = origin !== null;
  const paletteQuery = useGetPaletteQuery(undefined, { skip: !ready });
  const [createProject] = useCreateProjectMutation();
  const [setPaletteItem] = useSetPaletteItemMutation();
  const [setPaletteContext] = useSetPaletteContextMutation();
  const detail = useGetSampleQuery(kick?.sample_id ?? "", { skip: kick === null });

  useEffect(() => {
    if (!ready) {
      return;
    }
    if (paletteQuery.isError) {
      const failure = toLibraryError(paletteQuery.error);
      if (refusal(paletteQuery.error).code === UNKNOWN_PROJECT) {
        dispatch(paletteLoaded(null));
      } else {
        dispatch(paletteFailed(failure));
      }
      return;
    }
    // A re-read is only applied when no user action is in flight, so it can
    // never overwrite a newer selection the producer just made.
    if (paletteQuery.isSuccess && pending === null) {
      dispatch(paletteLoaded(paletteQuery.data.palette));
    }
  }, [ready, paletteQuery.isSuccess, paletteQuery.isError, paletteQuery.data, paletteQuery.error,
      pending, dispatch]);

  const adopt = useCallback(
    (data: { palette: Parameters<typeof paletteLoaded>[0] }) => data.palette,
    [],
  );

  const onChooseKick = useCallback(
    async (sampleId: string) => {
      if (palette.paletteId === null || pending !== null) {
        return;
      }
      const paletteId = palette.paletteId;
      const issuedEpoch = epoch + 1;
      dispatch(paletteActionIssued("kick"));
      try {
        const outcome = await sendPaletteMutation({
          send: (at) => setPaletteItem({ slot: "kick", paletteId, sampleId, revision: at }).unwrap(),
          revision,
          reread: async () => {
            const fresh = await paletteQuery.refetch();
            return fresh.data === undefined ? null : fresh.data.palette.revision;
          },
          mayRetry: () => shouldRetryConflict({ ...palette, epoch, conflict }, issuedEpoch, 1),
        });
        if (outcome.conflict) {
          dispatch(paletteConflicted(1));
        } else if (!outcome.applied) {
          dispatch(paletteFailed(toLibraryError(outcome.failure)));
        }
      } finally {
        setPickerOpen(false);
      }
    },
    [conflict, dispatch, epoch, palette, paletteQuery, pending, revision, setPaletteItem],
  );

  const saveContext = useCallback(
    async (values: ContextDraft) => {
      if (palette.paletteId === null || pending !== null) {
        return;
      }
      const request = toContextRequest(values);
      if (!request.ok) {
        dispatch(paletteSaveFailed({ code: "invalid_context", field: request.field }));
        return;
      }
      const paletteId = palette.paletteId;
      const issuedEpoch = epoch + 1;
      dispatch(paletteActionIssued("context"));
      const outcome = await sendPaletteMutation({
        send: (at) => setPaletteContext({ paletteId, revision: at, ...request.value }).unwrap(),
        revision,
        reread: async () => {
          const fresh = await paletteQuery.refetch();
          return fresh.data === undefined ? null : fresh.data.palette.revision;
        },
        mayRetry: () => shouldRetryConflict({ ...palette, epoch, conflict }, issuedEpoch, 1),
      });
      if (outcome.applied) {
        dispatch(paletteDraftCleared());
        return;
      }
      if (outcome.conflict) {
        dispatch(paletteSaveFailed({ code: REVISION_CONFLICT, field: null }));
        return;
      }
      const failure = toLibraryError(outcome.failure);
      const code = refusal(outcome.failure).code;
      dispatch(paletteSaveFailed({
        code: code ?? (failure.kind === "transport" ? failure.reason : "internal_error"),
        field: String(refusal(outcome.failure).details["field"] ?? "") || null,
      }));
    },
    [conflict, dispatch, epoch, palette, paletteQuery, pending, revision, setPaletteContext],
  );

  const onCreateProject = useCallback(async () => {
    try {
      const answer = await createProject({ name: projectName }).unwrap();
      dispatch(paletteLoaded(adopt(answer)));
    } catch (error) {
      const failure = toLibraryError(error);
      if (refusal(error).code === PROJECT_EXISTS) {
        // A project already resolves: that is a reload, not a failure.
        const fresh = await paletteQuery.refetch();
        if (fresh.data !== undefined) {
          dispatch(paletteLoaded(fresh.data.palette));
        }
        return;
      }
      dispatch(paletteFailed(failure));
    }
  }, [adopt, createProject, dispatch, paletteQuery, projectName]);

  if (!ready) {
    return (
      <section className="panel" data-testid="palette-panel" data-palette-state="service-unavailable" data-revision={revision}>
        <h2 className="panel__heading">Palette</h2>
        <p>The palette is not requested while the service is starting, unavailable or failed.</p>
      </section>
    );
  }

  if (status === "no-project" || status === "idle" || status === "loading") {
    const loading = paletteQuery.isLoading || status === "loading" || status === "idle";
    return (
      <section
        className="panel"
        data-testid="palette-panel"
        data-palette-state={loading ? "loading" : "no-project"}
        data-revision={revision}
      >
        <h2 className="panel__heading">Palette</h2>
        {loading ? (
          <p data-testid="palette-loading" aria-busy="true">
            Loading the palette…
          </p>
        ) : (
          <div data-testid="palette-project-create">
            <p>No project exists yet. Create one to start selecting a kick.</p>
            <label htmlFor="project-name">Project name</label>
            <input
              id="project-name"
              type="text"
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
            />
            <button type="button" data-testid="palette-project-create-submit" onClick={() => void onCreateProject()}>
              Create project
            </button>
          </div>
        )}
      </section>
    );
  }

  return (
    <section
      className="panel"
      data-testid="palette-panel"
      data-palette-state={status === "ready" ? "ready" : "error"}
      data-revision={revision}
    >
      <h2 className="panel__heading">Palette</h2>
      {palette.project === null ? null : (
        <p className="palette__project">
          {palette.project.name} — revision {revision}
        </p>
      )}
      <KickSlotControl
        kick={kick}
        state={kickState}
        fileLabel={detail.data?.sample.file_name ?? null}
        onChoose={() => setPickerOpen(true)}
      />
      <KickPicker
        open={pickerOpen}
        selectedSampleId={kick?.sample_id ?? null}
        onSelect={(sampleId) => void onChooseKick(sampleId)}
        onClose={() => setPickerOpen(false)}
      />
      <ContextEditor
        stored={palette.context}
        draft={draft}
        saving={pending !== null && pending.action === "context"}
        onField={(field: ContextField, next: FieldDraft) =>
          dispatch(paletteDraftChanged({ ...(draft.values ?? emptyDraft), [field]: next }))}
        onState={(field: ContextField, next: FieldDraft["state"]) =>
          dispatch(paletteDraftChanged({
            ...(draft.values ?? emptyDraft),
            [field]: { state: next, value: draft.values?.[field]?.value ?? "" },
          }))}
        onSave={() => void saveContext(draft.values ?? emptyDraft)}
        onRetry={() => void saveContext(draft.values ?? emptyDraft)}
      />
      {conflict === null ? null : (
        <div data-testid="palette-conflict">
          <p>The palette changed while this action was in flight.</p>
          <button type="button" data-testid="palette-conflict-retry" onClick={() => dispatch(paletteConflictCleared())}>
            Reload and try again
          </button>
        </div>
      )}
      {palette.lastError === null ? null : (
        <p data-testid="palette-error" data-kind={palette.lastError.kind}>
          The palette could not be updated. Retry, and check the service panel above.
        </p>
      )}
    </section>
  );
}

const emptyDraft: ContextDraft = {
  tempo: { state: "unset", value: "" },
  key: { state: "unset", value: "" },
  genre: { state: "unset", value: "" },
};
