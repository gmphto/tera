//! The child process, its snapshot and the ownership rules (issue #30).
//!
//! The shell owns a development service: it spawns the project interpreter on
//! the loopback interface, follows #27's listening line and port file instead of
//! inventing a second status channel, reaps a service orphaned by an earlier
//! window, and never leaves a process behind when it closes.
//!
//! Nothing here calls a route other than `GET /health`, and nothing here binds a
//! socket of its own.

use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

use crate::paths::{self, ConfigError};

pub const STATUS_EVENT: &str = "service:status";
pub const SERVICE_START_TIMEOUT_SECONDS: u64 = 120;
pub const SERVICE_STOP_TIMEOUT_SECONDS: u64 = 5;
pub const INSTANCE_HEARTBEAT_SECONDS: u64 = 2;
pub const INSTANCE_STALE_SECONDS: u64 = 10;
pub const LOG_LINES_KEPT: usize = 20;
pub const MAX_DIAGNOSTIC_LENGTH: usize = 500;
pub const PORT_FILE_NAME: &str = "service-port.json";
pub const INSTANCE_FILE_NAME: &str = "service-instance.json";
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);
const POLL: Duration = Duration::from_millis(100);
const PORT_FILE_POLL: Duration = Duration::from_millis(500);

// ---------------------------------------------------------------------------
// the snapshot
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct State {
    pub mode: &'static str,
    pub phase: &'static str,
    pub reason: Option<&'static str>,
    pub origin: Option<String>,
    pub port: Option<u16>,
    pub pid: Option<u32>,
    pub exit_code: Option<i32>,
    pub python_path: Option<String>,
    pub database_path: Option<String>,
    pub data_dir: Option<String>,
    pub port_file: Option<String>,
    pub health_seen: bool,
    pub started_at: Option<String>,
    pub diagnostic: Option<String>,
}

impl Default for State {
    fn default() -> Self {
        Self {
            mode: "owned",
            phase: "starting",
            reason: None,
            origin: None,
            port: None,
            pid: None,
            exit_code: None,
            python_path: None,
            database_path: None,
            data_dir: None,
            port_file: None,
            health_seen: false,
            started_at: None,
            diagnostic: None,
        }
    }
}

impl State {
    fn document(&self) -> Value {
        json!({
            "mode": self.mode,
            "phase": self.phase,
            "reason": self.reason,
            "origin": self.origin,
            "port": self.port,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "python_path": self.python_path,
            "database_path": self.database_path,
            "data_dir": self.data_dir,
            "port_file": self.port_file,
            "health_seen": self.health_seen,
            "started_at": self.started_at,
            "diagnostic": self.diagnostic,
        })
    }
}

/// One line of either piped stream.
enum Line {
    Out(String),
    Err(String),
}

// ---------------------------------------------------------------------------
// the supervisor
// ---------------------------------------------------------------------------

struct Inner {
    state: Mutex<State>,
    child: Mutex<Option<Child>>,
    app: Mutex<Option<AppHandle>>,
    /// Held across writing or removing the two files this window owns, so a
    /// heartbeat that is already in flight cannot recreate them after the
    /// shutdown path has removed them.
    files: Mutex<()>,
    generation: AtomicU64,
    stopping: AtomicBool,
}

/// A cheap handle: every clone shares one state, one child and one generation.
#[derive(Clone)]
pub struct Supervisor {
    inner: Arc<Inner>,
}

