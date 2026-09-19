"""The persisted local analysis job queue (issue #23).

The queue is the second half of #22's derived work: the scan stores path rows
and writes no job row, no attempt and no progress, and this module materialises
what still needs analysis as bounded, persisted rows so a large import makes
progress, resumes after a restart and can be cancelled without losing finished
work. The work itself comes from
`backend.library.indexer.pending_analysis(connection)`; this module only
records a run, its claims, its attempts and its history.

The two tables are added to #21's migration chain (`schema.MIGRATIONS`) and are
the only tables this module writes. Public writes run in short transactions from
`backend.library.repository.transaction`; the item-state helpers that take an
open transaction say so, so an analysis and the queue row that records it can
commit together. Reads are individual statements, so a caller can poll
`status` from another connection while a run drains.

Only the standard library plus `backend.library.*` and #9's names are imported:
no `backend.intelligence`, no credential, no network call, and no audio byte or
real library path is ever stored here. See `_docs/library-jobs.md` for the
tables, the transition table, the closed stage/code tables, the numeric bounds,
the cancellation semantics and the progress and summary schemas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets

from backend.library import indexer
from backend.library.repository import transaction
from backend.library.schema import utc_now


QUEUE_SCHEMA = "1.0"

# `QUEUE_POLICY_VERSION` names the queue's own rules: what a state means, which
# codes retry, how a lease is reconciled and what the summary holds. Bump it when
# one of those changes in a way a reader of an older run's rows would misread
# (for example a new item state, a new retry classification or a new claim rule);
# do not bump it for a bug fix that leaves every stored row's meaning intact.
QUEUE_POLICY_VERSION = "library-jobs-v1"

# Item states, the closed set the migration's CHECK constraint enforces.
ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_COMPLETE = "complete"
ITEM_FAILED = "failed"
ITEM_CANCELLED = "cancelled"
ITEM_ORPHANED = "orphaned"
ITEM_SUPERSEDED = "superseded"

ITEM_STATES = (ITEM_PENDING, ITEM_RUNNING, ITEM_COMPLETE, ITEM_FAILED, ITEM_CANCELLED,
               ITEM_ORPHANED, ITEM_SUPERSEDED)

# A terminal item never moves again, except that `retry_failed` may revive
# `failed` and `orphaned` rows and `enqueue` may revive a `cancelled` row.
TERMINAL_ITEM_STATES = (ITEM_COMPLETE, ITEM_FAILED, ITEM_CANCELLED, ITEM_ORPHANED,
                        ITEM_SUPERSEDED)

# Run states, the closed set the migration's CHECK constraint enforces.
RUN_RUNNING = "running"
RUN_COMPLETE = "complete"
RUN_CANCELLED = "cancelled"
RUN_INTERRUPTED = "interrupted"
RUN_FAILED = "failed"

RUN_STATES = (RUN_RUNNING, RUN_COMPLETE, RUN_CANCELLED, RUN_INTERRUPTED, RUN_FAILED)
TERMINAL_RUN_STATES = (RUN_COMPLETE, RUN_CANCELLED, RUN_INTERRUPTED, RUN_FAILED)

# #9's completion vocabulary; `disposition` is null until an item is complete.
DISPOSITION_ANALYZED = "analyzed"
DISPOSITION_REUSED = "reused"
DISPOSITIONS = (DISPOSITION_ANALYZED, DISPOSITION_REUSED)

# The stage vocabulary is #9's `{stage, code, message}` shape plus `queue` for
# the failures this module decides itself.
STAGE_READ = "read"
STAGE_DECODE = "decode"
STAGE_EXTRACT = "extract"
STAGE_QUEUE = "queue"
STAGES = (STAGE_READ, STAGE_DECODE, STAGE_EXTRACT, STAGE_QUEUE)

CODE_SAMPLE_MISSING = "sample_missing"
CODE_CONTENT_CHANGED = "content_changed"
CODE_ANALYSIS_VERSION_CHANGED = "analysis_version_changed"
CODE_EXTRACTOR_FAILURE = "extractor_failure"

# A retryable code can clear itself by trying again; the item returns to
# `pending` while it has attempts left. Every other code is terminal on the
# first attempt, and the two tuples below are the whole vocabulary.
RETRYABLE_CODES = ("not_found", "access_denied", "io_error", "source_changed", "not_file")
TERMINAL_CODES = ("unsupported_format", "unsupported_channels", "empty_audio", "invalid_audio",
                  CODE_EXTRACTOR_FAILURE, CODE_SAMPLE_MISSING, CODE_CONTENT_CHANGED,
                  CODE_ANALYSIS_VERSION_CHANGED)
ERROR_CODES = RETRYABLE_CODES + TERMINAL_CODES

# The stage each code is recorded with. A code the worker maps itself (a read
# failure, say) keeps the stage of the step that produced it.
CODE_STAGES = {
    "not_found": STAGE_READ,
    "access_denied": STAGE_READ,
    "io_error": STAGE_READ,
    "source_changed": STAGE_READ,
    "not_file": STAGE_READ,
    "unsupported_format": STAGE_DECODE,
    "unsupported_channels": STAGE_DECODE,
    "empty_audio": STAGE_DECODE,
    "invalid_audio": STAGE_DECODE,
    CODE_EXTRACTOR_FAILURE: STAGE_EXTRACT,
    CODE_SAMPLE_MISSING: STAGE_QUEUE,
    CODE_CONTENT_CHANGED: STAGE_QUEUE,
    CODE_ANALYSIS_VERSION_CHANGED: STAGE_QUEUE,
}

# Numeric bounds. `MAX_WORKERS`, `LEASE_SECONDS` and `HEARTBEAT_SECONDS` are
# contract: the CLI refuses `--workers` outside 1..MAX_WORKERS, a heartbeat
# younger than LEASE_SECONDS is a live owner, and the owner refreshes it at least
# every HEARTBEAT_SECONDS while an item is in flight.
DEFAULT_WORKERS = 1
MAX_WORKERS = 4
MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 10
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15

RUN_ID_PREFIX = "run-"


class QueueError(Exception):
    """A queue command, parameter, lease or run-state failure (exit 2).

    Deliberately not a coded error: every storage failure keeps raising #21's
    `backend.library.errors.LibraryError` and every per-item failure is a
    `{stage, code, message}` record in `job_items`, so this module introduces no
    second error vocabulary.
    """


# ---------------------------------------------------------------------------
# parameters and small helpers
# ---------------------------------------------------------------------------


def is_fingerprint(value) -> bool:
    """True for 64 lowercase hex characters, the only fingerprint this module takes."""

    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def content_identity(fingerprint_value) -> str:
    """`sha256:<fingerprint>` for a validated fingerprint; `QueueError` otherwise.

    This is the identity a `job_items` row is keyed by: `UNIQUE(sample_id,
    analysis_version)` bounds the queue by content identity, not by run.
    """

    if not is_fingerprint(fingerprint_value):
        raise QueueError("A content fingerprint must be 64 lowercase hex characters.")
    return "sha256:" + fingerprint_value


def fingerprint_of(sample_id):
    """The 64-hex fingerprint inside a stored `sample_id`, or None when malformed."""

    if isinstance(sample_id, str) and sample_id.startswith("sha256:") \
            and is_fingerprint(sample_id[len("sha256:"):]):
        return sample_id[len("sha256:"):]
    return None


def stage_for(code, fallback=STAGE_EXTRACT) -> str:
    """The stage a code is recorded with, or `fallback` for a code outside the tables."""

    return CODE_STAGES.get(code, fallback)


def is_retryable(code) -> bool:
    """True when a code may clear itself on a later attempt.

    A code outside `RETRYABLE_CODES` is terminal, so a defect cannot produce an
    endlessly retried item.
    """

    return code in RETRYABLE_CODES


def error_record(stage, code, message) -> dict:
    """#9's `{stage, code, message}` record, with a non-empty string in every field."""

    return {"stage": str(stage), "code": str(code), "message": str(message) or str(code)}


