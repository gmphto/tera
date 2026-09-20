//! The shell's audition roots and the one command that reads audio (issue #34).
//!
//! #27 serves no audio byte and no filesystem path, so playback needs a file the
//! shell can find from what the client holds: a content identity and a basename.
//! The producer registers the folder they imported; the registry keeps the
//! canonical form of each registered folder next to an opaque id, and
//! `read_audition_source` resolves one basename inside it and hands the bytes
//! back only when they hash to the identity the client asked for.
//!
//! Three things this module will not do: it never writes outside
//! `audition-roots.json` in the shell's data directory, it never opens a file
//! for writing, and it never returns bytes whose digest is not the one asked
//! for. A refusal is a `{code, message}` pair whose code is one of the codes the
//! client's closed vocabulary carries.

use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::ipc::Response;
use tauri::State;

/// The registry file's own version, and the name it is stored under.
pub const AUDITION_ROOTS_SCHEMA_VERSION: u32 = 1;
pub const AUDITION_ROOTS_FILE: &str = "audition-roots.json";

/// How many folders may be registered at once.
pub const AUDITION_ROOT_LIMIT: usize = 16;

/// The extensions the shell will hand to the webview.
pub const SUPPORTED_AUDITION_EXTENSIONS: [&str; 1] = ["wav"];

/// The largest file an audition will read.
pub const MAX_AUDITION_BYTES: u64 = 67_108_864;

/// A refusal the client maps to one of its own codes.
#[derive(Debug, Clone, Serialize)]
pub struct AuditionFailure {
    pub code: &'static str,
    pub message: String,
}

impl AuditionFailure {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

type AuditionResult<T> = Result<T, AuditionFailure>;

/// One registered folder: an opaque id and the canonical path behind it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditionRootEntry {
    pub root_id: String,
    pub canonical_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditionRoots {
    pub schema_version: u32,
    pub roots: Vec<AuditionRootEntry>,
}

impl Default for AuditionRoots {
    fn default() -> Self {
        Self {
            schema_version: AUDITION_ROOTS_SCHEMA_VERSION,
            roots: Vec::new(),
        }
    }
}

/// What the client is told about a folder it just registered.
#[derive(Debug, Clone, Serialize)]
pub struct AuditionRootHandle {
    pub root_id: String,
    pub root_label: String,
}

/// The registry as the window holds it: the entries plus the file they live in.
pub struct AuditionRegistry {
    entries: Mutex<Vec<AuditionRootEntry>>,
    path: Option<PathBuf>,
}

impl AuditionRegistry {
    /// Load the registry from `data_dir`, pruning entries that are no longer
    /// directories. A missing, unreadable or malformed file is an empty
    /// registry: the producer registers a folder again rather than being shown
    /// an error about a file they never asked for.
    pub fn load(data_dir: Option<PathBuf>) -> Self {
        let path = data_dir.map(|dir| dir.join(AUDITION_ROOTS_FILE));
        let mut entries = Vec::new();
        if let Some(file) = path.as_ref() {
            if let Ok(text) = fs::read_to_string(file) {
                if let Ok(stored) = serde_json::from_str::<AuditionRoots>(&text) {
                    if stored.schema_version == AUDITION_ROOTS_SCHEMA_VERSION {
                        entries = stored
                            .roots
                            .into_iter()
                            .filter(|entry| Path::new(&entry.canonical_path).is_dir())
                            .take(AUDITION_ROOT_LIMIT)
                            .collect();
                    }
                }
            }
        }
        Self {
            entries: Mutex::new(entries),
            path,
        }
    }

    fn snapshot(&self) -> Vec<AuditionRootEntry> {
        self.entries
            .lock()
            .map(|entries| entries.clone())
            .unwrap_or_default()
    }

    /// Replace the file atomically, or report why it could not be written.
    fn persist(&self, entries: &[AuditionRootEntry]) -> AuditionResult<()> {
        let Some(file) = self.path.as_ref() else {
            return Err(AuditionFailure::new(
                "root_invalid",
                "The shell has no data directory to remember folders in.",
            ));
        };
        let document = AuditionRoots {
            schema_version: AUDITION_ROOTS_SCHEMA_VERSION,
            roots: entries.to_vec(),
        };
        let text = serde_json::to_string(&document).map_err(|error| {
            AuditionFailure::new("root_invalid", format!("The registry could not be written: {error}"))
        })?;
        let temporary = file.with_extension("json.writing");
        fs::write(&temporary, text).map_err(|error| {
            AuditionFailure::new("root_invalid", format!("The registry could not be written: {error}"))
        })?;
        fs::rename(&temporary, file).map_err(|error| {
            let _ = fs::remove_file(&temporary);
            AuditionFailure::new("root_invalid", format!("The registry could not be replaced: {error}"))
        })
    }

