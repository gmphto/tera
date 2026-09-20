"""Drain the persisted local analysis queue (issue #23).

    python -m backend.library.worker --database DB.sqlite3 [--workers N]
                                     [--max-attempts N] [--once] [--run-id RUN_ID]
                                     [--cancel] [--retry-failed] [--summary OUT.json]

The worker derives pending work with #22's
`backend.library.indexer.pending_analysis`, claims it one item at a time through
`backend.library.queue`, executes each item with #9's
`backend.analysis.batch.snapshot` and `backend.analysis.batch.extract` only, and
stores what comes back through #21's `backend.library.repository`. It does not
reimplement discovery, hashing, decoding, extraction or Sample assembly, does not
change #9's or #21's modules, never decodes on the calling thread (the run row is
committed first and this module's blocking `run_queue` runs on worker threads or
in this process), never blocks library reads with a long transaction, and never
touches Jev, the network or a source audio file.

Exit codes are #9's: 0 finished with no per-item errors, 1 finished with one or
more per-item errors, 2 an invalid command, database, schema, worker or attempt
count, or a storage failure, 130 cancelled or interrupted. The run summary is
printed to stdout as one JSON line and, with `--summary`, written atomically to a
local file. See `_docs/library-jobs.md`.

The drain's footprint is the work in flight, not the queue: one item's snapshot,
decoded arrays and assembled Sample are locals of one step of one worker thread
and nothing but the item's row outlives it, a progress line reads counts rather
than the summary's per-item records, and the unreachable cycles one item's
contract validation leaves behind are reclaimed every `COLLECT_INTERVAL` items
(see that constant for the measurement). The drain's write transactions are
serialized by one process-local lock (`_WRITE_LOCK`) so four workers cannot
starve one another out of SQLite's write lock; nothing else changes, and a
snapshot or an extraction is never inside either the lock or a transaction.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import gc
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

from backend import audio
from backend.analysis import batch
from backend.contracts import Sample
from backend.library import indexer, queue
from backend.library.errors import (
    InvalidContentIdentity,
    LibraryError,
    UnknownSample,
)
from backend.library.repository import LibraryRepository, transaction
from backend.library.schema import open_database


EXIT_OK = 0
EXIT_ITEM_ERROR = 1
EXIT_INVALID = 2
EXIT_CANCELLED = 130

# The drain outcomes: everything finished, the run was cancelled, or a storage
# failure stopped it. The first outcome a thread records is the run's.
_DRAINED = "drained"
_CANCELLED = "cancelled"
_STORAGE = "storage"

# How many finalized items one drain may accumulate unreachable cycles for.
#
# One item's work leaves reference cycles in the heap: the assembled Sample is
# validated with #9's `contracts.Sample.from_dict`, and that layer re-evaluates
# every field annotation on every construction (`Model.__post_init__` calls
# `typing.get_type_hints`), so the objects it builds are unreachable the moment
# the item ends but are only returned to the allocator by a generation-2
# collection. Minor collections do not reclaim them (measured: a generation 0
# or 1 pass leaves the drain's traced peak growing by ~4.4 KB per item), so
# without an explicit collection a long drain's peak grows with the queue
# instead of with the work in flight. A full collection per item would pay for
# the whole heap per item, so the drain completes one every COLLECT_INTERVAL
# items and the per-item residual is bounded by this number. The measurement is
# recorded in `_docs/library-jobs.md`.
COLLECT_INTERVAL = 8

_PRINT_LOCK = threading.Lock()

# Serializes the drain's write transactions across its worker threads.
#
# SQLite has one writer, but its busy handler does not queue: with four workers
# each taking the write lock for one short claim or commit, a loser can miss
# every round and hit #21's 5 s busy timeout. Measured on this machine during a
# 200-item run under `tracemalloc`: lock holds of 0.01-0.15 s against waits of
# 2.0-5.0 s, and runs that ended `database is locked` with items left pending.
# One process-local lock turns that starvation into a queue — the thread that
# holds it holds the database's write lock for the length of one transaction and
# then hands both on — while connections stay per-thread and SQLite's own
# single-writer rule still does the serialising. It is held only around a claim,
# a finalize or a commit, never during a snapshot or an extraction.
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--workers", type=int, default=queue.DEFAULT_WORKERS)
    parser.add_argument("--max-attempts", type=int, default=queue.MAX_ATTEMPTS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--summary")
    arguments = parser.parse_args(argv)
    try:
        return run(arguments.database, workers=arguments.workers,
                   max_attempts=arguments.max_attempts, once=arguments.once,
                   run_id=arguments.run_id, cancel=arguments.cancel,
                   retry_failed=arguments.retry_failed, summary_path=arguments.summary)
    except KeyboardInterrupt:
        print("Interrupted before the run finished; it is cancelled and every committed "
              "item stays committed.", file=sys.stderr)
        return EXIT_CANCELLED
    except queue.QueueError as error:
        print(f"Queue command/run failure: {error}", file=sys.stderr)
        return EXIT_INVALID
    except LibraryError as error:
        print(f"Queue storage failure: {error} ({error.code})", file=sys.stderr)
        return EXIT_INVALID
    except sqlite3.Error as error:
        print(f"Queue storage failure: {error}", file=sys.stderr)
        return EXIT_INVALID


def run(database, *, workers=queue.DEFAULT_WORKERS, max_attempts=queue.MAX_ATTEMPTS,
        once=False, run_id=None, cancel=False, retry_failed=False, summary_path=None) -> int:
    """Drain the queue once, print the summary and return the exit code."""

    queue.require_workers(workers)
    queue.require_attempts(max_attempts)
    destination = _summary_destination(summary_path)
    database_path = _database_path(database)
    with closing(_open_database(database_path)) as connection:
        if cancel:
            return _cancel(connection, destination)
        if run_id is None:
            run_id = queue.open_run(connection, indexer.current_analysis_version(), workers,
                                    max_attempts)
        else:
            adopted = queue.get_run(connection, run_id)
            if adopted["state"] != queue.RUN_RUNNING:
                raise queue.QueueError(
                    f"Run {run_id} is {adopted['state']}, not running; nothing was drained.")
        if retry_failed:
            queue.retry_failed(connection, queue.get_run(connection, run_id)["analysis_version"])
        code = run_queue(connection, run_id, workers, max_attempts, once)
        payload = queue.summary(connection, run_id)
        _print(payload)
        if destination is not None:
            _write_summary(destination, payload)
    return code


def _cancel(connection, destination) -> int:
    """Flag every live run and return 0; nothing is extracted and no run is opened."""

    flagged = queue.request_cancel(connection)
    payload = {"queue_schema": queue.QUEUE_SCHEMA,
               "queue_policy_version": queue.QUEUE_POLICY_VERSION,
               "state": "cancel_requested",
               "runs": flagged}
    _print(payload)
    if destination is not None:
        _write_summary(destination, payload)
    return EXIT_OK


# ---------------------------------------------------------------------------
# the run loop (never on the calling thread)
# ---------------------------------------------------------------------------


def run_queue(connection, run_id, workers, max_attempts, once=False) -> int:
    """Drain one open run on `workers` threads and return the exit code.

    Blocking by design: the caller makes this call from this process or from a
    thread it started itself, never from a request handler or a UI event. The run
    row already exists, so a caller that used `queue.open_run` returned the run id
    before any byte was read. `once` claims only the work derived at start;
    otherwise the worker re-derives #22's queue after each drain and stops when
    the derivation adds nothing.
    """

    queue.require_workers(workers)
    queue.require_attempts(max_attempts)
    run = queue.get_run(connection, run_id)
    if run["state"] != queue.RUN_RUNNING:
        raise queue.QueueError(f"Run {run_id} is {run['state']}, not running.")
    version = run["analysis_version"]
    database = queue.database_file(connection)
    heartbeat = _Heartbeat(database, run_id, run["owner_token"])
    outcome, error = _DRAINED, None
    heartbeat.start()
    try:
        queue.enqueue(connection, version)
        horizon = queue.claim_horizon(connection) if once else None
        while True:
            outcome, error = _drain(database, run_id, version, workers, run["max_attempts"],
                                    horizon)
            if outcome != _DRAINED or once:
                break
            if queue.enqueue(connection, version) == 0:
                break
    except KeyboardInterrupt:
        outcome, error = _CANCELLED, None
        try:
            queue.request_cancel(connection)
        except (LibraryError, sqlite3.Error):
            pass
    except (LibraryError, sqlite3.Error) as failure:
        outcome, error = _STORAGE, failure
    except Exception as failure:  # a defect must fail the run, not leave it live
        outcome, error = _STORAGE, failure
    finally:
        heartbeat.stop()
    _settle(connection, run_id, version, outcome)
    if outcome == _CANCELLED:
        return EXIT_CANCELLED
    if outcome == _STORAGE:
        print(f"The run stopped and is marked failed; every committed item stays "
              f"readable: {error}", file=sys.stderr)
        return EXIT_INVALID
    counts = queue.progress(connection, run_id)["counts"]
    if counts[queue.ITEM_FAILED] + counts[queue.ITEM_ORPHANED] + counts[queue.ITEM_SUPERSEDED]:
        return EXIT_ITEM_ERROR
    return EXIT_OK


def _settle(connection, run_id, version, outcome) -> None:
    """Give the run its final state, cancelling its pending items when asked."""

    with transaction(connection):
        if outcome == _CANCELLED:
            queue.cancel_pending(connection, run_id, version)
            queue.finish_run(connection, run_id, queue.RUN_CANCELLED)
        elif outcome == _STORAGE:
            queue.finish_run(connection, run_id, queue.RUN_FAILED)
        else:
            queue.finish_run(connection, run_id, queue.RUN_COMPLETE)


def _drain(database, run_id, version, workers, max_attempts, horizon):
    """Run `workers` threads until the queue is empty, cancelled or broken."""

    state = _State(run_id, version, max_attempts, horizon)
    threads = [threading.Thread(target=_work, args=(database, state),
                                name=f"tera-analysis-{number}", daemon=True)
               for number in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return state.outcome, state.error


class _State:
    """What the threads of one drain share: the horizon, the stop flag and the outcome."""

    def __init__(self, run_id, version, max_attempts, horizon):
        self.run_id = run_id
        self.version = version
        self.max_attempts = max_attempts
        self.horizon = horizon
        self.stop = threading.Event()
        self.outcome = _DRAINED
        self.error = None
        self.lock = threading.Lock()
        self.finalized_items = 0

    def finalized(self) -> None:
        """Count one finalized item and reclaim the drain's cycles when due.

        See `COLLECT_INTERVAL`: the memory one item's contract validation leaves
        behind is only returned by a generation-2 pass, so the drain completes
        one every `COLLECT_INTERVAL` items — shared by all worker threads, so
        four workers do not collect four times as often. The collection happens
        on the thread that finalized the item, after the item's own state is
        committed and printed, and never while a claim or a transaction is open.
        """

        with self.lock:
            self.finalized_items += 1
            due = self.finalized_items % COLLECT_INTERVAL == 0
        if due:
            gc.collect()

    def finish(self, outcome, error=None) -> None:
        """Record the first terminal outcome and stop every thread."""

        with self.lock:
            if self.outcome == _DRAINED:
                self.outcome, self.error = outcome, error
        self.stop.set()


class _Heartbeat(threading.Thread):
    """Refresh one run's heartbeat from its own connection while items are in flight.

    One extraction can outlive the lease, so the owner proves it is alive between
    item transitions. A connection of its own keeps the heartbeat out of every
    worker's transaction, and a lost lease stops the thread instead of resurrecting
    a run another process has taken over.
    """

    def __init__(self, database, run_id, owner_token):
        super().__init__(name="tera-heartbeat", daemon=True)
        self.database = database
        self.run_id = run_id
        self.owner_token = owner_token
        self._stop = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing loop, exercised through a run
        try:
            with closing(open_database(self.database)) as connection:
                while not self._stop.wait(queue.HEARTBEAT_SECONDS):
                    with _WRITE_LOCK:
                        if not queue.heartbeat(connection, self.run_id, self.owner_token):
                            return
        except (LibraryError, sqlite3.Error):
            return

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=queue.HEARTBEAT_SECONDS * 2 + 1)


def _work(database, state) -> None:
    """One worker thread: its own connection, one item at a time."""

    try:
        with closing(open_database(database)) as connection:
            repository = LibraryRepository(connection)
            while not state.stop.is_set():
                if queue.cancel_requested(connection, state.run_id):
                    state.finish(_CANCELLED)
                    return
                with _WRITE_LOCK:
                    item = queue.claim(connection, state.run_id, state.horizon)
                if item is None:
                    return
                terminal = _execute(connection, repository, state, item)
                if terminal is not None:
                    state.finish(*terminal)
                    return
    except (LibraryError, sqlite3.Error) as failure:
        state.finish(_STORAGE, failure)
    except Exception as failure:  # one broken thread fails the run, never truncates it
        state.finish(_STORAGE, failure)


# ---------------------------------------------------------------------------
# one item
# ---------------------------------------------------------------------------


def _execute(connection, repository, state, item):
    """Execute one claimed item; return `(outcome, error)` to stop the drain, or None.

    The item's stored path and role are evidence of what was queued: the samples
    row for its content identity says where the file is now and which role it
    carries, and everything read from disk comes from that row.
    """

    item_id, version = item["item_id"], state.version
    fingerprint = queue.fingerprint_of(item["sample_id"])
    if fingerprint is None:
        _finalize(connection, item_id, queue.ITEM_SUPERSEDED,
                  error=queue.error_record(queue.STAGE_QUEUE, queue.CODE_CONTENT_CHANGED,
                                           "The item does not carry a content fingerprint."))
        _progress(connection, state)
        return None
    row = _resolve(repository, item, fingerprint)
    if row is None:
        return _orphan(connection, state, item_id)
    if repository.has_current_analysis(fingerprint, version):
        _complete(connection, state, item_id, queue.DISPOSITION_REUSED)
        return None
    path = Path(row.path)
    try:
        data = batch.snapshot(path)
    except batch.SourceError as failure:
        _fail(connection, state, item, queue.STAGE_READ, failure.code, failure)
        return None
    except OSError as failure:
        _fail(connection, state, item, queue.STAGE_READ, _read_code(failure),
              _read_message(failure))
        return None
    if hashlib.sha256(data).hexdigest() != fingerprint:
        _finalize(connection, item_id, queue.ITEM_SUPERSEDED,
                  error=queue.error_record(
                      queue.STAGE_QUEUE, queue.CODE_CONTENT_CHANGED,
                      "The file's bytes no longer hash to the queued content identity; the next "
                      "scan repairs the row."))
        _progress(connection, state)
        return None
    if queue.cancel_requested(connection, state.run_id):
        return _cancel_item(connection, state, item_id)
    try:
        payload = batch.extract(data, path, row.role, fingerprint, version)
    except audio.AudioReadError as failure:
        _fail(connection, state, item, queue.STAGE_DECODE, failure.code, failure)
        return None
    except Exception as failure:  # one bad file fails one item, never the run
        _fail(connection, state, item, queue.STAGE_EXTRACT, queue.CODE_EXTRACTOR_FAILURE, failure)
        return None
    if queue.cancel_requested(connection, state.run_id):
        return _cancel_item(connection, state, item_id)
    try:
        sample = Sample.from_dict(payload)
    except (TypeError, ValueError) as failure:
        _fail(connection, state, item, queue.STAGE_EXTRACT, queue.CODE_EXTRACTOR_FAILURE,
              f"The extractor assembled an invalid Sample: {failure}")
        return None
    return _commit(connection, repository, state, item, sample, fingerprint)


def _commit(connection, repository, state, item, sample, fingerprint) -> None:
    """Publish one item's analysis and its `complete` state in one transaction.

    The samples row is checked again inside the transaction, because a concurrent
    delete must orphan the item rather than recreate a row or raise. A commit that
    finds the analysis already stored keeps it, records `reused` and overwrites
    nothing, and a storage failure rolls the features, the analysis version and
    the item's state back together.
    """

    item_id, version = item["item_id"], state.version
    try:
        with _WRITE_LOCK, transaction(connection):
            current = _resolve(repository, item, fingerprint)
            if current is None:
                queue.requeue(connection, item_id)
                raise _Orphaned
            if repository.has_current_analysis(fingerprint, version):
                queue.finalize(connection, item_id, queue.ITEM_COMPLETE,
                               queue.DISPOSITION_REUSED)
            else:
                descriptor = batch.analysis_descriptor()
                registered = descriptor if batch.digest(descriptor) == version else None
                repository.store_analysis(sample, content_sha256=fingerprint,
                                          sample_id=current.sample_id, descriptor=registered)
                queue.finalize(connection, item_id, queue.ITEM_COMPLETE,
                               queue.DISPOSITION_ANALYZED)
    except _Orphaned:
        return _orphan(connection, state, item_id)
    except (UnknownSample, InvalidContentIdentity):
        return _orphan(connection, state, item_id)
    except (LibraryError, sqlite3.Error) as failure:
        return _STORAGE, failure
    _progress(connection, state)
    return None


class _Orphaned(Exception):
    """Internal: the samples row vanished inside the commit transaction."""


def _resolve(repository, item, fingerprint):
    """The current path row for the item's content identity, or None.

    `find_path_records_by_content` is #22's row-level lookup, so an unanalysed row
    is found too. Version 1 stores one row per content hash, so the row whose own
    id is the item's identity wins and the smallest stored path is next; several
    rows per identity are #167.
    """

    rows = repository.find_path_records_by_content(fingerprint)
    for row in rows:
        if row.sample_id == item["sample_id"]:
            return row
    return rows[0] if rows else None


def _finalize(connection, item_id, state, disposition=None, error=None) -> None:
    """Set one item's terminal state in its own short, serialized transaction."""

    with _WRITE_LOCK, transaction(connection):
        queue.finalize(connection, item_id, state, disposition, error)


