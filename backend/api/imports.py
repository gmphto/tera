"""The import operations of the local service (issue #27).

Four routes live here: start a folder import, read its status, cancel it and
retry its failures. All four share one rule: the request thread validates and
returns at once, and a service-owned runner thread does the work. `POST
/imports` opens the run through #23 before it scans a byte, so the client that
started an import already has its `run_id` while the folder is still being
enumerated.

The runner drives #22's scan and then #23's drain, unchanged:
`backend.library.scanner.reconcile` reconciles the folder through the runner's
own connection and `backend.library.worker.run_queue` drains the queue on its
own worker threads. Nothing here reimplements discovery, hashing, decoding,
extraction, claiming or retrying, and nothing here writes a library, feature or
queue row itself.

Every response is projected before it leaves this module: a stored path becomes
its file name, the human message #22/#23 attach to a failure is dropped (it may
quote a local path), and the scan summary is returned without its `root` and
`database` fields. No response carries the import root.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from pathlib import PurePosixPath, PureWindowsPath
import sqlite3
import sys
import threading

from backend.api import schemas
from backend.api.errors import ApiError
from backend.library import indexer, queue, scanner, worker
from backend.library.errors import LibraryError
from backend.library.repository import transaction
from backend.library.schema import open_database


#: The two phases a run this process drives goes through, before the terminal
#: state of the run replaces them. A run another process drives has no phase to
#: report and is reported with its #23 state.
PHASE_SCANNING = "scanning"
PHASE_ANALYZING = "analyzing"

#: What the runner drains with: #23's defaults, so an import behaves exactly
#: like the standalone worker command.
RUNNER_WORKERS = queue.DEFAULT_WORKERS
RUNNER_MAX_ATTEMPTS = queue.MAX_ATTEMPTS

#: How many runs' in-process facts stay available to the status route. A run
#: that falls out is reported from the database alone.
REGISTRY_LIMIT = 32


def status_path(run_id: str) -> str:
    """Where the run's status is read: the route path, never a filesystem path."""

    return f"/imports/{run_id}"


def _file_name(path):
    """The basename of a stored path, whichever separator the path uses.

    #21 stores the platform path and #22 stores POSIX relative paths, so a
    backslash means a Windows path here. This is the only projection that turns
    a location into something a client may see.
    """

    if path is None:
        return None
    if "\\" in path:
        return PureWindowsPath(path).name
    return PurePosixPath(path).name


def _live_details(app, run) -> dict:
    """The `import_already_running` details of the run that is live."""

    run_id = run["run_id"]
    return {"run_id": run_id, "state": run["state"],
            "phase": app.phase_of(run_id, run["state"]),
            "started_at": run["started_at"], "status_path": status_path(run_id)}


def _derived_role(failures):
    """The role of a run whose #23 row does not carry one, or None.

    A `job_runs` row has no role: a run drains every pending item at its
    analysis version, whatever role the scans that queued them stored. When the
    run's unfinished items agree on one role, that is the run's role; when they
    disagree the response says null rather than picking one of them.
    """

    roles = {record["role"] for record in failures if record.get("role")}
    return roles.pop() if len(roles) == 1 else None


class RunRecord:
    """What one in-process run adds to its #23 row."""

    def __init__(self, run_id: str, role, root_label):
        self.run_id = run_id
        self.role = role
        self.root_label = root_label
        self.phase = PHASE_SCANNING
        self.scan = None


class RunRegistry:
    """The runs this process drives, newest last, bounded by `REGISTRY_LIMIT`.

    A run that another process started, or one that has fallen out of this
    registry, is reported from the database alone: no scan summary, no role, and
    a phase that is its #23 state.
    """

    def __init__(self, limit: int = REGISTRY_LIMIT):
        self._limit = limit
        self._lock = threading.Lock()
        self._records = OrderedDict()

    def add(self, run_id: str, *, role, root_label) -> RunRecord:
        record = RunRecord(run_id, role, root_label)
        with self._lock:
            self._records[run_id] = record
            while len(self._records) > self._limit:
                self._records.popitem(last=False)
        return record

    def get(self, run_id):
        with self._lock:
            return self._records.get(run_id)

    def set_phase(self, run_id: str, phase: str) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is not None:
                record.phase = phase

    def set_scan(self, run_id: str, summary) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is not None:
                record.scan = summary


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------


