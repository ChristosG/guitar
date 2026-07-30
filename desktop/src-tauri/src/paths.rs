//! Filesystem layout. Data and logs live OUTSIDE the app bundle so a
//! drag-replace update never touches them. Also the shell's own log file —
//! everything the tutor's helper needs to ask about later has to survive in a
//! file, not on a stderr nobody can see.

use std::io::Write;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

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

// ---- the shell's own log ----------------------------------------------------

/// Append one timestamped line to `<logs>/app.log`, mirrored to stderr.
///
/// stderr ALONE is not enough and that is the whole point of this function: a
/// `.app` double-clicked in Finder has no terminal attached, so `eprintln!`
/// goes to a system log the tutor will never open — and Backend ▸ Show Logs
/// opens the log DIRECTORY. Anything support might have to ask about after the
/// fact — above all which ports this launch actually chose — has to land in a
/// file inside that directory.
///
/// Deliberately infallible: logging must never be the reason a boot fails.
pub fn app_log(msg: &str) {
    eprintln!("{msg}");
    let dir = log_dir();
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("app.log"))
    {
        let _ = writeln!(f, "{} {msg}", utc_stamp(SystemTime::now()));
    }
}

/// `YYYY-MM-DD HH:MM:SSZ`. A log line without a date cannot be matched against
/// "it broke on Tuesday", and pulling in a date crate for one prefix is not
/// worth the dependency.
fn utc_stamp(now: SystemTime) -> String {
    let secs = now
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (y, m, d) = civil_from_days(secs.div_euclid(86_400));
    let tod = secs.rem_euclid(86_400);
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02}Z",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    )
}

/// `YYYYMMDDTHHMMSSZ` — the stamp that goes into the NAME of anything this app
/// sets aside instead of destroying (`guitar_superseded_…`,
/// `pgdata_superseded_…`).
///
/// Two properties are load-bearing, and both come from the format rather than
/// from any code that reads it:
///
///   * it is a legal SQL identifier tail and a legal filename on both platforms
///     — no colons, no spaces, no hyphens, so a set-aside database never needs
///     anything but plain double quotes;
///   * it is FIXED WIDTH and big-endian, so lexicographic order IS chronological
///     order. The reaper therefore sorts names as strings and compares them
///     against a cutoff stamp, and never has to parse a date back out of a
///     directory listing it did not write.
pub fn utc_compact_stamp(now: SystemTime) -> String {
    let secs = now
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (y, m, d) = civil_from_days(secs.div_euclid(86_400));
    let tod = secs.rem_euclid(86_400);
    format!(
        "{y:04}{m:02}{d:02}T{:02}{:02}{:02}Z",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    )
}

/// Days since 1970-01-01 → (year, month, day) UTC. Howard Hinnant's
/// `civil_from_days`; integer division truncates toward zero in Rust exactly as
/// it does in the C++ original, so the published algorithm transfers verbatim.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }) / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn at(epoch: u64) -> String {
        utc_stamp(UNIX_EPOCH + Duration::from_secs(epoch))
    }

    /// A wrong log timestamp is worse than none — pin the epoch, a leap day and
    /// a recent date against `date -u`.
    #[test]
    fn stamps_match_the_gregorian_calendar() {
        assert_eq!(at(0), "1970-01-01 00:00:00Z");
        assert_eq!(at(951_782_400), "2000-02-29 00:00:00Z"); // leap year, /400 rule
        assert_eq!(at(1_753_834_800), "2025-07-30 00:20:00Z");
    }

    fn compact(epoch: u64) -> String {
        utc_compact_stamp(UNIX_EPOCH + Duration::from_secs(epoch))
    }

    /// The set-aside stamp. Its shape is not cosmetic: the reaper decides what
    /// it may DELETE by sorting these strings and comparing them against a
    /// cutoff, so "lexicographic order is chronological order" has to be true
    /// for every pair, across a minute, a day, a month and a year boundary. Get
    /// this wrong and the reaper picks the wrong database to remove.
    #[test]
    fn compact_stamps_are_fixed_width_and_sort_chronologically() {
        assert_eq!(compact(0), "19700101T000000Z");
        assert_eq!(compact(951_782_400), "20000229T000000Z");
        assert_eq!(compact(1_753_834_800), "20250730T002000Z");

        let moments = [
            0u64,
            59,
            60,
            3_599,
            3_600,
            86_399,
            86_400,
            951_782_399,
            951_782_400,
            1_753_834_800,
            1_753_921_199,
            1_767_225_599, // 2025-12-31T23:59:59Z
            1_767_225_600, // 2026-01-01T00:00:00Z
        ];
        for pair in moments.windows(2) {
            let (a, b) = (compact(pair[0]), compact(pair[1]));
            assert_eq!(a.len(), 16, "fixed width is what makes the compare valid: {a}");
            assert!(a < b, "{a} must sort before {b}");
        }
    }

    /// Nothing in the stamp may need quoting or escaping — it is pasted into a
    /// SQL identifier and into a filename.
    #[test]
    fn compact_stamps_are_safe_in_identifiers_and_filenames() {
        let s = compact(1_753_834_800);
        assert!(
            s.chars().all(|c| c.is_ascii_alphanumeric()),
            "no separators, no punctuation: {s}"
        );
    }
}