def _fail(connection, state, item, stage, code, message) -> None:
    """Record one failed attempt: retry while attempts remain, otherwise fail."""

    code = str(code)
    error = queue.error_record(queue.stage_for(code, stage), code, message)
    if queue.is_retryable(code) and item["attempts"] < state.max_attempts:
        with _WRITE_LOCK, transaction(connection):
            queue.requeue(connection, item["item_id"])
        return
    _finalize(connection, item["item_id"], queue.ITEM_FAILED, None, error)
    _progress(connection, state)


def _orphan(connection, state, item_id) -> None:
    """Finalize one item whose samples row is gone; nothing is written."""

    _finalize(connection, item_id, queue.ITEM_ORPHANED,
              error=queue.error_record(queue.STAGE_QUEUE, queue.CODE_SAMPLE_MISSING,
                                       "The samples row for this content identity is gone; the "
                                       "next scan re-indexes the file."))
    _progress(connection, state)
    return None


def _complete(connection, state, item_id, disposition) -> None:
    """Finalize one item whose analysis is already stored; nothing is extracted."""

    _finalize(connection, item_id, queue.ITEM_COMPLETE, disposition)
    _progress(connection, state)


def _cancel_item(connection, state, item_id):
    """Finalize one abandoned item as cancelled and tell the drain to stop."""

    _finalize(connection, item_id, queue.ITEM_CANCELLED)
    _progress(connection, state)
    return _CANCELLED, None