def start(context) -> schemas.Response:
    """`POST /imports`: validate, open the run, hand the work to a runner thread.

    The root is validated with #22's root rules and the role against #9's
    `ROLES` before anything is written, so a refusal leaves no run row, no queue
    row and no scan behind. A run that is already live is refused with its own
    id and state, which is how a client that lost the id recovers it.
    """

    request = schemas.import_request(context.body)
    app = context.app
    try:
        root = scanner.validate_root(request.root)
    except scanner.ScanError:
        raise ApiError("invalid_root", details={"field": "root"}) from None
    live = app.database.run(queue.live_run)
    if live is not None:
        raise ApiError("import_already_running", details=_live_details(app, live))
    try:
        run_id, started_at = app.database.run(
            lambda connection: _open(connection, indexer.current_analysis_version()))
    except queue.QueueError:
        # The check above is a read; `open_run` decides again inside its
        # transaction, and a run that appeared in between wins.
        live = app.database.run(queue.live_run)
        if live is None:
            raise ApiError("import_already_running") from None
        raise ApiError("import_already_running", details=_live_details(app, live)) from None
    app.runs.add(run_id, role=request.role, root_label=root.name)
    app.spawn_runner(run_id, root=root, role=request.role)
    return schemas.Response(
        202,
        {"api_schema": app.api_schema,
         "import": {"run_id": run_id, "state": queue.RUN_RUNNING, "phase": PHASE_SCANNING,
                    "role": request.role,
                    "analysis_version": indexer.current_analysis_version(),
                    "started_at": started_at, "status_path": status_path(run_id)}},
        headers=(("Location", status_path(run_id)),))


def _open(connection, analysis_version):
    """Open one run and read back its start instant, on the database thread."""

    run_id = queue.open_run(connection, analysis_version, RUNNER_WORKERS, RUNNER_MAX_ATTEMPTS)
    return run_id, queue.status(connection, run_id)["started_at"]


