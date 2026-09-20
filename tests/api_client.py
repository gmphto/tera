"""The shared client, library builder and service runner for the API tests.

Everything here is standard library plus #9's synthetic audio helpers: the
client speaks HTTP/1.1 through `http.client`, the library builder writes WAV
bytes produced by `tests.test_audio.wav` under a `tmp_path`, and
`start_service` runs `backend.api.service.create_server` in this process on
port 0 and hands back the bound port and a shutdown callback.

No test in this suite names a real library path, sample, fingerprint, credential
or byte of real audio, and no response is ever asserted against a real path.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import threading

import numpy as np

from backend.analysis import batch
from backend.api import service
from backend.contracts import Sample
from backend.library import scanner
from backend.library.repository import LibraryRepository, transaction
from backend.library.schema import open_database
from tests.test_audio import wav


def tone(frequency=110, frames=480, rate=48000, amplitude=0.3) -> bytes:
    """A deterministic synthetic tone, only ever written under a temporary root."""

    phase = 2 * np.pi * frequency * np.arange(frames) / rate
    return wav((amplitude * np.sin(phase))[:, None], rate)


def three_channel(frames=480, rate=48000) -> bytes:
    """A valid 3-channel WAV: the one layout #22's header refuses."""

    return wav(np.zeros((frames, 3)), rate)


def build_library(directory, folders) -> Path:
    """Write one synthetic library and return its root.

    `folders` maps a folder name to a mapping of file name to the bytes written
    there, so one call builds several roles at once:
    `build_library(tmp_path, {"kicks": {"a.wav": tone(55)}})`.
    """

    root = Path(directory)
    for folder, files in folders.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (root / folder / name).write_bytes(content)
    return root


def scan(root, database, role="kick") -> dict:
    """Reconcile one folder through #22's scanner and return its summary."""

    with closing(open_database(str(database))) as connection:
        return scanner.reconcile(Path(root), role, connection, str(database))


def connection(database):
    """#21's connection to a test database, configured and migrated."""

    return open_database(str(database))


def store_analysis(database, sample_id, *, version=None, descriptor=None) -> str:
    """Store the analysis of one stored row, exactly as #23's worker does.

    The row is read by its #21 `sample_id`, its stored path is what is
    extracted, and the features a test reads back are the ones the real
    extractor produced for that file. The row's content fingerprint is returned.
    """

    version = version or batch.digest(batch.analysis_descriptor())
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        row = opened.execute("SELECT * FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        path = Path(row["original_path"])
        data = batch.snapshot(path)
        fingerprint = hashlib.sha256(data).hexdigest()
        payload = batch.extract(data, path, row["role"], fingerprint, version)
        with transaction(opened):
            # A descriptor registers the version it digests to, which is how the
            # worker stores an analysis; without one the version would have to be
            # registered already.
            repository.store_analysis(
                Sample.from_dict(payload), content_sha256=fingerprint,
                sample_id=row["sample_id"],
                descriptor=descriptor if descriptor is not None
                else batch.analysis_descriptor())
    return fingerprint


def synthetic_hash(index: int) -> str:
    """A synthetic content hash whose value orders with `index`.

    `f"{index:064x}"` is 64 lowercase hex characters, so a row inserted with it
    sorts by its index under the content-identity ordering the paging routes use.
    """

    return f"{index:064x}"


def insert_samples(database, count, *, directory, start=1, role="bass",
                   status="unknown") -> list:
    """Insert `count` synthetic path rows without analysis; return their sample ids.

    #21's `insert_path_record` never opens the file, and the routes under test
    read neither the file nor its directory, so `directory` is only what the
    synthetic path is spelled from. Every row's id is its content identity, which
    is the id the API addresses a sample by.
    """

    ids = []
    root = Path(directory)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        for index in range(start, start + count):
            record = repository.insert_path_record(
                root / f"synth-{index:04d}.wav", role=role,
                content_sha256=synthetic_hash(index), sample_rate_hz=48000, channels=1,
                frame_count=480, duration_ms=10.0, file_status=status)
            ids.append(record.sample_id)
    return ids


def store_extra_version(database, sample_id, descriptor) -> str:
    """Store one more analysis version for a row, from the same extraction.

    The version is the descriptor's own digest, because that is how #21
    registers an analysis version; the caller never invents a version string.
    """

    version = batch.digest(descriptor)
    with closing(connection(database)) as opened:
        repository = LibraryRepository(opened)
        row = opened.execute("SELECT * FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        path = Path(row["original_path"])
        data = batch.snapshot(path)
        fingerprint = hashlib.sha256(data).hexdigest()
        payload = batch.extract(data, path, row["role"], fingerprint, version)
        with transaction(opened):
            repository.store_analysis(Sample.from_dict(payload), content_sha256=fingerprint,
                                      sample_id=row["sample_id"], descriptor=descriptor)
    return version


@dataclass(frozen=True)
class Reply:
    """One response: status, headers, decoded body and raw bytes."""

    status: int
    headers: dict
    body: object
    raw: bytes

    def code(self):
        """The error code of a non-2xx body, or None."""

        if self.status < 300:
            return None
        return self.body["error"]["code"]


class Client:
    """A standard-library HTTP client for one running service."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def call(self, method, path, *, body=None, headers=None, host=None):
        """One request; `body` is encoded as JSON unless it is already bytes.

        The `Host` header is the standard library's unless `host` overrides it,
        which is how the policy tests send a host the service did not bind.
        """

        outgoing = dict(headers or {})
        payload = None
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        if method not in ("GET", "HEAD", "OPTIONS"):
            # The service requires a media type on every non-GET request, body
            # or not: that header is what a cross-site form post cannot set.
            outgoing.setdefault("Content-Type", "application/json")
        if host is not None:
            outgoing["Host"] = host
        connection_ = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection_.request(method, path, body=payload, headers=outgoing)
            response = connection_.getresponse()
            raw = response.read()
            parsed = json.loads(raw) if raw else None
            return Reply(status=response.status,
                         headers={name.lower(): value for name, value in response.getheaders()},
                         body=parsed, raw=raw)
        finally:
            connection_.close()

    def get(self, path, **kwargs):
        return self.call("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.call("POST", path, body=body, **kwargs)


@dataclass(frozen=True)
class RunningService:
    """A service running in this process: the bound port and how to stop it."""

    port: int
    stop: object
    server: object
    app: object

    @property
    def client(self) -> Client:
        return Client(self.port)


def start_service(database, *, dev_origins=(), host=service.DEFAULT_HOST) -> RunningService:
    """Run `create_server` on port 0 in this process and return its port.

    The returned object's `stop` is the shutdown callback: it ends the accept
    loop, closes the listen socket and releases the database connection.
    """

    server = service.create_server(database, host=host, port=0, dev_origins=dev_origins)
    thread = threading.Thread(target=server.serve_forever, name="tera-test-service", daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        server.app.stop(service.SHUTDOWN_GRACE_SECONDS)
        server.app.close()

    return RunningService(port=server.server_address[1], stop=stop, server=server,
                          app=server.app)