impl Supervisor {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                state: Mutex::new(State::default()),
                child: Mutex::new(None),
                app: Mutex::new(None),
                files: Mutex::new(()),
                generation: AtomicU64::new(0),
                stopping: AtomicBool::new(false),
            }),
        }
    }

    pub fn attach(&self, app: AppHandle) {
        *self.inner.app.lock().expect("the app handle lock is not poisoned") = Some(app);
    }

    pub fn snapshot(&self) -> Value {
        self.inner
            .state
            .lock()
            .expect("the state lock is not poisoned")
            .document()
    }

    fn emit(&self) {
        let document = self.snapshot();
        if let Some(app) = self
            .inner
            .app
            .lock()
            .expect("the app handle lock is not poisoned")
            .as_ref()
        {
            let _ = app.emit(STATUS_EVENT, document);
        }
    }

    fn update<F: FnOnce(&mut State)>(&self, change: F) {
        {
            let mut state = self.inner.state.lock().expect("the state lock is not poisoned");
            change(&mut state);
        }
        self.emit();
    }

    /// Start or re-probe the service. It never blocks the caller's thread.
    pub fn start(&self) {
        // A retry replaces the previous child rather than adding a second one.
        self.terminate(Duration::from_secs(SERVICE_STOP_TIMEOUT_SECONDS));
        let generation = self.inner.generation.fetch_add(1, Ordering::SeqCst) + 1;
        self.inner.stopping.store(false, Ordering::SeqCst);
        self.update(|state| {
            *state = State {
                mode: "owned",
                phase: "starting",
                ..State::default()
            };
        });
        let supervisor = self.clone();
        thread::spawn(move || supervisor.supervise(generation));
    }

    /// Stop the child this window owns, or leave an attached service alone.
    pub fn stop(&self) {
        let owned = self
            .inner
            .state
            .lock()
            .expect("the state lock is not poisoned")
            .mode
            == "owned";
        if !owned {
            self.update(|state| {
                state.phase = "unavailable";
                state.reason = Some("connection_refused");
            });
            return;
        }
        self.inner.stopping.store(true, Ordering::SeqCst);
        self.inner.generation.fetch_add(1, Ordering::SeqCst);
        self.update(|state| {
            state.phase = "stopping";
            state.reason = None;
        });
        self.terminate(Duration::from_secs(SERVICE_STOP_TIMEOUT_SECONDS));
        self.update(|state| {
            state.phase = "unavailable";
            state.reason = Some("stopped_by_user");
            state.pid = None;
            state.origin = None;
            state.port = None;
            state.health_seen = false;
        });
    }

    /// The normal-exit path: kill the owned child, then let the app exit.
    pub fn shutdown(&self) {
        self.inner.stopping.store(true, Ordering::SeqCst);
        self.inner.generation.fetch_add(1, Ordering::SeqCst);
        self.terminate(Duration::from_secs(SERVICE_STOP_TIMEOUT_SECONDS));
    }

    /// Terminate the child, wait for it, and remove the two files we own.
    fn terminate(&self, timeout: Duration) {
        {
            let mut guard = self
                .inner
                .child
                .lock()
                .expect("the child lock is not poisoned");
            if let Some(child) = guard.as_mut() {
                let _ = child.kill();
                let deadline = Instant::now() + timeout;
                while Instant::now() < deadline {
                    match child.try_wait() {
                        Ok(Some(_)) => break,
                        Ok(None) => thread::sleep(POLL),
                        Err(_) => break,
                    }
                }
            }
            *guard = None;
        }
        let (data_dir, port_file) = {
            let state = self.inner.state.lock().expect("the state lock is not poisoned");
            (state.data_dir.clone(), state.port_file.clone())
        };
        // The lock is what makes the removal final: a heartbeat that already
        // passed its own checks must finish before these two lines run.
        let _guard = self.inner.files.lock().expect("the files lock is not poisoned");
        if let Some(dir) = data_dir {
            let _ = fs::remove_file(Path::new(&dir).join(PORT_FILE_NAME));
            let _ = fs::remove_file(Path::new(&dir).join(INSTANCE_FILE_NAME));
        }
        if let Some(file) = port_file {
            let _ = fs::remove_file(file);
        }
    }

    /// The whole lifecycle of one attempt, on its own thread.
    fn supervise(&self, generation: u64) {
        let root = paths::repository_root();
        let data_dir = match paths::data_dir() {
            Ok(dir) => dir,
            Err(error) => return self.refuse(generation, &error),
        };
        let python = match paths::python_path(&root) {
            Ok(path) => path,
            Err(error) => return self.refuse(generation, &error),
        };
        let database = match paths::database_path(&data_dir) {
            Ok(path) => path,
            Err(error) => return self.refuse(generation, &error),
        };
        let explicit_port = match paths::service_port() {
            Ok(port) => port,
            Err(error) => return self.refuse(generation, &error),
        };
        let attached = match paths::attached_origin() {
            Ok(origin) => origin,
            Err(error) => return self.refuse(generation, &error),
        };
        if fs::create_dir_all(&data_dir).is_err() {
            return self.refuse(
                generation,
                &ConfigError::new(
                    "dev_data_dir_unavailable",
                    "The development data directory could not be created.",
                ),
            );
        }
        let port_file = data_dir.join(PORT_FILE_NAME);
        self.update(|state| {
            state.mode = if attached.is_some() { "attached" } else { "owned" };
            state.python_path = Some(paths::display(&python));
            state.database_path = Some(paths::display(&database));
            state.data_dir = Some(paths::display(&data_dir));
            state.port_file = Some(paths::display(&port_file));
            state.started_at = Some(iso_now());
        });

        if let Some(origin) = attached {
            match probe(&origin, PROBE_TIMEOUT) {
                Some(body) => {
                    let port = origin.rsplit(':').next().and_then(|p| p.parse().ok());
                    let pid = body.get("pid").and_then(Value::as_u64).map(|p| p as u32);
                    self.update(|state| {
                        state.origin = Some(origin.clone());
                        state.port = port;
                        state.pid = pid;
                    });
                    self.settle_healthy(generation, &origin, body);
                }
                None => self.refuse(
                    generation,
                    &ConfigError::new(
                        "connection_refused",
                        "Nothing answered GET /health at the recorded origin.",
                    ),
                ),
            }
            return;
        }

        if let Some(record) = read_json(&data_dir.join(INSTANCE_FILE_NAME)) {
            match adopt_or_reap(&record) {
                Record::Adopt { origin, pid } => {
                    let port = origin.rsplit(':').next().and_then(|p| p.parse().ok());
                    self.update(|state| {
                        state.mode = "attached";
                        state.origin = Some(origin.clone());
                        state.port = port;
                        state.pid = Some(pid);
                    });
                    if let Some(body) = probe(&origin, PROBE_TIMEOUT) {
                        return self.settle_healthy(generation, &origin, body);
                    }
                }
                Record::Reap { pid } => {
                    self.log(format!("service_reaped: terminated orphan pid {pid}"));
                    taskkill(pid);
                }
                Record::Ignore => {}
            }
        }

        // A port file that existed before this spawn is never trusted.
        let _ = fs::remove_file(&port_file);
        let mut command = Command::new(&python);
        command
            .arg("-m")
            .arg("backend.api.service")
            .arg("--database")
            .arg(&database)
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(match explicit_port {
                Some(port) => port.to_string(),
                None => "0".to_string(),
            })
            .arg("--port-file")
            .arg(&port_file)
            .arg("--dev-origin")
            .arg("http://localhost:1420")
            .arg("--dev-origin")
            .arg("http://127.0.0.1:1420")
            .current_dir(&root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                return self.refuse(
                    generation,
                    &ConfigError::new(
                        "spawn_failed",
                        format!("The interpreter did not start: {error}"),
                    ),
                )
            }
        };
        let pid = child.id();
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        *self.inner.child.lock().expect("the child lock is not poisoned") = Some(child);
        self.update(|state| {
            state.pid = Some(pid);
            state.exit_code = None;
        });

        let (sender, receiver) = mpsc::channel::<Line>();
        if let Some(stream) = stdout {
            spawn_reader(stream, true, sender.clone());
        }
        if let Some(stream) = stderr {
            spawn_reader(stream, false, sender.clone());
        }
        drop(sender);

        let mut tail: VecDeque<String> = VecDeque::new();
        let deadline = Instant::now() + Duration::from_secs(SERVICE_START_TIMEOUT_SECONDS);
        let mut bind_failed = false;
        let mut last_file_check = Instant::now();
        loop {
            if self.stale(generation) {
                return;
            }
            if let Ok(Some(code)) = self.child_exit() {
                let text = tail.iter().cloned().collect::<Vec<_>>().join("\n");
                return self.fail(generation, "service_exited", Some(code), text);
            }
            if last_file_check.elapsed() >= PORT_FILE_POLL {
                last_file_check = Instant::now();
                if let Some((origin, port, reported_pid, body)) = self.from_port_file(&port_file, pid)
                {
                    return self.connected(generation, origin, port, reported_pid, body);
                }
            }
            match receiver.recv_timeout(POLL) {
                Ok(Line::Out(line)) => {
                    push(&mut tail, &line);
                    if let Some(listening) = listening(&line) {
                        let port = listening
                            .get("port")
                            .and_then(Value::as_u64)
                            .and_then(|p| u16::try_from(p).ok());
                        let reported = listening
                            .get("pid")
                            .and_then(Value::as_u64)
                            .map(|p| p as u32)
                            .unwrap_or(pid);
                        if let Some(port) = port {
                            let origin = format!("http://127.0.0.1:{port}");
                            return self
                                .wait_for_health(generation, origin, port, reported, &port_file);
                        }
                    }
                }
                Ok(Line::Err(line)) => {
                    push(&mut tail, &line);
                    if bind_failure(&line).as_deref() == Some("port_in_use") {
                        bind_failed = true;
                    }
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => thread::sleep(POLL),
            }
            if Instant::now() >= deadline {
                let reason = if bind_failed { "port_in_use" } else { "start_timeout" };
                let exit = self.child_exit().ok().flatten();
                let text = tail.iter().cloned().collect::<Vec<_>>().join("\n");
                return self.fail(generation, reason, exit, text);
            }
        }
    }

    /// The port file is a fallback: the listening line is the primary signal.
    fn from_port_file(&self, port_file: &Path, spawned_pid: u32) -> Option<(String, u16, u32, Value)> {
        let value = read_json(port_file)?;
        let port = u16::try_from(value.get("port").and_then(Value::as_u64)?).ok()?;
        let origin = format!("http://127.0.0.1:{port}");
        let body = probe(&origin, PROBE_TIMEOUT)?;
        let pid = value
            .get("pid")
            .and_then(Value::as_u64)
            .map(|p| p as u32)
            .unwrap_or(spawned_pid);
        Some((origin, port, pid, body))
    }

    /// The service is listening; the first successful health read settles it.
    fn wait_for_health(
        &self,
        generation: u64,
        origin: String,
        port: u16,
        pid: u32,
        port_file: &Path,
    ) {
        let deadline = Instant::now() + Duration::from_secs(SERVICE_START_TIMEOUT_SECONDS);
        let mut last_file_check = Instant::now();
        loop {
            if self.stale(generation) {
                return;
            }
            if let Ok(Some(code)) = self.child_exit() {
                return self.fail(generation, "service_exited", Some(code), String::new());
            }
            if let Some(body) = probe(&origin, PROBE_TIMEOUT) {
                return self.connected(generation, origin, port, pid, body);
            }
            if last_file_check.elapsed() >= PORT_FILE_POLL {
                last_file_check = Instant::now();
                if let Some((other, other_port, other_pid, body)) =
                    self.from_port_file(port_file, pid)
                {
                    return self.connected(generation, other, other_port, other_pid, body);
                }
            }
            if Instant::now() >= deadline {
                return self.fail(generation, "health_timeout", None, String::new());
            }
            thread::sleep(POLL);
        }
    }

    /// Record the connection and start following the child.
    fn connected(&self, generation: u64, origin: String, port: u16, pid: u32, body: Value) {
        self.update(|state| {
            state.origin = Some(origin.clone());
            state.port = Some(port);
            state.pid = Some(pid);
        });
        self.write_instance(&origin, pid);
        self.settle_healthy(generation, &origin, body);
        self.watch(generation);
        self.heartbeat(generation, origin, pid);
    }

    fn settle_healthy(&self, generation: u64, origin: &str, body: Value) {
        if self.stale(generation) {
            return;
        }
        let degraded = body.get("state").and_then(Value::as_str) == Some("degraded");
        self.update(|state| {
            state.phase = if degraded { "degraded" } else { "running" };
            state.reason = None;
            state.origin = Some(origin.to_string());
            state.health_seen = true;
        });
    }

    /// Follow the child until it exits, so a mid-session death is visible fast.
    fn watch(&self, generation: u64) {
        let supervisor = self.clone();
        thread::spawn(move || loop {
            if supervisor.stale(generation) {
                return;
            }
            if let Ok(Some(code)) = supervisor.child_exit() {
                if !supervisor.inner.stopping.load(Ordering::SeqCst) {
                    supervisor.update(|state| {
                        state.phase = "failed";
                        state.reason = Some("service_exited");
                        state.exit_code = Some(code);
                        state.health_seen = false;
                    });
                    let data_dir = {
                        let state = supervisor
                            .inner
                            .state
                            .lock()
                            .expect("the state lock is not poisoned");
                        state.data_dir.clone()
                    };
                    if let Some(dir) = data_dir {
                        let _ = fs::remove_file(Path::new(&dir).join(PORT_FILE_NAME));
                        let _ = fs::remove_file(Path::new(&dir).join(INSTANCE_FILE_NAME));
                    }
                }
                return;
            }
            thread::sleep(POLL);
        });
    }

    fn heartbeat(&self, generation: u64, origin: String, pid: u32) {
        let supervisor = self.clone();
        thread::spawn(move || loop {
            thread::sleep(Duration::from_secs(INSTANCE_HEARTBEAT_SECONDS));
            if supervisor.stale(generation) {
                return;
            }
            supervisor.write_instance(&origin, pid);
            if let Some(body) = probe(&origin, PROBE_TIMEOUT) {
                let degraded = body.get("state").and_then(Value::as_str) == Some("degraded");
                let phase = if degraded { "degraded" } else { "running" };
                let current = supervisor
                    .inner
                    .state
                    .lock()
                    .expect("the state lock is not poisoned")
                    .phase;
                if (current == "running" || current == "degraded") && current != phase {
                    supervisor.update(|state| {
                        state.phase = phase;
                    });
                }
            }
        });
    }

    fn write_instance(&self, origin: &str, service_pid: u32) {
        let (data_dir, database, started_at) = {
            let state = self.inner.state.lock().expect("the state lock is not poisoned");
            (
                state.data_dir.clone(),
                state.database_path.clone(),
                state.started_at.clone(),
            )
        };
        let Some(data_dir) = data_dir else { return };
        let _guard = self.inner.files.lock().expect("the files lock is not poisoned");
        // A write that was in flight when the window began closing must not
        // recreate the record the shutdown path is about to remove.
        if self.inner.stopping.load(Ordering::SeqCst) {
            return;
        }
        let record = json!({
            "schema": 1,
            "owner_pid": std::process::id(),
            "service_pid": service_pid,
            "port": origin.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()),
            "origin": origin,
            "mode": "owned",
            "database_path": database,
            "started_at": started_at,
            "heartbeat_at": iso_now(),
        });
        let _ = write_json_atomic(&Path::new(&data_dir).join(INSTANCE_FILE_NAME), &record);
    }

    fn refuse(&self, generation: u64, error: &ConfigError) {
        if self.stale(generation) {
            return;
        }
        let unavailable = matches!(
            error.reason,
            "connection_refused"
                | "python_not_found"
                | "dev_data_dir_unavailable"
                | "invalid_configuration"
        );
        let reason = error.reason;
        let message = truncate(&error.message);
        self.update(|state| {
            state.phase = if unavailable { "unavailable" } else { "failed" };
            state.reason = Some(reason);
            state.exit_code = None;
            state.diagnostic = Some(message);
        });
    }

    fn fail(&self, generation: u64, reason: &'static str, exit_code: Option<i32>, tail: String) {
        if self.stale(generation) {
            return;
        }
        let diagnostic = if tail.trim().is_empty() {
            None
        } else {
            Some(truncate(&tail))
        };
        self.update(|state| {
            state.phase = "failed";
            state.reason = Some(reason);
            state.exit_code = exit_code;
            state.health_seen = false;
            if diagnostic.is_some() {
                state.diagnostic = diagnostic;
            }
        });
    }

    fn log(&self, line: String) {
        self.update(|state| {
            state.diagnostic = Some(truncate(&line));
        });
    }

    fn stale(&self, generation: u64) -> bool {
        self.inner.generation.load(Ordering::SeqCst) != generation
    }

    fn child_exit(&self) -> Result<Option<i32>, ()> {
        let mut guard = self
            .inner
            .child
            .lock()
            .expect("the child lock is not poisoned");
        match guard.as_mut() {
            None => Err(()),
            Some(child) => match child.try_wait() {
                Ok(Some(status)) => Ok(Some(status.code().unwrap_or(-1))),
                Ok(None) => Ok(None),
                Err(_) => Err(()),
            },
        }
    }
}

