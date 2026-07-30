//! Filesystem layout. Data and logs live OUTSIDE the app bundle so a
//! drag-replace update never touches them.

use std::path::PathBuf;

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").expect("HOME is not set"))
}

/// `$VAR` if set and non-empty, else `~/<fallback>`.
fn xdg(var: &str, fallback: &str) -> PathBuf {
    match std::env::var_os(var) {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => home().join(fallback),
    }
}

/// macOS: `~/Library/Application Support/GuitarTutor`
/// Linux: `${XDG_DATA_HOME:-~/.local/share}/guitar-tutor`
pub fn data_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        home().join("Library/Application Support/GuitarTutor")
    } else {
        xdg("XDG_DATA_HOME", ".local/share").join("guitar-tutor")
    }
}

/// macOS: `~/Library/Logs/GuitarTutor`
/// Linux: `${XDG_STATE_HOME:-~/.local/state}/guitar-tutor/log`
pub fn log_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        home().join("Library/Logs/GuitarTutor")
    } else {
        xdg("XDG_STATE_HOME", ".local/state").join("guitar-tutor/log")
    }
}

/// Where webview downloads (backup exports, DOCX) land.
pub fn downloads_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        home().join("Downloads")
    } else {
        xdg("XDG_DOWNLOAD_DIR", "Downloads")
    }
}

/// Everything the app persists, resolved once at boot.
#[derive(Clone)]
pub struct Dirs {
    pub data: PathBuf,
    pub pgdata: PathBuf,
    pub media: PathBuf,
    pub secrets: PathBuf,
    pub logs: PathBuf,
}

pub fn ensure_dirs() -> std::io::Result<Dirs> {
    let data = data_dir();
    let dirs = Dirs {
        pgdata: data.join("pgdata"),
        media: data.join("media"),
        secrets: data.join("secrets"),
        logs: log_dir(),
        data,
    };
    for d in [&dirs.data, &dirs.pgdata, &dirs.media, &dirs.secrets, &dirs.logs] {
        std::fs::create_dir_all(d)?;
    }
    // pgdata must be 0700 or initdb refuses to run in it.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dirs.pgdata, std::fs::Permissions::from_mode(0o700))?;
        std::fs::set_permissions(&dirs.secrets, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(dirs)
}

/// Root of the bundled runtimes. `bundle.resources = ["resources/*"]` preserves
/// the relative path, so everything lands under `<resource_dir>/resources/`.
/// Falls back to the source-tree `resources/` in dev (`cargo tauri dev`/`run`).
pub fn resource_root(resource_dir: &std::path::Path) -> PathBuf {
    let nested = resource_dir.join("resources");
    if nested.is_dir() {
        nested
    } else {
        resource_dir.to_path_buf()
    }
}
