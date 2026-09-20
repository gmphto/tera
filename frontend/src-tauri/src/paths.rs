//! Where the shell finds the interpreter, the database and its own dev data.
//!
//! Every value is either a documented environment variable or the documented
//! default; nothing is guessed and no path is written into a log line.

use std::env;
use std::path::{Path, PathBuf};

pub const PYTHON_ENV: &str = "TERA_PYTHON";
pub const DATA_DIR_ENV: &str = "TERA_DEV_DATA_DIR";
pub const DATABASE_ENV: &str = "TERA_SERVICE_DATABASE";
pub const PORT_ENV: &str = "TERA_SERVICE_PORT";
pub const URL_ENV: &str = "TERA_SERVICE_URL";

/// A configuration refusal: the reason the panel shows and the detail behind it.
#[derive(Debug, Clone)]
pub struct ConfigError {
    pub reason: &'static str,
    pub message: String,
}

impl ConfigError {
    pub fn new(reason: &'static str, message: impl Into<String>) -> Self {
        Self {
            reason,
            message: message.into(),
        }
    }
}

/// The repository root, resolved at compile time.
///
/// The manifest lives in `<root>/frontend/src-tauri`, so the root is two
/// directories up. The built binary is a development artifact: packaging
/// resolves a different root and belongs to #38.
pub fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn absolute_from_env(name: &str) -> Result<Option<PathBuf>, ConfigError> {
    match env::var(name) {
        Err(_) => Ok(None),
        Ok(value) if value.trim().is_empty() => Ok(None),
        Ok(value) => {
            let path = PathBuf::from(&value);
            if path.is_absolute() {
                Ok(Some(path))
            } else {
                Err(ConfigError::new(
                    "invalid_configuration",
                    format!("{name} must be an absolute path; got a relative one."),
                ))
            }
        }
    }
}

/// The interpreter that runs the local service.
pub fn python_path(root: &Path) -> Result<PathBuf, ConfigError> {
    if let Some(configured) = absolute_from_env(PYTHON_ENV)? {
        if configured.is_file() {
            return Ok(configured);
        }
        return Err(ConfigError::new(
            "python_not_found",
            format!("{PYTHON_ENV} does not name an existing file."),
        ));
    }
    let default = root.join(".venv").join("Scripts").join("python.exe");
    if default.is_file() {
        Ok(default)
    } else {
        Err(ConfigError::new(
            "python_not_found",
            "The repository's .venv interpreter is missing; run uv sync.".to_string(),
        ))
    }
}

/// The development data directory: `%LOCALAPPDATA%\tera\dev` unless overridden.
pub fn data_dir() -> Result<PathBuf, ConfigError> {
    if let Some(configured) = absolute_from_env(DATA_DIR_ENV)? {
        return Ok(configured);
    }
    match env::var("LOCALAPPDATA") {
        Ok(local) if !local.trim().is_empty() => {
            Ok(PathBuf::from(local).join("tera").join("dev"))
        }
        _ => Err(ConfigError::new(
            "dev_data_dir_unavailable",
            format!("%LOCALAPPDATA% is not set and {DATA_DIR_ENV} is not configured."),
        )),
    }
}

/// The database the service opens. The shell never creates or rewrites it.
pub fn database_path(data_dir: &Path) -> Result<PathBuf, ConfigError> {
    Ok(absolute_from_env(DATABASE_ENV)?.unwrap_or_else(|| data_dir.join("tera.sqlite3")))
}

/// The explicit port, when the developer asked for one.
pub fn service_port() -> Result<Option<u16>, ConfigError> {
    match env::var(PORT_ENV) {
        Err(_) => Ok(None),
        Ok(value) if value.trim().is_empty() => Ok(None),
        Ok(value) => match value.trim().parse::<u16>() {
            Ok(port) if port > 0 => Ok(Some(port)),
            _ => Err(ConfigError::new(
                "invalid_configuration",
                format!("{PORT_ENV} must be a whole number between 1 and 65535."),
            )),
        },
    }
}

/// The origin of a service this window did not start, when one was named.
pub fn attached_origin() -> Result<Option<String>, ConfigError> {
    let value = match env::var(URL_ENV) {
        Err(_) => return Ok(None),
        Ok(value) if value.trim().is_empty() => return Ok(None),
        Ok(value) => value.trim().to_string(),
    };
    let rest = value
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| value.strip_prefix("http://localhost:"))
        .ok_or_else(|| {
            ConfigError::new(
                "invalid_configuration",
                format!("{URL_ENV} must be http://127.0.0.1:<port> or http://localhost:<port>."),
            )
        })?;
    match rest.parse::<u16>() {
        Ok(port) if port > 0 => Ok(Some(format!("http://127.0.0.1:{port}"))),
        _ => Err(ConfigError::new(
            "invalid_configuration",
            format!("{URL_ENV} names a port outside 1 to 65535."),
        )),
    }
}

/// `C:/Users/...` for a log line, with the separators the panel shows.
pub fn display(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}