    fn register(&self, canonical: &Path) -> AuditionResult<AuditionRootHandle> {
        let canonical_path = canonical.to_string_lossy().replace('\\', "/");
        let root_id = root_id_for(&canonical_path);
        let mut entries = self.entries.lock().map_err(|_| {
            AuditionFailure::new("root_invalid", "The registry is not available.")
        })?;
        if !entries.iter().any(|entry| entry.root_id == root_id) {
            if entries.len() >= AUDITION_ROOT_LIMIT {
                return Err(AuditionFailure::new(
                    "root_limit_reached",
                    format!("At most {AUDITION_ROOT_LIMIT} folders can be registered."),
                ));
            }
            entries.push(AuditionRootEntry {
                root_id: root_id.clone(),
                canonical_path: canonical_path.clone(),
            });
            self.persist(&entries)?;
        }
        Ok(AuditionRootHandle {
            root_id,
            // The label is the folder's own name: a registered path is never
            // rendered, logged or returned as a path.
            root_label: canonical
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_else(|| "folder".to_string()),
        })
    }

    fn forget(&self, root_id: &str) -> AuditionResult<()> {
        let mut entries = self.entries.lock().map_err(|_| {
            AuditionFailure::new("root_invalid", "The registry is not available.")
        })?;
        let before = entries.len();
        entries.retain(|entry| entry.root_id != root_id);
        if entries.len() == before {
            return Err(AuditionFailure::new(
                "root_invalid",
                "No registered folder carries that id.",
            ));
        }
        self.persist(&entries)
    }
}

/// An opaque id for one canonical path.
///
/// FNV-1a over the path's bytes: this is a name for a registry entry, not a
/// security check, and the registry holds at most [`AUDITION_ROOT_LIMIT`] of
/// them. It is deliberately not a digest of anything the service owns.
fn root_id_for(canonical_path: &str) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in canonical_path.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("root-{hash:016x}")
}

// ---------------------------------------------------------------------------
// #22's root rule
// ---------------------------------------------------------------------------

#[cfg(windows)]
fn drive_is_remote(anchor: &Path) -> bool {
    use std::os::windows::ffi::OsStrExt;

    // DRIVE_REMOTE, the one value #22's rule refuses. Declared here rather than
    // pulled in as a dependency: this module adds no crate.
    const DRIVE_REMOTE: u32 = 4;
    #[link(name = "kernel32")]
    extern "system" {
        fn GetDriveTypeW(root: *const u16) -> u32;
    }
    let mut wide: Vec<u16> = anchor.as_os_str().encode_wide().collect();
    wide.push(0);
    // SAFETY: `wide` is a NUL-terminated UTF-16 buffer that outlives the call.
    unsafe { GetDriveTypeW(wide.as_ptr()) == DRIVE_REMOTE }
}

#[cfg(not(windows))]
fn drive_is_remote(_anchor: &Path) -> bool {
    false
}

/// A symbolic link, a Windows junction or any other reparse point.
fn is_linked(path: &Path) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        return metadata.file_attributes() & 0x400 != 0;
    }
    #[cfg(not(windows))]
    false
}

/// #22's whole root rule, applied to a folder the producer picked.
///
/// The same rule the scanner applies to an import root, so the folder that was
/// imported is the folder that can be auditioned: a local path with no UNC
/// prefix, no mapped network drive and no linked ancestor, that is an existing
/// directory, canonicalised.
pub fn validate_audition_root(folder: &str) -> AuditionResult<PathBuf> {
    if folder.is_empty() || folder.contains('\0') || folder.contains("://") {
        return Err(AuditionFailure::new("root_invalid", "That is not a folder path."));
    }
    if folder.starts_with("\\\\") || folder.starts_with("//") {
        return Err(AuditionFailure::new(
            "root_invalid",
            "A network share cannot be used for playback.",
        ));
    }
    let path = PathBuf::from(folder);
    if !path.is_absolute() {
        return Err(AuditionFailure::new("root_invalid", "That is not an absolute path."));
    }
    if !path.is_dir() {
        return Err(AuditionFailure::new(
            "root_invalid",
            "That folder does not exist.",
        ));
    }
    if drive_is_remote(&path) {
        return Err(AuditionFailure::new(
            "root_invalid",
            "A mapped network drive cannot be used for playback.",
        ));
    }
    for ancestor in path.ancestors() {
        if is_linked(ancestor) {
            return Err(AuditionFailure::new(
                "root_invalid",
                "A linked folder cannot be used for playback.",
            ));
        }
    }
    fs::canonicalize(&path).map_err(|error| {
        AuditionFailure::new("root_invalid", format!("That folder could not be resolved: {error}"))
    })
}

