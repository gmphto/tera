"""The import routes: start, status, cancel, retry, restart (issue #27).

The integration check at the top drives a whole import through the API alone:
a temporary library of synthetic WAV files, one role per folder and one
three-channel file, and no assertion that reads the database directly except
the tree snapshot that proves the import never touched the library.

The remaining tests cover the requests that race: one import at a time, the
request thread returning before an extraction finishes, cancellation, retry,
the recovery of a run that was in flight when the service died, the shutdown
and port contract, and the exit-2 configuration codes.

Every root, file name, id and fingerprint here is synthetic.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from backend.analysis import batch
from backend.analysis.batch import ROLES
from backend.api import service
from backend.contracts import MEASURES
from backend.library import indexer, queue
from tests.api_client import (
    Client,
    build_library,
    connection,
    scan,
    start_service,
    three_channel,
    tone,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: How long the tests wait for a run to reach a terminal state.
IMPORT_TIMEOUT = 180.0


def _tree_state(root):
    """Every file under one root, its size and its content hash."""

    state = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            state[str(path.relative_to(root))] = (len(data), hashlib.sha256(data).hexdigest())
    return state


def _wait_for(client, run_id, *, phase=None, states=("complete", "cancelled", "interrupted",
                                                     "failed"), timeout=IMPORT_TIMEOUT):
    """Poll one run until it is terminal, or until it reaches a named phase."""

    deadline = time.monotonic() + timeout
    document = None
    while time.monotonic() < deadline:
        document = client.get(f"/imports/{run_id}").body["import"]
        if phase is not None and document["phase"] == phase:
            return document
        if phase is None and document["state"] in states:
            return document
        time.sleep(0.01)
    raise AssertionError(f"the run did not reach {phase or states}: {document}")


class BlockingExtract:
    """An extractor held on an event, with what it saw while it was held."""

    def __init__(self, real):
        self.real = real
        self.started = threading.Event()
        self.release = threading.Event()
        self.fingerprints = []
        self.idents = []
        self.calls = 0

    def __call__(self, data, path, role, fingerprint, version):
        self.calls += 1
        self.idents.append(threading.get_ident())
        self.fingerprints.append(fingerprint)
        self.started.set()
        self.release.wait(60)
        return self.real(data, path, role, fingerprint, version)


def _blocking_extract(monkeypatch):
    """Hold every extraction on an event, so a phase can be observed."""

    blocked = BlockingExtract(batch.extract)
    monkeypatch.setattr(batch, "extract", blocked)
    return blocked


def _import_folder(client, folder, role):
    """Start one import and wait for it; return its status document."""

    started = client.post("/imports", {"root": str(folder), "role": role})
    assert started.status == 202, started.raw
    return _wait_for(client, started.body["import"]["run_id"])


# ---------------------------------------------------------------------------
# the integration check
# ---------------------------------------------------------------------------


def test_the_integration_check_against_a_temporary_library(tmp_path, monkeypatch):
    # The library is one subdirectory, so the tree snapshot below covers the
    # library alone and not the database the service creates beside it.
    root = build_library(tmp_path / "library", {
        "kicks": {"kick-55.wav": tone(55, frames=24000)},
        "bass": {"bass-110.wav": tone(110, frames=24000)},
        "sub-bass": {"sub-41.wav": tone(41, frames=24000), "wide.wav": three_channel()},
    })
    database = tmp_path / "library.sqlite3"
    before = _tree_state(root)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    try:
        # 1. The kicks folder: 202 with phase scanning, then a poll that catches
        #    the analyzing phase while the one extraction is held.
        reply = running.client.post("/imports", {"root": str(root / "kicks"), "role": "kick"})
        assert reply.status == 202
        assert reply.body["api_schema"] == "1.0"
        import_body = reply.body["import"]
        assert import_body["state"] == "running"
        assert import_body["phase"] == "scanning"
        assert import_body["role"] == "kick"
        assert import_body["status_path"] == f"/imports/{import_body['run_id']}"
        assert reply.headers["location"] == f"/imports/{import_body['run_id']}"
        run_id = import_body["run_id"]
        assert blocked.started.wait(IMPORT_TIMEOUT), "the runner never reached the extractor"
        analyzing = _wait_for(running.client, run_id, phase="analyzing")
        assert analyzing["counts"]["remaining"] >= 1
        assert analyzing["root_label"] == "kicks"
        assert analyzing["scan"]["counts"]["added"] == 1
        blocked.release.set()
        done = _wait_for(running.client, run_id)
        assert done["state"] == "complete"
        assert done["counts"]["complete"] == 1
        assert done["counts"]["failed"] == 0
        assert done["failures"] == []
        assert done["finished_at"] is not None
        assert done["scan"]["state"] == "complete"
        blocked.release.clear()
        blocked.started.clear()

        # 2. The other two roles, one folder each.
        assert _import_folder(running.client, root / "bass", "bass")["state"] == "complete"
        sub = _import_folder(running.client, root / "sub-bass", "sub-bass")
        assert sub["state"] == "complete"
        # The landed classification: #22 refuses a header wider than two
        # channels while it scans and queues nothing for it, so the run's #23
        # counts never see the file and the refusal is reported by the scan
        # projection instead. See the conflict note in _docs/local-service.md.
        assert sub["counts"]["complete"] == 1
        assert sub["counts"]["failed"] == 0
        assert sub["failures"] == []
        assert sub["scan"]["counts"]["unsupported"] == 1
        assert sub["scan"]["counts"]["added"] == 1
        refused = [record for record in sub["scan"]["files"] if record["file_name"] == "wide.wav"]
        assert refused == [{"sample_id": None, "file_name": "wide.wav", "code": "unsupported",
                            "analysis": "none", "error_code": "unsupported_channels",
                            "stage": "read"}]

        # 3. The library: exactly the imported samples, with their roles, and
        #    complete paging.
        assert _import_folder(running.client, root / "kicks", "kick")["state"] == "complete"
        every = running.client.get("/library/samples?limit=2")
        assert every.body["page"]["count"] == 2
        assert every.body["page"]["has_more"] is True
        seen = list(every.body["items"])
        cursor = every.body["page"]["next_cursor"]
        while cursor is not None:
            page = running.client.get("/library/samples?limit=2&cursor=" + cursor)
            seen.extend(page.body["items"])
            cursor = page.body["page"]["next_cursor"] if page.body["page"]["has_more"] else None
        assert len(seen) == 3
        assert len({item["sample_id"] for item in seen}) == 3
        assert sorted(item["role"] for item in seen) == ["bass", "kick", "sub-bass"]
        assert all(item["file_status"] == "present" for item in seen)
        assert all(item["analysis"]["state"] == "current" for item in seen)
        for role in ROLES:
            by_role = running.client.get(f"/library/samples?role={role}").body
            assert by_role["page"]["count"] == 1
            assert {item["role"] for item in by_role["items"]} == {role}

        # 4. One sample's stored measurements: a 55 Hz fundamental inside the
        #    extractor tolerance, every MEASURES name exactly once.
        kick = [item for item in seen if item["role"] == "kick"][0]
        detail = running.client.get(f"/library/samples/{kick['sample_id']}").body["sample"]
        assert detail["file_name"] == "kick-55.wav"
        names = [item["name"] for item in detail["features"]["measurements"]]
        assert names == list(MEASURES)
        by_name = {item["name"]: item for item in detail["features"]["measurements"]}
        assert abs(by_name["fundamental"]["value"] - 55.0) <= 0.55
        assert by_name["fundamental"]["confidence"] is not None
        assert detail["audio"]["duration_ms"] == 500.0
    finally:
        blocked.release.set()
        running.stop()
    # 5. The library tree is byte-unchanged and holds no new file.
    assert _tree_state(root) == before


# ---------------------------------------------------------------------------
# the root rule
# ---------------------------------------------------------------------------


def test_a_relative_root_is_refused_before_it_is_resolved(tmp_path, monkeypatch):
    """A root is the folder itself, never the service's working directory.

    #22's `validate_root` canonicalises its argument with `os.path.abspath`,
    which resolves against the process working directory. The route owns an
    absolute root, so a relative value is 400 `invalid_root` before #22 sees
    it: `.` would otherwise import whatever folder the service was started
    in. The values below all resolve to real folders here, so the refusal is
    the rule and not a missing directory.
    """

    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55)}})
    monkeypatch.chdir(root)
    running = start_service(str(tmp_path / "library.sqlite3"))
    try:
        for value in (".", "kicks", "./kicks"):
            reply = running.client.post("/imports", {"root": value, "role": "kick"})
            assert (reply.status, reply.code()) == (400, "invalid_root")
            assert reply.body["error"]["details"] == {"field": "root"}
        # No run was opened for any of them.
        assert running.client.get("/health").body["import"]["state"] == "idle"
        # The same folder by its absolute path is still accepted.
        absolute = running.client.post("/imports",
                                       {"root": str(root / "kicks"), "role": "kick"})
        assert absolute.status == 202
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# one import at a time
# ---------------------------------------------------------------------------


def _kick_library(tmp_path, name="library"):
    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55, frames=24000)}})
    return root / "kicks", tmp_path / (name + ".sqlite3")


def test_a_live_run_refuses_a_second_import_and_keeps_the_database_untouched(tmp_path,
                                                                           monkeypatch):
    folder, database = _kick_library(tmp_path)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    try:
        first = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        assert first.status == 202
        run_id = first.body["import"]["run_id"]
        assert blocked.started.wait(IMPORT_TIMEOUT)
        second = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        assert (second.status, second.code()) == (409, "import_already_running")
        details = second.body["error"]["details"]
        assert details == {"run_id": run_id, "state": "running", "phase": "analyzing",
                           "started_at": first.body["import"]["started_at"],
                           "status_path": f"/imports/{run_id}"}
        # No scan, no run row and no queue write happened for the refused request.
        with closing(connection(database)) as opened:
            assert opened.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
            assert opened.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 1
            assert opened.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
        blocked.release.set()
        assert _wait_for(running.client, run_id)["state"] == "complete"
        # A terminal run does not block the next import, and re-importing the
        # same folder re-reconciles it without a second row.
        again = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        assert again.status == 202
        assert _wait_for(running.client, again.body["import"]["run_id"])["state"] == "complete"
        with closing(connection(database)) as opened:
            assert opened.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 2
            assert opened.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
            assert opened.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 1
    finally:
        blocked.release.set()
        running.stop()


def test_ten_concurrent_imports_produce_exactly_one_run(tmp_path, monkeypatch):
    folder, database = _kick_library(tmp_path)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    answers = [None] * 10
    barrier = threading.Barrier(10)

    def one(index):
        barrier.wait(30)
        answers[index] = running.client.post("/imports", {"root": str(folder), "role": "kick"})

    threads = [threading.Thread(target=one, args=(index,), daemon=True) for index in range(10)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        accepted = [answer for answer in answers if answer.status == 202]
        refused = [answer for answer in answers if answer.status == 409]
        assert len(accepted) == 1, [answer.status for answer in answers]
        assert len(refused) == 9
        assert all(answer.status < 500 for answer in answers)
        assert all(answer.code() == "import_already_running" for answer in refused)
        run_id = accepted[0].body["import"]["run_id"]
        with closing(connection(database)) as opened:
            assert opened.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
        blocked.release.set()
        assert _wait_for(running.client, run_id)["state"] == "complete"
        with closing(connection(database)) as opened:
            assert opened.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
            assert opened.execute("SELECT COUNT(*) FROM job_items").fetchone()[0] == 1
    finally:
        blocked.release.set()
        running.stop()


def test_the_request_thread_returns_before_extraction_finishes(tmp_path, monkeypatch):
    folder, database = _kick_library(tmp_path)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    handler_idents = []
    recorded = running.app.log_request

    def recording(method, route, status, code, began):
        handler_idents.append(threading.get_ident())
        return recorded(method, route, status, code, began)

    monkeypatch.setattr(running.app, "log_request", recording)
    try:
        began = time.monotonic()
        reply = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        elapsed = time.monotonic() - began
        assert reply.status == 202
        assert elapsed < 1.0, elapsed
        assert blocked.started.wait(IMPORT_TIMEOUT)
        assert blocked.calls >= 1
        # The extraction runs somewhere else than the request handler did.
        assert handler_idents
        assert set(blocked.idents).isdisjoint(set(handler_idents))
        assert blocked.idents[0] != threading.get_ident()
        blocked.release.set()
        assert _wait_for(running.client, reply.body["import"]["run_id"])["state"] == "complete"
    finally:
        blocked.release.set()
        running.stop()


# ---------------------------------------------------------------------------
# cancel and retry
# ---------------------------------------------------------------------------


def test_cancel_flags_a_live_run_and_keeps_committed_items_readable(tmp_path, monkeypatch):
    folder, database = _kick_library(tmp_path)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    try:
        started = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        run_id = started.body["import"]["run_id"]
        assert blocked.started.wait(IMPORT_TIMEOUT)
        cancelled = running.client.post(f"/imports/{run_id}/cancel")
        assert cancelled.status == 202
        assert cancelled.body["import"] == {"run_id": run_id, "state": "running",
                                            "cancel_requested": True,
                                            "status_path": f"/imports/{run_id}"}
        blocked.release.set()
        document = _wait_for(running.client, run_id)
        assert document["state"] == "cancelled"
        assert document["cancel_requested"] is True
        # The item that was in flight is cancelled, and the library still reads.
        assert document["counts"]["cancelled"] == 1
        assert running.client.get("/library/samples").body["items"][0]["analysis"]["state"] in (
            "pending", "failed", "absent", "stale")
        assert running.client.get("/health").status == 200
        # A run that is not live writes nothing and is refused.
        assert running.client.post(f"/imports/{run_id}/cancel").status == 409
        assert running.client.post(f"/imports/{run_id}/cancel").code() == "import_not_live"
        assert running.client.post("/imports/run-" + "0" * 32 + "/cancel").code() == "unknown_import"
        assert running.client.post("/imports/not%20a%20run/cancel").code() == "invalid_run_id"
    finally:
        blocked.release.set()
        running.stop()


def test_a_completed_run_stays_readable_and_a_second_run_reuses_it(tmp_path):
    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55, frames=24000),
                                              "kick-02.wav": tone(110, frames=24000)}})
    database = tmp_path / "library.sqlite3"
    running = start_service(str(database))
    try:
        finished = _import_folder(running.client, root / "kicks", "kick")
        assert finished["counts"]["complete"] == 2
        sample_ids = [item["sample_id"] for item in
                      running.client.get("/library/samples").body["items"]]
        assert len(sample_ids) == 2
        # A second import of the same folder has no pending work and completes.
        second = _import_folder(running.client, root / "kicks", "kick")
        assert second["counts"]["complete"] == 0
        assert second["counts"]["reused"] == 0
        for sample_id in sample_ids:
            detail = running.client.get(f"/library/samples/{sample_id}").body["sample"]
            assert detail["analysis"]["state"] == "current"
            assert detail["features"] is not None
    finally:
        running.stop()


def test_cancel_reaches_a_run_another_process_started(tmp_path):
    folder, database = _kick_library(tmp_path)
    scan(folder, database, "kick")
    with closing(connection(database)) as opened:
        run_id = queue.open_run(opened, indexer.current_analysis_version())
        # One queued item, so the run's unfinished work still names its role.
        queue.enqueue(opened, indexer.current_analysis_version())
    running = start_service(str(database))
    try:
        cancelled = running.client.post(f"/imports/{run_id}/cancel")
        assert cancelled.status == 202
        assert cancelled.body["import"]["cancel_requested"] is True
        with closing(connection(database)) as opened:
            assert queue.get_run(opened, run_id)["cancel_requested"] == 1
        # The run this process does not drive reports its own state as its phase.
        document = running.client.get(f"/imports/{run_id}").body["import"]
        assert document["state"] == "running"
        assert document["phase"] == "running"
        assert document["scan"] is None
        assert document["role"] == "kick"
    finally:
        running.stop()


def test_retry_resets_the_failures_and_drains_them_again(tmp_path, monkeypatch):
    folder, database = _kick_library(tmp_path)
    failed_once = {"done": False}
    real = batch.extract

    def failing(data, path, role, fingerprint, version):
        if not failed_once["done"]:
            failed_once["done"] = True
            raise RuntimeError("synthetic extractor failure")
        return real(data, path, role, fingerprint, version)

    monkeypatch.setattr(batch, "extract", failing)
    running = start_service(str(database))
    try:
        first = _import_folder(running.client, folder, "kick")
        assert first["state"] == "complete"
        assert first["counts"]["failed"] == 1
        assert first["counts"]["complete"] == 0
        assert first["failures"][0]["code"] == "extractor_failure"
        assert first["failures"][0]["stage"] == "extract"
        assert first["failures"][0]["file_name"] == "kick-01.wav"
        assert first["failures"][0]["sample_id"].startswith("sha256:")
        assert "message" not in first["failures"][0]
        retried = running.client.post(f"/imports/{first['run_id']}/retry")
        assert retried.status == 202
        assert retried.body["import"]["retried"] == 1
        new_run = retried.body["import"]["run_id"]
        assert new_run != first["run_id"]
        assert retried.body["import"]["status_path"] == f"/imports/{new_run}"
        second = _wait_for(running.client, new_run)
        assert second["state"] == "complete"
        assert second["counts"]["complete"] == 1
        assert second["scan"] is None
        # A retry run inherits the folder of the run it retried, so a client can
        # still name the import it is finishing; only the scan summary is absent.
        assert second["root_label"] == "kicks"
        # Nothing failed any more, so retrying again resets nothing: that is a
        # normal 202, not an error.
        nothing = running.client.post(f"/imports/{new_run}/retry")
        assert nothing.status == 202
        assert nothing.body["import"]["retried"] == 0
        assert _wait_for(running.client, nothing.body["import"]["run_id"])["state"] == "complete"
        assert running.client.post("/imports/run-" + "0" * 32 + "/retry").code() == "unknown_import"
    finally:
        running.stop()


def test_retry_is_refused_while_a_run_is_live(tmp_path, monkeypatch):
    folder, database = _kick_library(tmp_path)
    blocked = _blocking_extract(monkeypatch)
    running = start_service(str(database))
    try:
        started = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        run_id = started.body["import"]["run_id"]
        assert blocked.started.wait(IMPORT_TIMEOUT)
        refused = running.client.post(f"/imports/{run_id}/retry")
        assert (refused.status, refused.code()) == (409, "import_already_running")
        assert refused.body["error"]["details"]["run_id"] == run_id
        blocked.release.set()
        assert _wait_for(running.client, run_id)["state"] == "complete"
    finally:
        blocked.release.set()
        running.stop()


# ---------------------------------------------------------------------------
# restart and recovery
# ---------------------------------------------------------------------------


class CountingExtract:
    """An extractor that records what it was asked to extract."""

    def __init__(self, real):
        self.real = real
        self.fingerprints = []

    def __call__(self, data, path, role, fingerprint, version):
        self.fingerprints.append(fingerprint)
        return self.real(data, path, role, fingerprint, version)


def test_a_run_that_was_in_flight_is_reported_and_recovered(tmp_path, monkeypatch):
    root = build_library(tmp_path, {"kicks": {"kick-01.wav": tone(55, frames=24000)}})
    folder = root / "kicks"
    database = tmp_path / "library.sqlite3"
    # 1. A first import that finished: one item is complete and its features are
    #    stored and stay readable through everything that follows.
    running = start_service(str(database))
    try:
        first = _import_folder(running.client, folder, "kick")
        assert first["counts"]["complete"] == 1
    finally:
        running.stop()
    with closing(connection(database)) as opened:
        analysed = opened.execute("SELECT content_sha256 FROM samples WHERE filename = ?",
                                  ("kick-01.wav",)).fetchone()["content_sha256"]
    # 2. A second file, and a run that was opened and then died before finishing.
    (folder / "kick-02.wav").write_bytes(tone(110, frames=24000))
    scan(folder, database, "kick")
    with closing(connection(database)) as opened:
        version = indexer.current_analysis_version()
        crashed = queue.open_run(opened, version)
        queue.enqueue(opened, version)
        opened.execute("UPDATE job_runs SET heartbeat_at = ? WHERE run_id = ?",
                       ("2000-01-01T00:00:00Z", crashed))
        states = {row["state"]: row["n"] for row in opened.execute(
            "SELECT state, COUNT(*) AS n FROM job_items GROUP BY state")}
    assert states == {"complete": 1, "pending": 1}
    # 3. The restarted service reports the persisted state and invents no scan.
    counted = CountingExtract(batch.extract)
    monkeypatch.setattr(batch, "extract", counted)
    monkeypatch.setattr(queue, "LEASE_SECONDS", 1)
    running = start_service(str(database))
    try:
        health = running.client.get("/health").body
        assert health["import"] == {"run_id": None, "state": "idle", "phase": None,
                                    "started_at": None}
        assert health["library"]["samples"] == 2
        document = running.client.get(f"/imports/{crashed}").body["import"]
        assert document["state"] == "running"
        assert document["phase"] == "running"
        assert document["scan"] is None
        assert document["root_label"] is None
        # #23 counts a run's own items plus the pending work it can claim, so the
        # interrupted run reports the one pending item and no completion.
        assert document["counts"]["pending"] == 1
        assert document["counts"]["complete"] == 0
        # 4. The next import reconciles the stale run and finishes the work
        #    without re-extracting what is already complete.
        started = running.client.post("/imports", {"root": str(folder), "role": "kick"})
        assert started.status == 202
        done = _wait_for(running.client, started.body["import"]["run_id"])
        assert done["state"] == "complete"
        assert done["counts"]["complete"] == 1
        assert counted.fingerprints and analysed not in counted.fingerprints
        with closing(connection(database)) as opened:
            assert queue.get_run(opened, crashed)["state"] == "interrupted"
        samples = running.client.get("/library/samples").body["items"]
        assert len(samples) == 2
        assert all(item["analysis"]["state"] == "current" for item in samples)
    finally:
        running.stop()


# ---------------------------------------------------------------------------
# the process contract
# ---------------------------------------------------------------------------


def _child_environment():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT)] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH")
                                  else []))
    return environment


def _start_child(tmp_path, name, *arguments):
    """One service process with its stdout and stderr on files, never pipes."""

    out = tmp_path / (name + "-out.txt")
    err = tmp_path / (name + "-err.txt")
    stdout_file = open(out, "wb")
    stderr_file = open(err, "wb")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.api.service"] + list(arguments),
        cwd=str(REPOSITORY_ROOT), env=_child_environment(), stdout=stdout_file,
        stderr=stderr_file, creationflags=creationflags)
    return process, out, err


def _wait_for_port_file(port_file, process, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_file.exists():
            try:
                return json.loads(port_file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        if process.poll() is not None:
            break
        time.sleep(0.02)
    raise AssertionError(f"the service did not report a port (exit {process.poll()})")


def _stop_child(process):
    """One stop signal, if this platform can deliver one to the child."""

    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


def test_the_listening_line_the_port_file_and_a_clean_shutdown(tmp_path):
    folder, database = _kick_library(tmp_path)
    port_file = tmp_path / "service-port.json"
    process, out, err = _start_child(tmp_path, "service", "--database", str(database),
                                     "--port", "0", "--port-file", str(port_file))
    try:
        listening = _wait_for_port_file(port_file, process)
        assert set(listening) == {"event", "host", "port", "pid", "api_schema"}
        assert listening["event"] == "listening"
        assert listening["host"] == "127.0.0.1"
        # The venv's python.exe redirects to the base interpreter on this
        # machine, so the child's own pid is only checked to belong to some other
        # live process.
        assert isinstance(listening["pid"], int) and listening["pid"] > 0
        assert listening["pid"] != os.getpid()
        assert listening["api_schema"] == "1.0"
        assert 1024 <= listening["port"] <= 65535
        lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert json.loads(lines[0]) == listening
        # The child really serves: the whole contract over a real socket.
        client = Client(listening["port"])
        assert client.get("/health").status == 200
        started = client.post("/imports", {"root": str(folder), "role": "kick"})
        assert started.status == 202
        _wait_for(client, started.body["import"]["run_id"])
        assert client.get("/library/samples").body["page"]["count"] == 1
        _stop_child(process)
        assert process.wait(timeout=service.SHUTDOWN_GRACE_SECONDS + 2) == 0
        assert not port_file.exists()
        # A fresh server binds the same port immediately.
        server = service.create_server(str(database), port=listening["port"])
        try:
            assert server.server_address[1] == listening["port"]
        finally:
            server.server_close()
            server.app.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def test_a_second_stop_signal_leaves_at_once(tmp_path):
    folder, database = _kick_library(tmp_path)
    port_file = tmp_path / "service-port.json"
    process, out, err = _start_child(tmp_path, "signals", "--database", str(database),
                                     "--port", "0", "--port-file", str(port_file))
    stalled = None
    try:
        listening = _wait_for_port_file(port_file, process)
        # A request whose body never arrives holds a slot, so the first signal
        # enters the grace period and the second one arrives during it.
        stalled = socket.create_connection(("127.0.0.1", listening["port"]), timeout=10)
        stalled.sendall(("POST /imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                         "Content-Type: application/json\r\nContent-Length: 4096\r\n\r\n"
                         % listening["port"]).encode("ascii"))
        time.sleep(0.5)
        _stop_child(process)
        time.sleep(0.8)
        _stop_child(process)
        assert process.wait(timeout=service.SHUTDOWN_GRACE_SECONDS + 5) == service.EXIT_STOPPED
    finally:
        if stalled is not None:
            stalled.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def test_a_port_already_in_use_is_reported_and_the_database_stays_untouched(tmp_path, capfd):
    folder, database = _kick_library(tmp_path)
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    try:
        began = time.monotonic()
        assert service.main(["--database", str(database), "--port", str(port)]) == 2
        elapsed = time.monotonic() - began
    finally:
        occupied.close()
    assert elapsed < 5.0
    document = json.loads(capfd.readouterr().err.strip())
    assert document == {"event": "bind_failed", "code": "port_in_use", "host": "127.0.0.1",
                        "port": port}
    assert not database.exists()


def test_the_configuration_codes_are_reported_on_stderr(tmp_path, capfd):
    database = tmp_path / "library.sqlite3"
    cases = (
        (["--port", "abc"], "invalid_port"),
        (["--port", "80"], "port_out_of_range"),
        (["--port", "70000"], "port_out_of_range"),
        (["--port", "-1"], "port_out_of_range"),
        (["--host", "0.0.0.0"], "invalid_bind_host"),
        (["--host", "localhost"], "invalid_bind_host"),
        (["--dev-origin", "http://example.invalid:1"], "invalid_dev_origin"),
        (["--dev-origin", "http://localhost"], "invalid_dev_origin"),
        (["--dev-origin", "https://localhost:3000"], "invalid_dev_origin"),
        (["--port-file", str(tmp_path / "absent" / "port.json")], "invalid_port_file"),
        (["--database", str(tmp_path / "absent" / "library.sqlite3")], "database_not_found"),
    )
    for arguments, code in cases:
        assert service.main(["--database", str(database)] + arguments) == 2
        document = json.loads(capfd.readouterr().err.strip())
        assert set(document) == {"event", "code", "host", "port"}
        assert document["event"] == "configuration_error"
        assert document["code"] == code
    directory = tmp_path / "a-directory"
    directory.mkdir()
    assert service.main(["--database", str(directory)]) == 2
    assert json.loads(capfd.readouterr().err.strip())["code"] == "database_not_found"
    assert not database.exists()


def test_the_port_precedence_is_the_flag_then_the_environment_then_the_default(tmp_path):
    parser = service._parser()
    database = str(tmp_path / "library.sqlite3")
    explicit = parser.parse_args(["--database", database, "--port", "9000"])
    assert service._configuration(explicit, {service.PORT_ENV: "9100"}).port == 9000
    environment = parser.parse_args(["--database", database])
    assert service._configuration(environment, {service.PORT_ENV: "9100"}).port == 9100
    assert service._configuration(environment, {}).port == service.DEFAULT_PORT
    ephemeral = parser.parse_args(["--database", database, "--port", "0"])
    assert service._configuration(ephemeral, {}).port == 0
    absent = parser.parse_args(["--database", database])
    assert service._configuration(absent, {service.PORT_ENV: ""}).port == service.DEFAULT_PORT


def test_a_corrupt_or_unsupported_database_never_resets(tmp_path, capfd):
    folder, database = _kick_library(tmp_path)
    database.write_bytes(b"this file is not a SQLite database, and never was.")
    before = database.read_bytes()
    running = start_service(str(database))
    try:
        health = running.client.get("/health")
        assert health.status == 200
        assert health.body["state"] == "degraded"
        assert health.body["database"]["code"] == "not_a_database"
        assert health.body["database"]["schema_version"] is None
        assert health.body["library"] == {"samples": None, "by_role": None, "roots": None,
                                         "pending_analysis": None}
        assert health.body["import"] == {"run_id": None, "state": None, "phase": None,
                                        "started_at": None}
        assert running.client.get("/library/samples").code() == "database_unavailable"
        assert running.client.post("/imports", {"root": str(folder), "role": "kick"}).code() == (
            "database_unavailable")
    finally:
        running.stop()
    assert database.read_bytes() == before


def test_an_aborted_request_never_writes_a_traceback_or_a_path(tmp_path):
    """One path-free line replaces the standard library's traceback.

    A client that sends a request line, its headers and part of its declared
    body and then resets the connection makes `ThreadingHTTPServer` report the
    lost socket through `handle_error`, which by default prints the whole
    traceback with absolute interpreter paths. The service answers with one
    JSON line naming the exception type, and keeps serving.
    """

    _folder, database = _kick_library(tmp_path)
    port_file = tmp_path / "service-port.json"
    process, _out, err = _start_child(tmp_path, "aborted", "--database", str(database),
                                     "--port", "0", "--port-file", str(port_file))
    try:
        listening = _wait_for_port_file(port_file, process)
        aborted = socket.create_connection(("127.0.0.1", listening["port"]), timeout=10)
        aborted.sendall(("POST /imports HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                         "Content-Type: application/json\r\n"
                         "Content-Length: 4096\r\n\r\n" % listening["port"]).encode("ascii")
                        + b'{"root"')
        # A reset, not an orderly close: the handler is left mid-body.
        aborted.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                           struct.pack("ii", 1, 0))
        aborted.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if err.exists() and err.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.05)
        assert Client(listening["port"]).get("/health").status == 200
        _stop_child(process)
        assert process.wait(timeout=service.SHUTDOWN_GRACE_SECONDS + 5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    text = err.read_text(encoding="utf-8")
    assert "Traceback" not in text
    assert "Exception occurred" not in text
    assert str(tmp_path) not in text
    assert os.path.splitdrive(str(tmp_path))[0] not in text
    assert "\\" not in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, "the aborted request wrote nothing to stderr"
    for line in lines:
        # A traceback frame is not JSON, so parsing every line is the proof.
        document = json.loads(line)
        assert set(document) == {"event", "error"}
        assert document["event"].endswith("_aborted")
        assert document["error"].isidentifier()


def test_a_descriptor_with_no_reader_left_is_retired_instead_of_raised_on(monkeypatch):
    """The desktop host stops reading the service's pipes once it is healthy.

    Both of the service's streams are pipes to that host, and the first line
    written after it stops reading fails with `OSError`. Measured on this
    machine against a real import: the failure surfaced as `connection_aborted`
    on stderr, which named the client for a failure the client did not cause,
    and the same refusal out of the worker's per-item progress write ended the
    run after its first completed file. Logging is an observation channel, so a
    refused write retires its own descriptor and never reaches a caller.
    """

    real_write = os.write
    attempted = []

    def refuse(descriptor, data):
        if descriptor in (1, 2):
            attempted.append(descriptor)
            raise OSError(22, "Invalid argument")
        return real_write(descriptor, data)

    monkeypatch.setattr(service, "_PRINT_BROKEN", {1: False, 2: False})
    monkeypatch.setattr(os, "write", refuse)
    service._print_line({"event": "request"})
    service._print_line({"event": "request"})
    assert attempted == [1], "a retired descriptor is not written to again"
    service._print_line({"event": "internal_error"}, stream=sys.stderr)
    assert attempted == [1, 2], "each descriptor retires on its own"
    assert service._PRINT_BROKEN == {1: True, 2: True}