# ---------------------------------------------------------------------------
# progress, summary and small helpers
# ---------------------------------------------------------------------------


def _print(payload) -> None:
    """One canonical JSON line on stdout, never interleaved with another thread's."""

    with _PRINT_LOCK:
        print(batch.canonical(payload), flush=True)


def _progress(connection, state) -> None:
    """Print one line per item that reached a final state.

    The keys #9 uses for a manifest entry keep their meaning here: `completed`,
    `failed`, `remaining`, `analyzed` and `reused` count the run's items. The
    numbers come from `queue.progress`, so the line is the item's cost and not
    the remaining queue's: the summary's `failures` array is never built here.
    This call also ends the item, so it is where the drain reclaims its
    accumulated cycles.
    """

    payload = queue.progress(connection, state.run_id)
    counts = payload["counts"]
    _print({"state": payload["state"], "analysis_version": payload["analysis_version"],
            "completed": counts[queue.ITEM_COMPLETE], "failed": counts[queue.ITEM_FAILED],
            "remaining": counts["remaining"], "analyzed": counts[queue.DISPOSITION_ANALYZED],
            "reused": counts[queue.DISPOSITION_REUSED], "pending": counts[queue.ITEM_PENDING],
            "running": counts[queue.ITEM_RUNNING], "cancelled": counts[queue.ITEM_CANCELLED],
            "orphaned": counts[queue.ITEM_ORPHANED],
            "superseded": counts[queue.ITEM_SUPERSEDED]})
    state.finalized()


