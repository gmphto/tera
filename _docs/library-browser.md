# The library browser (issue #31)

Folder import and library browsing, inside the shell from
[#30](https://github.com/gmphto/tera/issues/30). It consumes six routes from
[#27](https://github.com/gmphto/tera/issues/27) and invents no route, no status
vocabulary and no second source of library state.

## The six routes it calls

| Route | Taken from the response |
| --- | --- |
| `GET /library/samples` | `items[].sample_id`, `.file_name`, `.role`, `.file_status`, `.analysis.state`, `.audio.duration_ms`, `.audio.sample_rate_hz`, `.audio.channels`; `page.count`, `page.has_more`, `page.next_cursor`; the `query` echo |
| `GET /library/samples/{sample_id}` | `sample.sample_id` and `sample.file_status` only |
| `POST /imports` | `import.run_id` |
| `GET /imports/{run_id}` | `counts`, `state`, `phase`, `cancel_requested`, `current`, `failures`, `scan`, `root_label`, `role` |
| `POST /imports/{run_id}/cancel` | `import.cancel_requested` |
| `POST /imports/{run_id}/retry` | `import.retried`, `import.run_id` |

It never calls `GET /health` (that belongs to #30), an audio-shaped path, or a
palette, project, recommendation or outcome route. The detail response's
`features`, `measurements`, `key` and `analysis` fields are never read: the
explanation panel is [#35](https://github.com/gmphto/tera/issues/35) and the
ranked cards are [#33](https://github.com/gmphto/tera/issues/33).

## The closed vocabularies

Every name is #27's. The visible label sits beside the value, never instead of
it.

| Vocabulary | Values |
| --- | --- |
| `role` (`PALETTE_ROLES`) | `kick`, `bass`, `sub-bass` |
| `file_status` | `present`, `missing`, `unknown`, `unreadable` |
| `analysis.state` | `current`, `stale`, `pending`, `failed`, `absent` |
| run `state` | `running`, `complete`, `cancelled`, `interrupted`, `failed` |
| run `phase` | `scanning`, `analyzing`, then the terminal `state` |
| `counts` | `pending` ("Queued"), `running`, `complete`, `failed`, `cancelled`, `orphaned`, `superseded`, `analyzed`, `reused`, `remaining` |
| picker reason | `not_tauri`, `permission_denied`, `dialog_failed` |
| transport reason | `connection_refused`, `timeout`, `unparseable`, `unavailable` |

## The error table

`LibraryError` is `{kind: "api", code, status, details}` or
`{kind: "transport", reason}`. `LIBRARY_ERROR_RECOVERY` is total over the
sixteen codes and the four reasons; each sentence is non-empty and at most 200
characters, and a code outside the closed table falls back to one generic
sentence rather than an empty panel.

| Code or reason | Sentence |
| --- | --- |
| `invalid_root` | Choose a folder on a local drive. A UNC path or a mapped network drive cannot be imported. |
| `invalid_role` | Pick one of the listed roles; the service accepts no other. |
| `invalid_query` | Shorten the search text: the service accepts 1 to 200 characters. |
| `invalid_page_size` | Pick one of the listed page sizes; the service accepts 1 to 200. |
| `unknown_query_parameter` | Reload the window: this client sent a parameter the service does not accept. |
| `invalid_cursor` | The page position was refused. The list returns to the first page. |
| `invalid_sample_id` | Reload the window: the sample identity it holds is not one the service issues. |
| `unknown_sample` | This sample is no longer in the library. The selection was cleared. |
| `import_already_running` | An import is already running in this library; its progress is shown here. |
| `import_not_live` | The run already finished, so cancelling it would write nothing. Its result is shown. |
| `unknown_import` | The service has no run with this id. Start a new import. |
| `database_unavailable` | The library database is unavailable. See the service panel above; the library stays readable. |
| `server_busy` | The service is at its request limit. Retry in a moment. |
| `unsupported_media_type` | Reload the window: this client sent a body the service does not accept. |
| `request_too_large` | The request was larger than the service accepts. Reload the window. |
| `internal_error` | The service failed to complete the request. Retry; nothing partial was written. |
| `connection_refused` | Nothing is listening on the service port. Start the service, then Retry. |
| `timeout` | The service did not answer in time. It may be busy; Retry. |
| `unparseable` | The service answered with something this client cannot read. Retry. |
| `unavailable` | The service origin is not known yet. See the service panel above. |

## The checkable surface

| Element | Attributes | Shown when |
| --- | --- | --- |
| `library-browser` | — | always; a sibling of #30's `service-status` |
| `import-controls`, `import-role`, `import-pick` | — | always; both disabled while a run is live |
| `import-picker-error` | `data-reason` | the picker was refused |
| `import-progress` | `data-run-id`, `data-state`, `data-phase`, `role="status"`, `aria-live="polite"` | a run is tracked |
| `import-count-pending` / `-running` / `-complete` / `-failed` | `data-count` | with a run |
| `import-progress-bar` | `data-mode`, `data-percent` (determinate only) | with a run |
| `import-current` | — | `current` is non-null |
| `import-scan-missing` | — | `scan` is null |
| `import-discovery-errors` | `data-count` | the count is non-zero |
| `import-partial` | `data-failed`, `data-total` | `state=complete` with `counts.failed > 0` |
| `import-failures`, `import-failure` | `data-origin`, `data-sample-id`, `data-file-name`, `data-stage`, `data-code`, `data-attempts` | any failure or scan error |
| `import-cancel`, `import-cancel-pending` | — | cancel is enabled only for a live run |
| `import-retry`, `import-retry-none` | — | retry needs a stopped run with failures |
| `import-terminal` | `data-state` | `complete`, `cancelled`, `interrupted`, `failed` |
| `library-search` | `maxlength="200"` | always |
| `library-role-filter` | `data-role` per checkbox | always |
| `library-page-size` | — | always |
| `library-loading` | `aria-busy="true"` | first load for the current arguments |
| `library-results` | `data-query-text`, `data-query-roles`, `data-busy` | a page is on screen |
| `library-empty`, `library-empty-import` | — | zero items, no filter set |
| `library-no-matches`, `library-clear-filters` | `data-query-text`, `data-query-roles` | zero items, a filter set |
| `library-error` | `data-kind`, `data-code` or `data-reason` | any failure |
| `library-retry` | — | with any failure |
| `library-degraded` | `data-code="database_unavailable"` | the service is up, the database is not |
| `library-service-unavailable` | — | #30's phase is `starting`, `unavailable` or `failed` |
| `selection-stale` | — | the service answered 404 for the selection |
| `sample-row` | `data-sample-id`, `data-role`, `data-file-status`, `data-analysis-state`, `data-selected`, `data-duration-ms`, `data-sample-rate-hz`, `data-channels` | one per item |
| `sample-analysis-unavailable` | `data-analysis-state` | the row's analysis is not current |
| `sample-reimport` | — | the row's file is `missing` |
| `library-pager`, `library-prev`, `library-next` | `data-page-index`, `data-page-count`, `data-has-more`, `data-next-cursor` | always |

## Constants

| Constant | Value |
| --- | --- |
| `SEARCH_DEBOUNCE_MS` | 250 |
| `IMPORT_POLL_INTERVAL_MS` | 1000 |
| `LIBRARY_REFRESH_INTERVAL_MS` | 5000 |
| `PAGE_SIZE_OPTIONS` | 25, 50, 100 |
| `DEFAULT_PAGE_SIZE` | 50 |
| `MAX_QUERY_LENGTH` | 200 |
| `PALETTE_ROLES` | `kick`, `bass`, `sub-bass` |

## The picker

`pickFolder()` returns `{kind: "selected", root}` | `{kind: "cancelled"}` |
`{kind: "unavailable", reason}`. It checks `isTauri()` first, so a plain browser
session gets `not_tauri` without invoking anything, and it never rethrows: a
throw whose text names `dialog:allow-open`, `not allowed` or `forbidden` is
`permission_denied`, and any other throw is `dialog_failed`. Dismissing the
picker creates no import — neither `cancelled` nor `unavailable` dispatches a
mutation.

The dependency is the approved `@tauri-apps/plugin-dialog` npm package pinned to
2.7.3 and the `tauri-plugin-dialog` Rust crate at major 2 (lockfile-resolved to
2.7.3). The capability file grants `dialog:allow-open` to the `main` window only,
so no message, save or confirm dialog is reachable.

The selected absolute path is a mutation argument and nothing else: it is never
written to the slice, persisted, logged, rendered or placed in a `data-*`
attribute. While `POST /imports` is in flight the panel shows the folder's base
name as transient component state, and afterwards the folder is named by
`root_label` from `GET /imports/{run_id}`.

## Polling

There is no push channel. `importPollingInterval(state)` is 1000 ms while a run
is `running` and 0 for every terminal state; `libraryPollingInterval(state)` is
5000 ms while a run is live and 0 otherwise. Both are passed to RTK Query, so the
list keeps its own arguments and the import panel keeps its own run. The list is
keyed by `sample_id` and its rows are never remounted on a poll, so the search
text, the input focus, the checkbox state and the scroll position survive a
refresh. A poll that resolves after a filter change lands under its own cache key
and is never rendered as the current list.

## Boundaries this task does not cross

No measurement, similarity, compatibility, confidence, score or ranking is
computed or rendered. No palette is written and no palette route is called. No
audio is decoded and no file or directory is read: the only path the client ever
holds is the one the picker returned, and it is handed straight to `POST
/imports`. No second store, api instance, base query, status vocabulary or
supervisor is created: the browser registers one reducer in #30's store and
injects its endpoints into #30's api.

## Reproducing the checks

```sh
cd frontend
npm ci                                                        # pins from the committed lockfile
npx vitest run --reporter=verbose                             # the six files and their case counts
npm run build                                                 # tsc --noEmit, then dist/
cd ../frontend && cargo test --manifest-path src-tauri/Cargo.toml   # the host's own states
```

`npm test -- --reporter=verbose` is the documented form, but npm 12 parses
`--reporter` as one of its own flags and fails with `EUNKNOWNCONFIG`; `npx
vitest run --reporter=verbose` is the same run.

`frontend/vite.config.ts` includes `src/**/*.test.tsx` as well as
`src/**/*.test.ts`, so a green run that collected no `.tsx` file is visible: the
recorded list names `views.test.tsx` with its case count.

No Python file changes, so `uv run pytest` reports the same result as before this
task and was replaced by `uv run python -m tools.focused`, which reports that
nothing under `backend/` or `tools/` changed.