// ---------------------------------------------------------------------------
// the file name rule
// ---------------------------------------------------------------------------

/// One basename the shell will look for, or the code that refuses it.
///
/// The client applies the same rule before it calls, so a producer sees "only
/// .wav files" rather than a generic refusal; the shell applies it again because
/// a command is a boundary and this one opens a file.
pub fn validate_audition_name(file_name: &str) -> AuditionResult<()> {
    if file_name.is_empty() || file_name.len() > 255 {
        return Err(AuditionFailure::new(
            "invalid_file_name",
            "The stored file name cannot be resolved to a file.",
        ));
    }
    let path = Path::new(file_name);
    let mut components = path.components();
    match (components.next(), components.next()) {
        (Some(Component::Normal(_)), None) => {}
        _ => {
            return Err(AuditionFailure::new(
                "invalid_file_name",
                "The stored file name cannot be resolved to a file.",
            ))
        }
    }
    if file_name.chars().any(|character| character.is_control()) {
        return Err(AuditionFailure::new(
            "invalid_file_name",
            "The stored file name cannot be resolved to a file.",
        ));
    }
    let extension = path
        .extension()
        .map(|value| value.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default();
    if !SUPPORTED_AUDITION_EXTENSIONS.contains(&extension.as_str()) {
        return Err(AuditionFailure::new(
            "unsupported_extension",
            "Only .wav files can be auditioned.",
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// reading the source
// ---------------------------------------------------------------------------

/// One candidate file inside the registered folders, in resolution order.
fn candidate_paths(entries: &[AuditionRootEntry], file_name: &str, root_id: Option<&str>)
    -> Vec<PathBuf> {
    let mut ordered: Vec<&AuditionRootEntry> = Vec::with_capacity(entries.len());
    if let Some(preferred) = root_id {
        ordered.extend(entries.iter().filter(|entry| entry.root_id == preferred));
    }
    ordered.extend(entries.iter().filter(|entry| Some(entry.root_id.as_str()) != root_id));
    ordered
        .into_iter()
        .map(|entry| Path::new(&entry.canonical_path).join(file_name))
        .collect()
}

/// The file's bytes, or the one code that says why not.
///
/// Nothing is returned that has not hashed to `sample_id`: a file that was
/// replaced, truncated or edited after it was imported is refused rather than
/// played as if it were the analysed sample.
pub fn read_source(
    entries: &[AuditionRootEntry],
    sample_id: &str,
    file_name: &str,
    root_id: Option<&str>,
) -> AuditionResult<Vec<u8>> {
    validate_audition_name(file_name)?;
    if entries.is_empty() {
        return Err(AuditionFailure::new(
            "no_registered_root",
            "No folder is registered for playback.",
        ));
    }
    let mut found: Option<PathBuf> = None;
    for candidate in candidate_paths(entries, file_name, root_id) {
        match fs::metadata(&candidate) {
            Ok(metadata) if metadata.is_file() => {
                found = Some(candidate);
                break;
            }
            Ok(_) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => continue,
        }
    }
    let Some(path) = found else {
        return Err(AuditionFailure::new(
            "missing_file",
            "That file is no longer in any registered folder.",
        ));
    };
    let metadata = fs::metadata(&path).map_err(|_| {
        AuditionFailure::new("unreadable_file", "That file could not be read.")
    })?;
    if metadata.len() > MAX_AUDITION_BYTES {
        return Err(AuditionFailure::new(
            "too_large",
            format!("A file larger than {MAX_AUDITION_BYTES} bytes cannot be auditioned."),
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    // Read-only, always: an audition never opens a source for writing.
    let mut file = fs::File::open(&path).map_err(|_| {
        AuditionFailure::new("unreadable_file", "That file could not be opened.")
    })?;
    file.read_to_end(&mut bytes).map_err(|_| {
        AuditionFailure::new("unreadable_file", "That file could not be read.")
    })?;
    let digest = format!("sha256:{}", sha256_hex(&bytes));
    if digest != sample_id {
        return Err(AuditionFailure::new(
            "content_mismatch",
            "That file's bytes are not the analysed sample.",
        ));
    }
    Ok(bytes)
}

// ---------------------------------------------------------------------------
// sha256
// ---------------------------------------------------------------------------

const SHA256_ROUND: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// The lowercase hex SHA-256 of one buffer, so the bytes can be checked against
/// the identity the client asked for. Implemented here because this module adds
/// no crate and `std` has no digest.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut state: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let mut message = bytes.to_vec();
    let bits = (bytes.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bits.to_be_bytes());

    for chunk in message.chunks_exact(64) {
        let mut schedule = [0u32; 64];
        for (index, word) in chunk.chunks_exact(4).enumerate() {
            schedule[index] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for index in 16..64 {
            let s0 = schedule[index - 15].rotate_right(7)
                ^ schedule[index - 15].rotate_right(18)
                ^ (schedule[index - 15] >> 3);
            let s1 = schedule[index - 2].rotate_right(17)
                ^ schedule[index - 2].rotate_right(19)
                ^ (schedule[index - 2] >> 10);
            schedule[index] = schedule[index - 16]
                .wrapping_add(s0)
                .wrapping_add(schedule[index - 7])
                .wrapping_add(s1);
        }
        let mut working = state;
        for index in 0..64 {
            let s1 = working[4].rotate_right(6)
                ^ working[4].rotate_right(11)
                ^ working[4].rotate_right(25);
            let choice = (working[4] & working[5]) ^ (!working[4] & working[6]);
            let first = working[7]
                .wrapping_add(s1)
                .wrapping_add(choice)
                .wrapping_add(SHA256_ROUND[index])
                .wrapping_add(schedule[index]);
            let s0 = working[0].rotate_right(2)
                ^ working[0].rotate_right(13)
                ^ working[0].rotate_right(22);
            let majority = (working[0] & working[1]) ^ (working[0] & working[2]) ^ (working[1] & working[2]);
            let second = s0.wrapping_add(majority);
            working = [
                first.wrapping_add(second),
                working[0],
                working[1],
                working[2],
                working[3].wrapping_add(first),
                working[4],
                working[5],
                working[6],
            ];
        }
        for index in 0..8 {
            state[index] = state[index].wrapping_add(working[index]);
        }
    }
    state.iter().map(|word| format!("{word:08x}")).collect()
}

// ---------------------------------------------------------------------------
// the commands
// ---------------------------------------------------------------------------

/// `register_audition_root`: remember one folder for playback.
#[tauri::command]
pub fn register_audition_root(
    path: String,
    registry: State<'_, AuditionRegistry>,
) -> AuditionResult<AuditionRootHandle> {
    let canonical = validate_audition_root(&path)?;
    registry.register(&canonical)
}

/// `forget_audition_root`: stop resolving inside one folder.
#[tauri::command]
pub fn forget_audition_root(
    root_id: String,
    registry: State<'_, AuditionRegistry>,
) -> AuditionResult<()> {
    registry.forget(&root_id)
}

/// `read_audition_source`: the bytes of one basename, verified against its id.
#[tauri::command]
pub fn read_audition_source(
    sample_id: String,
    file_name: String,
    root_id: Option<String>,
    registry: State<'_, AuditionRegistry>,
) -> AuditionResult<Response> {
    let entries = registry.snapshot();
    let bytes = read_source(&entries, &sample_id, &file_name, root_id.as_deref())?;
    // A raw payload, so `invoke` resolves to an ArrayBuffer rather than to a
    // JSON document of numbers.
    Ok(Response::new(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_digest_matches_the_published_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        // Longer than one block, so the padding and the block loop are exercised.
        assert_eq!(
            sha256_hex(&b"a".repeat(1000)),
            "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3"
        );
    }

    #[test]
    fn a_name_is_one_component_ending_in_wav() {
        assert!(validate_audition_name("kick-001.wav").is_ok());
        assert!(validate_audition_name("Kick One Shot.WAV").is_ok());
        for refused in ["", "..", ".", "sub/kick.wav", "sub\\kick.wav", "kick.mp3", "kick"] {
            assert!(validate_audition_name(refused).is_err(), "{refused} was accepted");
        }
    }

    #[test]
    fn only_the_matching_bytes_are_returned() {
        let directory = std::env::temp_dir().join("tera-audition-test");
        let _ = fs::remove_dir_all(&directory);
        fs::create_dir_all(&directory).unwrap();
        let bytes = b"RIFF....WAVE".to_vec();
        fs::write(directory.join("kick-001.wav"), &bytes).unwrap();
        let entries = vec![AuditionRootEntry {
            root_id: "root-0000000000000001".to_string(),
            canonical_path: directory.to_string_lossy().replace('\\', "/"),
        }];
        let identity = format!("sha256:{}", sha256_hex(&bytes));
        assert_eq!(
            read_source(&entries, &identity, "kick-001.wav", None).unwrap(),
            bytes
        );
        let wrong = format!("sha256:{}", "0".repeat(64));
        assert_eq!(
            read_source(&entries, &wrong, "kick-001.wav", None).unwrap_err().code,
            "content_mismatch"
        );
        assert_eq!(
            read_source(&entries, &identity, "kick-002.wav", None).unwrap_err().code,
            "missing_file"
        );
        assert!(read_source(&[], &identity, "kick-001.wav", None).is_err());
        fs::remove_dir_all(&directory).unwrap();
    }
}