def _read_code(error) -> str:
    """#22's mapping from a filesystem failure to its stable code."""

    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, IsADirectoryError):
        return "not_file"
    if isinstance(error, PermissionError):
        return "access_denied"
    return "io_error"


def _read_message(error) -> str:
    """#22's human message for the same failures."""

    if isinstance(error, FileNotFoundError):
        return "The file disappeared between the scan and the read."
    if isinstance(error, IsADirectoryError):
        return "The stored path is no longer a regular file."
    if isinstance(error, PermissionError):
        return "Permission denied while reading the file; release the lock or fix the rights."
    return f"Filesystem error while reading the file: {error}"


def _database_path(database) -> Path:
    try:
        return batch.local_path(database)
    except (batch.BatchError, OSError, TypeError, ValueError) as error:
        raise queue.QueueError(f"database: {error}") from error


def _open_database(database):
    try:
        return open_database(database)
    except LibraryError as error:
        raise queue.QueueError(f"database: {error} ({error.code})") from error


def _summary_destination(path):
    if path is None:
        return None
    try:
        destination = batch.local_path(path)
    except (batch.BatchError, OSError, TypeError, ValueError) as error:
        raise queue.QueueError(f"summary: {error}") from error
    if destination.is_dir():
        raise queue.QueueError("summary: the summary path is a directory.")
    if not destination.parent.is_dir():
        raise queue.QueueError("summary: the summary parent is not an existing directory.")
    return destination


def _write_summary(destination, summary) -> None:
    """Atomically replace the summary file; #9's checkpoint pattern."""

    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                                 dir=destination.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(batch.canonical(summary) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    except OSError as error:
        raise queue.QueueError(f"summary: {error}") from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