/// One reader thread per piped stream, so neither can block the other.
fn spawn_reader<R: Read + Send + 'static>(stream: R, is_stdout: bool, sender: Sender<Line>) {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let message = if is_stdout {
                Line::Out(line)
            } else {
                Line::Err(line)
            };
            if sender.send(message).is_err() {
                break;
            }
        }
    });
}

/// What the previous window's record means for this one.
enum Record {
    Adopt { origin: String, pid: u32 },
    Reap { pid: u32 },
    Ignore,
}

fn adopt_or_reap(record: &Value) -> Record {
    let origin = match record.get("origin").and_then(Value::as_str) {
        Some(origin) => origin.to_string(),
        None => return Record::Ignore,
    };
    let recorded_pid = record
        .get("service_pid")
        .and_then(Value::as_u64)
        .map(|p| p as u32);
    let Some(body) = probe(&origin, PROBE_TIMEOUT) else {
        // Nothing answers: the record is stale and its pid is never killed.
        return Record::Ignore;
    };
    let live_pid = body
        .get("pid")
        .and_then(Value::as_u64)
        .map(|p| p as u32)
        .or(recorded_pid);
    let Some(pid) = live_pid else {
        return Record::Ignore;
    };
    if Some(pid) != recorded_pid {
        // A recycled pid: the record does not describe the process answering.
        return Record::Ignore;
    }
    match heartbeat_age_seconds(record) {
        Some(age) if age > INSTANCE_STALE_SECONDS => Record::Reap { pid },
        Some(_) => Record::Adopt { origin, pid },
        None => Record::Ignore,
    }
}