def status(context) -> schemas.Response:
    """`GET /imports/{run_id}`: the persisted progress of one run.

    Everything countable comes from #23's rows, so the answer survives a
    restart; the scan summary, the role and the phase come from this process
    when it drove the run, and are null or the run's own state when it did not.
    Per-file failures never change the run's state: they are `counts.failed`
    and one `failures` record each, which keeps #23's outcome that one corrupt
    sample does not abort an import visible to the client.
    """

    run_id = schemas.require_run_id(context.params["run_id"])
    app = context.app
    try:
        run = app.database.run(lambda connection: _read_run(connection, run_id))
    except queue.QueueError:
        raise ApiError("unknown_import", details={"run_id": run_id}) from None
    record = app.runs.get(run_id)
    role = record.role if record is not None else None
    if role is None:
        role = _derived_role(run["failures"])
    return schemas.Response(200, {
        "api_schema": app.api_schema,
        "import": {
            "run_id": run_id,
            "state": run["state"],
            "phase": app.phase_of(run_id, run["state"]),
            "role": role,
            "analysis_version": run["analysis_version"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "cancel_requested": run["cancel_requested"],
            "root_label": (record.root_label if record is not None else None),
            "scan": (None if record is None or record.scan is None
                     else _project_scan(record.scan)),
            "counts": dict(run["counts"]),
            "current": _project_current(run["current"]),
            "failures": [_project_failure(record_) for record_ in run["failures"]],
        },
    })


def cancel(context) -> schemas.Response:
    """`POST /imports/{run_id}/cancel`: flag a live run and return at once.

    The flag is #23's own cancellation flag, so a run another process started is
    cancelled just as well; the run reaches `cancelled` at the next item
    boundary and everything already committed stays readable. A run that is not
    live writes nothing and is refused, which keeps a stale `running` row from
    being flagged by a request that cannot affect it.
    """

    run_id = schemas.require_run_id(context.params["run_id"])
    app = context.app
    try:
        state = app.database.run(lambda connection: _cancel(connection, run_id))
    except queue.QueueError:
        raise ApiError("unknown_import", details={"run_id": run_id}) from None
    if state is None:
        raise ApiError("import_not_live", details={"run_id": run_id})
    return schemas.Response(202, {
        "api_schema": app.api_schema,
        "import": {"run_id": run_id, "state": state, "cancel_requested": True,
                   "status_path": status_path(run_id)}})


def _cancel(connection, run_id):
    """Flag the run that is live when it is this one; None when it is not."""

    queue.get_run(connection, run_id)
    live = queue.live_run(connection)
    if live is None or live["run_id"] != run_id:
        return None
    queue.request_cancel(connection)
    return queue.status(connection, run_id)["state"]


def retry(context) -> schemas.Response:
    """`POST /imports/{run_id}/retry`: revive the failures and drain them again.

    #23's `retry_failed` resets this analysis version's `failed` and
    `orphaned` items to `pending`; a new run is opened for them and drained on
    a runner thread. Nothing failed is a normal result (`retried: 0`), not an
    error. A live run refuses the request before anything is reset.
    """

    run_id = schemas.require_run_id(context.params["run_id"])
    app = context.app
    try:
        retried, new_run, role = app.database.run(
            lambda connection: _retry(app, connection, run_id))
    except queue.QueueError:
        raise ApiError("unknown_import", details={"run_id": run_id}) from None
    previous = app.runs.get(run_id)
    app.runs.add(new_run,
                 role=(previous.role if previous is not None and previous.role else role),
                 root_label=(previous.root_label if previous is not None else None))
    app.spawn_runner(new_run, root=None, role=None)
    return schemas.Response(202, {
        "api_schema": app.api_schema,
        "import": {"run_id": new_run, "retried": retried,
                   "status_path": status_path(new_run)}})


def _retry(app, connection, run_id):
    """Reset the failures, open the new run and read the old run's role."""

    queue.get_run(connection, run_id)
    live = queue.live_run(connection)
    if live is not None:
        raise ApiError("import_already_running", details=_live_details(app, live))
    role = _derived_role(queue.summary(connection, run_id)["failures"])
    version = indexer.current_analysis_version()
    retried = queue.retry_failed(connection, version)
    return retried, queue.open_run(connection, version, RUNNER_WORKERS, RUNNER_MAX_ATTEMPTS), role


# ---------------------------------------------------------------------------
# the projections
# ---------------------------------------------------------------------------


def _read_run(connection, run_id) -> dict:
    """The stored half of one run's status, read on the database thread."""

    run = queue.get_run(connection, run_id)
    progress = queue.status(connection, run_id)
    summary = queue.summary(connection, run_id)
    return {"run_id": run_id, "state": run["state"],
            "analysis_version": run["analysis_version"], "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "cancel_requested": bool(run["cancel_requested"]),
            "counts": summary["counts"], "current": progress["current"],
            "failures": summary["failures"]}


def _project_scan(summary) -> dict:
    """#22's scan summary with every path reduced to its file name.

    `root` and `database` are dropped: they are the two fields that spell a
    location. `counts` is passed through verbatim. Each per-file record keeps
    its stable codes and loses the human message, which may quote a local path;
    the discovery records lose theirs the same way and keep the file name of the
    folder that could not be enumerated.
    """

    return {
        "scan_schema": summary["scan_schema"],
        "scan_policy_version": summary["scan_policy_version"],
        "role": summary["role"],
        "analysis_version": summary["analysis_version"],
        "state": summary["state"],
        "counts": dict(summary["counts"]),
        "files": [{"sample_id": item["sample_id"], "file_name": _file_name(item["path"]),
                   "code": item["code"], "analysis": item["analysis"],
                   "error_code": item["error_code"], "stage": item["stage"]}
                  for item in summary["files"]],
        "discovery_errors": [{"file_name": _file_name(item["path"]),
                              "stage": item["error"]["stage"],
                              "code": item["error"]["code"]}
                             for item in summary["discovery_errors"]],
    }


def _project_current(current):
    """#23's running item, with its stored path reduced to a file name."""

    if current is None:
        return None
    return {"sample_id": current["sample_id"], "file_name": _file_name(current["path"]),
            "attempts": current["attempts"]}


def _project_failure(record) -> dict:
    """One #23 failure record without its path and without its message."""

    return {"sample_id": record["sample_id"], "file_name": _file_name(record["path"]),
            "stage": record["stage"], "code": record["code"], "attempts": record["attempts"]}


# ---------------------------------------------------------------------------
# the runner thread
# ---------------------------------------------------------------------------


def runner(app, run_id: str, *, root=None, role=None) -> None:
    """Drive one open run: the #22 scan, then the #23 drain, on this thread.

    Never called by a request handler. The run row is already committed, so the
    client has its `run_id` before the first file is hashed; everything read
    from disk happens here and inside `worker.run_queue`'s own threads. A run
    opened by `retry` has no root and starts at the drain.
    """

    state = queue.RUN_FAILED
    try:
        with closing(open_database(app.database.path)) as connection:
            if root is not None:
                app.runs.set_scan(run_id, scanner.reconcile(root, role, connection,
                                                            app.database.path))
            app.runs.set_phase(run_id, PHASE_ANALYZING)
            worker.run_queue(connection, run_id, RUNNER_WORKERS, RUNNER_MAX_ATTEMPTS)
            state = queue.status(connection, run_id)["state"]
    except (LibraryError, sqlite3.Error, queue.QueueError) as error:
        state = _abandon(app, run_id, error)
    except Exception as error:  # a defect must fail the run, not leave it live
        state = _abandon(app, run_id, error)
    finally:
        app.runs.set_phase(run_id, state)
        app.runner_finished(run_id)


def _abandon(app, run_id: str, error) -> str:
    """Give a run that stopped early its terminal state and report the type only.

    One line on stderr names the exception's type and nothing else: unlike
    stdout, stderr is not the access-log channel, and a storage message can
    quote the database path, which the API must never return.
    """

    print(f"tera-local-service: an import stopped early ({type(error).__name__}).",
          file=sys.stderr, flush=True)
    state = queue.RUN_FAILED
    try:
        with closing(open_database(app.database.path)) as connection:
            state = queue.status(connection, run_id)["state"]
            if state == queue.RUN_RUNNING:
                with transaction(connection):
                    queue.finish_run(connection, run_id, queue.RUN_FAILED)
                state = queue.RUN_FAILED
    except (LibraryError, sqlite3.Error, queue.QueueError):
        pass
    return state