def _require_version(analysis_version) -> str:
    if not is_fingerprint(analysis_version):
        raise QueueError("An analysis version must be 64 lowercase hex characters.")
    return analysis_version


def require_workers(workers) -> int:
    if isinstance(workers, bool) or not isinstance(workers, int):
        raise QueueError("workers must be an integer.")
    if not 1 <= workers <= MAX_WORKERS:
        raise QueueError(f"workers must be between 1 and {MAX_WORKERS}; got {workers}.")
    return workers


def require_attempts(max_attempts) -> int:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise QueueError("max_attempts must be an integer.")
    if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
        raise QueueError(
            f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}; got {max_attempts}.")
    return max_attempts


def _cutoff(seconds) -> str:
    """The instant before `now` by `seconds`, in `schema.utc_now`'s format.

    The format is fixed width and UTC, so comparing two stamps as strings is
    comparing them in time.
    """

    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# enqueueing (derived, idempotent)
# ---------------------------------------------------------------------------


def enqueue(connection, analysis_version) -> int:
    """Materialise #22's pending work as one `pending` item per request.

    `indexer.pending_analysis(connection)` is the only source of work, and only
    its requests at `analysis_version` are taken. The item's `sample_id` is the
    request's content identity, so `INSERT ... ON CONFLICT (sample_id,
    analysis_version) DO NOTHING` makes duplicate calls, a concurrent scan and a
    second worker harmless: a key can hold one item. A `cancelled` row for the
    same key is revived to `pending` with attempts, run and error cleared; every
    other existing state is left untouched. Returns the number of items inserted
    or revived.
    """

    _require_version(analysis_version)
    now = utc_now()
    changed = 0
    with transaction(connection):
        for request in indexer.pending_analysis(connection):
            if request.analysis_version != analysis_version:
                continue
            identity = content_identity(request.fingerprint)
            cursor = connection.execute(
                "INSERT INTO job_items (sample_id, analysis_version, path, role, state, "
                "disposition, attempts, run_id, claimed_at, finished_at, error_stage, error_code, "
                "error_message, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, ?) "
                "ON CONFLICT (sample_id, analysis_version) DO NOTHING",
                (identity, analysis_version, request.path, request.role, now))
            changed += cursor.rowcount
            revived = connection.execute(
                "UPDATE job_items SET state = 'pending', disposition = NULL, attempts = 0, "
                "run_id = NULL, claimed_at = NULL, finished_at = NULL, error_stage = NULL, "
                "error_code = NULL, error_message = NULL "
                "WHERE sample_id = ? AND analysis_version = ? AND state = 'cancelled'",
                (identity, analysis_version))
            changed += revived.rowcount
    return changed