fn push(tail: &mut VecDeque<String>, line: &str) {
    tail.push_back(line.to_string());
    while tail.len() > LOG_LINES_KEPT {
        tail.pop_front();
    }
}

fn listening(line: &str) -> Option<Value> {
    let value: Value = serde_json::from_str(line.trim()).ok()?;
    match value.get("event").and_then(Value::as_str) {
        Some("listening") => Some(value),
        _ => None,
    }
}

fn bind_failure(line: &str) -> Option<String> {
    let value: Value = serde_json::from_str(line.trim()).ok()?;
    if value.get("event").and_then(Value::as_str) != Some("bind_failed") {
        return None;
    }
    value.get("code").and_then(Value::as_str).map(str::to_string)
}

/// One raw `GET /health`. No crate, no client, no header of our own.
pub fn probe(origin: &str, timeout: Duration) -> Option<Value> {
    let authority = origin
        .strip_prefix("http://")
        .unwrap_or(origin)
        .trim_end_matches('/');
    let mut stream = TcpStream::connect(authority).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let request = format!(
        "GET /health HTTP/1.1\r\nHost: {authority}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).ok()?;
    let text = String::from_utf8_lossy(&raw);
    let (head, body) = text.split_once("\r\n\r\n")?;
    let status = head.lines().next()?.split_whitespace().nth(1)?;
    if status != "200" {
        return None;
    }
    serde_json::from_str(body.trim()).ok()
}

fn read_json(path: &Path) -> Option<Value> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn write_json_atomic(path: &Path, value: &Value) -> std::io::Result<()> {
    let temporary = path.with_extension("json.tmp");
    fs::write(&temporary, value.to_string())?;
    // Windows will not always replace an existing file through a rename, and a
    // silently failing heartbeat write is worse than a missing one: the record
    // would keep a stale timestamp while the panel showed a healthy service.
    let _ = fs::remove_file(path);
    match fs::rename(&temporary, path) {
        Ok(()) => Ok(()),
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            Err(error)
        }
    }
}

fn taskkill(pid: u32) {
    let _ = Command::new("taskkill")
        .arg("/PID")
        .arg(pid.to_string())
        .arg("/F")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn truncate(text: &str) -> String {
    let trimmed = text.trim();
    match trimmed.char_indices().nth(MAX_DIAGNOSTIC_LENGTH) {
        Some((index, _)) => trimmed[..index].to_string(),
        None => trimmed.to_string(),
    }
}

// ---------------------------------------------------------------------------
// timestamps, without a date crate
// ---------------------------------------------------------------------------

fn iso_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0);
    iso_from_epoch(seconds as i64)
}

fn iso_from_epoch(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        (rest % 3600) / 60,
        rest % 60
    )
}

fn epoch_from_iso(text: &str) -> Option<i64> {
    if text.len() < 20 {
        return None;
    }
    let number = |from: usize, to: usize| text.get(from..to)?.parse::<i64>().ok();
    let (year, month, day) = (number(0, 4)?, number(5, 7)?, number(8, 10)?);
    let (hour, minute, second) = (number(11, 13)?, number(14, 16)?, number(17, 19)?);
    Some(days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second)
}

fn heartbeat_age_seconds(record: &Value) -> Option<u64> {
    let stamp = record.get("heartbeat_at").and_then(Value::as_str)?;
    let then = epoch_from_iso(stamp)?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs() as i64;
    u64::try_from(now - then).ok()
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let adjusted = month + if month > 2 { -3 } else { 9 };
    let doy = (153 * adjusted + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (if month <= 2 { year + 1 } else { year }, month, day)
}