def claim_horizon(connection):
    """The largest item id that exists now, or None for an empty queue.

    `--once` records this after its one `enqueue` call and claims nothing above
    it, so the run processes exactly the work derived at start.
    """

    row = connection.execute("SELECT MAX(item_id) FROM job_items").fetchone()
    return None if row is None or row[0] is None else row[0]


def retry_failed(connection, analysis_version) -> int:
    """Reset this version's `failed` and `orphaned` items to `pending`.

    Attempts restart at zero and the error and run are cleared. Idempotent: a
    second call changes nothing. `complete`, `cancelled`, `superseded` and
    `running` items and every stored feature are never touched. Returns the
    number of items reset.
    """

    _require_version(analysis_version)
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE job_items SET state = 'pending', attempts = 0, run_id = NULL, "
            "claimed_at = NULL, finished_at = NULL, error_stage = NULL, error_code = NULL, "
            "error_message = NULL "
            "WHERE analysis_version = ? AND state IN ('failed', 'orphaned')",
            (analysis_version,))
    return cursor.rowcount


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def get_run(connection, run_id):
    """One `job_runs` row, or `QueueError` when the id is unknown."""

    row = connection.execute("SELECT * FROM job_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise QueueError(f"No analysis run with run_id {run_id!r}.")
    return row


def open_run(connection, analysis_version, workers=DEFAULT_WORKERS,
             max_attempts=MAX_ATTEMPTS) -> str:
    """Commit one `running` run row and return its `run_id`; extract nothing.

    Reconciliation runs first, so a run whose owner died is `interrupted` and its
    items are back in the queue. A live owner (a `running` run whose heartbeat is
    younger than `LEASE_SECONDS`) refuses this call with a `QueueError` naming
    the run id, and no row, claim or extraction happens. This is the call a
    caller makes before handing work to a non-UI thread or process.
    """

    _require_version(analysis_version)
    require_workers(workers)
    require_attempts(max_attempts)
    now = utc_now()
    with transaction(connection):
        reconcile_locked(connection, now)
        live = connection.execute(
            "SELECT run_id, heartbeat_at FROM job_runs WHERE state = 'running' "
            "ORDER BY started_at ASC, run_id ASC LIMIT 1").fetchone()
        if live is not None:
            raise QueueError(
                f"Another analysis run is live on this database: {live['run_id']} "
                f"(heartbeat {live['heartbeat_at']}). Let it finish, cancel it with "
                "--cancel, or wait for its lease to expire.")
        run_id = RUN_ID_PREFIX + secrets.token_hex(16)
        connection.execute(
            "INSERT INTO job_runs (run_id, state, analysis_version, workers, max_attempts, "
            "owner_token, heartbeat_at, started_at, finished_at, cancel_requested) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?, NULL, 0)",
            (run_id, analysis_version, workers, max_attempts, secrets.token_hex(16), now, now))
    return run_id


def reconcile(connection) -> dict:
    """Return abandoned runs and their in-flight items to the queue.

    A `running` run whose heartbeat is older than `LEASE_SECONDS` becomes
    `interrupted`, and every `running` item whose run is not live becomes
    `pending` again with its attempts preserved and its claim cleared. It never
    touches a `complete` item, a feature row or an analysis version. Returns
    `{"runs": n, "items": m}` for what it changed.
    """

    now = utc_now()
    with transaction(connection):
        return reconcile_locked(connection, now)


def reconcile_locked(connection, now) -> dict:
    """`reconcile` inside the caller's transaction."""

    stale = connection.execute(
        "UPDATE job_runs SET state = 'interrupted', finished_at = ? "
        "WHERE state = 'running' AND heartbeat_at <= ?", (now, _cutoff(LEASE_SECONDS)))
    requeued = connection.execute(
        "UPDATE job_items SET state = 'pending', run_id = NULL, claimed_at = NULL, "
        "finished_at = NULL, error_stage = NULL, error_code = NULL, error_message = NULL "
        "WHERE state = 'running' AND NOT EXISTS (SELECT 1 FROM job_runs AS r "
        "                                         WHERE r.run_id = job_items.run_id "
        "                                         AND r.state = 'running')")
    return {"runs": stale.rowcount, "items": requeued.rowcount}


def heartbeat(connection, run_id, owner_token) -> bool:
    """Refresh one run's heartbeat if this owner still holds it; False otherwise."""

    with transaction(connection):
        cursor = connection.execute(
            "UPDATE job_runs SET heartbeat_at = ? "
            "WHERE run_id = ? AND owner_token = ? AND state = 'running'",
            (utc_now(), run_id, owner_token))
    return cursor.rowcount == 1


def cancel_requested(connection, run_id) -> bool:
    """True when cancellation was requested for that run."""

    row = connection.execute("SELECT cancel_requested FROM job_runs WHERE run_id = ?",
                             (run_id,)).fetchone()
    return bool(row is not None and row["cancel_requested"])


def request_cancel(connection) -> int:
    """Flag every live run for cancellation and return how many were flagged.

    A run that is not live is not flagged: cancellation is for work in progress,
    and finished runs are already final.
    """

    with transaction(connection):
        cursor = connection.execute(
            "UPDATE job_runs SET cancel_requested = 1 WHERE state = 'running'")
    return cursor.rowcount


def finish_run(connection, run_id, state) -> None:
    """Set a run's terminal state inside the caller's transaction."""

    if state not in TERMINAL_RUN_STATES:
        raise QueueError(f"Not a terminal run state: {state!r}.")
    connection.execute("UPDATE job_runs SET state = ?, finished_at = ? WHERE run_id = ?",
                       (state, utc_now(), run_id))


# ---------------------------------------------------------------------------
# claims and item transitions
# ---------------------------------------------------------------------------


def claim(connection, run_id, horizon=None):
    """Claim the lowest-id `pending` item for `run_id`, or None when none is left.

    The claim is one conditional statement whose subselect picks the item, so two
    claimers can never receive the same row; the stored row is then re-read to
    confirm the claim before any byte is read. `horizon` bounds it to items at or
    below that id (`--once`). A `pending` item at another analysis version is
    finalized `superseded` with `analysis_version_changed` first and is never
    executed, and a run that was cancelled (or is no longer running) claims
    nothing.
    """

    now = utc_now()
    with transaction(connection):
        run = get_run(connection, run_id)
        if run["state"] != RUN_RUNNING or run["cancel_requested"]:
            return None
        _supersede_other_versions(connection, run_id, run["analysis_version"], now)
        if horizon is None:
            claimed = connection.execute(
                "UPDATE job_items SET state = 'running', run_id = ?, claimed_at = ?, "
                "attempts = attempts + 1 "
                "WHERE item_id = (SELECT item_id FROM job_items WHERE state = 'pending' "
                "                 ORDER BY item_id LIMIT 1) "
                "AND state = 'pending' RETURNING item_id", (run_id, now)).fetchone()
        else:
            claimed = connection.execute(
                "UPDATE job_items SET state = 'running', run_id = ?, claimed_at = ?, "
                "attempts = attempts + 1 "
                "WHERE item_id = (SELECT item_id FROM job_items WHERE state = 'pending' "
                "                 AND item_id <= ? ORDER BY item_id LIMIT 1) "
                "AND state = 'pending' RETURNING item_id", (run_id, now, horizon)).fetchone()
        if claimed is None:
            return None
        item = connection.execute("SELECT * FROM job_items WHERE item_id = ?",
                                  (claimed["item_id"],)).fetchone()
    if item is None or item["state"] != ITEM_RUNNING or item["run_id"] != run_id:
        return None
    return item


def _supersede_other_versions(connection, run_id, analysis_version, now) -> int:
    """Finalize pending work for another analysis version; it is never executed.

    The row is attributed to the run that superseded it, so the run's summary and
    status count the work it decided about even though no attempt was made.
    """

    cursor = connection.execute(
        "UPDATE job_items SET state = 'superseded', disposition = NULL, run_id = ?, "
        "finished_at = ?, error_stage = ?, error_code = ?, error_message = ? "
        "WHERE state = 'pending' AND analysis_version <> ?",
        (run_id, now, STAGE_QUEUE, CODE_ANALYSIS_VERSION_CHANGED,
         "The queued analysis version is no longer the run's analysis version.", analysis_version))
    return cursor.rowcount


def requeue(connection, item_id) -> None:
    """Return one claimed item to `pending` inside the caller's transaction.

    Attempts are kept, because the decision to retry counts them; the claim and
    the previous error are cleared so the next attempt starts clean.
    """

    connection.execute(
        "UPDATE job_items SET state = 'pending', disposition = NULL, run_id = NULL, "
        "claimed_at = NULL, finished_at = NULL, error_stage = NULL, error_code = NULL, "
        "error_message = NULL WHERE item_id = ?", (item_id,))


def finalize(connection, item_id, state, disposition=None, error=None) -> None:
    """Set one item's terminal state inside the caller's transaction.

    `state` is one of the terminal states, `disposition` is #9's `analyzed` or
    `reused` for a `complete` item and None otherwise, and `error` is a
    `{stage, code, message}` record (#9's shape) for the states that carry one.
    """

    if state not in TERMINAL_ITEM_STATES:
        raise QueueError(f"Not a terminal item state: {state!r}.")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise QueueError(f"Not a stored disposition: {disposition!r}.")
    stage = code = message = None
    if error is not None:
        stage, code, message = error["stage"], error["code"], error["message"]
    connection.execute(
        "UPDATE job_items SET state = ?, disposition = ?, finished_at = ?, error_stage = ?, "
        "error_code = ?, error_message = ? WHERE item_id = ?",
        (state, disposition, utc_now(), stage, code, message, item_id))


def cancel_pending(connection, run_id, analysis_version) -> int:
    """Finalize every not-yet-started `pending` item of this version as `cancelled`.

    Called inside the caller's transaction once cancellation is observed: the run
    claims nothing new, and each item that never started is recorded as
    cancelled rather than left for a later run to guess at.
    """

    cursor = connection.execute(
        "UPDATE job_items SET state = 'cancelled', disposition = NULL, run_id = ?, "
        "finished_at = ? WHERE state = 'pending' AND analysis_version = ?",
        (run_id, utc_now(), analysis_version))
    return cursor.rowcount


# ---------------------------------------------------------------------------
# progress and summary
# ---------------------------------------------------------------------------


def _state_counts(connection, run_id=None, analysis_version=None) -> dict:
    """Item counts, per state, for one run or for the whole queue."""

    if run_id is None:
        rows = connection.execute("SELECT state, COUNT(*) FROM job_items GROUP BY state")
    else:
        rows = connection.execute(
            "SELECT state, COUNT(*) FROM job_items "
            "WHERE run_id = ? OR (state = 'pending' AND analysis_version = ?) GROUP BY state",
            (run_id, analysis_version))
    counts = {state: 0 for state in ITEM_STATES}
    for state, number in rows.fetchall():
        counts[state] = number
    counts["remaining"] = counts[ITEM_PENDING] + counts[ITEM_RUNNING]
    return counts


def status(connection, run_id=None) -> dict:
    """The progress of one run, or of the newest run, from SQL aggregates only.

    Safe to call from any connection while the worker runs: every statement is a
    short read and no transaction is held between them. Without `run_id` the
    newest run (by `started_at`, then `run_id`) is reported; with no run at all
    the counts still describe the whole queue.
    """

    if run_id is None:
        run = connection.execute("SELECT * FROM job_runs ORDER BY started_at DESC, "
                                 "run_id DESC LIMIT 1").fetchone()
    else:
        run = get_run(connection, run_id)
    if run is None:
        return {"run_id": None, "state": None, "analysis_version": None, "workers": None,
                "started_at": None, "counts": _state_counts(connection), "current": None}
    counts = _state_counts(connection, run["run_id"], run["analysis_version"])
    current = connection.execute(
        "SELECT sample_id, path, attempts FROM job_items "
        "WHERE run_id = ? AND state = 'running' ORDER BY item_id ASC LIMIT 1",
        (run["run_id"],)).fetchone()
    return {"run_id": run["run_id"], "state": run["state"],
            "analysis_version": run["analysis_version"], "workers": run["workers"],
            "started_at": run["started_at"], "counts": counts,
            "current": None if current is None else {
                "sample_id": current["sample_id"], "path": current["path"],
                "attempts": current["attempts"]}}


def summary(connection, run_id) -> dict:
    """The finished or in-progress summary of one run.

    `counts` holds one number per item state plus #9's `analyzed`, `reused` and
    `remaining`, and equals the persisted rows when the run ends. `failures`
    holds one record per non-`complete` item of the run in item-id order, each
    with `sample_id`, `path`, `role`, `stage`, `code`, `message` and `attempts`
    (the last three are null for an item that never failed, such as one cancelled
    before it started).
    """

    run = get_run(connection, run_id)
    counts = _state_counts(connection, run["run_id"], run["analysis_version"])
    completed = connection.execute(
        "SELECT disposition, COUNT(*) FROM job_items WHERE state = 'complete' AND run_id = ? "
        "GROUP BY disposition", (run["run_id"],)).fetchall()
    by_disposition = {row[0]: row[1] for row in completed}
    rows = connection.execute(
        "SELECT sample_id, path, role, error_stage, error_code, error_message, attempts "
        "FROM job_items WHERE state <> 'complete' "
        "AND (run_id = ? OR (state = 'pending' AND analysis_version = ?)) "
        "ORDER BY item_id ASC", (run["run_id"], run["analysis_version"])).fetchall()
    return {
        "queue_schema": QUEUE_SCHEMA,
        "queue_policy_version": QUEUE_POLICY_VERSION,
        "run_id": run["run_id"],
        "analysis_version": run["analysis_version"],
        "state": run["state"],
        "workers": run["workers"],
        "max_attempts": run["max_attempts"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "counts": {
            ITEM_PENDING: counts[ITEM_PENDING], ITEM_RUNNING: counts[ITEM_RUNNING],
            ITEM_COMPLETE: counts[ITEM_COMPLETE], ITEM_FAILED: counts[ITEM_FAILED],
            ITEM_CANCELLED: counts[ITEM_CANCELLED], ITEM_ORPHANED: counts[ITEM_ORPHANED],
            ITEM_SUPERSEDED: counts[ITEM_SUPERSEDED],
            DISPOSITION_ANALYZED: by_disposition.get(DISPOSITION_ANALYZED, 0),
            DISPOSITION_REUSED: by_disposition.get(DISPOSITION_REUSED, 0),
            "remaining": counts["remaining"],
        },
        "failures": [{"sample_id": row["sample_id"], "path": row["path"], "role": row["role"],
                      "stage": row["error_stage"], "code": row["error_code"],
                      "message": row["error_message"], "attempts": row["attempts"]}
                     for row in rows],
    }


def database_file(connection) -> str:
    """The main database file behind one connection, or `QueueError` for memory.

    A worker thread opens its own connection through #21's
    `schema.open_database`, which needs the file path; an in-memory database
    cannot be shared between threads and is refused rather than silently
    serialised onto one connection.
    """

    rows = connection.execute("PRAGMA database_list").fetchall()
    for row in rows:
        if row[1] == "main" and row[2]:
            return row[2]
    raise QueueError("The queue needs a file-backed database; an in-memory database "
                     "cannot be worked on by several threads.")
