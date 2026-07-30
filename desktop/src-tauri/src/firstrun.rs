//! First-run provisioning: secrets, port choice, initdb (with the Greek
//! collation guard), database creation and optional seed restore.

use std::fs;
use std::io::Write;
use std::net::{Ipv4Addr, Ipv6Addr, TcpListener};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::paths::{app_log, utc_compact_stamp, Dirs};
use crate::supervisor::pg_command;

/// Preferred app ports. NOT frozen: if either is taken the scan below rolls
/// forward, exactly like the postgres port already does.
///
/// What IS still frozen is the ORIGIN the webview loads — literal `localhost`,
/// never 127.0.0.1, never tauri://. The session cookie is host-only and the web
/// app falls back to deriving its API base from that origin. Only the PORT
/// floats, and because the derived pair can no longer be guessed from the
/// origin, the shell TELLS the page the API base
/// (`main.rs::show_main_window` → `window.__GT_API_BASE__`) and tells the API
/// child which browser origin to trust (`CORS_ORIGINS`, set in
/// `supervisor::api_env`).
pub const WEB_PORT_DEFAULT: u16 = 8790;
pub const API_PORT_DEFAULT: u16 = 8791;

/// The app's whole port window is `8790..=8829`. BOTH ports come out of this
/// one range, with the api strictly above the web port — deliberately NOT a
/// fixed sub-range anchored to whichever web port won.
///
/// That distinction is the difference between a scan that can give up while
/// free ports remain and one that cannot: because an api candidate may run all
/// the way to the top of the window, "the api window is exhausted" implies
/// "every port above this web candidate is busy", which implies every LATER web
/// candidate is busy too. So the scan gives up only when the window genuinely
/// cannot seat two ports.
const APP_PORT_LAST: u16 = 8829;

/// The pair of app ports chosen for this run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AppPorts {
    pub web: u16,
    pub api: u16,
}

impl AppPorts {
    /// Did the scan get the historical 8790/8791 pair, or did it have to roll?
    pub fn are_defaults(&self) -> bool {
        self.web == WEB_PORT_DEFAULT && self.api == API_PORT_DEFAULT
    }

    /// Could this pair have come out of `pick_app_ports`? Guards the values
    /// read back from `meta.json`, which is a file on the tutor's disk and not
    /// a trusted input: a hand-edited or truncated one must fall back to a
    /// scan, not push the app onto some arbitrary port.
    fn plausible(&self) -> bool {
        self.api > self.web && self.web >= WEB_PORT_DEFAULT && self.api <= APP_PORT_LAST
    }

    /// The origin the main window loads. LITERAL `localhost` — load-bearing.
    pub fn web_origin(&self) -> String {
        format!("http://localhost:{}", self.web)
    }

    /// What the page is told to call. Literal `localhost` for the same reason
    /// (this is the value the origin-derivation used to produce by itself).
    pub fn api_base(&self) -> String {
        format!("http://localhost:{}", self.api)
    }

    /// `CORS_ORIGINS` for the API child. The browser enforces CORS between the
    /// web port and the api port even with auth off, and the API sets
    /// `allow_credentials=True`, so a wildcard is not an option. The 127.0.0.1
    /// variant is not redundant: origins are compared as strings.
    pub fn cors_origins(&self) -> String {
        format!("http://localhost:{w},http://127.0.0.1:{w}", w = self.web)
    }
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Secrets {
    pub app_secret: String,
    pub encryption_secret: String,
}

#[derive(Serialize, Deserialize)]
struct Meta {
    app_version: String,
    pg_major: u32,
    pg_port: u16,
    /// The app ports this install used last. `Option` because meta.json files
    /// written by earlier builds do not carry them.
    #[serde(default)]
    web_port: Option<u16>,
    #[serde(default)]
    api_port: Option<u16>,
}

// ---- durable writes ---------------------------------------------------------
//
// `fs::write` truncates the target in place and returns as soon as the kernel
// has the bytes in page cache. Neither half of that is acceptable for a file a
// LATER LAUNCH consults before deciding it may move the tutor's database out of
// the way:
//
//   * truncate-in-place means a crash mid-write leaves a zero-length or
//     half-written file — which `disk_state` reads as `Unmarked`, i.e. "no
//     information";
//   * returning before the bytes are on the medium means a POWER LOSS after
//     `finalize_install` returned Ok can leave the OLD `seeding` content on
//     disk. The app has by then shown the tutor a window; he types real
//     curricula into a database the next launch is entitled to displace.
//
// That second one is the whole reason this section exists. It is not
// theoretical: it is the exact sequence "boot finishes → marker believed
// complete → power cut → next launch reads seeding".

/// Replace `path` with `bytes` so that a crash or a power loss can only ever
/// leave EITHER the old file or the new one, and so that `Ok(())` means the new
/// one is on the medium.
///
/// The sequence, and why every step is there:
///   1. write a temp file IN THE SAME DIRECTORY — `rename` is only atomic
///      within one filesystem;
///   2. `sync_all` — the bytes and the inode are on the medium;
///   3. `rename` — atomic replacement, so no reader ever sees a partial file;
///   4. fsync the DIRECTORY — the rename is a metadata change of the DIRECTORY,
///      and without this step the directory entry can still be lost even though
///      the file's own bytes were made durable. This is the step that is easy
///      to forget and the one the whole guarantee actually rests on.
///
/// Steps 1–3 are hard errors. Step 4 is logged rather than fatal: a few
/// filesystems reject `fsync` on a directory descriptor outright, and turning a
/// durability upgrade into a refusal to boot would trade a rare risk for a
/// certain one. What is lost in that case is only the rename's own durability —
/// the bytes are already down, and the previous file is still intact.
///
/// The temp file is removed on every failure path, so a full disk cannot leave
/// a drift of `.install-state.json.tmp*` behind.
fn write_durably(path: &Path, bytes: &[u8], mode: Option<u32>) -> Result<(), String> {
    let dir = path
        .parent()
        .ok_or_else(|| format!("{} has no parent directory", path.display()))?;
    let stem = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "state".to_string());
    // Same directory (rename must not cross a filesystem), dot-prefixed so a
    // stray one is not mistaken for content, pid-tagged so two processes cannot
    // scribble over each other's temp file.
    let tmp = dir.join(format!(".{stem}.tmp{}", std::process::id()));

    let written = (|| -> std::io::Result<()> {
        let mut f = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&tmp)?;
        #[cfg(unix)]
        if let Some(m) = mode {
            use std::os::unix::fs::PermissionsExt;
            f.set_permissions(fs::Permissions::from_mode(m))?;
        }
        #[cfg(not(unix))]
        let _ = mode;
        f.write_all(bytes)?;
        f.sync_all()
    })();
    if let Err(e) = written {
        let _ = fs::remove_file(&tmp);
        return Err(format!("cannot write {}: {e}", tmp.display()));
    }
    if let Err(e) = fs::rename(&tmp, path) {
        let _ = fs::remove_file(&tmp);
        return Err(format!("cannot replace {}: {e}", path.display()));
    }
    if let Err(e) = fs::File::open(dir).and_then(|d| d.sync_all()) {
        app_log(&format!(
            "wrote {} and synced its contents, but could not fsync the directory \
             ({e}) — the new bytes are on the medium; only the durability of the \
             rename itself is unproven on this filesystem",
            path.display()
        ));
    }
    Ok(())
}

fn random_hex64() -> String {
    let mut buf = [0u8; 32];
    getrandom::getrandom(&mut buf).expect("OS RNG unavailable");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// Why `load_or_create_secrets` could not hand back a `Secrets`.
///
/// TWO variants because the two failures need OPPOSITE advice, and collapsing
/// them into one `String` is what let a full disk on first run reach the tutor
/// as a raw English filesystem error. `Existing` must never suggest freeing disk
/// space (nothing about space is wrong, and the fix is to restore a file);
/// `NotWritable` must, because that is exactly what is wrong. `main` picks the
/// dialog off the variant, so the distinction cannot be lost by a later edit.
#[derive(Debug)]
pub enum SecretsError {
    /// `secrets.json` is on disk and cannot be read or parsed. NOT a disk-space
    /// problem, and never a reason to overwrite the file.
    Existing(String),
    /// There is no `secrets.json` and one could not be created — a full disk, a
    /// read-only data folder, a permissions problem.
    NotWritable(String),
}

impl SecretsError {
    /// The technical half, for the log and for the tail of the dialog. The
    /// bilingual half is chosen by `main` off the VARIANT, never off this text.
    pub fn detail(&self) -> &str {
        match self {
            SecretsError::Existing(d) | SecretsError::NotWritable(d) => d,
        }
    }
}

impl std::fmt::Display for SecretsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.detail())
    }
}

/// Create `secrets/secrets.json` (chmod 600) ONLY if absent. NEVER regenerate:
/// losing ENCRYPTION_SECRET bricks the tutor's stored Anthropic key, so an
/// unreadable existing file is a hard error, not a rewrite.
pub fn load_or_create_secrets(secrets_dir: &Path) -> Result<Secrets, SecretsError> {
    let path = secrets_dir.join("secrets.json");
    if path.exists() {
        let raw = fs::read_to_string(&path)
            .map_err(|e| SecretsError::Existing(format!("cannot read {}: {e}", path.display())))?;
        return serde_json::from_str(&raw).map_err(|e| {
            SecretsError::Existing(format!(
                "{} exists but cannot be parsed ({e}). Refusing to overwrite it — \
                 it protects the stored Anthropic key. Fix or restore the file.",
                path.display()
            ))
        });
    }
    let secrets = Secrets {
        app_secret: random_hex64(),
        encryption_secret: random_hex64(),
    };
    let body = serde_json::to_string_pretty(&secrets).expect("serialize secrets");
    // DURABLE, and atomic, for the same reason the install marker is — but with
    // a different failure in mind. This file is written once and never again:
    // the reader above treats an existing-but-unparseable secrets.json as a HARD
    // ERROR rather than regenerating it, because regenerating ENCRYPTION_SECRET
    // silently bricks the tutor's stored Anthropic key. A torn `fs::write` here
    // — a power loss in the instant between truncate and flush — therefore did
    // not just lose a file, it produced an install that refuses to boot forever
    // and cannot be repaired without deleting something the app itself calls
    // irreplaceable. 0600 is set on the temp file BEFORE the rename, so the
    // secret is never briefly world-readable.
    write_durably(&path, body.as_bytes(), Some(0o600)).map_err(SecretsError::NotWritable)?;
    Ok(secrets)
}

/// Bind-probe on BOTH loopback families.
///
/// 127.0.0.1 alone is not enough and the difference is not academic: the
/// webview connects to literal `localhost`, and on macOS that resolves `::1`
/// FIRST. A process listening only on `[::1]:8790` would read as "free" on the
/// v4 probe, we would hand 8790 to node — which binds v4 — and the tutor's
/// browser would still reach the other program. Requiring both families to
/// bind is the only probe that matches what the browser will actually do.
///
/// A FAILED v6 probe is only evidence of a listener when it failed with
/// `AddrInUse`. `AddrNotAvailable` means no `::1` is configured and
/// `Unsupported` (EAFNOSUPPORT) means IPv6 is disabled outright — on those
/// machines nothing can possibly be listening on the v6 loopback, and reading
/// them as "busy" would reject every port in the window and refuse to boot the
/// app at all. See `v6_probe_proves_busy`.
///
/// The listeners are dropped, and the sockets closed, before this returns: a
/// probed port must be handed to the child immediately, never held open across
/// the spawn. `std`'s `TcpListener` sets `SO_REUSEADDR` on unix, so a port
/// whose last connections are still in TIME_WAIT correctly reads as free.
pub fn port_free(port: u16) -> bool {
    if TcpListener::bind((Ipv4Addr::LOCALHOST, port)).is_err() {
        return false;
    }
    match TcpListener::bind((Ipv6Addr::LOCALHOST, port)) {
        Ok(_) => true,
        Err(e) => !v6_probe_proves_busy(&e),
    }
}

/// Does a v6 bind failure prove someone is listening on `[::1]:port`?
/// Only `EADDRINUSE` does. Split out so the classification is testable without
/// a machine that has IPv6 turned off.
fn v6_probe_proves_busy(err: &std::io::Error) -> bool {
    err.kind() == std::io::ErrorKind::AddrInUse
}

/// Pick the web and api ports, the same way `pick_pg_port` picks the database
/// port. The api always lands strictly ABOVE the web port, so the two can never
/// be the same number, and `pg_port` is excluded from both — today the ranges
/// cannot overlap numerically, and this keeps that true if a range is ever
/// moved.
///
/// The web candidate is the OUTER loop and the api candidate scans everything
/// above it up to `APP_PORT_LAST`: a busy web port simply moves the outer loop
/// on, and a band of busy ports above a free web port cannot end the scan while
/// two ports remain seatable. (A blocked-off band is exactly the shape a
/// corporate agent or another Electron app leaves behind, so this is not
/// hypothetical.)
///
/// `remembered` is last launch's pair, preferred whenever it is still free —
/// see `remembered_app_ports` for why that matters.
pub fn pick_app_ports(pg_port: u16, remembered: Option<AppPorts>) -> Option<AppPorts> {
    pick_app_ports_with(pg_port, remembered, &port_free)
}

/// The scan itself, with the probe injected so the invariants above can be
/// tested against a fixed "these are busy" set instead of against whatever this
/// machine happens to have bound.
fn pick_app_ports_with(
    pg_port: u16,
    remembered: Option<AppPorts>,
    is_free: &dyn Fn(u16) -> bool,
) -> Option<AppPorts> {
    if let Some(p) = remembered {
        if p.plausible() && p.web != pg_port && p.api != pg_port && is_free(p.web) && is_free(p.api)
        {
            return Some(p);
        }
    }
    for web in WEB_PORT_DEFAULT..APP_PORT_LAST {
        if web == pg_port || !is_free(web) {
            continue;
        }
        for api in (web + 1)..=APP_PORT_LAST {
            if api == pg_port || !is_free(api) {
                continue;
            }
            return Some(AppPorts { web, api });
        }
    }
    None
}

/// Postgres port: prefer 5434, scan up to 5444. The chosen port is passed to
/// `pg_ctl -o "-p …"` at every start, so it may differ between runs.
pub fn pick_pg_port() -> Option<u16> {
    (5434..=5444).find(|p| port_free(*p))
}

/// Is there a cluster on disk at all? NOT a freshness test on its own — see
/// `disk_state`, which is the only thing boot is allowed to branch on.
pub fn cluster_exists(dirs: &Dirs) -> bool {
    dirs.pgdata.join("PG_VERSION").exists()
}

// ---- first-run state machine ------------------------------------------------
//
// "Is this a first run?" used to be `!cluster_exists()`, and answering it that
// way loses the tutor's LIBRARY — the one outcome this product cannot have.
// `initdb` writes `PG_VERSION` within seconds of the very first launch, while
// the Greek-collation guard, `createdb`, a ~250MB `pg_restore` and
// `alembic upgrade head` all run AFTER it and together take minutes. Quit the
// app in that window — or let the machine sleep, or the battery die — and the
// next launch saw PG_VERSION, decided the install was established, and skipped
// seeding forever: an app with no library and no error to explain it.
//
// Freshness therefore means "the first run COMPLETED", written down explicitly
// in `<data>/install-state.json` only after the restore AND the migrations have
// succeeded, and written DURABLY (temp file → fsync → rename → fsync the
// directory) because a marker a power loss can roll back to `seeding` is a
// marker that arms the resume path against a database the tutor has since
// filled. An unfinished first run is RESUMED, not skipped — and resumed
// idempotently, by getting whatever fragment is there out of the way and
// restoring again, so a half-restored database ends up correct rather than
// doubled. "Out of the way" means RENAMED ASIDE for anything that holds data;
// see the displacement section below.
//
// The one case that must never be mistaken for an interrupted run is an install
// created by a build from BEFORE this marker existed: complete, full of the
// tutor's work, and unmarked. `DiskState::Unmarked` covers both, and only the
// running cluster can separate them — hence the probe below. The rule the whole
// machine is built around: NEVER remove a database that holds data. Not on a
// guess, and — since the displacement section below — not on a certainty
// either: when the marker says we were part-way through creating this install,
// the database is RENAMED out of the way instead.
//
// The marker still has to be right, because a wrong one still costs the tutor a
// confusing morning and a support call. That rule is only SOUND while
// `phase == "seeding"` genuinely implies "no boot has ever finished on this
// install" — i.e. the tutor has never had a window to type into. The moment a
// boot can complete with the marker still reading `seeding`, the next launch is
// entitled to move a database full of his work.
// So the marker carries a second, equally load-bearing invariant, enforced by
// `finalize_install` and the `InstallComplete` receipt it hands back:
//
//     IF THE MAIN WINDOW CAN APPEAR, THE MARKER READS `complete`.
//
// `finalize_install` is the only function that writes `complete`, it matches
// EXHAUSTIVELY on `SeedPlan` (so a variant added later cannot quietly inherit
// "do nothing"), and `main::show_main_window` takes its receipt by reference —
// which makes the two halves inseparable at compile time rather than by
// convention.

const INSTALL_STATE_FILE: &str = "install-state.json";
const PHASE_SEEDING: &str = "seeding";
const PHASE_COMPLETE: &str = "complete";

#[derive(Serialize, Deserialize)]
struct InstallState {
    /// `seeding` or `complete`. The ONLY field anything branches on.
    phase: String,
    app_version: String,
    /// Unix seconds — forensics for whoever reads this file over the phone.
    at: u64,
    /// Free text saying, in English, what happened. Never parsed.
    note: String,
    /// Everything this install has RENAMED OUT OF THE WAY rather than
    /// destroyed. Nothing branches on it — it exists so that a set-aside
    /// database is discoverable from the one file support already asks for.
    /// `default` because markers written by earlier builds do not carry it.
    #[serde(default)]
    set_aside: Vec<SetAside>,
}

/// What this app can HONESTLY say about the OTHER half of a displacement.
///
/// A database and the media tree it indexes are one recoverable unit, so the
/// note has to name the pair or it is telling whoever reads it where to find
/// half of the tutor's work. What it must never do is name a pairing that did
/// not happen — and that was not hypothetical. This used to be a plain
/// `Option<String>`, and `displace_media` was handed the name the database was
/// ABOUT to be renamed to and recorded it BEFORE `ALTER DATABASE` had run. That
/// rename returns Err whenever anything at all is still connected to `guitar`
/// (an autovacuum worker suffices; the `pg_terminate_backend` before it is
/// best-effort and racy), and on Err the boot is fatal with the media already
/// moved. Two lies came out of that one window:
///
///   * the media entry claimed a companion database that does not exist and
///     never did; and
///   * the next launch found `<data>/media` empty, set the database aside with
///     no companion, and the note said "no media folder was set aside with this
///     one — the media folder was empty at the time" — about a database whose
///     entire library was sitting in the `media_superseded_*` folder the first
///     entry had already mis-described.
///
/// Three states, because there really are three. Collapsing the last two into
/// one `None` is precisely what let a measurement ("there was nothing to move")
/// be printed in place of an ignorance ("we never found out").
#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq, Eq)]
enum Companion {
    /// Named, and BOTH renames really happened. Written by `reconcile_pairing`
    /// after the second of the two returned Ok, or by `announce_set_aside` for
    /// the half that is recorded last — never from what a rename was going to
    /// be called.
    #[serde(rename = "goes-with")]
    Is(String),
    /// There is no other half, and that is a MEASUREMENT rather than an
    /// assumption: `<data>/media` held nothing to move, or the database these
    /// files belonged to was already gone before they were found.
    #[serde(rename = "nothing-to-pair")]
    Alone,
    /// There is no other half either, and the reason is a DIFFERENT measurement
    /// — one `Alone`'s own sentence gets wrong.
    ///
    /// `Alone` prints "the database these files belonged to was already gone
    /// when they were found". That is true of a database that was `Absent`, and
    /// FALSE of one that was measured to hold zero user relations and dropped a
    /// moment later: that database was there, we looked inside it, and we
    /// removed it because it was empty. Same absence, different fact, and the
    /// note prints the fact. Distinguishing them costs one variant and stops the
    /// file asserting something nobody measured — which is the whole reason
    /// `Alone` and `Unknown` are already two things rather than one `None`.
    #[serde(rename = "nothing-to-pair-empty-database")]
    AloneEmptyDatabase,
    /// The other half has not happened — yet, or ever. Every media displacement
    /// is recorded as this at the instant of its rename, and stays that way
    /// unless a later step proves otherwise. Also what a marker written by an
    /// earlier build reads as, which is the only honest reading of a field
    /// those builds never wrote.
    #[default]
    #[serde(rename = "undetermined")]
    Unknown,
}

/// One thing displaced instead of destroyed.
#[derive(Serialize, Deserialize, Clone, Debug)]
struct SetAside {
    /// `database`, `directory`, `media`, `file` or `quarantine` — plain words,
    /// because a human reads this.
    kind: String,
    /// What it is called NOW. This is the handle for getting it back.
    name: String,
    /// The OTHER half of the same displacement. A database and its media are
    /// never restored apart, so the pairing has to survive in the one file
    /// support already asks for — not only in the shared name suffix, which a
    /// rename by hand can break.
    ///
    /// DELIBERATELY NOT NAMED `companion`, and that rename is load-bearing: a
    /// marker written by an earlier build carries a `companion` key whose
    /// meaning ("the name we were about to use") this build cannot stand
    /// behind. serde ignores the unknown key and `default` makes this
    /// `Undetermined`, which is exactly what such a record is worth.
    #[serde(default)]
    pairing: Companion,
    at: u64,
    why: String,
    /// Has the tutor been TOLD about this one? A set-aside he never hears about
    /// is no better than a deletion, and the one boot most likely to displace
    /// something is also the one most likely to die before it can show a
    /// window. So the announcement is a durable to-do, drained by the next boot
    /// that gets far enough to put a window on screen — see `unannounced`.
    /// `default` (false) for markers from earlier builds, which announces their
    /// contents once. That is the right side to err on.
    #[serde(default)]
    announced: bool,
    /// Unix seconds at which the reaper removed this, if it ever did.
    ///
    /// WITHOUT THIS THE MARKER OUTLIVES WHAT IT DESCRIBES. Entries are never
    /// deleted from this file — that is deliberate, it is the ledger — but the
    /// reaper CAN delete the thing an entry is about, and until now it left the
    /// entry standing with nothing to say so. Whoever reads the marker then
    /// follows a recovery note describing how to rename back a database that was
    /// removed a month ago, and concludes that something has gone wrong when
    /// nothing has. The note file records the removal in a line of its own
    /// (`append_reap_note`); this is the same fact in the file that is parsed.
    ///
    /// Also stops `unannounced` telling the tutor about a set-aside that no
    /// longer exists — the one announcement that could not possibly help him.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reaped_at: Option<u64>,
}

/// What the DISK says about this install, before any postgres is running.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DiskState {
    /// No cluster: `initdb` has not run, or its pgdata was wiped.
    NoCluster,
    /// A cluster, and the marker says the first run finished. The ordinary
    /// second-and-later launch.
    Complete,
    /// A cluster, and the marker says a seed restore was IN PROGRESS when the
    /// app last stopped. Whatever is in the database is a fragment.
    SeedInterrupted,
    /// A cluster with no usable marker. Two very different installs look
    /// identical from here, and only the cluster itself can tell them apart:
    ///   * one written by a build that predates this marker — complete, and
    ///     holding everything the tutor has ever put in;
    ///   * a first run of THIS build interrupted in the seconds between initdb
    ///     and the start of the restore.
    Unmarked,
}

/// What the RUNNING cluster says about the `guitar` database.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DbProbe {
    /// No `guitar` database in this cluster.
    Absent,
    /// It exists but holds no tables of ours — nothing was ever restored into
    /// it, or the restore died before it created anything.
    Empty,
    /// It exists and holds tables: there is data here, and it is not ours to
    /// throw away on a guess.
    Full,
    /// The cluster could not be asked.
    Unknown,
}

/// What boot must actually do about seeding.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SeedPlan {
    /// Completed install: migrate and go. The overwhelmingly common answer.
    Skip,
    /// Create the database and restore the seed. Nothing to throw away.
    Seed,
    /// Same, but get the existing `guitar` database out of the way first: it
    /// holds the fragment of an interrupted restore, and running `pg_restore`
    /// on top of that would duplicate the rows that did land rather than
    /// replace them. "Out of the way" is a RENAME whenever the database holds
    /// anything at all, and a drop only when it is measured to hold nothing —
    /// see `displace_database`.
    Reseed,
    /// A cluster with no marker whose database already holds data: an install
    /// from a build that predates the marker. Record that it is complete and
    /// touch NOTHING else.
    Adopt,
    /// The cluster could not be asked. Change NOTHING about the database — but
    /// the boot carries on, and if it finishes, `finalize_install` still writes
    /// `complete` like every other variant. (`alembic` runs either way and
    /// fails loudly if the database really is missing, so this cannot pass
    /// silently.)
    Undecided,
}

impl SeedPlan {
    /// Every variant, so the tests can sweep the whole space instead of the
    /// cases somebody remembered to list.
    ///
    /// Adding a variant without adding it here is caught two ways: the
    /// exhaustive match in `finalize_install` stops compiling, and
    /// `seed_plan_all_lists_every_variant` stops compiling.
    #[cfg(test)]
    pub const ALL: [SeedPlan; 5] = [
        SeedPlan::Skip,
        SeedPlan::Seed,
        SeedPlan::Reseed,
        SeedPlan::Adopt,
        SeedPlan::Undecided,
    ];
}

/// What the seed step actually DID on this boot — the one fact
/// `finalize_install` cannot work out from the plan alone, and only for the
/// wording of the note it writes down.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SeedOutcome {
    /// No seed step ran (`Skip`, `Adopt`, `Undecided`).
    NotAttempted,
    /// `pg_restore` put the bundled starter library in.
    Restored,
    /// The database was created, but this build carries no starter library.
    NothingToRestore,
}

/// Proof that `<data>/install-state.json` reads `complete`.
///
/// There is exactly one constructor — `finalize_install`, below, whose private
/// field nobody outside this module can name — and `main::show_main_window`
/// demands one. So "the boot finished" and "the marker is complete" cannot
/// drift apart: not by a new `SeedPlan` variant (the match that mints this is
/// exhaustive), not by a new early return (there is nothing else to pass), and
/// not by a write that silently failed (the receipt is minted from a re-READ of
/// the file, not from the write's return value).
#[must_use = "the receipt is the proof that the install marker reads `complete`"]
#[derive(Debug)]
pub struct InstallComplete(());

fn install_state_path(dirs: &Dirs) -> PathBuf {
    dirs.data.join(INSTALL_STATE_FILE)
}

fn read_state(dirs: &Dirs) -> Option<InstallState> {
    let raw = fs::read_to_string(install_state_path(dirs)).ok()?;
    serde_json::from_str(&raw).ok()
}

fn read_phase(dirs: &Dirs) -> Option<String> {
    read_state(dirs).map(|s| s.phase)
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn save_state(dirs: &Dirs, state: &InstallState) -> Result<(), String> {
    let body = serde_json::to_string_pretty(state).expect("serialize install state");
    // DURABLY. This file is the sole authority for the one path that moves the
    // tutor's database, and a plain `fs::write` can leave the previous `seeding`
    // bytes on disk after this function has returned Ok — see `write_durably`.
    write_durably(&install_state_path(dirs), body.as_bytes(), Some(0o600))
}

fn write_phase(dirs: &Dirs, phase: &str, note: &str) -> Result<(), String> {
    // The set-aside list is carried forward, never rewritten: it is the record
    // of what is recoverable and where, and a phase change must not erase it.
    let set_aside = read_state(dirs).map(|s| s.set_aside).unwrap_or_default();
    let state = InstallState {
        phase: phase.to_string(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
        at: now_secs(),
        note: note.to_string(),
        set_aside,
    };
    save_state(dirs, &state)
}

/// Claim the first run BEFORE anything is restored. Its caller treats a failure
/// here as fatal, and that is deliberate: an unrecorded restore that then gets
/// interrupted looks, next launch, exactly like a finished install — which is
/// the half-library this whole marker exists to prevent.
pub fn mark_seeding(dirs: &Dirs) -> Result<(), String> {
    write_phase(
        dirs,
        PHASE_SEEDING,
        "seed restore started; if this is still the phase at the next launch the \
         restore was interrupted, and it will be redone from scratch",
    )
}

/// The install is finished: seed restored (or absent by design) AND migrations
/// applied. PRIVATE on purpose — `finalize_install` is the only caller, so
/// "who may declare an install complete" has exactly one answer.
fn mark_complete(dirs: &Dirs, note: &str) -> Result<(), String> {
    write_phase(dirs, PHASE_COMPLETE, note)
}

/// The single place a boot is declared finished. Call it once, after the
/// migrations, on EVERY path that can go on to show the main window.
///
/// Why every path, including `Skip` (where the marker already says `complete`)
/// and `Undecided` (where we could not even ask the cluster what it holds):
/// the invariant that protects the tutor's library is not "the seed finished",
/// it is "`seeding` implies no boot has ever finished here". `Undecided` used
/// to break it outright — a boot could complete, the tutor could work all day,
/// and the marker would still read `seeding`; the NEXT launch would classify
/// that as an interrupted restore and DROP the database, work and all. Writing
/// `complete` on every completed boot is what makes the `Reseed` cells below
/// defensible.
///
/// The price on the `Undecided` path is real and worth stating: if the probe
/// failed while the database genuinely held an interrupted restore's fragment,
/// this forfeits the automatic re-seed and the tutor gets a partial starter
/// library. That is recoverable (re-import, or restore a backup). When two
/// risks cannot both be avoided, this code always spends the recoverable one —
/// the same principle the displacement section applies to the database itself.
///
/// Note what has ALREADY had to succeed before any caller gets here: `alembic
/// upgrade head` ran against this database. A missing or unusable `guitar`
/// database is therefore a fatal boot, not a completed one — so `Undecided` can
/// only reach this function with a database that at least exists and takes
/// migrations.
pub fn finalize_install(
    dirs: &Dirs,
    plan: SeedPlan,
    outcome: SeedOutcome,
) -> Result<InstallComplete, String> {
    // EXHAUSTIVE by design: a `SeedPlan` variant added later cannot reach the
    // main window without someone deciding, here, what its note says — because
    // this stops compiling until they do.
    let note = match plan {
        SeedPlan::Skip => {
            "an established install: nothing to seed, schema brought up to date".to_string()
        }
        SeedPlan::Adopt => "adopted: a cluster from a build that predates this marker, with \
             data already in it"
            .to_string(),
        SeedPlan::Seed | SeedPlan::Reseed => match outcome {
            SeedOutcome::Restored => "first run: starter library restored".to_string(),
            SeedOutcome::NothingToRestore => {
                "first run: this build carried no starter library".to_string()
            }
            // Unreachable through `main`, which always reports what the seed
            // did. Recorded rather than refused: the boot finished either way,
            // and leaving `seeding` behind is the one outcome to avoid.
            SeedOutcome::NotAttempted => "first run finished, but the seed step reported no \
                 outcome — recorded complete because the boot completed"
                .to_string(),
        },
        SeedPlan::Undecided => "the cluster could not be asked what the guitar database held, \
             so nothing was seeded and nothing was dropped — but this boot got as far as \
             applying the migrations, and an install that reaches that point must never read \
             `seeding` again: the next launch would take it for an interrupted restore"
            .to_string(),
    };

    let wrote = mark_complete(dirs, &note);
    // The receipt is minted from what the FILE says, never from what the write
    // returned. Two things fall out of that, both of them the behaviour we want:
    //
    //   * an established install whose data folder has gone read-only or full
    //     still boots — the marker already reads `complete`, so the failed
    //     refresh changes nothing and is logged, not fatal (the same reasoning
    //     as `write_meta`, and the reason this is not simply `mark_complete(..)?`);
    //   * a write that "succeeded" onto a disk that then dropped it, or onto a
    //     file something else rewrote, does NOT produce a receipt.
    match read_phase(dirs).as_deref() {
        Some(PHASE_COMPLETE) => {
            if let Err(e) = wrote {
                app_log(&format!(
                    "could not refresh the install marker ({e}) — harmless: it already \
                     records this install as complete"
                ));
            }
            Ok(InstallComplete(()))
        }
        other => Err(match wrote {
            Err(e) => e,
            Ok(()) => format!(
                "wrote the completion marker to {} but it reads back as {other:?}",
                install_state_path(dirs).display()
            ),
        }),
    }
}

/// The disk half of the decision — no processes, no sockets, no cluster needed.
pub fn disk_state(dirs: &Dirs) -> DiskState {
    if !cluster_exists(dirs) {
        return DiskState::NoCluster;
    }
    match read_phase(dirs).as_deref() {
        Some(PHASE_COMPLETE) => DiskState::Complete,
        Some(PHASE_SEEDING) => DiskState::SeedInterrupted,
        // Missing, unreadable, truncated or carrying a phase this build does
        // not know: none of that is evidence of anything. Fall through to the
        // probe, which never destroys data it cannot account for.
        _ => DiskState::Unmarked,
    }
}

/// The whole decision, pure, so the rule that protects the library can be
/// tested exhaustively without a cluster.
///
/// THE FULL TRUTH TABLE — {marker absent, seeding, complete} × {no cluster,
/// cluster without our db, cluster with an empty db, cluster with a populated
/// db}. Read the right-hand column as "what happens to the tutor's library".
/// (`marker` is the phase in `<data>/install-state.json`; "absent" also covers
/// truncated, unparseable, and a phase written by a build we do not know.)
///
/// ```text
/// marker    cluster                       plan       what happens to the data
/// --------  ----------------------------  ---------  ----------------------------------
/// absent    no cluster                    Seed       initdb, createdb, restore. Day one.
///                                                    Nothing exists yet.
/// absent    db missing                    Seed       createdb, restore. NOTHING REMOVED.
/// absent    db present, no relations      Reseed     DROPPED — and only because it was
///                                                    MEASURED empty: zero tables, views,
///                                                    matviews, foreign tables, sequences.
/// absent    db present, holds relations   Adopt      TOUCH NOTHING. Pre-marker install.
/// seeding   no cluster                    Seed       marker written before initdb; initdb
///                                                    then died. Nothing exists to lose.
/// seeding   db missing                    Seed       died between initdb and createdb.
/// seeding   db present, no relations      Reseed     DROPPED — measured empty, as above.
/// seeding   db present, holds relations   Reseed     RENAMED ASIDE to
///                                                    guitar_superseded_<stamp>, then a
///                                                    fresh one is built beside it.
///                                                    NOTHING IS DESTROYED.
/// complete  no cluster                    Seed       pgdata gone (moved/lost) — the
///                                                    marker alone is not a cluster.
/// complete  db missing                    Skip       probe not even run; alembic will
///                                                    fail loudly rather than us guessing.
/// complete  db present, no relations      Skip       ditto — never reseed a finished
///                                                    install behind the tutor's back.
/// complete  db present, holds relations   Skip       the overwhelmingly common launch.
/// ```
///
/// Plus the column the disk cannot produce: if the cluster is up but cannot be
/// ASKED (psql failed, unparseable answer), `absent`/`seeding` both give
/// `Undecided` — nothing is created, nothing is removed, nothing is renamed.
/// `complete` never asks.
///
/// THE ROW THAT USED TO BE THE DANGEROUS ONE — `seeding` + a populated database
/// — IS NO LONGER DESTRUCTIVE. It used to `DROP DATABASE guitar`, defended by
/// the argument that `seeding` implies "we were part-way through CREATING this
/// install, and no boot has ever finished here". That argument still holds (the
/// marker is written before `initdb` and again before the restore;
/// `finalize_install` writes `complete` on EVERY completed boot; and the marker
/// is now written durably, so a power loss cannot resurrect an old `seeding`).
/// It was retired as a LOAD-BEARING argument anyway, because it protects only
/// against the failures we thought of. The database is now renamed out of the
/// way, so every residual risk in this table — including the ones not yet
/// imagined — costs disk space instead of a library. See `displace_database`.
///
/// The two `Reseed`-on-EMPTY cells (rows 3 and 7) still drop, and that is not a
/// judgement about likelihood: the probe MEASURES zero user relations of any
/// kind immediately beforehand, and `displace_database` measures again before it
/// acts. `alembic` creates `alembic_version` on any install that has ever been
/// migrated, so an install with a history cannot present as empty.
///
/// And the cell that must never remove anything: row 4, an install from a build
/// older than the marker. Unmarked and populated is ADOPTED — recorded as
/// complete, otherwise untouched.
fn seed_plan(disk: DiskState, probe: DbProbe) -> SeedPlan {
    match (disk, probe) {
        // Nothing exists yet; there is nothing to protect and nothing to ask.
        (DiskState::NoCluster, _) => SeedPlan::Seed,
        // The marker is definitive.
        (DiskState::Complete, _) => SeedPlan::Skip,
        // Could not ask: do nothing at all rather than act on a guess.
        (_, DbProbe::Unknown) => SeedPlan::Undecided,
        // No database to lose either way.
        (_, DbProbe::Absent) => SeedPlan::Seed,
        // The marker says WE were mid-restore, so whatever is in there is our
        // own fragment — the one case where dropping is right.
        (DiskState::SeedInterrupted, _) => SeedPlan::Reseed,
        // Unmarked and empty: an initdb that never got as far as restoring.
        // Dropping an empty database costs nothing and keeps createdb happy.
        (DiskState::Unmarked, DbProbe::Empty) => SeedPlan::Reseed,
        // Unmarked and full: an install older than the marker. HANDS OFF.
        (DiskState::Unmarked, DbProbe::Full) => SeedPlan::Adopt,
    }
}

/// `seed_plan` with the probe actually run. Called once, with postgres up.
pub fn plan_first_run(res: &Path, dirs: &Dirs, port: u16, disk: DiskState) -> SeedPlan {
    let probe = match disk {
        // Neither branches on the probe (see `seed_plan`), and on a fresh
        // install there is no cluster to ask in the first place.
        DiskState::NoCluster | DiskState::Complete => DbProbe::Unknown,
        DiskState::SeedInterrupted | DiskState::Unmarked => probe_database(res, dirs, port),
    };
    seed_plan(disk, probe)
}

/// Does `guitar` exist, and does it hold anything?
fn probe_database(res: &Path, dirs: &Dirs, port: u16) -> DbProbe {
    match psql_scalar(
        res,
        dirs,
        port,
        "postgres",
        "SELECT count(*) FROM pg_database WHERE datname = 'guitar'",
    ) {
        Ok(n) if n == "0" => return DbProbe::Absent,
        Ok(n) if n == "1" => {}
        Ok(other) => {
            app_log(&format!(
                "install probe: unexpected answer '{other}' when asking whether the \
                 guitar database exists — changing nothing"
            ));
            return DbProbe::Unknown;
        }
        Err(e) => {
            app_log(&format!(
                "install probe: cannot ask the cluster whether the guitar database \
                 exists ({e}) — changing nothing"
            ));
            return DbProbe::Unknown;
        }
    }
    // ANY user object outside the system schemas means something was restored or
    // migrated into this database. That is the line this code refuses to cross,
    // and it is drawn deliberately wide: tables and partitioned tables
    // ('r','p') are what an install actually holds, but views, matviews,
    // foreign tables and sequences ('v','m','f','S') all mean somebody put
    // something here too — and so do user FUNCTIONS and LARGE OBJECTS, which a
    // `pg_restore` creates before it has created a single table (it restores in
    // dependency order: schemas, types and functions first). A restore killed in
    // that window leaves a database with real content and not one relation.
    //
    // Every term added here can only ever turn a `Drop` into a `Rename` and a
    // `Reseed` into an `Adopt` — i.e. widening this can only make this code
    // destroy LESS, never more. That asymmetry is why it is drawn wide.
    match psql_scalar(
        res,
        dirs,
        port,
        "guitar",
        "SELECT (SELECT count(*) FROM pg_class c \
                 JOIN pg_namespace n ON n.oid = c.relnamespace \
                 WHERE c.relkind IN ('r','p','v','m','f','S') \
                 AND n.nspname NOT IN ('pg_catalog','information_schema')) \
              + (SELECT count(*) FROM pg_proc p \
                 JOIN pg_namespace n ON n.oid = p.pronamespace \
                 WHERE n.nspname NOT IN ('pg_catalog','information_schema')) \
              + (SELECT count(*) FROM pg_largeobject_metadata)",
    ) {
        Ok(n) if n == "0" => DbProbe::Empty,
        Ok(n) if n.parse::<u64>().is_ok() => DbProbe::Full,
        Ok(other) => {
            app_log(&format!(
                "install probe: unexpected table count '{other}' — changing nothing"
            ));
            DbProbe::Unknown
        }
        Err(e) => {
            app_log(&format!(
                "install probe: cannot count tables in the guitar database ({e}) — \
                 changing nothing"
            ));
            DbProbe::Unknown
        }
    }
}

// ---- displacement, not destruction -----------------------------------------
//
// THE RULE THIS SECTION EXISTS FOR: when this app decides it must get something
// of the tutor's out of the way, it RENAMES IT ASIDE. It does not delete it.
//
// Every safety argument above — the marker, its ordering, the probe, the
// exhaustive plan table — is an argument that a particular cell of a truth table
// cannot be reached wrongly. Those arguments are good, and they are still not
// worth a guitar teacher's only copy of years of teaching material, because
// they are arguments about the failures we THOUGHT OF. Displacement is
// different in kind: it degrades every failure we did not think of, in every
// cell, from "his curricula are gone" to "his curricula are sitting right there
// under another name".
//
// The price is disk space, bounded by the reaper below. That is the entire
// cost, and it is not a close call.
//
// The one thing that is still deleted outright is a database with ZERO user
// relations — no tables, no views, no sequences, nothing. That is not a
// judgement call about likelihood; it is a measurement, taken immediately
// before the delete, of a database that demonstrably contains nothing at all.

/// Prefix for a database renamed out of the way. Lower case and underscore-only
/// so it survives Postgres identifier folding unchanged.
const SUPERSEDED_DB_PREFIX: &str = "guitar_superseded_";
/// Prefix for a pgdata directory moved out of the way, alongside `pgdata` in
/// the data folder — deliberately a SIBLING, so it is visible the moment anyone
/// opens that folder.
const SUPERSEDED_DIR_PREFIX: &str = "pgdata_superseded_";
/// Prefix for the MEDIA tree moved out of the way with whatever indexed it.
///
/// `<data>/media` holds the original PDF of every book the tutor ever uploaded
/// (`<uuid>/source.pdf`) plus its page renders, and the rows that give those
/// files meaning live in the `guitar` database. The two are one thing; see
/// `displace_media` for what happens when they are separated.
const SUPERSEDED_MEDIA_PREFIX: &str = "media_superseded_";
/// The plain-text explanation dropped next to the data. Named so it sorts to
/// the top of the folder and reads as important before it is opened.
const RECOVERY_NOTE: &str = "READ-ME-superseded-data.txt";

// ---- the commands the recovery note prints ---------------------------------
//
// EVERY COMMAND IN THAT FILE HAS TO WORK WHEN IT IS TYPED LITERALLY, by
// somebody who did not write this app, on the phone, at 1am. The note used to
// print three bare `psql` lines, and BOTH states of the machine defeated them,
// in opposite directions:
//
//   * with GuitarTutor QUIT — which the note itself instructs, and which is
//     required, because `ALTER DATABASE … RENAME` has no FORCE and the app holds
//     a connection to `guitar` — there is no postgres running at all. Nothing is
//     listening, on any port or socket, so `psql` cannot connect to anything.
//   * with GuitarTutor OPEN there is a server, and the rename is refused for
//     exactly the reason the note says to quit first.
//
// And even given a running server, `psql` on its own could not have reached it:
// the binary is not on anybody's PATH (it ships inside the app bundle), the
// cluster answers on a unix socket inside `<data>/pgdata` rather than on any
// default, on a port that ROLLS from launch to launch (`pick_pg_port` scans
// 5434..=5444 every time), and the role is `guitar` — never the account name
// libpq would otherwise assume.
//
// So the note stopped assuming a server and now BRINGS ONE UP ITSELF, for the
// repair only, and takes it down again. Every path in it is absolute and
// interpolated at write time, because this file is generated on the machine it
// describes and there is no reason for it to be vague about anything.

/// The port the recovery note starts the repair server on, and the port its
/// `psql` lines connect to.
///
/// PINNED, deliberately, rather than read off this install. The app's own port
/// rolls, so no number written into this file would still be true when somebody
/// reads it — but the repair does not have to inherit it: the note starts the
/// cluster itself with `-o "-p 5434"`, which overrides `postgresql.conf` and
/// whatever the app last chose.
///
/// What makes a pinned number safe is the OTHER flag on that line,
/// `-c listen_addresses=`: the repair server binds NO TCP PORT AT ALL and is
/// reachable only through the unix socket inside `<data>/pgdata`. A Homebrew
/// postgres, or a second GuitarTutor install, can be sitting on TCP 5434 and
/// this still starts — measured, not assumed. The number then only names the
/// socket FILE in a directory that is ours, so it cannot collide with anything.
const REPAIR_PORT: u16 = 5434;

/// One path, quoted for a shell.
///
/// Not cosmetic: the macOS data folder is
/// `~/Library/Application Support/GuitarTutor`. An unquoted path in a file that
/// says "type this exactly" is a command that fails on the tutor's own machine
/// and on nobody else's.
fn shell_quoted(p: &Path) -> String {
    format!("\"{}\"", p.display())
}

/// The absolute path of one bundled postgres binary, quoted. `psql`, `pg_ctl`
/// and the rest live inside the app bundle and are on no PATH anywhere.
fn bundled_bin(res: &Path, bin: &str) -> String {
    shell_quoted(&res.join("pg/bin").join(bin))
}

/// Bring the cluster up for a repair — socket only, on `REPAIR_PORT`.
fn repair_start_cmd(res: &Path, dirs: &Dirs) -> String {
    format!(
        "{} -D {} -o \"-p {REPAIR_PORT} -c listen_addresses=\" -w start",
        bundled_bin(res, "pg_ctl"),
        shell_quoted(&dirs.pgdata)
    )
}

/// …and take it down again. Not optional: GuitarTutor starts its own postgres
/// from this same directory and will not get past a postmaster already holding
/// it.
fn repair_stop_cmd(res: &Path, dirs: &Dirs) -> String {
    format!(
        "{} -D {} -m fast -w stop",
        bundled_bin(res, "pg_ctl"),
        shell_quoted(&dirs.pgdata)
    )
}

/// Everything a repair `psql` needs before its own `-c`: the bundled binary,
/// `-X` (never read anybody's `.psqlrc`), the socket directory, the pinned port,
/// the `guitar` role that `initdb -U guitar` created, and `postgres` as the
/// database to be connected to — you cannot rename the database you are in.
fn repair_psql(res: &Path, dirs: &Dirs) -> String {
    format!(
        "{} -X -h {} -p {REPAIR_PORT} -U guitar -d postgres",
        bundled_bin(res, "psql"),
        shell_quoted(&dirs.pgdata)
    )
}

/// The line that clears whatever is still attached to `db`, for the one failure
/// this repair can actually hit: `ALTER DATABASE … RENAME` has no FORCE, and one
/// connected backend — an autovacuum worker will do — refuses it outright with
/// `database "…" is being accessed by other users`.
fn repair_evict_cmd(res: &Path, dirs: &Dirs, db: &str) -> String {
    format!(
        "{} -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
         WHERE datname = '{db}' AND pid <> pg_backend_pid()\"",
        repair_psql(res, dirs)
    )
}

/// How many set-aside things of each kind are kept unconditionally, newest
/// first. See `reapable` for the policy this is half of.
const SUPERSEDED_KEEP: usize = 3;
/// …and how old the rest must be before they may be removed.
const SUPERSEDED_MIN_AGE: Duration = Duration::from_secs(30 * 24 * 60 * 60);

/// Everything this boot has moved out of the way — RECORDED on disk, and not
/// yet said out loud to the tutor.
///
/// THE TYPE IS THE ENFORCEMENT, the same trick `InstallComplete` uses, pointed
/// the other way. `InstallComplete` says "this cannot happen unless that
/// happened first"; this says "if this happened, you are holding the proof, and
/// dropping it is a compiler warning".
///
///   * The fields are PRIVATE and there is exactly one thing in this module
///     that can produce a non-empty one: `announce_set_aside`, which writes all
///     three durable records before it returns. So a displacement cannot be
///     expressed in this program without also being written down.
///   * `#[must_use]`, and every function that can displace anything returns
///     one. Three of the four paths that displace media used to throw the value
///     away and show nothing at all: the tutor opened the app, saw a starter
///     library, and his five books were gone with no explanation anywhere he
///     would look. With `-D warnings` in CI, doing that again does not compile.
///
/// It accumulates because ONE BOOT CAN DISPLACE MORE THAN ONE THING — an
/// initdb that fails on this machine's locale sets a pgdata aside, and the
/// retry's `create_and_seed_db` can set a media tree aside after it — and the
/// tutor gets ONE dialog naming all of it, not three in a row or (as before)
/// none.
#[must_use = "a displacement the tutor is never told about is no better than a deletion: \
              absorb it into the boot's accumulator so the one-time dialog names it"]
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Displaced {
    /// New names of databases that were RENAMED because they held user
    /// relations. These are the handles the tutor's rows come back by.
    databases: Vec<String>,
    /// New names of whole pgdata directories moved aside.
    directories: Vec<String>,
    /// New names of media trees moved aside — where his uploaded PDFs and page
    /// scans physically are.
    media: Vec<String>,
    /// New names of INDIVIDUAL FILES renamed in place, each shown relative to
    /// the data folder. One entry per file the bundled seed would otherwise
    /// have overwritten — see `set_aside_file`, which is the only producer.
    files: Vec<String>,
}

impl Displaced {
    /// Nothing was displaced. The only public constructor, and it can only ever
    /// make the empty value — see the type's docstring.
    pub fn nothing() -> Self {
        Self::default()
    }

    /// Is there anything the tutor needs to be told about? Drives the one
    /// dialog: a set-aside thing nobody knows about is no better than a
    /// deleted one.
    pub fn anything_set_aside(&self) -> bool {
        !self.databases.is_empty()
            || !self.directories.is_empty()
            || !self.media.is_empty()
            || !self.files.is_empty()
    }

    /// Fold another displacement into this one, keeping each name once.
    ///
    /// De-duplicating is not tidiness: `main` unions what THIS boot displaced
    /// with what the marker says was never announced, and on the ordinary path
    /// those are the same entries seen twice. Naming the tutor's media folder
    /// twice in one dialog reads as two folders.
    pub fn absorb(&mut self, other: Displaced) {
        for (into, from) in [
            (&mut self.databases, other.databases),
            (&mut self.directories, other.directories),
            (&mut self.media, other.media),
            (&mut self.files, other.files),
        ] {
            for name in from {
                if !into.contains(&name) {
                    into.push(name);
                }
            }
        }
    }

    /// The media tree this one displacement moved, if it moved one. Only ever
    /// asked of the value `displace_media` just returned, which holds at most
    /// one — the callers need the name to reconcile the pairing with.
    fn sole_media(&self) -> Option<&str> {
        self.media.first().map(String::as_str)
    }
}

/// Identifiers this app builds for itself, checked before they are interpolated
/// into SQL. Nothing here comes from the tutor or from the network — the check
/// is against OUR OWN bug (a stamp format change, a truncation) putting a quote
/// or a backslash into a statement, not against an attacker.
fn is_safe_identifier(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 63 // Postgres NAMEDATALEN - 1; longer is silently cut
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn database_exists(res: &Path, dirs: &Dirs, port: u16, name: &str) -> bool {
    matches!(
        psql_scalar(
            res,
            dirs,
            port,
            "postgres",
            &format!("SELECT count(*) FROM pg_database WHERE datname = '{name}'"),
        )
        .as_deref(),
        Ok("1")
    )
}

/// ONE suffix for a whole displacement, accepted by `free` for every name it
/// will be pasted into.
///
/// A displacement always moves TWO things — a database and its media, or a
/// pgdata directory and its media — and they are named `guitar_superseded_<s>`
/// / `media_superseded_<s>` from the SAME `<s>`. That shared suffix is not
/// decoration: it is how whoever is helping the tutor knows which media folder
/// belongs to which database, months later, from a directory listing alone. So
/// the suffix has to be free for both halves before either is used.
///
/// The stamp has one-second resolution, so two displacements inside the same
/// second — a crash loop, or a test — would collide. Colliding would make
/// `ALTER DATABASE … RENAME` fail, which is safe but leaves the boot dead; a
/// uniquifier keeps it alive without ever reusing a name.
fn free_suffix(free: impl Fn(&str) -> bool) -> String {
    let stamp = utc_compact_stamp(SystemTime::now());
    if free(&stamp) {
        return stamp;
    }
    for n in 2..1000 {
        let candidate = format!("{stamp}_{n}");
        if free(&candidate) {
            return candidate;
        }
    }
    // 999 displacements in one second is not a situation; if it ever happened
    // the rename would fail loudly rather than overwrite anything.
    format!("{stamp}_{}", std::process::id())
}

/// The suffix for displacing the `guitar` database: free as a database name and
/// free as a media directory name.
fn free_database_suffix(res: &Path, dirs: &Dirs, port: u16) -> String {
    free_suffix(|s| {
        !database_exists(res, dirs, port, &format!("{SUPERSEDED_DB_PREFIX}{s}"))
            && !dirs.data.join(format!("{SUPERSEDED_MEDIA_PREFIX}{s}")).exists()
    })
}

/// The suffix for displacing the whole cluster directory: free as a pgdata
/// directory name and free as a media directory name.
fn free_pgdata_suffix(dirs: &Dirs) -> String {
    free_suffix(|s| {
        !dirs.data.join(format!("{SUPERSEDED_DIR_PREFIX}{s}")).exists()
            && !dirs.data.join(format!("{SUPERSEDED_MEDIA_PREFIX}{s}")).exists()
    })
}

/// Files that are evidence of NOTHING: what a file browser, a backup tool or a
/// search indexer drops into any folder it is pointed at.
///
/// This is not fussiness. The tutor's dad's Mac is a real machine, and somebody
/// helping him over the phone WILL open the data folder in Finder — which
/// writes `.DS_Store` into every directory it displays, `._name` AppleDouble
/// files onto any non-Apple filesystem, `.localized`, and `Icon\r` if a folder
/// ever got a custom icon. Spotlight adds `.Spotlight-V100`, Time Machine
/// `.DocumentRevisions-V100` and `.fseventsd`, the Trash `.Trashes`, and a
/// Windows machine that has ever seen the folder over a share leaves
/// `Thumbs.db` and `desktop.ini`. Every one of those is a regular file, and
/// every question this file asks about a directory ("is there a cluster in
/// here?", "is there a library in here?") used to answer YES on the strength of
/// one of them.
///
/// The dot rule covers all of the Apple ones at once and is the right shape:
/// nothing this app or PostgreSQL writes into pgdata or media begins with a
/// dot.
fn is_foreign_debris(name: &str) -> bool {
    name.starts_with('.')
        || name.starts_with("Icon\r")
        || name.eq_ignore_ascii_case("Thumbs.db")
        || name.eq_ignore_ascii_case("desktop.ini")
}

/// Does `dir` hold at least one file that is CONTENT — anything at all that is
/// not `is_foreign_debris` and not one of the explanations this app itself
/// leaves lying around? Recursive, and stops at the first one it finds.
///
/// Depth-bounded exactly like `tree_size_at_least`, and for the same reason:
/// some of its callers run on the splash screen.
///
/// Symlinks count as neither files nor directories — `file_type()` does not
/// follow them — so a symlink can neither fake content nor walk this out of the
/// tree it was given.
fn contains_content(dir: &Path, ignore: &[&str], depth: u32) -> bool {
    if depth > 8 {
        return false;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if is_foreign_debris(&name) || ignore.contains(&name.as_str()) {
            continue;
        }
        match entry.file_type() {
            Ok(t) if t.is_file() => return true,
            Ok(t) if t.is_dir() => {
                if contains_content(&entry.path(), ignore, depth + 1) {
                    return true;
                }
            }
            _ => {}
        }
    }
    false
}

/// The one file the API child leaves in `<data>/media` that is an EXPLANATION
/// rather than the tutor's data: `app/brain/media.py`'s quarantine note.
const API_QUARANTINE_NOTE: &str = "READ-ME-superseded-media.txt";
/// …and the folder it sits in. `app/brain/media.py::QUARANTINE_DIR`; the two
/// have to agree, and `describe_api_quarantine` is what makes the disagreement
/// visible (it would simply find nothing).
const API_QUARANTINE_DIR: &str = "_superseded";

/// Is there anything in `<data>/media` the tutor would call his?
///
/// NOT `read_dir(..).next().is_none()`, which is the question this used to ask
/// and which stopped being the right one the moment the API child grew a
/// quarantine. `app.brain.media.sweep_orphaned_media` renames orphaned sources
/// into `<data>/media/_superseded/<stamp>/` and writes a README beside them, so
/// after ONE sweep `<data>/media` is never `read_dir`-empty again — and every
/// later displacement would move, record and ANNOUNCE a tree containing
/// nothing but an empty dated folder and a note. A dialog telling the tutor
/// that data of his has been set aside, naming a folder that holds none, is the
/// same class of failure as saying nothing: it teaches him the dialog is noise.
///
/// So: RECURSIVELY, is there a file in there that is neither foreign debris nor
/// that README? That answer is right in both directions, and the second one
/// matters more — a quarantine that DOES hold his books makes this non-empty,
/// so those books still travel with the database that indexes them.
fn media_is_empty(dirs: &Dirs) -> bool {
    !contains_content(&dirs.media, &[API_QUARANTINE_NOTE], 0)
}

/// An empty `<data>/media` where the API child expects one.
fn ensure_media_dir(dirs: &Dirs) -> Result<(), String> {
    fs::create_dir_all(&dirs.media).map_err(|e| {
        format!("cannot recreate {} ({e})", dirs.media.display())
    })
}

/// Move `<data>/media` out of the way under a name that PAIRS it with whatever
/// it was displaced alongside, and leave an empty `media` in its place.
/// `Ok(None)` means there was nothing in it to move.
///
/// WHY THIS EXISTS — the layer boundary displacement was being defeated across.
/// `<data>/media` is not scratch space. It holds the ORIGINAL PDF of every book
/// the tutor ever uploaded (`<uuid>/source.pdf`) as well as the regenerable page
/// renders. What gives those files meaning is a `KnowledgeSource` row, and those
/// rows live in the `guitar` database. Minutes after this shell renames a
/// database aside and builds a starter database beside it, the API child runs
/// `app.brain.media.sweep_orphaned_media` at startup, which sets aside every
/// `<uuid>/` directory that has no `KnowledgeSource` row IN THE DATABASE IT IS
/// CONNECTED TO — and right after a reseed that is EVERY book he owns. Before
/// this function existed that sweep called `shutil.rmtree`, so displacing the
/// database WITHOUT its media was not displacement at all: it was a delayed
/// delete, and the recovery note's promise that renaming the database back
/// restores everything was false.
///
/// ORDER, AND WHY IT IS THE WHOLE CRASH-SAFETY ARGUMENT. Two renames cannot be
/// made one atomic operation, so the only question is which half-done state a
/// power cut is allowed to leave behind. Every caller moves the MEDIA FIRST and
/// the database (or the pgdata directory) SECOND:
///
///   * media moved, database not yet — `<data>/media` is empty and the database
///     is untouched. The sweep finds no orphans because there is nothing there
///     to be an orphan. NOTHING CAN BE LOST, and the next launch simply
///     displaces the database, under a later stamp; the recovery note says in
///     as many words what that looks like and how to read it.
///   * database moved, media not yet — the sweep meets a fresh database and the
///     tutor's whole library, and (before the API-side quarantine) deleted it.
///
/// One of those two is survivable and the other is the bug this round exists to
/// kill, so only one of the two orders is allowed. The API side now quarantines
/// rather than deletes, which makes even the wrong order recoverable — but that
/// is defence in depth, not a licence to reorder these two lines.
/// A RECORD IS ONLY EVER WRITTEN ABOUT SOMETHING THAT ALREADY HAPPENED. This
/// function knows one fact when it returns — these files are now called
/// `media_superseded_<suffix>` — and it writes down exactly that fact and no
/// other. What they belong to is somebody else's fact to establish, which is
/// why `pairing` is `Companion::Unknown` on every path that has a second rename
/// still ahead of it (see `reconcile_pairing`), and `Companion::Alone` only
/// where the absence of a partner has actually been measured.
fn displace_media(
    res: &Path,
    dirs: &Dirs,
    suffix: &str,
    pairing: Companion,
    why: &str,
) -> Result<Displaced, String> {
    if media_is_empty(dirs) {
        ensure_media_dir(dirs)?;
        return Ok(Displaced::nothing());
    }
    let mut name = format!("{SUPERSEDED_MEDIA_PREFIX}{suffix}");
    let mut n = 2;
    while dirs.data.join(&name).exists() && n < 1000 {
        name = format!("{SUPERSEDED_MEDIA_PREFIX}{suffix}_{n}");
        n += 1;
    }
    let target = dirs.data.join(&name);
    // A sibling of `media` inside the data folder: same filesystem, so the
    // rename is atomic and instant however many gigabytes of scans are in it,
    // and it lands where anyone opening that folder will see it.
    fs::rename(&dirs.media, &target).map_err(|e| {
        format!(
            "cannot move {} aside to {} ({e}) — nothing was changed",
            dirs.media.display(),
            target.display()
        )
    })?;
    // RECORD BEFORE ANYTHING ELSE IS ALLOWED TO FAIL. `ensure_media_dir` used
    // to run in between, and it can fail — a full disk, a read-only data folder
    // — at which point this returned Err with the tutor's entire library moved
    // and NOT ONE WORD written down anywhere: not the log, not the marker, not
    // the note. The empty directory is for the API child a few seconds later
    // and can fail loudly; the record is the recovery and costs a few hundred
    // bytes. (If `ensure_media_dir` does fail, the boot dies here with the
    // record already on disk and `announced: false` against it, so the next
    // boot that reaches a window tells him — see `unannounced`.)
    let displaced = announce_set_aside(res, dirs, "media", &name, &pairing, why);
    ensure_media_dir(dirs)?;
    Ok(displaced)
}

/// Write the pairing down NOW THAT BOTH HALVES HAVE REALLY HAPPENED.
///
/// The media entry was recorded at the moment of its own rename as
/// `Undetermined`, because at that moment it was: the `ALTER DATABASE` (or the
/// pgdata rename) that gives it a partner had not run, and it can fail. This
/// runs only after that second rename returned Ok.
///
/// The note file is APPEND-ONLY — that is the property that makes it trustworthy
/// — so this does not go back and edit the media entry already in it. It appends
/// a short update instead, which is both honest about the sequence and exactly
/// how a paper ledger records a correction.
fn reconcile_pairing(dirs: &Dirs, media: &str, partner: &str) {
    let Some(mut state) = read_state(dirs) else {
        app_log(&format!(
            "could not record that '{media}' belongs to '{partner}' in \
             {INSTALL_STATE_FILE} (unreadable) — it is in app.log and {RECOVERY_NOTE}"
        ));
        append_note_block(
            dirs,
            &pairing_update_text(dirs, media, partner),
        );
        return;
    };
    // The LAST matching entry: a crash loop can leave several media folders
    // recorded, and the one this call is about is the one just written.
    if let Some(entry) = state
        .set_aside
        .iter_mut()
        .rev()
        .find(|s| s.kind == "media" && s.name == media)
    {
        entry.pairing = Companion::Is(partner.to_string());
    }
    if let Err(e) = save_state(dirs, &state) {
        app_log(&format!(
            "could not record that '{media}' belongs to '{partner}' in \
             {INSTALL_STATE_FILE} ({e}) — it is in app.log and {RECOVERY_NOTE}"
        ));
    }
    app_log(&format!(
        "pairing confirmed: the media folder '{media}' belongs to '{partner}' — \
         both halves were renamed, so this is now recorded as a pair"
    ));
    append_note_block(dirs, &pairing_update_text(dirs, media, partner));
}

fn pairing_update_text(dirs: &Dirs, media: &str, partner: &str) -> String {
    format!(
        "{stamp}\n\
         ΣΥΜΠΛΗΡΩΜΑΤΙΚΗ ΣΗΜΕΙΩΣΗ — FOLLOW-UP NOTE\n\
         \n\
         Ο φάκελος αρχείων «{media}» ανήκει στο «{partner}».\n\
         Επαναφέρετέ τα ΜΑΖΙ, ποτέ χωριστά.\n\
         \n\
         The media folder \"{media}\" belongs to \"{partner}\".\n\
         Both renames have now completed, so this pairing is certain. The entry\n\
         for the media folder above was written the moment those files were\n\
         moved, before it could be known what they would end up paired with;\n\
         this line settles it. RESTORE THEM TOGETHER, never one without the\n\
         other.\n\
         Folder: {data}\n",
        stamp = utc_compact_stamp(SystemTime::now()),
        data = dirs.data.display(),
    )
}

/// The choke point EVERY freshly created `guitar` database passes through.
///
/// `create_and_seed_db` calls this immediately before `createdb`. Normally it
/// does nothing at all: on the `Reseed` path `displace_database` has already
/// moved the media with the database it belonged to, and on a genuine day one
/// `<data>/media` is empty.
///
/// It is NOT nothing after a boot that set a pgdata directory aside and then
/// died before it got here, and it is not nothing for any database/media
/// desync nobody has thought of yet. The rule it enforces is the simplest
/// statement of the whole problem: A NEWLY CREATED DATABASE IS NEVER PUT BESIDE
/// A MEDIA TREE IT DOES NOT INDEX. `displace_database` is the paired,
/// well-labelled version of this; this is the one that cannot be bypassed.
fn displace_media_for_a_fresh_database(res: &Path, dirs: &Dirs) -> Result<Displaced, String> {
    let suffix = free_suffix(|s| {
        !dirs.data.join(format!("{SUPERSEDED_MEDIA_PREFIX}{s}")).exists()
    });
    displace_media(
        res,
        dirs,
        &suffix,
        // `Alone`, and it is a measurement rather than a shrug: this function
        // runs immediately before `createdb`, i.e. at a moment when there is
        // demonstrably no `guitar` database in this cluster for these files to
        // belong to. Nothing is coming that could become their partner.
        Companion::Alone,
        "media left over from an install whose database is gone: a database is \
         about to be created fresh, it does not index these files, and the API's \
         startup sweep treats files no database indexes as orphans",
    )
}

/// Bring the API child's OWN quarantine into the main recovery note.
///
/// `app/brain/media.py::sweep_orphaned_media` runs in the API's startup
/// lifespan, on every launch, and renames every `<uuid>/` directory the database
/// it is connected to has no row for into `<data>/media/_superseded/<stamp>/`.
/// After a database that was replaced, restored or renamed by hand, that is the
/// tutor's ENTIRE LIBRARY in one pass — the same displacement this whole module
/// exists to make survivable, performed by a different process, in a different
/// language, minutes later.
///
/// It explains itself well, and it explains itself IN THERE: a README beside
/// the files, four directories down. `<data>/READ-ME-superseded-data.txt` — the
/// file the dialog names, the file support asks for, the file somebody actually
/// opens — said nothing about it at all. So a reader following the main note
/// from the top learned everything except the one thing that had happened to
/// his books. The shell cannot do the sweep's job and must not try; what it can
/// do is SEE the result and describe it in the file that is read.
///
/// Called after the backend reports ready, which is after that lifespan has run,
/// so a batch created on THIS boot is described on THIS boot. Idempotent: a
/// batch already recorded in the marker is never described twice, so the note
/// does not grow a block per launch.
///
/// A batch holding nothing is ignored. `sweep_orphaned_media` removes its own
/// empty batch directory, but a batch whose contents were moved back by hand —
/// exactly what the instructions tell somebody to do — must not go on being
/// announced as data set aside.
///
/// AND IT REACHES THE DIALOG, which is the one part of this worth arguing.
/// `media_is_empty`'s docstring makes the case against noise: a dialog naming a
/// folder that holds nothing of his teaches him the dialog is noise, and this
/// sweep's ordinary workload is leaked page renders he would never miss. Two
/// things settle it the other way. First, this is the ONLY set-aside in the
/// product on a clock — `reap_quarantined_media` deletes a batch for good after
/// thirty days — so staying quiet here is the one case where silence eventually
/// becomes a deletion. Second, it is said exactly ONCE per batch and only when
/// the batch actually holds files, so it cannot become the recurring noise the
/// objection is about.
pub fn describe_api_quarantine(res: &Path, dirs: &Dirs) -> Displaced {
    let root = dirs.media.join(API_QUARANTINE_DIR);
    let Ok(entries) = fs::read_dir(&root) else {
        return Displaced::nothing(); // the ordinary case: no sweep ever fired
    };
    let known: Vec<String> = read_state(dirs)
        .map(|s| {
            s.set_aside
                .iter()
                .filter(|e| e.kind == "quarantine")
                .map(|e| e.name.clone())
                .collect()
        })
        .unwrap_or_default();

    let mut batches: Vec<String> = entries
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        // Only stamps the sweep itself writes. Anything else in there was put
        // there by a person, and describing somebody's own folder as something
        // GuitarTutor set aside would be a lie in the file that must not lie.
        .filter(|n| is_compact_stamp(n))
        .collect();
    batches.sort();

    let mut out = Displaced::nothing();
    for stamp in batches {
        // Displayed the way the reader will have to navigate it, relative to
        // the data folder every entry already names.
        let name = format!("media/{API_QUARANTINE_DIR}/{stamp}");
        if known.contains(&name) || !contains_content(&root.join(&stamp), &[], 0) {
            continue;
        }
        out.absorb(announce_set_aside(
            res,
            dirs,
            "quarantine",
            &name,
            // Which database these belonged to is not something this side can
            // establish: the sweep's inference was made against whatever
            // database the API child was connected to, and the shell was not
            // consulted. `Unknown` is the only honest value, and the entry
            // spells out what to do about it.
            &Companion::Unknown,
            "GuitarTutor's startup check found book files in the media folder that \
             nothing in the current database refers to, and set them aside instead \
             of deleting them",
        ));
    }
    out
}

/// What may be done to the `guitar` database, given what the probe found.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Displacement {
    /// There is no database. Do nothing.
    Nothing,
    /// It was MEASURED to hold zero user relations. The only remaining case in
    /// this app where a database is destroyed.
    Drop,
    /// Anything else. Rename it out of the way.
    Rename,
}

/// The one rule that decides whether a database is destroyed or displaced.
///
/// Pure and separate from the SQL so it can be swept over every `DbProbe`
/// variant, present and future: a variant added later cannot inherit `Drop` by
/// falling through a wildcard, because there is no wildcard. `Unknown` maps to
/// `Rename` on purpose — "we could not count the tables" is not a measurement
/// of emptiness, and the only safe reading of an unanswered question is the one
/// that loses nothing.
fn displacement_for(probe: DbProbe) -> Displacement {
    match probe {
        DbProbe::Absent => Displacement::Nothing,
        DbProbe::Empty => Displacement::Drop,
        DbProbe::Full => Displacement::Rename,
        DbProbe::Unknown => Displacement::Rename,
    }
}

/// Get the `guitar` database — AND THE MEDIA TREE IT INDEXES — out of the way
/// WITHOUT destroying anything that holds data. The resume path's first move,
/// and what makes a re-restore idempotent (`pg_restore` never runs twice into
/// one database).
///
/// The media half is not an extra: `<data>/media` holds the tutor's uploaded
/// PDFs, and the API child deletes — now quarantines — every media directory
/// the CURRENT database has no row for. Renaming the database away from its
/// media is therefore the same act as orphaning every book he owns. The two
/// travel together, media first, under one shared suffix; see `displace_media`
/// for the ordering argument and for what a crash between the two renames
/// leaves behind.
///
/// This replaces an unconditional `DROP DATABASE guitar WITH (FORCE)`. The drop
/// was defensible on the argument that `phase == seeding` implies no boot has
/// ever finished here — and that argument survives every review it has had. It
/// is still the wrong shape: it makes every residual risk in the marker state
/// machine, including the ones nobody has thought of yet, UNRECOVERABLE. A
/// rename costs disk space and makes all of them recoverable.
///
/// The measurement, not the guess, is what authorises the one remaining drop:
/// the probe counts tables, partitioned tables, views, matviews, foreign tables
/// and sequences outside the system schemas. Zero of all six means `createdb`
/// ran and nothing else ever did. Anything else — including a probe that FAILS,
/// which cannot distinguish an empty database from a full one — is renamed.
///
/// THE RECEIPT COMES BACK EITHER WAY, and that is not a style choice — it is the
/// one window this function has that costs the tutor an explanation. The media
/// moves FIRST, deliberately (see `displace_media`), and the `ALTER DATABASE`
/// after it can return Err: it has no FORCE, and one connected backend refuses
/// it. As a plain `Result<Displaced, String>` the `?` on that statement threw
/// away the very receipt that said WHERE HIS BOOKS WENT, and `main` put up a
/// fatal dialog saying only that a database could not be set aside — with his
/// entire library already sitting under another name. So the two halves are
/// separated: `Displaced` is what happened, `Result<(), String>` is whether it
/// finished, and the caller is handed both. `Displaced` is still `#[must_use]`,
/// so the receipt cannot be dropped on the way past.
pub fn displace_database(
    res: &Path,
    dirs: &Dirs,
    port: u16,
) -> (Displaced, Result<(), String>) {
    let what = displacement_for(probe_database(res, dirs, port));
    // ONE suffix for both halves, chosen before either is touched, so
    // `guitar_superseded_<s>` and `media_superseded_<s>` name two parts of one
    // recoverable unit rather than two unrelated things that happened to be
    // renamed on the same evening.
    let suffix = free_database_suffix(res, dirs, port);
    let db_name = format!("{SUPERSEDED_DB_PREFIX}{suffix}");

    // MEDIA FIRST, unconditionally — including when there is no database to
    // move and when the database is about to be dropped as measured-empty.
    // Both of those cases end with `create_and_seed_db` building a fresh
    // database, and a fresh database beside the old media is exactly the
    // desync the API's sweep reads as "every one of these is an orphan".
    let moved = displace_media(
        res,
        dirs,
        &suffix,
        match what {
            // THE BLOCKER THIS FIXES. The name `db_name` is what the database is
            // ABOUT to be called, and `ALTER DATABASE` below can return Err —
            // it has no FORCE, and one autovacuum worker connected to `guitar`
            // is enough to refuse it. Recording the pairing here recorded a
            // pairing that had not happened and, on the fatal path, never
            // would. So nothing is claimed until the rename returns Ok; see
            // `reconcile_pairing` at the bottom of the `Rename` arm.
            Displacement::Rename => Companion::Unknown,
            // No `guitar` database in this cluster at all. Measured, and the
            // sentence `Alone` prints — "was already gone when they were found"
            // — is exactly what was measured.
            Displacement::Nothing => Companion::Alone,
            // A DIFFERENT measurement, and it needs a different sentence. The
            // database is right there; the probe counted zero user relations in
            // it and it is about to be dropped ON THAT COUNT. `Alone` would
            // print "already gone", which is untrue here and reads as data
            // vanishing on its own.
            Displacement::Drop => Companion::AloneEmptyDatabase,
        },
        match what {
            Displacement::Rename => {
                "the uploaded books, PDFs and page scans of the GuitarTutor \
                 database that is being set aside in the same moment — they are \
                 one thing and must be restored together"
            }
            _ => {
                "media left behind by an install whose database is empty or gone: \
                 nothing indexes these files any more, so they were set aside \
                 rather than left for the startup sweep to collect"
            }
        },
    );
    // The media rename is the FIRST thing that happens and the one that moves
    // his books, so its own failure is the only one with nothing to report.
    let mut displaced = match moved {
        Ok(d) => d,
        Err(e) => return (Displaced::nothing(), Err(e)),
    };
    // The name of what just moved, if anything did — needed to reconcile the
    // pairing below, and to tell the database's own record what it goes with.
    let media = displaced.sole_media().map(str::to_string);

    match what {
        Displacement::Nothing => (displaced, Ok(())),
        Displacement::Drop => {
            if let Err(e) = psql_exec(
                res,
                dirs,
                port,
                "postgres",
                // `WITH (FORCE)` (PG13+, we ship 16) evicts a connection rather
                // than failing. psql rather than `dropdb` because psql is one of
                // the binaries `stage-postgres.sh` proves is in the bundle.
                "DROP DATABASE IF EXISTS guitar WITH (FORCE)",
            ) {
                return (displaced, Err(e));
            }
            app_log(
                "the half-installed database held no tables, views or sequences at \
                 all, so it was dropped rather than set aside — there was nothing \
                 in it to keep",
            );
            (displaced, Ok(()))
        }
        Displacement::Rename => {
            if !is_safe_identifier(&db_name) {
                return (
                    displaced,
                    Err(format!(
                        "refusing to rename the database to {db_name:?}: not a plain \
                         identifier. This is a bug in GuitarTutor; nothing was changed."
                    )),
                );
            }
            // `ALTER DATABASE … RENAME` has no FORCE, so evict first. At this
            // point in boot neither uvicorn nor node has been started, so there
            // is nothing of ours connected; this is for a psql someone left open
            // while helping over the phone — and for the autovacuum worker that
            // needs no human at all. Best effort, and racy by nature: a backend
            // can attach again between this statement and the next, which is
            // exactly why nothing above claims the rename has happened.
            let _ = psql_exec(
                res,
                dirs,
                port,
                "postgres",
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
                 WHERE datname = 'guitar' AND pid <> pg_backend_pid()",
            );
            if let Err(e) = psql_exec(
                res,
                dirs,
                port,
                "postgres",
                &format!("ALTER DATABASE guitar RENAME TO \"{db_name}\""),
            ) {
                // THE FAILED-ALTER WINDOW. The media has already moved; the
                // receipt travels out with the error so the fatal dialog can
                // name it. Nothing claims the database was renamed, because it
                // was not.
                return (displaced, Err(e));
            }
            // PAST THIS LINE, AND ONLY PAST IT, THE PAIR EXISTS. The database's
            // own record is written here, after both renames, so it can name
            // the media truthfully; and the media's record — written earlier as
            // undetermined, because at that point it was — is settled now.
            displaced.absorb(announce_set_aside(
                res,
                dirs,
                "database",
                &db_name,
                &match &media {
                    Some(m) => Companion::Is(m.clone()),
                    None => Companion::Alone,
                },
                "an earlier first run was interrupted part-way through installing the \
                 starter library, and this database was what it left behind",
            ));
            if let Some(m) = &media {
                reconcile_pairing(dirs, m, &db_name);
            }
            (displaced, Ok(()))
        }
    }
}

/// Say — loudly, durably, and in more than one place — that something was set
/// aside. A database renamed where nobody can find it is no better than a
/// database dropped, so this is not logging: it is half the feature.
///
/// Three places, because they fail differently:
///   * `app.log` — what support asks for, and the only one that is guaranteed
///     to exist even when the data folder is unwritable;
///   * `install-state.json` — the file whose phase already decides everything
///     here, so the record travels with the decision that made it;
///   * a plain-text note IN THE DATA FOLDER — the only one the tutor himself
///     can stumble across, and the only one that survives someone deleting the
///     logs.
///
/// THE ONLY CONSTRUCTOR OF A NON-EMPTY `Displaced`. Everything above that moves
/// something of the tutor's has to come through here to have anything to
/// return, and what it returns is `#[must_use]` — so "it was displaced", "it was
/// recorded" and "he will be told" are one act rather than three that can drift.
#[must_use = "the receipt is what carries this displacement to the tutor's dialog"]
fn announce_set_aside(
    res: &Path,
    dirs: &Dirs,
    kind: &str,
    name: &str,
    companion: &Companion,
    why: &str,
) -> Displaced {
    app_log(&format!(
        "\n\
         ============================================================\n\
         NOTHING WAS DELETED. A {kind} was renamed out of the way.\n\
           new name: {name}\n\
           goes with: {}\n\
           because:  {why}\n\
         It can be restored — see {}\n\
         ============================================================",
        match companion {
            Companion::Is(c) => c.as_str(),
            Companion::Alone => "nothing — there was no other half to move",
            Companion::AloneEmptyDatabase =>
                "nothing — the database beside it was measured empty and removed",
            Companion::Unknown =>
                "not yet determined — see the pairing note in the recovery file",
        },
        dirs.data.join(RECOVERY_NOTE).display()
    ));
    append_recovery_note(res, dirs, kind, name, companion, why);
    record_set_aside(dirs, kind, name, companion, why);
    displaced_for(kind, name)
}

/// Which bucket of the receipt one `kind` lands in. THE SINGLE MAPPING —
/// `announce_set_aside` mints receipts from it and `unannounced` rebuilds them
/// from the marker with it, so the two can never disagree about what a recorded
/// kind is called in the tutor's dialog.
///
/// `quarantine` deliberately shares the `media` bucket: what the API's boot
/// sweep set aside IS his uploaded books, and the dialog's media line ("YOUR
/// FILES: the book PDFs and the page scans") is the true sentence about it. Only
/// the recovery note needs to tell the two apart, and it does — by `kind`.
fn displaced_for(kind: &str, name: &str) -> Displaced {
    let mut displaced = Displaced::nothing();
    match kind {
        "database" => displaced.databases.push(name.to_string()),
        "media" | "quarantine" => displaced.media.push(name.to_string()),
        "file" => displaced.files.push(name.to_string()),
        _ => displaced.directories.push(name.to_string()),
    }
    displaced
}

/// Everything the marker records that the tutor has NOT been told about yet.
///
/// The durable half of the promise in `Displaced`'s docstring. A boot that
/// displaces something and then dies — a failed `ALTER DATABASE`, an initdb
/// that will not run, a full disk — never reaches the dialog, and the in-process
/// receipt dies with the process. The record on disk does not, so the next boot
/// that gets far enough to put a window on screen says it then.
///
/// Read-only: `mark_announced` is separate and runs AFTER the dialog has been
/// dismissed, so a crash while it is on screen re-announces rather than
/// swallowing.
pub fn unannounced(dirs: &Dirs) -> Displaced {
    let mut out = Displaced::nothing();
    let Some(state) = read_state(dirs) else {
        return out;
    };
    // REAPED ENTRIES ARE NOT NEWS. The record stays in the file for good — it
    // is a ledger — but telling the tutor his data was set aside and naming
    // something the reaper removed a month ago is the one announcement that
    // cannot possibly help him.
    for entry in state
        .set_aside
        .iter()
        .filter(|s| !s.announced && s.reaped_at.is_none())
    {
        out.absorb(displaced_for(&entry.kind, &entry.name));
    }
    out
}

/// The tutor has now seen the dialog. Best-effort by design: the cost of
/// failing here is that he is told once more on the next launch, which is a
/// nuisance; the cost of marking BEFORE the dialog would be a displacement he
/// is never told about at all.
pub fn mark_announced(dirs: &Dirs) {
    let Some(mut state) = read_state(dirs) else {
        return;
    };
    if state.set_aside.iter().all(|s| s.announced) {
        return;
    }
    for entry in state.set_aside.iter_mut() {
        entry.announced = true;
    }
    if let Err(e) = save_state(dirs, &state) {
        app_log(&format!(
            "could not mark the set-aside records as announced ({e}) — harmless: \
             the next launch will show the same notice again"
        ));
    }
}

/// Append this set-aside to `install-state.json`, preserving the phase exactly.
///
/// If the marker cannot be read, this writes NOTHING rather than inventing a
/// phase. Writing a phase into an unreadable marker would be writing the one
/// field that arms the displacement path, on no evidence — the opposite of what
/// this whole module is for. The log and the note file still carry the record.
fn record_set_aside(dirs: &Dirs, kind: &str, name: &str, companion: &Companion, why: &str) {
    let Some(mut state) = read_state(dirs) else {
        app_log(&format!(
            "could not record the set-aside {kind} '{name}' in {} (unreadable) — it \
             is recorded in app.log and in {} instead, and the phase was NOT \
             rewritten on a marker we cannot read",
            INSTALL_STATE_FILE, RECOVERY_NOTE
        ));
        return;
    };
    state.set_aside.push(SetAside {
        kind: kind.to_string(),
        name: name.to_string(),
        pairing: companion.clone(),
        at: now_secs(),
        why: why.to_string(),
        announced: false,
        reaped_at: None,
    });
    if let Err(e) = save_state(dirs, &state) {
        app_log(&format!(
            "could not record the set-aside {kind} '{name}' in {INSTALL_STATE_FILE} \
             ({e}) — it is recorded in app.log and in {RECOVERY_NOTE}"
        ));
    }
}

/// How to put back THIS entry's thing — the commands, with its own name already
/// in them.
///
/// PER KIND, and that is a correction, not a refinement. The note used to print
/// one standing block listing all three kinds, and that block interpolated this
/// entry's `{name}` into the DATABASE line whatever the entry actually was. So a
/// media entry told whoever was helping to run
/// `ALTER DATABASE "media_superseded_…" RENAME TO guitar;` and a pgdata entry
/// told them to run it against a directory name. Both are instructions that
/// cannot work, printed under the heading "to put it back", in a file the tutor
/// is told to trust — which is exactly the failure this whole round is about,
/// in the one place it is hardest to notice. Each entry now carries only
/// commands that are true of itself.
fn restore_instructions(
    res: &Path,
    dirs: &Dirs,
    kind: &str,
    name: &str,
    companion: &Companion,
) -> String {
    // Built line by line rather than as one continued literal: this text is
    // READ, under stress, by somebody who has to type what it says, and a
    // continued string literal makes its indentation invisible in the source.
    let mut lines: Vec<String> = Vec::new();
    match kind {
        // THE ONE ENTRY THAT NEEDS A RUNNING SERVER, and therefore the only one
        // that has to bring one up. See the `REPAIR_PORT` block above for why
        // the three bare `psql` lines this replaces could not be run in either
        // state of the machine.
        "database" => {
            // Somewhere for the CURRENT `guitar` to go. Named from this entry's
            // own suffix rather than a fixed `guitar_replaced`, so running the
            // repair twice — or repairing two set-asides — cannot collide with
            // a name that is already taken and fail at step 3.
            let replaced = format!(
                "guitar_replaced_{}",
                name.strip_prefix(SUPERSEDED_DB_PREFIX).unwrap_or(name)
            );
            let psql = repair_psql(res, dirs);
            lines.push("This is a DATABASE: the lessons, the curricula, the notes.".into());
            lines.push("Putting it back is five steps and they have to be done in".into());
            lines.push("this order. Type each command exactly as it is printed —".into());
            lines.push("the long paths are what make it work on this computer.".into());
            lines.push("".into());
            lines.push("  1. QUIT GUITARTUTOR — the whole application, not just its".into());
            lines.push("     window. While it runs it holds a connection to the".into());
            lines.push("     database called \"guitar\", and step 3 cannot rename a".into());
            lines.push("     database that anything is connected to.".into());
            lines.push("".into());
            lines.push("  2. Start the database on its own, for this repair only:".into());
            lines.push(format!("        {}", repair_start_cmd(res, dirs)));
            lines.push("     IT WORKED IF the last line printed is:  server started".into());
            lines.push("     This server opens NO network port — only a file inside".into());
            lines.push("     the folder named above — so nothing else running on".into());
            lines.push("     this computer can be in its way.".into());
            lines.push("".into());
            lines.push("  3. Move the CURRENT \"guitar\" out of the way:".into());
            lines.push(format!(
                "        {psql} -v ON_ERROR_STOP=1 -c 'ALTER DATABASE guitar RENAME TO \"{replaced}\"'"
            ));
            lines.push("     IT WORKED IF it prints:  ALTER DATABASE".into());
            lines.push("     IF INSTEAD it prints:  database \"guitar\" does not exist".into());
            lines.push("     then there is nothing there to move and nothing was".into());
            lines.push("     changed. That is fine — go straight to step 4.".into());
            lines.push("".into());
            lines.push("  4. Put THIS one back in its place:".into());
            lines.push(format!(
                "        {psql} -v ON_ERROR_STOP=1 -c 'ALTER DATABASE \"{name}\" RENAME TO guitar'"
            ));
            lines.push("     IT WORKED IF it prints:  ALTER DATABASE".into());
            lines.push("".into());
            lines.push("  5. Stop the repair server. This is NOT optional —".into());
            lines.push("     GuitarTutor starts its own database from that same".into());
            lines.push("     folder and will not get past one that is already up:".into());
            lines.push(format!("        {}", repair_stop_cmd(res, dirs)));
            lines.push("     IT WORKED IF the last line printed is:  server stopped".into());
            lines.push("".into());
            lines.push("  Then start GuitarTutor. It opens the database you have".into());
            lines.push("  just put back.".into());
            lines.push("".into());
            lines.push("  IF STEP 3 OR STEP 4 SAYS  is being accessed by other users".into());
            lines.push("  then something is still connected to that database.".into());
            lines.push("  NOTHING HAS BEEN CHANGED and nothing is broken. First make".into());
            lines.push("  sure GuitarTutor is really closed (step 1). If it is, run".into());
            lines.push("  the line below and then repeat the step that failed — for".into());
            lines.push("  step 3 exactly as printed, for step 4 with".into());
            lines.push(format!("  {name}"));
            lines.push("  in place of guitar inside the quotes:".into());
            lines.push(format!("        {}", repair_evict_cmd(res, dirs, "guitar")));
            lines.push("  IF STEP 2 SAYS a server is already running, GuitarTutor is".into());
            lines.push("  still open: go back to step 1.".into());
        }
        "media" => {
            lines.push("This is a FOLDER OF FILES: the uploaded books, the original".into());
            lines.push("PDFs and the page scans. It belongs to ONE database and must".into());
            lines.push("be restored together with that database, never on its own.".into());
            lines.push("It is put back by MOVING FOLDERS — there is nothing to type".into());
            lines.push("into a database and no server to start:".into());
            lines.push("  1. QUIT GUITARTUTOR completely.".into());
            lines.push("  2. In the folder named above, move the current \"media\"".into());
            lines.push("     folder out of the way, then".into());
            lines.push(format!("     rename \"{name}\" back to \"media\"."));
            lines.push("  3. Put the database it belongs to back as well — see the".into());
            lines.push("     rest of this entry — and then start GuitarTutor.".into());
            lines.push("  IT WORKED IF the books are in GuitarTutor's library again.".into());
        }
        "file" => {
            lines.push("This is ONE FILE OF YOURS, renamed where it stood.".into());
            lines.push("GuitarTutor was about to write a file of its own with the".into());
            lines.push("same name in the same place, and renamed yours instead of".into());
            lines.push("writing over it. It is in the folder it always was in, under".into());
            lines.push("the name above.".into());
            lines.push("  1. QUIT GUITARTUTOR.".into());
            lines.push("  2. Move GuitarTutor's own file — the one under the".into());
            lines.push("     ORIGINAL name — somewhere else.".into());
            lines.push("  3. Rename this one back to that original name: the name".into());
            lines.push("     above with the \".superseded-…\" ending removed.".into());
            lines.push("  4. Start GuitarTutor.".into());
        }
        "quarantine" => {
            lines.push("THESE ARE FILES OF YOURS. GuitarTutor's own startup check".into());
            lines.push("found them in the media folder with nothing in the database".into());
            lines.push("pointing at them, and set them aside rather than delete".into());
            lines.push("them. That check runs on every launch, and it reaches this".into());
            lines.push("conclusion about EVERY book at once whenever the database".into());
            lines.push("has been replaced, restored or renamed. If your whole".into());
            lines.push("library is in here, that is what happened — the files are".into());
            lines.push("all present and nothing has been lost.".into());
            lines.push("  The layout in here is".into());
            lines.push("      media/_superseded/<date>/<book-id>/source.pdf".into());
            lines.push("  and the tree GuitarTutor actually reads is".into());
            lines.push("      media/<book-id>/source.pdf".into());
            lines.push("  So put the right database back FIRST — the other entries".into());
            lines.push("  in this file say which and how — and only then move each".into());
            lines.push("  <book-id> folder TWO levels up, out of the dated folder".into());
            lines.push("  and into \"media\" itself. One level up lands in".into());
            lines.push("  \"_superseded\", which is still not where GuitarTutor".into());
            lines.push("  looks. There is a note of its own beside the files:".into());
            lines.push(format!(
                "      {}",
                dirs.media
                    .join(API_QUARANTINE_DIR)
                    .join(API_QUARANTINE_NOTE)
                    .display()
            ));
            lines.push("  WARNING — THIS ONE IS ON A CLOCK. Everything else in this".into());
            lines.push("  file stays until somebody moves it. A dated folder in".into());
            lines.push("  here is DELETED FOR GOOD 30 days after the date in its".into());
            lines.push("  name. If you need anything out of it, copy it somewhere".into());
            lines.push("  else NOW.".into());
            // The path above is relative to `media` AS IT STOOD when this was
            // written, and `media` is itself something this app moves. A later
            // displacement carries the whole quarantine inside
            // `media_superseded_<stamp>/`, at which point the path in this entry
            // is no longer where the files are — and a recovery note whose paths
            // have quietly stopped being true is the failure this file exists to
            // avoid. It cannot go back and edit itself (it is append-only), so
            // it says where to look instead.
            lines.push("  IF THERE IS NO \"media\" FOLDER with a \"_superseded\" in".into());
            lines.push("  it any more, read on down this file: a later entry will".into());
            lines.push("  say that the whole \"media\" folder was set aside under a".into());
            lines.push("  \"media_superseded_…\" name, and this quarantine went".into());
            lines.push("  inside it. Nothing was lost — the path above is simply".into());
            lines.push("  one folder deeper than it was.".into());
        }
        _ => {
            lines.push("This is a COMPLETE PostgreSQL 16 DATA DIRECTORY — every".into());
            lines.push("database at once. It is put back by MOVING FOLDERS: there".into());
            lines.push("is nothing to type into a database and no server to start.".into());
            lines.push("  1. QUIT GUITARTUTOR completely.".into());
            lines.push("  2. In the folder named above, move the current \"pgdata\"".into());
            lines.push("     folder out of the way — rename it to".into());
            lines.push(format!(
                "     \"pgdata_replaced_{}\". If there is no \"pgdata\" folder,",
                name.strip_prefix(SUPERSEDED_DIR_PREFIX).unwrap_or(name)
            ));
            lines.push("     skip this step.".into());
            lines.push(format!("  3. rename \"{name}\" back to \"pgdata\"."));
            lines.push("  4. Start GuitarTutor.".into());
            lines.push("  IT WORKED IF GuitarTutor starts and your material is".into());
            lines.push("  there.".into());
            lines.push("  IF A RENAME IS REFUSED, GuitarTutor — or a postgres it".into());
            lines.push("  left behind — is still running. Quit it and try again;".into());
            lines.push("  if that is not enough, restart the computer and do steps".into());
            lines.push("  2 and 3 BEFORE starting GuitarTutor.".into());
        }
    }
    // WHAT PAIRS WITH THIS ONE — keyed on the kind, because the same
    // `Companion` value means a different sentence for each. A single file and
    // an API quarantine batch have no second half at all and must not be handed
    // the media/database wording, which asserts things about a `media` folder
    // that were never measured for them.
    match kind {
        "file" | "quarantine" => {}
        "media" => media_companion_lines(dirs, res, companion, &mut lines),
        _ => database_companion_lines(dirs, companion, &mut lines),
    }
    lines
        .iter()
        .map(|l| format!("           {l}\n"))
        .collect()
}

/// The pairing tail for a DATABASE or a pgdata directory.
fn database_companion_lines(dirs: &Dirs, companion: &Companion, lines: &mut Vec<String>) {
    match companion {
        // The half that was missing, and the sentence that makes it stick.
        Companion::Is(c) => {
            lines.push("AND ITS FILES. Do both. RESTORING THE DATABASE WITHOUT ITS".into());
            lines.push("MEDIA FOLDER IS NOT ENOUGH: the lessons and curricula come".into());
            lines.push("back and every book's PDF and page images do not.".into());
            lines.push("    move the current \"media\" folder out of the way, then".into());
            lines.push(format!("    rename \"{c}\" back to \"media\"."));
        }
        // A DATABASE (or pgdata) with nothing paired to it. The old text said
        // flatly "there is nothing else to restore alongside it", which is only
        // true if no media folder is unaccounted for — and after a failed
        // `ALTER DATABASE` on an earlier boot, one is: the tutor's whole
        // library, in a folder this very file had already described. So the
        // sentence stops after the fact it can actually prove, and the state of
        // the disk is reported rather than assumed.
        Companion::Alone | Companion::AloneEmptyDatabase | Companion::Unknown => {
            lines.push("No media folder was set aside at the same moment as this".into());
            lines.push("one: the \"media\" folder held nothing when this happened.".into());
            let orphans = unpaired_media_on_disk(dirs);
            if orphans.is_empty() {
                lines.push("There is no unaccounted-for media folder in this data".into());
                lines.push("folder either, so this one really is all there is to".into());
                lines.push("put back.".into());
            } else {
                lines.push("THAT DOES NOT MEAN THERE ARE NO FILES. These media".into());
                lines.push("folders are in this data folder and GuitarTutor could".into());
                lines.push("NOT establish which database they belong to:".into());
                for o in &orphans {
                    lines.push(format!("    {o}"));
                }
                lines.push("If this database's books look missing after you put it".into());
                lines.push("back, one of those is very probably its files. Read".into());
                lines.push(format!("their own entries in this file and in {INSTALL_STATE_FILE}"));
                lines.push("before moving anything.".into());
            }
        }
    }
}

/// The pairing tail for a MEDIA folder — every arm a different fact about a
/// database, so every arm a different sentence.
fn media_companion_lines(
    dirs: &Dirs,
    res: &Path,
    companion: &Companion,
    lines: &mut Vec<String>,
) {
    match companion {
        Companion::Is(c) => {
            lines.push("The database (or database folder) these files belong to is".into());
            lines.push(format!("\"{c}\". Restore that too, or these files will have"));
            lines.push("nothing to describe them.".into());
        }
        Companion::Alone => {
            lines.push("The database these files belonged to was already gone when".into());
            lines.push("they were found, so there is nothing to pair them with. They".into());
            lines.push("were kept anyway: the PDFs in here are often the tutor's only".into());
            lines.push("copy of a scan he made himself.".into());
        }
        // NOT "was already gone" — that is `Alone`'s fact and it is not this
        // one. Here the database WAS there; GuitarTutor looked inside it, found
        // not one table, view or sequence, and removed it on that measurement.
        // Printing the other sentence would tell whoever is reading that a
        // database vanished on its own, which is both untrue and alarming.
        Companion::AloneEmptyDatabase => {
            lines.push("The database that stood beside these files was NOT already".into());
            lines.push("gone: GuitarTutor looked inside it, found not one table,".into());
            lines.push("view or sequence — nothing had ever been put in it — and".into());
            lines.push("removed it on that measurement. So there is no database".into());
            lines.push("for these files to be paired with, and there is nothing".into());
            lines.push("missing that could have been. They were kept anyway: the".into());
            lines.push("PDFs in here are often the tutor's only copy of a scan he".into());
            lines.push("made himself.".into());
        }
        // THE HONEST CASE, and the one this file used to get wrong by never
        // admitting it existed.
        Companion::Unknown => {
            lines.push("WHICH DATABASE THESE FILES BELONG TO WAS NOT KNOWN WHEN".into());
            lines.push("THIS ENTRY WAS WRITTEN — which was the instant the files".into());
            lines.push("were moved, before anything else could go wrong.".into());
            lines.push("FIRST, LOOK FURTHER DOWN THIS FILE. If a later block headed".into());
            lines.push("\"FOLLOW-UP NOTE\" names this folder, the pairing was settled".into());
            lines.push("moments later and that block is the answer. Read it and stop".into());
            lines.push("here. If there is no such block, read on.".into());
            lines.push("".into());
            lines.push("GuitarTutor moved these files aside and then stopped — or".into());
            lines.push("failed — before it could finish moving the database they go".into());
            lines.push("with. Nothing was deleted; the pairing is simply not known,".into());
            lines.push("and this file will not guess at it.".into());
            lines.push("WHAT TO CHECK, in this order:".into());
            lines.push("  1. the timestamp in this folder's name, against the".into());
            lines.push(format!("     \"at\" times in {INSTALL_STATE_FILE} in this same folder;"));
            lines.push("  2. app.log in the \"logs\" folder around that time — it".into());
            lines.push("     records every rename this app makes, in order;".into());
            lines.push("  3. which databases exist now. QUIT GUITARTUTOR — with it".into());
            lines.push("     closed there is no database running at all, so start".into());
            lines.push("     one just to look, then stop it again. Three lines, in".into());
            lines.push("     this order:".into());
            lines.push(format!("         {}", repair_start_cmd(res, dirs)));
            lines.push(format!("         {} -l", repair_psql(res, dirs)));
            lines.push(format!("         {}", repair_stop_cmd(res, dirs)));
            lines.push("     The middle line prints one row per database.".into());
            lines.push("     A \"guitar_superseded_…\" with a timestamp close to this".into());
            lines.push("     folder's is the likely partner. IF THERE IS NONE, the".into());
            lines.push("     database these files belong to was never renamed — it".into());
            lines.push("     is still the one called \"guitar\", and these files".into());
            lines.push("     belong to IT.".into());
            lines.push("     DO NOT SKIP THE LAST LINE: GuitarTutor will not start".into());
            lines.push("     while that server is still up.".into());
            lines.push("Restore these files only together with the database you".into());
            lines.push("identify that way, never on their own.".into());
        }
    }
}

/// Media folders that are physically in the data folder AND recorded with no
/// partner this app was ever able to name.
///
/// Read off the marker rather than off a directory listing, because the listing
/// cannot answer the question: `guitar_superseded_<stamp>` is a DATABASE and is
/// invisible in `<data>`. The shared timestamp is a hint for a human; the
/// marker is the record.
///
/// Both halves of the condition matter. A folder the reaper has since removed
/// must not be named as something to look at, and a record we have since
/// settled must not be printed as unresolved.
fn unpaired_media_on_disk(dirs: &Dirs) -> Vec<String> {
    let Some(state) = read_state(dirs) else {
        return Vec::new();
    };
    let mut out: Vec<String> = Vec::new();
    for entry in &state.set_aside {
        if entry.kind != "media" || entry.pairing != Companion::Unknown {
            continue;
        }
        if !dirs.data.join(&entry.name).is_dir() || out.contains(&entry.name) {
            continue;
        }
        out.push(entry.name.clone());
    }
    out
}

/// The bilingual note left in the data folder. Appended, never rewritten: each
/// set-aside adds an entry, and an entry is never removed even when the reaper
/// removes what it describes (the reaper adds an entry of its own instead).
fn append_recovery_note(
    res: &Path,
    dirs: &Dirs,
    kind: &str,
    name: &str,
    companion: &Companion,
    why: &str,
) {
    let entry = format!(
        "{stamp}\n\
         ΜΗΝ ΔΙΑΓΡΑΨΕΤΕ ΑΥΤΟΝ ΤΟΝ ΦΑΚΕΛΟ — DO NOT DELETE THIS FOLDER\n\
         \n\
         Το GuitarTutor βρήκε δεδομένα από προηγούμενη, ημιτελή εγκατάσταση.\n\
         ΔΕΝ τα διέγραψε. Τα μετονόμασε και τα άφησε στην άκρη:\n\
           τι:      {kind_el}\n\
           όνομα:   {name}\n\
           μαζί με: {companion_el}\n\
           φάκελος: {data}\n\
         Αν διαπιστώσετε ότι λείπει κάτι δικό σας από το GuitarTutor, δείξτε\n\
         αυτό το αρχείο σε όποιον σας υποστηρίζει: τίποτα δεν έχει χαθεί και\n\
         όλα μπορούν να επανέλθουν από εδώ.\n\
         \n\
         GuitarTutor found data from an earlier, unfinished installation and did\n\
         NOT delete it. It was renamed and left aside:\n\
           what:      {kind}\n\
           name:      {name}\n\
           goes with: {companion}\n\
           folder:    {data}\n\
           why:       {why}\n\
         \n\
         ΓΙΑ ΝΑ ΤΟ ΕΠΑΝΑΦΕΡΕΤΕ (για όποιον σας υποστηρίζει): κλείστε πρώτα το\n\
         GuitarTutor. Οι εντολές πιο κάτω είναι γραμμένες ώστε να δουλεύουν\n\
         ακριβώς όπως είναι — αντιγράψτε τις αυτούσιες, με τη σειρά. Δεν\n\
         χρειάζεται να εγκαταστήσετε τίποτα: όλα όσα χρειάζονται βρίσκονται\n\
         ήδη μέσα στην εφαρμογή, και οι διαδρομές πιο κάτω τα δείχνουν.\n\
         \n\
         TO PUT THIS ONE BACK (for whoever supports this install). Quit\n\
         GuitarTutor first. The commands below are written to work exactly as\n\
         they are printed — copy them literally, in order. Nothing has to be\n\
         installed: everything they need already ships inside the application,\n\
         and the long paths are where it is.\n\
         {how}\
         \n\
         HOW TO TELL WHICH GOES WITH WHICH. A database (or a pgdata directory)\n\
         and its media folder are set aside together, one immediately after the\n\
         other, and they SHARE THE TRAILING TIMESTAMP:\n\
           guitar_superseded_<stamp>  goes with  media_superseded_<stamp>\n\
           pgdata_superseded_<stamp>  goes with  media_superseded_<stamp>\n\
         THE STAMP IS A HINT, NOT THE RECORD. The record is the \"goes with:\"\n\
         line of each entry above, and it is written only once BOTH halves have\n\
         actually been renamed — so it is never a guess. A later entry headed\n\
         FOLLOW-UP NOTE settles a pairing that was undetermined when it was\n\
         first written.\n\
         If a \"media_superseded_…\" folder has no partner with the same stamp,\n\
         read the \"goes with:\" line of its own entry above. Four things can\n\
         put it in that state, and the entry says which: the database it\n\
         belonged to was already gone when these files were found; or that\n\
         database was still there but was measured to hold nothing at all —\n\
         not one table — and was removed on that measurement; or GuitarTutor\n\
         was stopped between those two steps, in which case the entry reads\n\
         NOT DETERMINED and lists exactly what to check; or the pairing is\n\
         recorded here and the folder was renamed by hand afterwards.\n\
         Nothing was deleted in any of these cases.\n",
        stamp = utc_compact_stamp(SystemTime::now()),
        how = restore_instructions(res, dirs, kind, name, companion),
        kind_el = match kind {
            "database" => "βάση δεδομένων",
            "media" => "φάκελος αρχείων (media)",
            "file" => "ένα δικό σας αρχείο",
            "quarantine" => "αρχεία σας που ξεχώρισε ο ίδιος ο έλεγχος εκκίνησης",
            _ => "φάκελος",
        },
        companion = match companion {
            Companion::Is(c) => c.as_str(),
            Companion::Alone => "nothing — there was no other half to move",
            Companion::AloneEmptyDatabase =>
                "nothing — the database beside these files was measured to hold \
                 nothing at all and was removed on that measurement",
            Companion::Unknown =>
                "NOT DETERMINED YET — see \"to put this one back\" below, and check \
                 for a later FOLLOW-UP NOTE",
        },
        companion_el = match companion {
            Companion::Is(c) => c.as_str(),
            Companion::Alone => "τίποτε άλλο — δεν υπήρχε δεύτερο μέρος",
            Companion::AloneEmptyDatabase =>
                "τίποτε άλλο — η βάση δίπλα σε αυτά τα αρχεία δεν περιείχε απολύτως \
                 τίποτα και αφαιρέθηκε γι' αυτόν ακριβώς τον λόγο",
            Companion::Unknown =>
                "ΔΕΝ ΠΡΟΣΔΙΟΡΙΣΤΗΚΕ ΑΚΟΜΑ — τα αρχεία είναι ΕΔΩ και ασφαλή· \
                 δείτε πιο κάτω αν υπάρχει σημείωση «FOLLOW-UP NOTE»",
        },
        data = dirs.data.display(),
    );
    append_note_block(dirs, &entry);
}

/// Append one block to the recovery note, framed so a reader can see where it
/// starts and stops.
///
/// APPEND, never rewrite, and best-effort. Append-only is what makes the file
/// worth trusting — nothing in it is ever quietly revised, so a correction
/// arrives as a new block (see `pairing_update_text`) rather than as a silent
/// edit to an old one. Best-effort because the log and the marker already carry
/// the same facts, and a note that could not be written is not a reason to fail
/// a boot that has already put the tutor's data safely to one side.
fn append_note_block(dirs: &Dirs, body: &str) {
    let path = dirs.data.join(RECOVERY_NOTE);
    let framed = format!(
        "\n============================================================\n{body}\
         ============================================================\n"
    );
    match fs::OpenOptions::new().create(true).append(true).open(&path) {
        Ok(mut f) => {
            if let Err(e) = f.write_all(framed.as_bytes()) {
                app_log(&format!("could not append to {}: {e}", path.display()));
            }
        }
        Err(e) => app_log(&format!("could not open {}: {e}", path.display())),
    }
}

/// What the tutor is told, once, after his window is on screen. Greek first —
/// he reads Greek, and only whoever is helping him reads the English half.
///
/// It has to say the true and reassuring thing (nothing was deleted) AND leave
/// behind something findable, because he will not remember a dialog and cannot
/// be expected to open a log.
pub fn set_aside_notice(dirs: &Dirs, displaced: &Displaced) -> String {
    // EVERY name, always, each labelled with what it holds in words he uses.
    // The database alone was what this dialog used to say, and it is the half
    // that reads best — while the media folder is where his uploaded books
    // physically are. Naming one and not the other sends whoever helps him
    // looking for a library that is sitting right there under the other name.
    let mut lines_el: Vec<String> = Vec::new();
    let mut lines_en: Vec<String> = Vec::new();
    for name in &displaced.databases {
        lines_el.push(format!("  {name}\n      — η βάση: μαθήματα, curricula, σημειώσεις"));
        lines_en.push(format!("  {name}\n      — the database: lessons, curricula, notes"));
    }
    for name in &displaced.media {
        lines_el.push(format!("  {name}\n      — ΤΑ ΑΡΧΕΙΑ ΣΑΣ: τα βιβλία σε PDF και οι σελίδες"));
        lines_en.push(format!("  {name}\n      — YOUR FILES: the book PDFs and the page scans"));
    }
    for name in &displaced.directories {
        lines_el.push(format!("  {name}\n      — ολόκληρος ο φάκελος της βάσης δεδομένων"));
        lines_en.push(format!("  {name}\n      — the whole database folder"));
    }
    for name in &displaced.files {
        lines_el.push(format!(
            "  {name}\n      — ΕΝΑ ΔΙΚΟ ΣΑΣ ΑΡΧΕΙΟ: μετονομάστηκε εκεί που ήταν"
        ));
        lines_en.push(format!(
            "  {name}\n      — ONE OF YOUR OWN FILES: renamed where it stood"
        ));
    }
    // `main` only shows this when something WAS set aside, so an empty list is
    // unreachable — and still has to say something true rather than nothing.
    if lines_el.is_empty() {
        lines_el.push("  — (δείτε το αρχείο παρακάτω)".to_string());
        lines_en.push("  — (see the file below)".to_string());
    }
    format!(
        "Το GuitarTutor βρήκε δεδομένα από προηγούμενη εγκατάσταση και τα έβαλε \
         στην άκρη για να μπορέσει να ξεκινήσει.\n\n\
         ΔΕΝ ΔΙΑΓΡΑΦΗΚΕ ΤΙΠΟΤΑ. Μετονομάστηκαν και βρίσκονται εδώ:\n\
         {names_el}\n\
         στον φάκελο:\n  {data}\n\n\
         Αν λείπει κάτι δικό σας — βιβλία, μαθήματα, curricula — δείξτε σε όποιον \
         σας υποστηρίζει το αρχείο {file} που βρίσκεται στον ίδιο φάκελο:\n\
         {note}\n\n\
         ------------------------------------------------------------\n\n\
         GuitarTutor found data from an earlier installation and set it aside so \
         that it could start.\n\n\
         NOTHING WAS DELETED. It was renamed, and it is here:\n\
         {names_en}\n\
         in this folder:\n  {data}\n\n\
         If anything of yours seems to be missing — books, lessons, curricula — \
         show whoever supports GuitarTutor the file {file} in that same folder:\n\
         {note}",
        names_el = lines_el.join("\n"),
        names_en = lines_en.join("\n"),
        data = dirs.data.display(),
        file = RECOVERY_NOTE,
        note = dirs.data.join(RECOVERY_NOTE).display()
    )
}

fn run_logged(mut cmd: Command, log_path: &Path) -> Result<(), String> {
    let out = cmd
        .output()
        .map_err(|e| format!("failed to launch {:?}: {e}", cmd.get_program()))?;
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = f.write_all(&out.stdout);
        let _ = f.write_all(&out.stderr);
    }
    if out.status.success() {
        Ok(())
    } else {
        Err(format!(
            "{:?} exited with {}: {}",
            cmd.get_program(),
            out.status,
            String::from_utf8_lossy(&out.stderr)
        ))
    }
}

fn initdb_once(res: &Path, dirs: &Dirs, extra_args: &[&str]) -> Result<(), String> {
    // Throwaway --pwfile: postgres only ever listens on 127.0.0.1, the password
    // is a formality initdb requires for scram host auth.
    let pwfile = dirs.data.join(".initdb-pw");
    fs::write(&pwfile, "guitar\n").map_err(|e| e.to_string())?;
    let mut cmd = pg_command(res, dirs, "initdb");
    cmd.arg("-U")
        .arg("guitar")
        .arg(format!("--pwfile={}", pwfile.display()))
        .arg("--auth-host=scram-sha-256")
        .arg("--auth-local=trust")
        .arg("-E")
        .arg("UTF8")
        .args(extra_args)
        .arg("-D")
        .arg(&dirs.pgdata);
    let result = run_logged(cmd, &dirs.logs.join("postgres.log"));
    let _ = fs::remove_file(&pwfile);
    result
}

fn append_conf_overrides(dirs: &Dirs, port: u16) -> Result<(), String> {
    let conf = dirs.pgdata.join("postgresql.conf");
    let block = format!(
        "\n# --- GuitarTutor overrides (appended at first run) ---\n\
         listen_addresses = '127.0.0.1'\n\
         port = {port}\n\
         unix_socket_directories = '{}'\n\
         shared_buffers = 128MB\n",
        dirs.pgdata.display()
    );
    let mut f = fs::OpenOptions::new()
        .append(true)
        .open(&conf)
        .map_err(|e| format!("cannot open {}: {e}", conf.display()))?;
    f.write_all(block.as_bytes()).map_err(|e| e.to_string())
}

/// initdb a fresh cluster. Locale: en_US.UTF-8 (exists on macOS; on Linux fall
/// back to C.UTF-8 if unavailable). `icu` switches to `--locale-provider=icu
/// --icu-locale=el` for the Greek-collation retry.
pub fn init_cluster(res: &Path, dirs: &Dirs, port: u16, icu: bool) -> Result<Displaced, String> {
    let mut displaced = clear_unfinished_pgdata(res, dirs)?;
    if icu {
        initdb_once(res, dirs, &["--locale-provider=icu", "--icu-locale=el", "--locale=C.UTF-8"])?;
    } else if let Err(first) = initdb_once(res, dirs, &["--locale=en_US.UTF-8"]) {
        if cfg!(target_os = "macos") {
            return Err(first);
        }
        // Linux without en_US.UTF-8 generated: C.UTF-8 always exists. What is
        // being moved aside here is the wreckage of the initdb that just failed,
        // seconds ago, on this thread — but it is moved rather than deleted all
        // the same, because "I am sure this directory is worthless" is precisely
        // the belief that has to stop being load-bearing.
        displaced.absorb(set_aside_pgdata(
            res,
            dirs,
            "an initdb that failed on this machine's en_US.UTF-8 locale",
        )?);
        initdb_once(res, dirs, &["--locale=C.UTF-8"])
            .map_err(|second| format!("initdb failed twice: {first} / then {second}"))?;
    }
    append_conf_overrides(dirs, port)?;
    Ok(displaced)
}

/// Does the bundled initdb understand ICU? (zonky builds vary by platform.)
pub fn initdb_supports_icu(res: &Path, dirs: &Dirs) -> bool {
    pg_command(res, dirs, "initdb")
        .arg("--help")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("--icu-locale"))
        .unwrap_or(false)
}

/// Is `pgdata` empty (or absent, or unreadable — in which case `initdb` will
/// produce the real error and it is not ours to pre-empt)?
fn pgdata_is_empty(dirs: &Dirs) -> bool {
    // LITERALLY empty, debris included, and deliberately NOT the
    // content-aware question `media_is_empty` asks. This one is not "is there
    // anything of the tutor's in here" — it is "WILL `initdb` RUN IN HERE", and
    // initdb refuses any directory with an entry in it, `.DS_Store` very much
    // included. Answering it with `contains_content` would leave a Finder
    // dropping (or an interrupted initdb's skeleton of empty directories)
    // exactly where it is and hand initdb a directory it will not touch — the
    // day-one brick `clear_unfinished_pgdata` exists to prevent.
    //
    // What debris must NOT be allowed to do is masquerade as a cluster, and
    // that is a different predicate on a different question: `cluster_evidence`
    // / `contains_a_postgres_file`, which is where the `.DS_Store` fix lives.
    fs::read_dir(&dirs.pgdata)
        .map(|mut entries| entries.next().is_none())
        .unwrap_or(true)
}

/// Everything found in `pgdata` that only a REAL cluster puts there.
///
/// This exists because `PG_VERSION` is a three-byte file, and the old code read
/// its absence as "this is a half-built cluster, clear it". That is a
/// catastrophic amount of weight for three bytes to carry: a single corrupt or
/// lost file — a bad sector, an over-eager cleaner, a restore-from-backup that
/// missed one entry — and the entire cluster went with it. `PG_VERSION` is also
/// written EARLY by initdb, so its absence does not even reliably mean what it
/// was taken to mean.
///
/// So the question is asked the other way round: not "is the completion marker
/// there" but "is there anything here that a finished cluster would have left".
/// Each item below is written by a cluster and by nothing else:
///   * `global/pg_control` — the control file. If this exists, this IS a
///     cluster, whatever happened to PG_VERSION.
///   * a `base/` containing FILES — the per-database storage. This is where the
///     tutor's rows physically live.
///   * `pg_wal/` or `pg_xact/` containing FILES — write-ahead log and commit log.
///   * a megabyte of anything at all. An interrupted initdb leaves a skeleton of
///     empty directories and a few small files; a cluster with data in it does
///     not fit in 1 MiB.
///
/// FILES, RECURSIVELY — NOT DIRECTORY ENTRIES. This used to ask
/// `read_dir(sub).next().is_some()`, and that question bricked day one. The very
/// first thing `initdb` does is `mkdir` its whole skeleton, and that skeleton is
/// NESTED: `base/1`, `pg_wal/archive_status`, `pg_multixact/members`,
/// `pg_logical/snapshots`. Interrupt initdb in those first milliseconds — before
/// it has written one byte, before `PG_VERSION` — and `base/` and `pg_wal/` both
/// have an entry in them while the directory holds nothing whatsoever. The guard
/// then concluded "a real cluster is in here", refused to build, and said so in
/// a dialog blaming data that did not exist — on that launch and on every launch
/// after it, forever, with no way for the tutor to get past it. An empty
/// directory is not evidence of anything; a FILE is.
///
/// Bounded the same way `tree_size_at_least` is, and for the same reason: this
/// runs on the splash, and `base/` on a real install is thousands of files. It
/// stops at the first file it finds.
///
/// Returned as descriptions rather than a bool so the refusal can TELL the tutor
/// what was found instead of asserting something he has to take on faith.
fn cluster_evidence(pgdata: &Path) -> Vec<String> {
    let mut found = Vec::new();
    if pgdata.join("global").join("pg_control").exists() {
        found.push("global/pg_control (a cluster's control file)".to_string());
    }
    for sub in ["base", "global", "pg_wal", "pg_xact"] {
        if contains_a_postgres_file(&pgdata.join(sub), 0) {
            found.push(format!("{sub}/ with PostgreSQL's own files in it"));
        }
    }
    let bytes = tree_size_at_least(pgdata, 1024 * 1024);
    if bytes >= 1024 * 1024 {
        found.push("at least 1 MB of data files".to_string());
    }
    found
}

/// Does `dir` hold at least one file POSTGRES ITSELF would have written, at any
/// depth? Stops at the first one; depth-bounded like `tree_size_at_least` so a
/// pathological tree cannot hang the splash.
///
/// "Postgres itself", not "any file", and that distinction is the whole point.
/// This used to count any regular file, which made a permanent, unrecoverable
/// refusal out of a single `.DS_Store` — and `base/` is a directory Finder is
/// perfectly happy to display and therefore to write into. On the tutor's dad's
/// Mac, somebody browsing the data folder to help him over the phone would have
/// bricked the install: the refusal is deliberately permanent, so there is no
/// next launch that recovers from it. A cluster is proved by PostgreSQL's own
/// files, and by nothing else.
///
/// Which way the remaining doubt is spent: `is_postgres_file` errs towards YES
/// (any hex-ish name counts). A false YES refuses to build and costs a support
/// call; a false NO builds a new cluster on top of the tutor's data. Those are
/// not comparable, so the rule only ever excludes names we can positively
/// identify as somebody else's litter.
///
/// Symlinks count as neither files nor directories here — `file_type()` does not
/// follow them, so a symlink cannot be used to make an empty skeleton look like
/// a cluster, nor to walk this out of `pgdata`.
fn contains_a_postgres_file(dir: &Path, depth: u32) -> bool {
    if depth > 8 {
        return false;
    }
    let Ok(entries) = fs::read_dir(dir) else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        match entry.file_type() {
            Ok(t) if t.is_file() => {
                if is_postgres_file(&name) {
                    return true;
                }
            }
            Ok(t) if t.is_dir() => {
                if is_foreign_debris(&name) {
                    continue; // .Trashes, .Spotlight-V100, .fseventsd, …
                }
                if contains_a_postgres_file(&entry.path(), depth + 1) {
                    return true;
                }
            }
            _ => {}
        }
    }
    false
}

/// Would PostgreSQL itself have written a file by this name into `base/`,
/// `global/`, `pg_wal/` or `pg_xact/`?
///
/// Everything it puts there is either one of a handful of fixed names or named
/// from a NUMBER: a relation's filenode (`1259`, `1259_fsm`, `1259_vm`,
/// `16384.1`) or a log segment in hex (`000000010000000000000001`, and its
/// `.ready`/`.done`/`.history`/`.partial`/`.backup` companions, `0000` in
/// pg_xact). Nothing it writes begins with a dot.
fn is_postgres_file(name: &str) -> bool {
    if is_foreign_debris(name) {
        return false;
    }
    if matches!(
        name,
        "PG_VERSION" | "pg_control" | "pg_filenode.map" | "pg_internal.init"
    ) {
        return true;
    }
    // The stem before any `.suffix` or `_fork` must be a number — decimal for a
    // filenode, hex for a WAL or xact segment. Hex covers both.
    let stem = name.split(['.', '_']).next().unwrap_or("");
    !stem.is_empty() && stem.chars().all(|c| c.is_ascii_hexdigit())
}

/// Total size of `dir`, stopping as soon as `limit` is reached. Bounded on
/// purpose: the only question asked of it is "is this bigger than a skeleton",
/// and walking a 250 GB directory to answer that would hang the splash.
fn tree_size_at_least(dir: &Path, limit: u64) -> u64 {
    fn walk(dir: &Path, limit: u64, total: &mut u64, depth: u32) {
        if *total >= limit || depth > 8 {
            return;
        }
        let Ok(entries) = fs::read_dir(dir) else { return };
        for entry in entries.flatten() {
            if *total >= limit {
                return;
            }
            // Debris does not count towards "a megabyte of data files" either:
            // a Spotlight or Time Machine sidecar tree is easily a megabyte, and
            // it is not evidence of a cluster.
            if is_foreign_debris(&entry.file_name().to_string_lossy()) {
                continue;
            }
            match entry.file_type() {
                Ok(t) if t.is_dir() => walk(&entry.path(), limit, total, depth + 1),
                Ok(t) if t.is_file() => {
                    *total += entry.metadata().map(|m| m.len()).unwrap_or(0);
                }
                _ => {}
            }
        }
    }
    let mut total = 0;
    walk(dir, limit, &mut total, 0);
    total
}

/// May this app build a new cluster in `pgdata`?
///
/// Only when there is demonstrably nothing of the tutor's in there. If there is
/// evidence of a real cluster whose `PG_VERSION` has gone missing, this REFUSES
/// — and says what it found, and says that it changed nothing. That is the
/// honest answer: a cluster missing one small file is very often repairable by
/// somebody who knows what they are doing, and is never repairable once this app
/// has built a new one on top of it.
///
/// Checked in two places on purpose: `main::boot` calls it BEFORE it writes the
/// `seeding` marker (so a refusal does not also change the install's recorded
/// state), and `clear_unfinished_pgdata` calls it again as the last thing
/// standing between any future caller and the directory.
pub fn pgdata_is_safe_to_build_in(dirs: &Dirs) -> Result<(), String> {
    if pgdata_is_empty(dirs) {
        return Ok(());
    }
    let evidence = cluster_evidence(&dirs.pgdata);
    if evidence.is_empty() {
        return Ok(());
    }
    Err(format!(
        "Ο φάκελος της βάσης δεδομένων του GuitarTutor υπάρχει και περιέχει \
         δεδομένα, αλλά λείπει ένα μικρό αρχείο που χρειάζεται για να ανοίξει.\n\
         Το GuitarTutor ΔΕΝ άλλαξε και ΔΕΝ διέγραψε τίποτα. Μην διαγράψετε τον \
         φάκελο — στείλτε αυτό το μήνυμα σε όποιον σας υποστηρίζει.\n\n\
         GuitarTutor's database folder exists and contains data, but the small \
         file that identifies it (PG_VERSION) is missing.\n\
         Nothing has been changed and nothing has been deleted. Building a new \
         database here would destroy what is in it, so GuitarTutor stopped \
         instead.\n\n\
         Folder: {}\n\
         What is in there: {}",
        dirs.pgdata.display(),
        evidence.join(", ")
    ))
}

/// A non-empty pgdata with no `PG_VERSION` is USUALLY an `initdb` that never
/// finished — the machine died, or the tutor quit, in the seconds it takes.
/// `initdb` refuses to run in a directory that is not empty, so leaving it would
/// brick the install permanently on a message nobody outside Postgres
/// understands.
///
/// "Usually" is the word the old version of this function did not have. It
/// treated `!PG_VERSION` as proof that nothing of value could be in there and
/// deleted the lot. Now: anything that looks like a real cluster makes this
/// REFUSE (see `pgdata_is_safe_to_build_in`), and what is left — a genuine
/// initdb skeleton — is MOVED ASIDE rather than deleted, so even a wrong call
/// here costs disk space and not data.
fn clear_unfinished_pgdata(res: &Path, dirs: &Dirs) -> Result<Displaced, String> {
    if pgdata_is_empty(dirs) {
        return Ok(Displaced::nothing());
    }
    pgdata_is_safe_to_build_in(dirs)?;
    app_log(
        "pgdata holds a half-written cluster (no PG_VERSION, and nothing in it that \
         only a real cluster leaves) — an earlier initdb did not finish. Moving it \
         aside and starting over.",
    );
    // The receipt is RETURNED, not `.map(|_| ())`-ed away. This path can move
    // `<data>/media` too — `set_aside_pgdata` takes the media with the cluster —
    // and the old discard is one of the three that let the tutor's whole library
    // move with nothing said about it anywhere he would look.
    set_aside_pgdata(res, dirs, "an initdb that did not finish: no PG_VERSION was ever written")
}

/// Move `pgdata` out of the way and leave an empty one in its place.
///
/// This replaces `wipe_pgdata`, which did `remove_dir_all`. Both of its callers
/// had a good argument that the directory could not matter — one had just
/// created it seconds earlier, the other had checked there was no PG_VERSION —
/// and `remove_dir_all` on the tutor's database directory is still not a thing
/// this app should contain. A rename is the same operation minus the part that
/// cannot be undone.
///
/// `Ok(None)` means there was nothing there to move.
///
/// THE MEDIA GOES TOO. A pgdata directory is EVERY database at once, `guitar`
/// included, so setting it aside orphans `<data>/media` exactly as renaming the
/// database would — and the boot that follows builds a new cluster, a new
/// `guitar`, and a fresh library, after which the API's startup sweep meets the
/// tutor's whole media tree with no rows behind it. Same displacement, same
/// shared suffix, same order: media first (see `displace_media`).
pub fn set_aside_pgdata(res: &Path, dirs: &Dirs, why: &str) -> Result<Displaced, String> {
    // Read-only checks first: an empty pgdata is a no-op for BOTH halves. The
    // media belongs to the cluster, so it does not move when the cluster does
    // not move.
    if pgdata_is_empty(dirs) {
        ensure_pgdata_dir(dirs)?;
        return Ok(Displaced::nothing());
    }
    let suffix = free_pgdata_suffix(dirs);
    let name = format!("{SUPERSEDED_DIR_PREFIX}{suffix}");
    // UNDETERMINED, not `Some(&name)`. The pgdata rename below can fail — a
    // permission problem, a postmaster still holding the directory — and the
    // same rule applies here as to `ALTER DATABASE`: a record is only ever
    // written about something that has already happened.
    let mut displaced = displace_media(
        res,
        dirs,
        &suffix,
        Companion::Unknown,
        "the uploaded books, PDFs and page scans of the GuitarTutor database \
         directory that is being set aside in the same moment — they are one \
         thing and must be restored together",
    )?;
    let media = displaced.sole_media().map(str::to_string);
    let target = dirs.data.join(&name);
    // A sibling of pgdata inside the data folder: same filesystem, so the rename
    // is atomic and instant however big the cluster is, and it lands where
    // anyone opening that folder will see it.
    fs::rename(&dirs.pgdata, &target).map_err(|e| {
        format!(
            "cannot move {} aside to {} ({e}) — nothing was changed",
            dirs.pgdata.display(),
            target.display()
        )
    })?;
    // ANNOUNCE BEFORE `ensure_pgdata_dir`, which can fail on a full or
    // read-only data folder — and used to fail with the whole cluster AND the
    // whole media tree already moved and nothing written down. Same correction
    // as in `displace_media`, same reason: the record is the recovery.
    //
    // The companion here is what displacement ACTUALLY produced, never what it
    // was going to be called: an empty `<data>/media` moves nothing, and a note
    // that names a folder which does not exist is exactly the kind of untrue
    // recovery instruction this file exists to avoid.
    displaced.absorb(announce_set_aside(
        res,
        dirs,
        "directory",
        &name,
        &match &media {
            Some(m) => Companion::Is(m.clone()),
            None => Companion::Alone,
        },
        why,
    ));
    if let Some(m) = &media {
        reconcile_pairing(dirs, m, &name);
    }
    ensure_pgdata_dir(dirs)?;
    Ok(displaced)
}

/// An empty `pgdata`, 0700 — the mode `initdb` refuses to run without.
fn ensure_pgdata_dir(dirs: &Dirs) -> Result<(), String> {
    fs::create_dir_all(&dirs.pgdata).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&dirs.pgdata, fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

// ---- reaping ----------------------------------------------------------------
//
// THE POLICY: a set-aside thing may be removed only when ALL FOUR hold.
//
//   1. This boot reached `complete`. `reap_superseded` takes an
//      `&InstallComplete`, the receipt minted by `finalize_install` from a
//      RE-READ of the marker, so this is enforced by the compiler and not by
//      call ordering. An install that is still in trouble never reaps: the
//      fallback stays exactly where it is until there is a working install
//      beside it.
//   2. It is not among the `SUPERSEDED_KEEP` (3) most recent of its kind.
//   3. Its stamp is not the OLDEST stamp of its kind — see `reapable`.
//   4. It is older than `SUPERSEDED_MIN_AGE` (30 days).
//
// Together, 2 and 3 mean nothing is removed until there are at least FIVE
// set-asides of one kind, and even then the first and the last three survive.
//
// A DATABASE AND ITS MEDIA ARE ONE UNIT ON THE WAY OUT TOO. A `media_superseded_`
// folder is released only when the `guitar_superseded_`/`pgdata_superseded_` it
// shares a stamp with is released, in the same pass. Releasing one and keeping
// the other would leave either rows nothing can illustrate or files nothing can
// name — half a recovery, which is the failure this whole round is about. A
// media folder with no partner (the crash window in `displace_media`) is
// therefore never released at all.
//
// WHAT THIS IS *NOT* FOR — a rationale that was in this comment and was FALSE.
// It used to say the reaper existed because a boot that sets a database aside
// and then dies before `finalize_install` leaves the marker on `seeding`, so the
// next launch sets another one aside, and so on until the disk fills. That
// scenario is real, and this reaper CANNOT ADDRESS IT: condition 1 requires an
// `InstallComplete`, and a boot in that loop never produces one. The comment was
// citing, as its reason to exist, the one case it can never run in.
//
// So the honest statement, chosen over making the reaper run on failed boots:
//
//     UNBOUNDED GROWTH IN A PERMANENT CRASH LOOP IS ACCEPTED.
//
// The alternative was to let a process that has just demonstrated it cannot
// finish an install delete the tutor's set-aside data on the way past — trusting
// our own judgement at the exact moment it is least trustworthy, to reclaim
// space in a situation where the only thing on that disk that matters is the
// data we would be deleting. Note also what a crash loop does NOT threaten:
// displacement is `rename(2)`, which needs no free space, so the set-asides
// themselves survive a full disk. What fails on a full disk is `initdb` and
// `pg_restore` — this boot — and that failure is a bilingual dialog and a
// support call, not a loss. A crash loop persisting for the thirty days it would
// take to make anything eligible anyway is a support call long before it is a
// disk-space problem.
//
// What the reaper IS for, then: an install that was interrupted a few times and
// then SUCCEEDED, whose set-asides are still sitting there a month later on a
// machine that has been working fine ever since. That is a real (if rare)
// sequence, it ends in a completed boot, and it is exactly what conditions 1–4
// describe.
//
// Why 3 and 30 days rather than something tighter. The path that creates
// set-asides closes permanently the moment one first run completes (from then on
// `disk_state` is `Complete` and the plan is `Skip` forever), so the realistic
// count is zero or one and NOTHING IS EVER ACTUALLY REAPED. The policy exists
// for the pathological case only, and its numbers are chosen so the ordinary
// case can never touch it: three is more than any non-pathological sequence
// produces, and thirty days is longer than it takes anyone to notice that
// something is missing and ask.
//
// Why recency is not the only ordering. Keeping "the most recent N" is exactly
// wrong if the OLDEST set-aside is the precious one — and in a crash loop it
// usually is, because the first one holds whatever was there before this app
// started failing while the later ones are successive fragments of the bundled
// starter restore. Rule 3 is the fix: the oldest of each kind is pinned and
// never released, so both ends of the history survive and only the middle of a
// long pathological run is ever released.
//
// What remains, stated rather than buried: this is still the only place in the
// desktop shell that removes something of the tutor's for good. It is bounded by
// four independent conditions, none of which the ordinary install can meet.

/// Which names may be removed, given all of them. Pure — the policy is decided
/// here and tested exhaustively, and the two callers below only do the I/O.
///
/// A name whose stamp cannot be read is NEVER reapable, AND TAKES NO PART IN THE
/// POLICY AT ALL: an unparseable name is something this build did not write, so
/// it must not be able to fill a slot in the keep window or to stand in for the
/// pinned oldest either.
///
/// That second half is the bug this version fixes. The policy used to be applied
/// positionally over `names.sort()` — every name, parseable or not. Two ways
/// that goes wrong, both of them by EXPOSING a genuine set-aside:
///
///   * a name sorting BELOW every timestamp (a folder called
///     `pgdata_superseded_` with nothing after it, or `…_.hidden`, or anything
///     starting with a character under `'0'`) takes index 0 — so the pin lands
///     on the alien, and the genuinely oldest set-aside, the one most likely to
///     hold the tutor's own work, becomes reapable;
///   * a name sorting ABOVE every timestamp (`…_someone-elses-backup`) takes a
///     slot in the "3 most recent" window — so a set-aside from last week drops
///     out of the keep window and can be released.
///
/// Both disappear once the policy is expressed over PARSED STAMPS. The pin is
/// then by timestamp rather than by position, which also means a tie — two
/// set-asides made in the same second, distinguished only by the `_2`
/// uniquifier — pins BOTH, because "the oldest" is a moment in time and both of
/// those are it.
fn reapable(names: &[String], prefix: &str, cutoff_stamp: &str, keep: usize) -> Vec<String> {
    // Only names this build wrote take part. Fixed-width big-endian stamps, so
    // comparing the parsed stamps as strings compares them as times.
    let mut ours: Vec<(&str, &String)> = names
        .iter()
        .filter_map(|n| stamp_of(n, prefix).map(|s| (s, n)))
        .collect();
    // By stamp, then by full name so the order is total and stable across the
    // `_2`/`_3` uniquifiers that share one stamp.
    ours.sort_by(|a, b| a.0.cmp(b.0).then_with(|| a.1.cmp(b.1)));

    let too_recent_from = ours.len().saturating_sub(keep);
    if too_recent_from == 0 {
        return Vec::new();
    }
    // THE OLDEST STAMP IS NEVER RELEASED.
    //
    // This is the answer to the one real weakness of "keep the most recent N".
    // In a crash loop the FIRST set-aside is the one most likely to hold
    // something of the tutor's (whatever was there before this app started
    // failing), while the later ones are successive fragments of the bundled
    // starter restore. Retaining by recency alone would release exactly the
    // wrong one first. Pinning the oldest MOMENT costs one database (or the
    // handful that share its second) in the pathological case — the only case
    // that ever reaches this code — and removes the only ordering in this file
    // that could have been argued the other way.
    let oldest = ours[0].0;
    ours[..too_recent_from]
        .iter()
        .filter(|(stamp, _)| *stamp != oldest && *stamp < cutoff_stamp)
        .map(|(_, name)| (*name).clone())
        .collect()
}

/// The media folder set aside with `name`, if this build would have named one.
///
/// `guitar_superseded_<s>` → `media_superseded_<s>`; likewise for
/// `pgdata_superseded_<s>`. The suffix is the WHOLE tail including any `_2`
/// uniquifier, because `displace_media` uses the same tail — see `free_suffix`.
/// `None` for a name this build did not write, which is also the only answer
/// that could ever make the reaper touch something it does not recognise.
fn companion_media_name(name: &str, prefix: &str) -> Option<String> {
    let suffix = name.strip_prefix(prefix)?;
    stamp_of(name, prefix)?; // it must really be one of ours
    Some(format!("{SUPERSEDED_MEDIA_PREFIX}{suffix}"))
}

/// The `YYYYMMDDTHHMMSSZ` out of `<prefix><stamp>[_n]`, if it is really one.
fn stamp_of<'a>(name: &'a str, prefix: &str) -> Option<&'a str> {
    let stamp = name.strip_prefix(prefix)?.split('_').next()?;
    is_compact_stamp(stamp).then_some(stamp)
}

/// Is this exactly one of our `utc_compact_stamp` strings?
///
/// Split out because two different things ask it now: the reaper, about names
/// THIS build wrote, and `describe_api_quarantine`, about batch folders the API
/// child wrote (`app/brain/media.py` uses the same format, deliberately, so one
/// glance at `<data>/` and `<data>/media/_superseded/` reads as one story).
fn is_compact_stamp(stamp: &str) -> bool {
    let b = stamp.as_bytes();
    b.len() == 16
        && b[8] == b'T'
        && b[15] == b'Z'
        && b[..8].iter().all(u8::is_ascii_digit)
        && b[9..15].iter().all(u8::is_ascii_digit)
}

/// Remove the set-aside databases and directories the policy above releases.
///
/// Takes the completion receipt BY REFERENCE for the same reason
/// `show_main_window` does: there is exactly one way to obtain an
/// `InstallComplete`, so "we only reap from a proven-good install" is a fact the
/// compiler checks rather than a convention a later edit can drop.
///
/// Infallible by design. Everything here is housekeeping; a failure to reap
/// costs disk space, and no amount of disk space is worth failing a boot that
/// has already succeeded.
pub fn reap_superseded(res: &Path, dirs: &Dirs, port: u16, _complete: &InstallComplete) {
    let Some(cutoff_at) = SystemTime::now().checked_sub(SUPERSEDED_MIN_AGE) else {
        return; // a clock before 1970 + 30 days: reap nothing
    };
    let cutoff = utc_compact_stamp(cutoff_at);

    for name in reapable(
        &list_superseded_databases(res, dirs, port),
        SUPERSEDED_DB_PREFIX,
        &cutoff,
        SUPERSEDED_KEEP,
    ) {
        if !is_safe_identifier(&name) {
            continue;
        }
        let sql = format!("DROP DATABASE IF EXISTS \"{name}\" WITH (FORCE)");
        match psql_exec(res, dirs, port, "postgres", &sql) {
            Ok(()) => {
                let line = format!(
                    "reaped the set-aside database '{name}': this install has been \
                     complete for over 30 days and at least {SUPERSEDED_KEEP} newer \
                     set-aside databases remain"
                );
                app_log(&line);
                append_reap_note(dirs, &line);
                mark_reaped(dirs, &name);
                reap_companion_media(dirs, &name, SUPERSEDED_DB_PREFIX);
            }
            Err(e) => app_log(&format!("could not reap '{name}' ({e}) — leaving it")),
        }
    }

    for name in reapable(
        &list_superseded_dirs(dirs),
        SUPERSEDED_DIR_PREFIX,
        &cutoff,
        SUPERSEDED_KEEP,
    ) {
        let path = dirs.data.join(&name);
        match fs::remove_dir_all(&path) {
            Ok(()) => {
                let line = format!(
                    "reaped the set-aside directory '{name}': this install has been \
                     complete for over 30 days and at least {SUPERSEDED_KEEP} newer \
                     set-aside directories remain"
                );
                app_log(&line);
                append_reap_note(dirs, &line);
                mark_reaped(dirs, &name);
                reap_companion_media(dirs, &name, SUPERSEDED_DIR_PREFIX);
            }
            Err(e) => app_log(&format!(
                "could not reap {} ({e}) — leaving it",
                path.display()
            )),
        }
    }
}

/// Release the media folder that was set aside with `name`, now that `name`
/// itself has been released.
///
/// The pair goes out together or not at all — the same rule that governs the way
/// in. Keeping the media after its database is gone would leave a folder of
/// `<uuid>/` directories nothing on the machine can name, and the recovery note
/// would be pointing at half a rescue.
///
/// Only ever called with a name the policy has ALREADY released, and only ever
/// looks for the one directory this build would itself have written for that
/// name — so the pairing can never widen what the reaper touches.
fn reap_companion_media(dirs: &Dirs, name: &str, prefix: &str) {
    let Some(media) = companion_media_name(name, prefix) else {
        return;
    };
    let path = dirs.data.join(&media);
    if !path.is_dir() {
        return; // nothing travelled with it — `<data>/media` was empty then
    }
    match fs::remove_dir_all(&path) {
        Ok(()) => {
            let line = format!(
                "reaped the set-aside media folder '{media}' with the '{name}' it \
                 belongs to — a database and its media are released together or \
                 not at all"
            );
            app_log(&line);
            append_reap_note(dirs, &line);
            mark_reaped(dirs, &media);
        }
        Err(e) => app_log(&format!(
            "could not reap {} ({e}) — leaving it; note that '{name}' is gone, so \
             this folder now has no partner",
            path.display()
        )),
    }
}

/// Write "this one is gone" against the marker entry the reaper has just
/// released.
///
/// The entry itself STAYS — this file is a ledger and nothing is ever removed
/// from it — but until now it stayed with no way to tell that what it describes
/// no longer exists. Whoever read the marker (or the note entry beside it) was
/// then following instructions to rename back a database the reaper had dropped
/// a month earlier, and would conclude something had gone badly wrong when
/// nothing had. `append_reap_note` records the same fact in the plain-text file;
/// this is it in the file that is actually parsed, which is also what stops
/// `unannounced` offering it to the tutor as news.
///
/// Best-effort, like every other write in the reaping section: failing to record
/// a successful reap costs a confusing line in one file, and no amount of that
/// is worth failing a boot that has already succeeded.
fn mark_reaped(dirs: &Dirs, name: &str) {
    let Some(mut state) = read_state(dirs) else {
        return;
    };
    let mut touched = false;
    for entry in state.set_aside.iter_mut() {
        if entry.name == name && entry.reaped_at.is_none() {
            entry.reaped_at = Some(now_secs());
            touched = true;
        }
    }
    if !touched {
        return;
    }
    if let Err(e) = save_state(dirs, &state) {
        app_log(&format!(
            "reaped '{name}' but could not mark its record in {INSTALL_STATE_FILE} \
             ({e}) — the removal is in app.log and in {RECOVERY_NOTE}"
        ));
    }
}

/// The note records removals too. An entry that describes something no longer
/// there would otherwise send whoever is helping on a hunt for a database that
/// was reaped a year ago.
fn append_reap_note(dirs: &Dirs, line: &str) {
    let path = dirs.data.join(RECOVERY_NOTE);
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "\n{} {line}", utc_compact_stamp(SystemTime::now()));
    }
}

fn list_superseded_databases(res: &Path, dirs: &Dirs, port: u16) -> Vec<String> {
    match psql_scalar(
        res,
        dirs,
        port,
        "postgres",
        // The underscores are escaped: unescaped they are LIKE wildcards, and a
        // pattern that matches more than it should is the last thing a function
        // feeding a DROP wants.
        "SELECT datname FROM pg_database WHERE datname LIKE 'guitar\\_superseded\\_%'",
    ) {
        Ok(out) => out
            .lines()
            .map(|l| l.trim().to_string())
            .filter(|l| l.starts_with(SUPERSEDED_DB_PREFIX))
            .collect(),
        Err(e) => {
            app_log(&format!("could not list set-aside databases ({e}) — reaping nothing"));
            Vec::new()
        }
    }
}

fn list_superseded_dirs(dirs: &Dirs) -> Vec<String> {
    let Ok(entries) = fs::read_dir(&dirs.data) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|e| e.file_type().map(|t| t.is_dir()).unwrap_or(false))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with(SUPERSEDED_DIR_PREFIX))
        .collect()
}

/// Every psql this app runs, built one way.
///
/// `-X` is the load-bearing flag. Without it psql sources the DEVELOPER'S (or
/// the tutor's) `~/.psqlrc` before doing anything else, and a single `\timing`
/// or `\set` line in there prepends text to stdout — which turns
/// `greek_collation_ok` into a false negative and hands a perfectly good Mac a
/// fatal dialog blaming its text collation, or makes `probe_database` read
/// "unexpected answer" and decline to act. The whole point of `-tA` is a
/// machine-readable answer; `-X` is what makes that promise keepable.
/// `pg_command` separately strips the libpq environment, so `PGDATABASE`,
/// `PGOPTIONS` and friends cannot redirect or reconfigure us either.
fn psql_command(res: &Path, dirs: &Dirs, port: u16, db: &str) -> Command {
    let mut cmd = pg_command(res, dirs, "psql");
    cmd.arg("-X")
        .arg("-U")
        .arg("guitar")
        .arg("-h")
        .arg(&dirs.pgdata) // unix socket — auth-local=trust, no password needed
        .arg("-p")
        .arg(port.to_string())
        .arg("-d")
        .arg(db)
        .arg("-tA");
    cmd
}

fn psql_scalar(res: &Path, dirs: &Dirs, port: u16, db: &str, sql: &str) -> Result<String, String> {
    let out = psql_command(res, dirs, port, db)
        .arg("-c")
        .arg(sql)
        .output()
        .map_err(|e| format!("psql: {e}"))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

/// Run SQL for its effect. `ON_ERROR_STOP=1` is the difference between "psql
/// ran" and "the statement worked" — without it psql happily exits 0 after a
/// failed command, which would let a failed DROP pass for a successful one.
fn psql_exec(res: &Path, dirs: &Dirs, port: u16, db: &str, sql: &str) -> Result<(), String> {
    let out = psql_command(res, dirs, port, db)
        .arg("-v")
        .arg("ON_ERROR_STOP=1")
        .arg("-c")
        .arg(sql)
        .output()
        .map_err(|e| format!("psql: {e}"))?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!(
            "psql exited with {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr).trim()
        ))
    }
}

/// The app's Greek search is non-negotiable: `lower('ΚΙΘΑΡΑ')` must fold to
/// 'κιθαρα' in this cluster's collation. Run right after first initdb+start.
pub fn greek_collation_ok(res: &Path, dirs: &Dirs, port: u16) -> bool {
    matches!(
        psql_scalar(res, dirs, port, "postgres", "SELECT lower('ΚΙΘΑΡΑ') = 'κιθαρα'").as_deref(),
        Ok("t")
    )
}

/// `createdb guitar`, then restore the bundled seed if the build carries one.
/// Returns what the seed step DID, plus the receipt for anything it had to move
/// out of the way on the way there.
///
/// Only ever called with NO `guitar` database present — either because there
/// never was one (`SeedPlan::Seed`) or because `displace_database` has just
/// moved the fragment an interrupted run left behind out of the way
/// (`SeedPlan::Reseed`). That is what makes it idempotent: `pg_restore` never
/// runs twice into one database. The media copy is idempotent by SKIPPING what
/// is already there rather than by overwriting it — see `copy_tree`, and note
/// that `<data>/media` holds the tutor's own uploads too.
pub fn create_and_seed_db(
    res: &Path,
    dirs: &Dirs,
    port: u16,
) -> Result<(SeedOutcome, Displaced), String> {
    // BEFORE `createdb`, and before anything else: a newly created database is
    // never put beside a media tree it does not index. Normally a no-op — the
    // media has already travelled with whatever was displaced — but this is the
    // one line EVERY freshly created database passes through, including the
    // `SeedPlan::Seed` paths that never call `displace_database` at all. See
    // `displace_media_for_a_fresh_database`.
    //
    // The receipt travels out with the outcome. This is the fourth path that
    // used to swallow it, and the worst of the four: it is the LAST thing
    // between the tutor's uploaded books and a fresh starter library, so when it
    // fires it has almost always just moved his entire library.
    let mut displaced = displace_media_for_a_fresh_database(res, dirs)?;

    let mut cmd = pg_command(res, dirs, "createdb");
    cmd.arg("-U")
        .arg("guitar")
        .arg("-h")
        .arg(&dirs.pgdata)
        .arg("-p")
        .arg(port.to_string())
        .arg("guitar");
    run_logged(cmd, &dirs.logs.join("postgres.log"))?;

    // CI unpacks seed.tar.gz (db.dump + media/ + manifest.json — the same shape
    // scripts/make-seed.sh produces) into Resources/seed/, so at runtime the
    // seed is plain files: pg_restore the dump, copy media/ recursively. No
    // archive handling in Rust at all — see desktop/README.md.
    let seed = res.join("seed");
    let dump = seed.join("db.dump");
    if !dump.exists() {
        return Ok((SeedOutcome::NothingToRestore, displaced));
    }
    let mut cmd = pg_command(res, dirs, "pg_restore");
    cmd.arg("--no-owner")
        .arg("--no-privileges")
        .arg("-U")
        .arg("guitar")
        .arg("-h")
        .arg(&dirs.pgdata)
        .arg("-p")
        .arg(port.to_string())
        .arg("-d")
        .arg("guitar")
        .arg(&dump);
    run_logged(cmd, &dirs.logs.join("postgres.log"))?;

    let media_src = seed.join("media");
    if media_src.is_dir() {
        // The receipt for anything of HIS this copy had to rename in place —
        // see `set_aside_file`. Absorbed rather than discarded for the same
        // reason every other displacement in this file is: a file of the
        // tutor's that moved and was never mentioned is, to him, a file that
        // disappeared.
        displaced.absorb(copy_tree(res, dirs, &media_src, &dirs.media)?);
    }
    Ok((SeedOutcome::Restored, displaced))
}

/// Copy the bundled seed media into `<data>/media`, WITHOUT overwriting
/// anything the tutor put there.
///
/// `<data>/media` is not a scratch directory — it is where his uploaded audio,
/// scores and images live. A plain recursive copy overwrites any file whose
/// relative path happens to match a bundled asset's, and on the `Reseed` path
/// this runs against a media directory that has already been used. The window is
/// narrow (a name collision with a bundled asset) and the loss would be total
/// and silent, which is the worst combination a narrow window can have.
///
/// So: a destination that already exists is never overwritten in place.
///   * same size → skip. This is the ordinary case, a re-copy of the identical
///     bundled asset, and skipping it makes the seed step idempotent by doing
///     LESS rather than by writing more.
///   * different size → the existing file is renamed aside first
///     (`<name>.superseded-<stamp>`), then the seed asset is written.
fn copy_tree(res: &Path, dirs: &Dirs, src: &Path, dst: &Path) -> Result<Displaced, String> {
    let mut displaced = Displaced::nothing();
    fs::create_dir_all(dst).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(src).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let to: PathBuf = dst.join(entry.file_name());
        let ty = entry.file_type().map_err(|e| e.to_string())?;
        if ty.is_dir() {
            displaced.absorb(copy_tree(res, dirs, &entry.path(), &to)?);
        } else if ty.is_file() {
            let from = entry.path();
            match existing_file_len(&to) {
                Some(there) if Some(there) == existing_file_len(&from) => continue,
                Some(_) => displaced.absorb(set_aside_file(res, dirs, &to)?),
                None => {}
            }
            fs::copy(&from, &to)
                .map_err(|e| format!("copy {} -> {}: {e}", from.display(), to.display()))?;
        }
    }
    Ok(displaced)
}

fn existing_file_len(path: &Path) -> Option<u64> {
    let meta = fs::metadata(path).ok()?;
    meta.is_file().then_some(meta.len())
}

/// Rename one file out of the way, in place, keeping the original name as the
/// prefix so it stays next to its replacement and is obvious in a file listing.
///
/// UNDER THE SAME RULE AS EVERYTHING ELSE IN THIS FILE, which it was not.
/// This was the one displacement recorded in `app.log` and NOWHERE ELSE: no
/// entry in `install-state.json`, no block in the recovery note, and never a
/// word in the dialog. The rule this module is built on is that a set-aside
/// thing nobody can find is worth no more than a deleted one — and a file inside
/// `<data>/media` is his own upload, under a name that happened to collide with
/// a bundled seed asset. It is a smaller loss than a database and it is the same
/// KIND of loss, so it goes through `announce_set_aside` like the rest and comes
/// back as a receipt the caller cannot drop.
fn set_aside_file(res: &Path, dirs: &Dirs, path: &Path) -> Result<Displaced, String> {
    let stamp = utc_compact_stamp(SystemTime::now());
    let base = path.as_os_str().to_string_lossy().into_owned();
    let mut target = PathBuf::from(format!("{base}.superseded-{stamp}"));
    let mut n = 2;
    while target.exists() && n < 1000 {
        target = PathBuf::from(format!("{base}.superseded-{stamp}-{n}"));
        n += 1;
    }
    fs::rename(path, &target).map_err(|e| {
        format!(
            "cannot move {} aside to {} ({e}) — nothing was overwritten",
            path.display(),
            target.display()
        )
    })?;
    // Shown RELATIVE TO THE DATA FOLDER, which is the folder every other entry
    // and the dialog itself already name: "media/lesson/backing.mp3.superseded-…"
    // is something a reader can find, where an absolute path repeated in full is
    // something they have to compare character by character.
    let shown = target
        .strip_prefix(&dirs.data)
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| target.display().to_string());
    Ok(announce_set_aside(
        res,
        dirs,
        "file",
        &shown,
        // Measured, and trivially: a single file has no second half. Nothing
        // else was moved with it and nothing is coming.
        &Companion::Alone,
        "a file of yours in the media folder had the same name as one of the files \
         GuitarTutor installs, and a different size — so yours was renamed instead \
         of being written over",
    ))
}

/// Written on EVERY boot, right after the ports are chosen — not just on the
/// first run — because `web_port`/`api_port` are what the next launch reads
/// back to keep the origin stable.
///
/// A CONVENIENCE, never a correctness requirement, and its caller treats a
/// failure as such: the worst a lost meta.json can do is make the next launch
/// scan for ports instead of reusing yesterday's. It used to be fatal, which —
/// once it moved from first-run-only to every boot — meant a full disk or a
/// read-only data directory turned a working install into one that refused to
/// launch. The file that IS worth refusing to launch over is
/// `install-state.json`; see `mark_complete`.
pub fn write_meta(dirs: &Dirs, port: u16, ports: AppPorts) -> Result<(), String> {
    let meta = Meta {
        app_version: env!("CARGO_PKG_VERSION").to_string(),
        pg_major: 16,
        pg_port: port,
        web_port: Some(ports.web),
        api_port: Some(ports.api),
    };
    fs::write(
        dirs.data.join("meta.json"),
        serde_json::to_string_pretty(&meta).expect("serialize meta"),
    )
    .map_err(|e| e.to_string())
}

/// The pair the previous launch used, if meta.json records one.
///
/// Preferring it is not cosmetic. localStorage and IndexedDB are partitioned
/// by ORIGIN, and an origin includes the port (cookies do not — that is why
/// only these two matter here). A pair that rolls on every launch would
/// therefore hand the tutor a blank slate every time: theme, view preferences,
/// anything the web app keeps client-side, silently reset. Reusing the last
/// pair whenever it is still free keeps `http://localhost:8790` — or whatever
/// this install settled on — the same origin for good.
///
/// Missing/garbage file → `None`, and the scan runs as if this were day one.
pub fn remembered_app_ports(dirs: &Dirs) -> Option<AppPorts> {
    let raw = fs::read_to_string(dirs.data.join("meta.json")).ok()?;
    let meta: Meta = serde_json::from_str(&raw).ok()?;
    Some(AppPorts {
        web: meta.web_port?,
        api: meta.api_port?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The scan under test, with a fixed set of "busy" ports instead of this
    /// machine's real ones. Every invariant below is therefore decided by the
    /// closure, not by whatever the CI runner or Chris's laptop has bound —
    /// the whole point: a build must never fail because a developer had
    /// something on 8790.
    fn pick(busy: &[u16], pg: u16, remembered: Option<AppPorts>) -> Option<AppPorts> {
        let busy = busy.to_vec();
        pick_app_ports_with(pg, remembered, &move |p| !busy.contains(&p))
    }

    /// Nothing in the way: the historical pair, so an ordinary install keeps
    /// the origin it has always had.
    #[test]
    fn empty_machine_gets_the_default_pair() {
        assert_eq!(
            pick(&[], 5434, None),
            Some(AppPorts { web: 8790, api: 8791 })
        );
    }

    /// The invariants the rest of the shell relies on, over a spread of
    /// arbitrary busy sets: api strictly above web (so never equal), neither
    /// equal to the database port, neither busy, both inside the window.
    #[test]
    fn picked_ports_never_collide() {
        let pg = 8795; // deliberately INSIDE the app window, which pg's own
                       // range never is — proves the exclusion is real
        for busy in [
            vec![],
            vec![8790],
            vec![8790, 8791, 8792],
            (8790..8800).collect::<Vec<u16>>(),
            vec![8791, 8793, 8797, 8801],
        ] {
            let ports = pick(&busy, pg, None).expect("a pair exists for these busy sets");
            assert!(ports.api > ports.web, "{ports:?} for busy={busy:?}");
            assert_ne!(ports.web, pg);
            assert_ne!(ports.api, pg);
            assert!(!busy.contains(&ports.web), "{ports:?} picked a busy web port");
            assert!(!busy.contains(&ports.api), "{ports:?} picked a busy api port");
            assert!((WEB_PORT_DEFAULT..APP_PORT_LAST).contains(&ports.web));
            assert!(ports.api <= APP_PORT_LAST);
        }
    }

    /// A busy web candidate moves the OUTER loop on — the pair does not have to
    /// contain 8790.
    #[test]
    fn a_busy_web_candidate_moves_the_outer_loop_on() {
        assert_eq!(
            pick(&[8790], 5434, None),
            Some(AppPorts { web: 8791, api: 8792 })
        );
    }

    /// The case the old scan got wrong. It committed to the first free web port
    /// and then searched a FIXED 20-port sub-range above it: with 8791..=8810
    /// all busy that sub-range was full, every later web candidate was busy
    /// too, and it gave up — while 8790 + 8811 was sitting there free. The api
    /// candidate now runs to the top of the window, so a blocked band cannot
    /// end the scan.
    #[test]
    fn a_blocked_band_above_the_web_port_does_not_end_the_scan() {
        let busy: Vec<u16> = (8791..=8810).collect();
        assert_eq!(
            pick(&busy, 5434, None),
            Some(AppPorts { web: 8790, api: 8811 })
        );
    }

    /// Giving up must mean the window genuinely cannot seat two ports — one
    /// free port is not enough, since the api must sit above the web port.
    #[test]
    fn exhausted_window_gives_up() {
        let all: Vec<u16> = (8000..9000).collect();
        assert_eq!(pick(&all, 5434, None), None);

        // Only APP_PORT_LAST free: nothing can go above it.
        let busy: Vec<u16> = (8000..APP_PORT_LAST).collect();
        assert_eq!(pick(&busy, 5434, None), None);

        // Exactly two free, at opposite ends of the window.
        let busy: Vec<u16> = (8000..9000)
            .filter(|p| *p != WEB_PORT_DEFAULT && *p != APP_PORT_LAST)
            .collect();
        assert_eq!(
            pick(&busy, 5434, None),
            Some(AppPorts {
                web: WEB_PORT_DEFAULT,
                api: APP_PORT_LAST
            })
        );
    }

    /// Last launch's pair wins when it is still free: the browser origin — and
    /// therefore localStorage/IndexedDB — stays put across launches.
    #[test]
    fn remembered_ports_are_preferred() {
        let last = AppPorts { web: 8794, api: 8800 };
        assert_eq!(pick(&[], 5434, Some(last)), Some(last));
    }

    /// …but only when they are actually free, sane, and not the pg port.
    #[test]
    fn implausible_or_taken_remembered_ports_fall_back_to_the_scan() {
        let scanned = AppPorts { web: 8790, api: 8791 };
        // api taken
        assert_eq!(
            pick(&[8800], 5434, Some(AppPorts { web: 8794, api: 8800 })),
            Some(scanned)
        );
        // web taken
        assert_eq!(
            pick(&[8794], 5434, Some(AppPorts { web: 8794, api: 8800 })),
            Some(scanned)
        );
        // api below web — a hand-edited or corrupt meta.json
        assert_eq!(
            pick(&[], 5434, Some(AppPorts { web: 8800, api: 8794 })),
            Some(scanned)
        );
        // outside the advertised window
        assert_eq!(
            pick(&[], 5434, Some(AppPorts { web: 3000, api: 3001 })),
            Some(scanned)
        );
        // collides with the database port
        assert_eq!(
            pick(&[], 8794, Some(AppPorts { web: 8794, api: 8800 })),
            Some(AppPorts { web: 8790, api: 8791 })
        );
    }

    /// One real-socket check, written so it CANNOT fail spuriously: a port we
    /// are holding ourselves must read as taken. Anything stronger belongs in
    /// the injected-probe tests above.
    #[test]
    fn real_probe_sees_a_listener_we_hold() {
        let held = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("ephemeral port");
        let port = held.local_addr().expect("addr").port();
        assert!(!port_free(port), "a port we are listening on is not free");
        drop(held);
        // Deliberately NOT asserting it is free again: between the drop and the
        // probe the machine may hand that ephemeral port to someone else.
        let _ = port_free(port);
    }

    /// …and the same for a listener held ONLY on the v6 loopback, which is the
    /// case the v4-only probe missed: the webview asks for `localhost`, macOS
    /// hands it `::1` first, and it would have reached the squatter. Skipped
    /// (not failed) where there is no `::1` to bind — that machine cannot have
    /// the problem.
    #[test]
    fn real_probe_sees_a_v6_only_listener() {
        let Ok(held) = TcpListener::bind((Ipv6Addr::LOCALHOST, 0)) else {
            return; // no IPv6 loopback here
        };
        let port = held.local_addr().expect("addr").port();
        // Only meaningful if v4 is genuinely free — otherwise this proves
        // nothing about the v6 half.
        if TcpListener::bind((Ipv4Addr::LOCALHOST, port)).is_ok() {
            assert!(!port_free(port), "a v6-only listener must count as busy");
        }
    }

    /// The classification that keeps the app bootable on a machine with IPv6
    /// switched off: only EADDRINUSE proves a v6 listener.
    #[test]
    fn only_address_in_use_proves_a_v6_listener() {
        let busy = std::io::Error::from_raw_os_error(libc::EADDRINUSE);
        assert!(v6_probe_proves_busy(&busy));
        for benign in [libc::EADDRNOTAVAIL, libc::EAFNOSUPPORT, libc::EACCES] {
            let e = std::io::Error::from_raw_os_error(benign);
            assert!(
                !v6_probe_proves_busy(&e),
                "errno {benign} must not read as a listener ({e})"
            );
        }
    }

    /// The origin the browser sees must stay literal `localhost` — the session
    /// cookie is host-only and CORS compares origins as strings.
    #[test]
    fn origins_are_literal_localhost() {
        let ports = AppPorts { web: 8792, api: 8793 };
        assert_eq!(ports.web_origin(), "http://localhost:8792");
        assert_eq!(ports.api_base(), "http://localhost:8793");
        assert_eq!(
            ports.cors_origins(),
            "http://localhost:8792,http://127.0.0.1:8792"
        );
        assert!(!ports.are_defaults());
        assert!(AppPorts {
            web: WEB_PORT_DEFAULT,
            api: API_PORT_DEFAULT
        }
        .are_defaults());
    }

    /// A throwaway `Dirs` rooted at a directory of this test's own. Tests run in
    /// parallel in one process, so the tag has to make each one unique.
    fn tmp_dirs(tag: &str) -> Dirs {
        let data = std::env::temp_dir().join(format!("gt-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&data);
        fs::create_dir_all(data.join("pgdata")).expect("tmpdir");
        Dirs {
            pgdata: data.join("pgdata"),
            media: data.join("media"),
            secrets: data.join("secrets"),
            logs: data.join("logs"),
            data,
        }
    }

    fn touch_pg_version(dirs: &Dirs) {
        fs::write(dirs.pgdata.join("PG_VERSION"), "16\n").expect("PG_VERSION");
    }

    /// A resource root for tests that only need the recovery note to NAME one.
    ///
    /// Shaped like the real thing (`…/Contents/Resources/resources`, which is
    /// what `paths::resource_root` hands `firstrun`) so the paths the note
    /// prints look, in a test's output, exactly like the paths it prints on the
    /// tutor's machine. Nothing is executed out of it: the note is text. The
    /// tests that need commands that RUN use `bundled_res()` below.
    fn tmp_res(dirs: &Dirs) -> PathBuf {
        dirs.data.join("GuitarTutor.app/Contents/Resources/resources")
    }

    /// The REAL staged resource tree — `desktop/src-tauri/resources`, the same
    /// directory `stage-postgres.sh` fills and `tauri.conf.json` bundles.
    /// `None` when this checkout has not staged it (a fresh clone, or CI before
    /// the staging step), which is the only reason the live-cluster test skips.
    fn bundled_res() -> Option<PathBuf> {
        let res = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("resources");
        res.join("pg/bin/initdb").is_file().then_some(res)
    }

    /// The disk half, over every shape `<data>/install-state.json` can take.
    #[test]
    fn disk_state_reads_the_marker() {
        let dirs = tmp_dirs("diskstate");

        // Day one: initdb has not run.
        assert_eq!(disk_state(&dirs), DiskState::NoCluster);

        // …and a marker without a cluster is still day one: the marker alone is
        // not a cluster, and initdb has to run either way.
        mark_complete(&dirs, "stray").expect("write marker");
        assert_eq!(disk_state(&dirs), DiskState::NoCluster);

        touch_pg_version(&dirs);
        assert_eq!(disk_state(&dirs), DiskState::Complete);

        mark_seeding(&dirs).expect("write marker");
        assert_eq!(disk_state(&dirs), DiskState::SeedInterrupted);

        // The case that matters most: a cluster from a build that predates the
        // marker. Unmarked, NOT complete, NOT interrupted — the probe decides.
        fs::remove_file(install_state_path(&dirs)).expect("remove marker");
        assert_eq!(disk_state(&dirs), DiskState::Unmarked);

        // Junk, truncation and a phase from some future build all mean the same
        // thing: no information, ask the cluster.
        for junk in [
            "{ not json",
            "",
            r#"{"phase":"restoring-quantum","app_version":"9.9","at":0,"note":""}"#,
        ] {
            fs::write(install_state_path(&dirs), junk).expect("write junk");
            assert_eq!(disk_state(&dirs), DiskState::Unmarked, "for {junk:?}");
        }
    }

    /// The whole decision table, spelled out. Read the right-hand column as
    /// "what happens to the tutor's library".
    #[test]
    fn the_seed_decision_table() {
        use DbProbe::*;
        use DiskState::*;
        use SeedPlan::*;
        let cases = [
            // Day one.
            (NoCluster, Unknown, Seed),
            // The ordinary launch: seed nothing, ever.
            (Complete, Full, Skip),
            (Complete, Absent, Skip),
            // Interrupted first run — the resume paths.
            (SeedInterrupted, Absent, Seed), // died before createdb
            (SeedInterrupted, Empty, Reseed), // died between createdb and restore
            (SeedInterrupted, Full, Reseed), // died mid-restore: a FRAGMENT
            // No marker at all.
            (Unmarked, Absent, Seed),  // initdb ran, nothing else did
            (Unmarked, Empty, Reseed), // createdb ran, nothing else did
            (Unmarked, Full, Adopt),   // an install older than the marker
            // Cannot ask: do nothing rather than guess.
            (SeedInterrupted, Unknown, Undecided),
            (Unmarked, Unknown, Undecided),
        ];
        for (disk, probe, want) in cases {
            assert_eq!(seed_plan(disk, probe), want, "for {disk:?} + {probe:?}");
        }
    }

    /// The invariant the whole file exists for, asserted over the entire input
    /// space rather than the cases someone remembered to list, and now in its
    /// STRONGEST form: across every combination of what the disk says and what
    /// the cluster says, THERE IS NO PATH THAT DESTROYS A DATABASE HOLDING DATA.
    ///
    /// Two independent rules compose to give that, and this sweeps the
    /// composition rather than either half:
    ///   * `seed_plan` reaches `Reseed` — the only plan that touches an existing
    ///     database at all — only from `SeedInterrupted`, or from `Unmarked`
    ///     with a measured-empty database;
    ///   * `displacement_for` then destroys only what has been measured to hold
    ///     zero user relations, and RENAMES everything else.
    ///
    /// The old version of this test could only assert the first half, because
    /// the second half was an unconditional DROP.
    #[test]
    fn no_combination_of_disk_and_cluster_can_destroy_data() {
        for disk in [
            DiskState::NoCluster,
            DiskState::Complete,
            DiskState::SeedInterrupted,
            DiskState::Unmarked,
        ] {
            for probe in [
                DbProbe::Absent,
                DbProbe::Empty,
                DbProbe::Full,
                DbProbe::Unknown,
            ] {
                // What the boot would actually DO to the existing database.
                let destroys = match seed_plan(disk, probe) {
                    SeedPlan::Reseed => displacement_for(probe) == Displacement::Drop,
                    // Skip/Seed/Adopt/Undecided never touch an existing database
                    // — `Seed` only ever runs with `Absent` (asserted below).
                    _ => false,
                };
                assert!(
                    !destroys || probe == DbProbe::Empty,
                    "{disk:?} + {probe:?} would destroy a database that was not \
                     measured to be empty"
                );
                // And the mirror image: an existing cluster is never seeded from
                // scratch while a database is sitting there.
                if matches!(seed_plan(disk, probe), SeedPlan::Seed) && disk != DiskState::NoCluster
                {
                    assert_eq!(
                        probe,
                        DbProbe::Absent,
                        "{disk:?} + {probe:?} would createdb over an existing database"
                    );
                }
            }
        }
    }

    /// The plan-level half of the rule above, kept separate because it is the
    /// one a future edit to `seed_plan` would break: a database that HOLDS DATA
    /// is only ever disturbed at all when the marker itself says we were
    /// part-way through creating it. Anything else — an unmarked install from an
    /// older build, a cluster we could not reach — is left exactly as it is.
    #[test]
    fn a_database_with_data_is_never_disturbed_on_a_guess() {
        for disk in [
            DiskState::NoCluster,
            DiskState::Complete,
            DiskState::SeedInterrupted,
            DiskState::Unmarked,
        ] {
            for probe in [
                DbProbe::Absent,
                DbProbe::Empty,
                DbProbe::Full,
                DbProbe::Unknown,
            ] {
                let plan = seed_plan(disk, probe);
                let disturbs = matches!(plan, SeedPlan::Reseed);
                if disturbs {
                    assert!(
                        probe != DbProbe::Full || disk == DiskState::SeedInterrupted,
                        "{disk:?} + {probe:?} would disturb a database holding data"
                    );
                }
                // And the mirror image: an existing cluster is never seeded from
                // scratch while a database is sitting there.
                if matches!(plan, SeedPlan::Seed) && disk != DiskState::NoCluster {
                    assert_eq!(
                        probe,
                        DbProbe::Absent,
                        "{disk:?} + {probe:?} would createdb over an existing database"
                    );
                }
            }
        }
    }

    /// `SeedPlan::ALL` really is all of them.
    ///
    /// The match below is exhaustive with no wildcard arm, so adding a variant
    /// stops this file compiling until it is listed — and once it is listed,
    /// `every_plan_variant_finalizes_the_marker` sweeps it automatically.
    #[test]
    fn seed_plan_all_lists_every_variant() {
        for plan in SeedPlan::ALL {
            match plan {
                SeedPlan::Skip
                | SeedPlan::Seed
                | SeedPlan::Reseed
                | SeedPlan::Adopt
                | SeedPlan::Undecided => {}
            }
        }
        // …and lists each exactly once, so the sweep is over five distinct
        // cases rather than one case five times.
        for (i, plan) in SeedPlan::ALL.iter().enumerate() {
            assert!(!SeedPlan::ALL[..i].contains(plan), "{plan:?} listed twice");
        }
    }

    /// THE BLOCKER, as a test. A boot that finishes must leave the marker
    /// reading `complete` — for EVERY plan variant, `Undecided` included.
    ///
    /// `Undecided` was the hole: a boot could complete, the tutor could work in
    /// the app for weeks, and `install-state.json` would still say `seeding`.
    /// The next launch reads `seeding`, classifies a database full of his
    /// curricula as an interrupted restore, and DROPs it. So the assertion is
    /// not "finalize wrote something" but the consequence: after any completed
    /// boot, the next launch's plan is `Skip` no matter what it finds.
    #[test]
    fn every_plan_variant_finalizes_the_marker() {
        for (i, plan) in SeedPlan::ALL.iter().enumerate() {
            let dirs = tmp_dirs(&format!("finalize{i}"));
            touch_pg_version(&dirs);
            // The state a boot is actually in when it reaches the finalize
            // call: `seeding` is on disk for the paths that write it, and this
            // is the marker that must NOT survive.
            if matches!(plan, SeedPlan::Seed | SeedPlan::Reseed) {
                mark_seeding(&dirs).expect("mark");
                assert_eq!(disk_state(&dirs), DiskState::SeedInterrupted);
            }
            let outcome = match plan {
                SeedPlan::Seed | SeedPlan::Reseed => SeedOutcome::Restored,
                _ => SeedOutcome::NotAttempted,
            };

            let receipt = finalize_install(&dirs, *plan, outcome);
            assert!(receipt.is_ok(), "{plan:?} failed to finalize");

            assert_eq!(
                disk_state(&dirs),
                DiskState::Complete,
                "a completed boot on plan {plan:?} left the marker unfinished"
            );
            // The consequence, which is the thing that actually matters: no
            // later launch may decide it is entitled to drop anything.
            for probe in [DbProbe::Absent, DbProbe::Empty, DbProbe::Full, DbProbe::Unknown] {
                assert_eq!(
                    seed_plan(disk_state(&dirs), probe),
                    SeedPlan::Skip,
                    "after a completed boot on {plan:?}, a {probe:?} cluster must be left alone"
                );
            }
            let _ = fs::remove_dir_all(&dirs.data);
        }
    }

    /// The note is forensics, not decoration: whoever reads this file over the
    /// phone has to be able to tell an adopted install from a seeded one, and —
    /// above all — to see that an `Undecided` boot was recorded complete
    /// deliberately rather than by accident.
    #[test]
    fn the_completion_note_says_which_path_wrote_it() {
        let dirs = tmp_dirs("finalizenote");
        touch_pg_version(&dirs);
        for (plan, outcome, needle) in [
            (SeedPlan::Skip, SeedOutcome::NotAttempted, "established install"),
            (SeedPlan::Adopt, SeedOutcome::NotAttempted, "predates this marker"),
            (SeedPlan::Seed, SeedOutcome::Restored, "starter library restored"),
            (
                SeedPlan::Reseed,
                SeedOutcome::NothingToRestore,
                "no starter library",
            ),
            (SeedPlan::Undecided, SeedOutcome::NotAttempted, "could not be asked"),
        ] {
            let _receipt = finalize_install(&dirs, plan, outcome).expect("finalize");
            let raw = fs::read_to_string(install_state_path(&dirs)).expect("read marker");
            let state: InstallState = serde_json::from_str(&raw).expect("parse marker");
            assert_eq!(state.phase, PHASE_COMPLETE);
            assert!(state.note.contains(needle), "{plan:?} wrote: {}", state.note);
        }
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// The receipt is minted from a RE-READ, never from the write's return
    /// value — so a marker that is not actually complete cannot produce one.
    /// (Simulated by making the file unwritable AND unreadable-as-complete: a
    /// directory where the marker should be. On a first run that is fatal, and
    /// it should be: continuing would leave `seeding` behind.)
    #[test]
    fn no_receipt_without_a_marker_that_reads_complete() {
        let dirs = tmp_dirs("finalizefail");
        touch_pg_version(&dirs);
        // A DIRECTORY at the marker's path: the write fails, and the read can
        // never return `complete` either.
        fs::create_dir_all(install_state_path(&dirs)).expect("blocking directory");
        assert!(
            finalize_install(&dirs, SeedPlan::Seed, SeedOutcome::Restored).is_err(),
            "a marker that cannot be made to read `complete` must not mint a receipt"
        );
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// …but an ESTABLISHED install whose marker already reads `complete` must
    /// still boot when the refresh fails. Making a failed rewrite fatal here is
    /// how a full disk turns a working install into one that refuses to launch
    /// — the same trap `write_meta` was pulled out of. The receipt is honest
    /// either way: the file really does read `complete`.
    #[test]
    fn an_unwritable_but_already_complete_marker_still_boots() {
        let dirs = tmp_dirs("finalizero");
        touch_pg_version(&dirs);
        mark_complete(&dirs, "an earlier boot finished").expect("mark");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            // The DIRECTORY, not the file. That is what an unwritable data
            // folder actually looks like, and — since the marker is now written
            // through a temp file and a rename — it is also the only thing that
            // can still stop the write: renaming over a read-only FILE succeeds,
            // because rename needs permission on the directory, not the target.
            // Making this the read-only directory case keeps the test testing
            // the thing it claims to.
            fs::set_permissions(&dirs.data, fs::Permissions::from_mode(0o500)).expect("chmod");
            // Root ignores the mode bits, so only assert when the write really
            // did fail — the point is the receipt, not the errno.
            let readonly = fs::write(dirs.data.join("probe"), b"x").is_err();
            let receipt = finalize_install(&dirs, SeedPlan::Skip, SeedOutcome::NotAttempted);
            assert!(receipt.is_ok(), "an already-complete install must still boot");
            assert_eq!(disk_state(&dirs), DiskState::Complete);
            let _ = readonly;
            fs::set_permissions(&dirs.data, fs::Permissions::from_mode(0o700)).expect("chmod back");
        }
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// An interrupted first run must be RESUMABLE, and a completed one must be
    /// left alone — the two halves of the fix, as a round trip through the file
    /// the state actually lives in.
    #[test]
    fn an_interrupted_first_run_resumes_and_a_finished_one_does_not() {
        let dirs = tmp_dirs("resume");
        touch_pg_version(&dirs);

        // Crash mid-seed: the marker survives, and the next launch redoes the
        // restore from scratch instead of shrugging and starting empty.
        mark_seeding(&dirs).expect("mark");
        assert_eq!(
            seed_plan(disk_state(&dirs), DbProbe::Full),
            SeedPlan::Reseed
        );

        // Finish it. From here the same cluster must never be seeded again, no
        // matter what the probe says.
        mark_complete(&dirs, "first run finished").expect("mark");
        for probe in [DbProbe::Absent, DbProbe::Empty, DbProbe::Full] {
            assert_eq!(seed_plan(disk_state(&dirs), probe), SeedPlan::Skip);
        }
    }

    /// The ordering invariant boot depends on: the marker is written BEFORE
    /// initdb, so a cluster can never appear next to a `complete` marker left
    /// over from an install whose pgdata is gone. Get this backwards and an
    /// interruption in between reads as a finished install with an empty
    /// database — the exact silent empty library this is all for.
    #[test]
    fn a_stale_complete_marker_cannot_survive_a_new_cluster() {
        let dirs = tmp_dirs("stalemarker");
        mark_complete(&dirs, "an install that no longer has a cluster").expect("mark");
        assert_eq!(disk_state(&dirs), DiskState::NoCluster, "no cluster is day one");

        mark_seeding(&dirs).expect("mark"); // boot's first move, before initdb
        touch_pg_version(&dirs); // …and now initdb lands
        assert_eq!(disk_state(&dirs), DiskState::SeedInterrupted);
        assert_eq!(
            seed_plan(disk_state(&dirs), DbProbe::Absent),
            SeedPlan::Seed,
            "an interruption here must still seed"
        );
    }

    /// `init_cluster` has to survive an initdb that died half-way: the leftover
    /// pgdata is cleared out of the way, because a non-empty directory is one
    /// initdb refuses to touch and the install would be bricked for good.
    ///
    /// The assertion that used to be here — "pgdata is empty afterwards" — is
    /// kept, because initdb still depends on it. What is ADDED is the half that
    /// makes the operation survivable: the leftovers are still on disk, under a
    /// `pgdata_superseded_…` name, with their contents intact.
    #[test]
    fn a_half_written_pgdata_is_moved_aside_not_deleted() {
        let dirs = tmp_dirs("halfinitdb");
        fs::write(dirs.pgdata.join("postgresql.conf"), b"leftovers").expect("leftover");
        assert!(!cluster_exists(&dirs), "no PG_VERSION: not a cluster");

        let _ = clear_unfinished_pgdata(&tmp_res(&dirs), &dirs).expect("clear");
        assert!(
            fs::read_dir(&dirs.pgdata).expect("read").next().is_none(),
            "pgdata must be empty for initdb"
        );

        // NOTHING WAS DELETED. That is the whole change.
        let aside: Vec<String> = list_superseded_dirs(&dirs);
        assert_eq!(aside.len(), 1, "expected exactly one set-aside directory: {aside:?}");
        assert_eq!(
            fs::read_to_string(dirs.data.join(&aside[0]).join("postgresql.conf"))
                .expect("the leftovers are still there"),
            "leftovers"
        );
        // …and the tutor can find out that it exists without reading a log.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("recovery note");
        assert!(note.contains(&aside[0]), "the note must name it: {note}");

        // …and it is a no-op on an already-empty directory: no second set-aside.
        let _ = clear_unfinished_pgdata(&tmp_res(&dirs), &dirs).expect("clear again");
        assert_eq!(list_superseded_dirs(&dirs).len(), 1, "an empty pgdata moves nothing");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// THE BLOCKER `PG_VERSION` used to be. It is a three-byte file, and its
    /// absence was treated as proof that a directory could be deleted whole. A
    /// bad sector, an over-eager cleaner or a restore that missed one entry
    /// would therefore cost the tutor the entire cluster.
    ///
    /// Now the question is asked the other way round — is there anything here
    /// that only a real cluster leaves? — and if there is, the app REFUSES and
    /// says what it found. Each sub-case below is a separate piece of evidence,
    /// each on its own sufficient.
    #[test]
    fn a_cluster_that_lost_its_pg_version_is_refused_not_cleared() {
        for (tag, plant) in [
            (
                "control",
                &(|d: &Dirs| {
                    fs::create_dir_all(d.pgdata.join("global")).expect("global");
                    fs::write(d.pgdata.join("global/pg_control"), b"\0\0\0\0").expect("control");
                }) as &dyn Fn(&Dirs),
            ),
            ("base", &|d: &Dirs| {
                fs::create_dir_all(d.pgdata.join("base/16384")).expect("base");
                fs::write(d.pgdata.join("base/16384/2619"), b"rows").expect("rel");
            }),
            ("wal", &|d: &Dirs| {
                fs::create_dir_all(d.pgdata.join("pg_wal")).expect("wal");
                fs::write(d.pgdata.join("pg_wal/000000010000000000000001"), b"w").expect("seg");
            }),
            ("bulk", &|d: &Dirs| {
                fs::write(d.pgdata.join("something.dat"), vec![7u8; 2 * 1024 * 1024])
                    .expect("2MB");
            }),
        ] {
            let dirs = tmp_dirs(&format!("evidence-{tag}"));
            plant(&dirs);
            assert!(!cluster_exists(&dirs), "PG_VERSION is what went missing");

            let refused = pgdata_is_safe_to_build_in(&dirs)
                .expect_err(&format!("{tag}: must refuse to build over a real cluster"));
            // The message has to be true and has to say so in both languages.
            assert!(refused.contains("PG_VERSION"), "{tag}: {refused}");
            assert!(refused.contains("nothing has been deleted")
                || refused.contains("Nothing has been changed"), "{tag}: {refused}");
            let greek = refused.find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c));
            let english = refused.find("GuitarTutor's database folder");
            assert!(greek.is_some() && english.is_some() && greek < english, "{tag}: {refused}");

            // And the refusal is REAL: clear_unfinished_pgdata refuses too, and
            // everything is exactly where it was.
            let before = tree_size_at_least(&dirs.pgdata, u64::MAX);
            clear_unfinished_pgdata(&tmp_res(&dirs), &dirs).expect_err(&format!("{tag}: must not clear"));
            assert_eq!(
                tree_size_at_least(&dirs.pgdata, u64::MAX),
                before,
                "{tag}: not one byte may be touched"
            );
            assert!(list_superseded_dirs(&dirs).is_empty(), "{tag}: nothing moved either");
            let _ = fs::remove_dir_all(&dirs.data);
        }
    }

    /// …and the mirror image, so the guard cannot be "refuse everything": a
    /// genuine initdb skeleton is still recognised as safe to build in.
    ///
    /// THE TEST THIS REPLACES PASSED VACUOUSLY. It created `base`, `global`,
    /// `pg_wal` and `pg_xact` as four FLAT, EMPTY directories — a shape no real
    /// `initdb` ever leaves — so `read_dir(sub).next().is_some()` was false for
    /// all four and the guard was never actually asked the question. The real
    /// skeleton, which is the FIRST thing initdb writes (before `PG_VERSION`,
    /// before one byte of data), is NESTED: `base/1`, `pg_wal/archive_status`,
    /// `pg_multixact/members`, `pg_logical/snapshots`. Against that shape the
    /// old guard found "base/ with files in it" and "pg_wal/ with files in it",
    /// refused to build, and bricked the install on every launch thereafter with
    /// a dialog about data that did not exist.
    ///
    /// So this plants the directory list `initdb` actually creates (PG 16
    /// `initdb.c`, `create_data_directory`/`subdirs[]`) and asserts the guard
    /// lets it through — and then that the whole recovery works end to end,
    /// which is what the tutor's day one depends on.
    #[test]
    fn a_genuine_initdb_skeleton_is_still_safe_to_build_in() {
        let dirs = tmp_dirs("skeleton");
        assert!(pgdata_is_safe_to_build_in(&dirs).is_ok(), "an empty pgdata");

        // Exactly initdb's `subdirs[]`, nesting included. Not one file: this is
        // the state after `mkdir`, before anything is written.
        for sub in [
            "global",
            "pg_wal/archive_status",
            "pg_commit_ts",
            "pg_dynshmem",
            "pg_notify",
            "pg_serial",
            "pg_snapshots",
            "pg_subtrans",
            "pg_twophase",
            "pg_multixact/members",
            "pg_multixact/offsets",
            "base/1",
            "pg_replslot",
            "pg_tblspc",
            "pg_stat",
            "pg_stat_tmp",
            "pg_xact",
            "pg_logical/snapshots",
            "pg_logical/mappings",
        ] {
            fs::create_dir_all(dirs.pgdata.join(sub)).expect("skeleton dir");
        }
        assert!(!pgdata_is_empty(&dirs), "the skeleton is not an empty directory");
        assert!(
            cluster_evidence(&dirs.pgdata).is_empty(),
            "not one of these directories holds a FILE — there is no cluster here: {:?}",
            cluster_evidence(&dirs.pgdata)
        );
        assert!(
            pgdata_is_safe_to_build_in(&dirs).is_ok(),
            "a real initdb skeleton must not be mistaken for a cluster — this is \
             the shape an interrupted first run leaves, and refusing it bricks \
             day one forever"
        );

        // …and the recovery actually completes: the skeleton is moved aside
        // (never deleted) and initdb is handed the empty directory it demands.
        let _ = clear_unfinished_pgdata(&tmp_res(&dirs), &dirs).expect("the install must be able to proceed");
        assert!(
            fs::read_dir(&dirs.pgdata).expect("read").next().is_none(),
            "pgdata must be empty for initdb"
        );
        assert_eq!(list_superseded_dirs(&dirs).len(), 1, "moved aside, not deleted");

        // A single small file in the skeleton (a half-written postgresql.conf,
        // which initdb writes before the bootstrap) is still not a cluster.
        for sub in ["base/1", "pg_wal/archive_status"] {
            fs::create_dir_all(dirs.pgdata.join(sub)).expect("skeleton dir");
        }
        fs::write(dirs.pgdata.join("postgresql.conf"), b"# half written\n").expect("conf");
        assert!(
            pgdata_is_safe_to_build_in(&dirs).is_ok(),
            "a config file at the top level is not per-database storage"
        );

        // But ONE file under base/ is, and the guard must still say so — the
        // fix must not have turned into "let everything through".
        fs::write(dirs.pgdata.join("base/1/1259"), b"catalog").expect("rel");
        let refused = pgdata_is_safe_to_build_in(&dirs)
            .expect_err("a FILE under base/ is per-database storage");
        assert!(refused.contains("base/ with PostgreSQL's own files in it"), "{refused}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// `set_aside_pgdata` on an empty pgdata must move nothing and still leave a
    /// usable, 0700 directory for initdb — and must NOT move the media either,
    /// because the media belongs to the cluster and the cluster did not move.
    #[test]
    fn setting_aside_an_empty_pgdata_moves_nothing() {
        let dirs = tmp_dirs("asideempty");
        plant_a_book(&dirs, "aaaaaaaa-0000-0000-0000-000000000001");
        assert!(!set_aside_pgdata(&tmp_res(&dirs), &dirs, "nothing here")
            .expect("no-op")
            .anything_set_aside());
        assert!(dirs.pgdata.is_dir());
        assert!(list_superseded_dirs(&dirs).is_empty());
        assert!(
            list_superseded_media(&dirs).is_empty(),
            "an empty pgdata is a no-op for BOTH halves"
        );
        assert!(dirs.media.join("aaaaaaaa-0000-0000-0000-000000000001/source.pdf").is_file());
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A `.DS_Store` IS NOT A CLUSTER, and this is not a theoretical tidiness
    /// point: `cluster_evidence` counted ANY regular file under `base/`, and
    /// `pgdata_is_safe_to_build_in`'s refusal is DELIBERATELY PERMANENT — there
    /// is no next launch that recovers from it. So one Finder dropping, written
    /// the moment somebody opened the data folder to help the tutor over the
    /// phone, refused the install forever with a dialog blaming data that does
    /// not exist. On his dad's Mac that is not a hypothetical: Finder writes
    /// `.DS_Store` into every directory it displays.
    #[test]
    fn a_file_browsers_droppings_are_never_evidence_of_a_cluster() {
        let dirs = tmp_dirs("finder");
        for sub in ["base/1", "global", "pg_wal/archive_status", "pg_xact"] {
            fs::create_dir_all(dirs.pgdata.join(sub)).expect("skeleton");
        }
        // Everything a Mac (and a Windows machine that has seen the folder over
        // a share) leaves behind, in the places it leaves it.
        for litter in [
            "base/.DS_Store",
            "base/1/.DS_Store",
            "base/1/._1259",
            "global/.localized",
            "pg_wal/Thumbs.db",
            "pg_xact/desktop.ini",
        ] {
            fs::write(dirs.pgdata.join(litter), b"finder").expect("litter");
        }
        fs::create_dir_all(dirs.pgdata.join(".Spotlight-V100/store")).expect("spotlight");
        fs::write(dirs.pgdata.join(".Spotlight-V100/store/index"), vec![0u8; 2 << 20])
            .expect("a big index");

        assert!(
            cluster_evidence(&dirs.pgdata).is_empty(),
            "none of this is written by PostgreSQL: {:?}",
            cluster_evidence(&dirs.pgdata)
        );
        assert!(
            pgdata_is_safe_to_build_in(&dirs).is_ok(),
            "the install must not be refused — permanently — over a .DS_Store"
        );
        // …and the size rule must not be fooled either: a Spotlight index is
        // megabytes and is not "at least 1 MB of data files".
        assert!(tree_size_at_least(&dirs.pgdata, 1024 * 1024) < 1024 * 1024);

        // The guard is NOT simply weakened: one relation file under base/ is
        // still per-database storage and still refuses.
        fs::write(dirs.pgdata.join("base/1/1259"), b"catalog").expect("relation");
        let refused =
            pgdata_is_safe_to_build_in(&dirs).expect_err("a real relation file is a cluster");
        assert!(refused.contains("base/ with PostgreSQL's own files in it"), "{refused}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// What PostgreSQL itself writes into those four directories, against what
    /// everybody else does. Doubt is spent towards YES on purpose — a false yes
    /// refuses to build and costs a support call; a false no builds a cluster on
    /// top of the tutor's data.
    #[test]
    fn only_postgres_own_file_names_count_as_evidence() {
        for pg in [
            "PG_VERSION", "pg_control", "pg_filenode.map", "pg_internal.init",
            "1259", "1259_fsm", "1259_vm", "16384.1",
            "000000010000000000000001", "000000010000000000000001.ready",
            "00000002.history", "0000",
        ] {
            assert!(is_postgres_file(pg), "{pg} is postgres's own");
        }
        for theirs in [
            ".DS_Store", "._1259", ".localized", "Thumbs.db", "thumbs.db",
            "desktop.ini", "Icon\r", ".fseventsd", "notes.txt", "backup.zip",
        ] {
            assert!(!is_postgres_file(theirs), "{theirs} is somebody else's");
        }
    }

    // ---- the media travels with what indexes it -----------------------------

    /// One uploaded book, exactly as the API child lays it out: a UUID-named
    /// directory holding the ORIGINAL PDF (irreplaceable) and a page render
    /// (regenerable, and only if the original survives).
    fn plant_a_book(dirs: &Dirs, id: &str) {
        let d = dirs.media.join(id);
        fs::create_dir_all(&d).expect("book dir");
        fs::write(d.join("source.pdf"), b"HIS ONLY COPY").expect("source.pdf");
        fs::write(d.join("0001.jpg"), b"page render").expect("render");
    }

    fn list_superseded_media(dirs: &Dirs) -> Vec<String> {
        let Ok(entries) = fs::read_dir(&dirs.data) else {
            return Vec::new();
        };
        let mut found: Vec<String> = entries
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with(SUPERSEDED_MEDIA_PREFIX))
            .collect();
        found.sort();
        found
    }

    /// THE BLOCKER, on the pgdata half. Setting a cluster aside sets aside every
    /// database in it at once, `guitar` included — so the media those rows
    /// describe has to go with it. Left behind, it meets a brand-new cluster
    /// with a brand-new `guitar`, and the API child's startup sweep sees every
    /// book the tutor owns as a directory no database has a row for.
    ///
    /// What is asserted: the files survive, `<data>/media` is left EMPTY (which
    /// is what makes the sweep a no-op), the two halves share one suffix, and —
    /// the crash-safety half — the media was moved FIRST.
    #[test]
    fn a_displaced_cluster_takes_its_media_with_it() {
        let dirs = tmp_dirs("pgdatamedia");
        mark_seeding(&dirs).expect("marker"); // so the record can be written
        fs::write(dirs.pgdata.join("postgresql.conf"), b"leftovers").expect("cluster");
        plant_a_book(&dirs, "bbbbbbbb-0000-0000-0000-000000000002");

        let aside = set_aside_pgdata(&tmp_res(&dirs), &dirs, "postgres would not start").expect("set aside");
        let pgdata_name = aside.directories.first().expect("something was moved").clone();

        // 1. Nothing was deleted: the tutor's only copy of the PDF is on disk.
        let media = list_superseded_media(&dirs);
        assert_eq!(media.len(), 1, "the media must have travelled: {media:?}");
        assert_eq!(
            fs::read_to_string(
                dirs.data
                    .join(&media[0])
                    .join("bbbbbbbb-0000-0000-0000-000000000002/source.pdf")
            )
            .expect("his original PDF is still there"),
            "HIS ONLY COPY"
        );

        // 2. `<data>/media` is left empty — this is what the API's sweep will
        //    walk, and an empty tree has no orphans to collect.
        assert!(dirs.media.is_dir(), "the API child still needs a media dir");
        assert!(media_is_empty(&dirs), "the sweep must find nothing to collect");

        // 3. ONE SUFFIX. This is how whoever is helping him knows, months later
        //    and from a directory listing alone, which media folder belongs to
        //    which cluster.
        let suffix = pgdata_name
            .strip_prefix(SUPERSEDED_DIR_PREFIX)
            .expect("our own name");
        assert_eq!(media[0], format!("{SUPERSEDED_MEDIA_PREFIX}{suffix}"));

        // 4. ORDER. Two renames cannot be made atomic, so the only question is
        //    which half-done state a power cut may leave. Media first means the
        //    survivable one: an empty media folder beside an untouched cluster.
        //    Each record is written immediately after its own rename, so their
        //    order in the marker is the order the renames happened in.
        let state = read_state(&dirs).expect("marker");
        let kinds: Vec<&str> = state.set_aside.iter().map(|s| s.kind.as_str()).collect();
        assert_eq!(
            kinds,
            vec!["media", "directory"],
            "the media must be moved BEFORE the thing that indexes it"
        );
        // …and each half names the other, in the file support actually asks for.
        // The media's own entry is recorded UNDETERMINED at the moment of its
        // rename — the pgdata rename that gives it a partner had not run yet —
        // and is settled by `reconcile_pairing` once that rename returns Ok.
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::Is(pgdata_name.clone()),
            "the pairing must be settled once BOTH renames have happened"
        );
        assert_eq!(state.set_aside[1].pairing, Companion::Is(media[0].clone()));
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// The choke point: EVERY freshly created `guitar` database passes through
    /// `create_and_seed_db`, including the `SeedPlan::Seed` paths that never
    /// call `displace_database` at all. A boot that set a cluster aside and then
    /// died before it got here leaves exactly this state — media on disk, no
    /// database anywhere — and the next boot creates a fresh one beside it.
    #[test]
    fn a_fresh_database_is_never_created_beside_media_it_does_not_index() {
        let dirs = tmp_dirs("freshdb");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "cccccccc-0000-0000-0000-000000000003");

        let receipt = displace_media_for_a_fresh_database(&tmp_res(&dirs), &dirs).expect("displace");
        let moved = receipt.sole_media().expect("there was media to move").to_string();

        assert!(media_is_empty(&dirs), "a fresh database gets a clean media tree");
        assert_eq!(
            fs::read_to_string(
                dirs.data
                    .join(&moved)
                    .join("cccccccc-0000-0000-0000-000000000003/source.pdf")
            )
            .expect("kept"),
            "HIS ONLY COPY"
        );
        // It is recorded like everything else — a set-aside nobody can find is
        // worth no more than a deleted one.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains(&moved), "{note}");

        // …and on the ordinary day one — an empty media tree — it does nothing
        // at all and leaves no debris behind.
        assert!(!displace_media_for_a_fresh_database(&tmp_res(&dirs), &dirs)
            .expect("no-op")
            .anything_set_aside());
        assert_eq!(list_superseded_media(&dirs).len(), 1, "no second set-aside");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// "Empty" has to mean what the TUTOR would mean by it.
    ///
    /// The API child's `sweep_orphaned_media` renames orphans into
    /// `<data>/media/_superseded/<stamp>/` and writes a README beside them, so
    /// after one sweep `<data>/media` is never `read_dir`-empty again. Judged
    /// that way, every later displacement moved, recorded and ANNOUNCED a tree
    /// holding one dated folder and a note — a dialog telling him data of his
    /// had been set aside, naming a folder with none of it in it, which teaches
    /// him that the dialog is noise.
    ///
    /// And the direction that matters more: if that quarantine DOES hold his
    /// books, the tree is not empty and they still travel with the database
    /// that indexes them.
    #[test]
    fn an_emptied_quarantine_is_an_empty_media_folder_and_a_full_one_is_not() {
        let dirs = tmp_dirs("quarantine");
        fs::create_dir_all(&dirs.media).expect("media");
        assert!(media_is_empty(&dirs), "day one");

        // What one sweep that found something, and a later reap, leave behind:
        // the quarantine folder, its dated batch, and the API's own README.
        let q = dirs.media.join("_superseded");
        fs::create_dir_all(q.join("20260701T000000Z")).expect("batch");
        fs::write(q.join("READ-ME-superseded-media.txt"), b"explanation").expect("readme");
        fs::write(dirs.media.join(".DS_Store"), b"finder").expect("litter");
        assert!(
            media_is_empty(&dirs),
            "a quarantine holding nothing, a note and a Finder dropping is not a library"
        );

        // Now put a book in the quarantine. That is his only copy of a scan he
        // made himself, and it must count.
        let kept = q.join("20260701T000000Z/55555555-0000-0000-0000-000000000005");
        fs::create_dir_all(&kept).expect("kept");
        fs::write(kept.join("source.pdf"), b"HIS ONLY COPY").expect("pdf");
        assert!(
            !media_is_empty(&dirs),
            "quarantined books are still his books and must travel with the database"
        );

        // …and when they do travel, they travel whole.
        mark_seeding(&dirs).expect("marker");
        let moved = displace_media(&tmp_res(&dirs), &dirs, "20260730T142530Z", Companion::Alone, "a test")
            .expect("displace");
        let name = moved.sole_media().expect("moved");
        assert_eq!(
            fs::read_to_string(
                dirs.data
                    .join(name)
                    .join("_superseded/20260701T000000Z/55555555-0000-0000-0000-000000000005/source.pdf")
            )
            .expect("the quarantined PDF came too"),
            "HIS ONLY COPY"
        );
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// The note is a set of INSTRUCTIONS somebody will follow at 1am, so every
    /// claim in it has to be true. It used to say that renaming the database
    /// back restores everything; after the sweep had run, that was a lie.
    #[test]
    fn the_recovery_note_explains_restoring_both_halves() {
        let dirs = tmp_dirs("noteboth");
        mark_seeding(&dirs).expect("marker");
        fs::write(dirs.pgdata.join("postgresql.conf"), b"leftovers").expect("cluster");
        plant_a_book(&dirs, "dddddddd-0000-0000-0000-000000000004");
        let pgdata_name = set_aside_pgdata(&tmp_res(&dirs), &dirs, "a test")
            .expect("aside")
            .directories
            .remove(0);
        let media_name = list_superseded_media(&dirs).remove(0);

        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains(&pgdata_name), "names the cluster: {note}");
        assert!(note.contains(&media_name), "names the media folder: {note}");
        // The instruction that was missing, and the warning that makes it stick.
        assert!(note.contains("media_superseded_"), "{note}");
        assert!(
            note.contains("RESTORING THE DATABASE WITHOUT ITS")
                && note.contains("MEDIA FOLDER IS NOT ENOUGH"),
            "the note must say why the database alone is not enough:\n{note}"
        );
        // NO ENTRY MAY PRINT A COMMAND THAT CANNOT WORK. The note used to carry
        // one standing "to put it back" block for all three kinds, with THIS
        // entry's name interpolated into the DATABASE line whatever the entry
        // actually was — so a media entry said
        // `ALTER DATABASE "media_superseded_…" RENAME TO guitar;` and this
        // pgdata entry said it against a directory name. An instruction that
        // cannot work, printed under "to put it back", in the file the tutor is
        // told to trust.
        for wrong in [
            format!("ALTER DATABASE \"{media_name}\""),
            format!("ALTER DATABASE \"{pgdata_name}\""),
        ] {
            assert!(
                !note.contains(&wrong),
                "the note must not tell anyone to run `{wrong}` — there is no such \
                 database:\n{note}"
            );
        }
        // …and each entry does carry the command that IS true of it.
        assert!(
            note.contains(&format!("rename \"{pgdata_name}\" back to \"pgdata\"")),
            "the cluster entry must say how to restore a cluster:\n{note}"
        );
        assert!(
            note.contains(&format!("rename \"{media_name}\" back to \"media\"")),
            "the media entry must say how to restore the media:\n{note}"
        );

        // The pairing rule, including what an interrupted displacement looks
        // like — the one state a crash between the two renames can leave — and
        // the OTHER reason a media folder can be unpaired, so the instruction is
        // true of both and not just of the case that was on our minds.
        assert!(note.contains("SHARE THE TRAILING TIMESTAMP"), "{note}");
        assert!(
            note.contains("no partner with the same stamp"),
            "the note must explain the crash window it can produce:\n{note}"
        );
        assert!(
            note.contains("stopped between those two steps")
                && note.contains("already gone when these files were found"),
            "an unpaired media folder has two possible causes and the note must \
             not assert only one of them:\n{note}"
        );
        // Still bilingual, still Greek first, in every entry.
        let greek = note
            .find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c))
            .expect("Greek");
        let english = note.find("GuitarTutor found data").expect("English");
        assert!(greek < english, "Greek first:\n{note}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A companion is only ever claimed when one really exists. A note that
    /// names a folder which is not on disk sends whoever is helping looking for
    /// something that was never there, which is the same failure as saying
    /// nothing — with more confidence.
    #[test]
    fn an_empty_media_folder_is_never_claimed_as_a_companion() {
        let dirs = tmp_dirs("nocompanion");
        mark_seeding(&dirs).expect("marker");
        fs::write(dirs.pgdata.join("postgresql.conf"), b"leftovers").expect("cluster");
        // No media at all — day one, or an install that never ingested a book.
        let name = set_aside_pgdata(&tmp_res(&dirs), &dirs, "a test")
            .expect("aside")
            .directories
            .remove(0);

        assert!(list_superseded_media(&dirs).is_empty(), "nothing to move");
        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1, "one record, not two");
        assert_eq!(state.set_aside[0].name, name);
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::Alone,
            "no media travelled with it, so none may be claimed — and `Alone` \
             says that as a measurement rather than as an absence"
        );
        let _ = fs::remove_dir_all(&dirs.data);
    }

    // ---- the note must not describe a pairing that never happened ----------

    /// THE BLOCKER. `ALTER DATABASE … RENAME` has no FORCE and one connected
    /// backend refuses it — an autovacuum worker is enough, and the
    /// `pg_terminate_backend` before it is best-effort and racy. On that Err the
    /// app shows a fatal dialog and quits WITH THE MEDIA ALREADY MOVED, because
    /// the media moves first on purpose (that ordering is the crash-safety
    /// argument and is not up for negotiation).
    ///
    /// Everything this test asserts is about what the app then SAYS. It used to
    /// say two false things, and the second one is the one that costs a library:
    ///
    ///   1. the media entry named a companion database that does not exist and
    ///      never did — the name the rename was ABOUT to use; and
    ///   2. the next launch, finding `<data>/media` empty, set the database
    ///      aside "with no media" and the note stated flatly that the media
    ///      folder had been empty and there was nothing else to restore — while
    ///      his whole library sat in the `media_superseded_*` folder that this
    ///      same file had already mis-described.
    ///
    /// Driven with a resource root that holds no psql, so every `psql_exec` in
    /// `displace_database` fails: the probe cannot answer (`Unknown` → `Rename`,
    /// the safe reading), the media is displaced for real, and the `ALTER` then
    /// returns Err. That is the failure window exactly, with no cluster needed.
    #[test]
    fn a_failed_database_rename_leaves_a_note_that_does_not_lie() {
        let dirs = tmp_dirs("failedalter");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "eeeeeeee-0000-0000-0000-000000000005");
        let no_psql = dirs.data.join("no-such-resource-root");

        // ---- boot 1: media moved, ALTER DATABASE fails, app dies -----------
        let (receipt, renamed) = displace_database(&no_psql, &dirs, 5999);
        let failed = renamed.expect_err("there is no psql here, so the rename cannot happen");
        assert!(failed.contains("psql"), "{failed}");

        // THE RECEIPT SURVIVES THE FAILURE. This is what `main`'s fatal dialog
        // is built out of, and it used to be thrown away by the `?` on the
        // `ALTER DATABASE` — leaving the tutor with "could not set the database
        // aside" and no word about the library that had just moved.
        assert_eq!(
            receipt.media.len(),
            1,
            "the media moved before the rename could fail, and the receipt must \
             say so: {receipt:?}"
        );
        assert!(
            receipt.databases.is_empty(),
            "nothing may claim the database moved — it did not: {receipt:?}"
        );

        // The media really did move — this is the window, not a hypothetical.
        let media = list_superseded_media(&dirs);
        assert_eq!(receipt.media[0], media[0], "the receipt names what is on disk");
        assert_eq!(media.len(), 1, "the media moved first, by design: {media:?}");
        assert_eq!(
            fs::read_to_string(
                dirs.data
                    .join(&media[0])
                    .join("eeeeeeee-0000-0000-0000-000000000005/source.pdf")
            )
            .expect("his original PDF is still there"),
            "HIS ONLY COPY"
        );

        // 1. NO PAIRING IS CLAIMED. Not in the marker…
        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1, "only the media moved: {:?}", state.set_aside);
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::Unknown,
            "the database was never renamed, so nothing may be named as its partner"
        );
        // …and not in the note, which must not name the database the rename was
        // about to create. It may talk ABOUT `guitar_superseded_…` in the
        // abstract — that is how it explains what to look for — but no concrete
        // `guitar_superseded_<stamp>` may appear, because none exists.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        for at in note.match_indices(SUPERSEDED_DB_PREFIX).map(|(i, _)| i) {
            let tail = &note[at + SUPERSEDED_DB_PREFIX.len()..];
            assert!(
                !tail.starts_with(|c: char| c.is_ascii_digit()),
                "the note names a database that does not exist: {:?}\n{note}",
                tail.chars().take(20).collect::<String>()
            );
        }
        // …and it says so in as many words, and says what to check instead.
        assert!(note.contains("WAS NOT KNOWN WHEN"), "{note}");
        assert!(note.contains("NOT DETERMINED YET"), "{note}");
        // …and NOT settled by a later block, because it never was.
        assert!(!note.contains("FOLLOW-UP NOTE\n\n"), "nothing settled it:\n{note}");
        // WHAT TO CHECK, as three lines that can be run. "use the psql that
        // ships inside the app bundle: psql -l" was neither: `psql` is on no
        // PATH, and with GuitarTutor quit — which the same paragraph instructs
        // — there is no server for it to reach. So the note starts one, lists,
        // and stops it again.
        assert!(note.contains(&repair_start_cmd(&no_psql, &dirs)), "start it:\n{note}");
        assert!(
            note.contains(&format!("{} -l", repair_psql(&no_psql, &dirs))),
            "list the databases:\n{note}"
        );
        assert!(note.contains(&repair_stop_cmd(&no_psql, &dirs)), "stop it again:\n{note}");
        assert!(
            note.contains("is still the one called \"guitar\", and these files"),
            "the true answer for THIS window — the rename failed, so the database \
             is still `guitar` — must be in the note:\n{note}"
        );

        // ---- boot 2: `<data>/media` is empty now ---------------------------
        assert!(media_is_empty(&dirs), "the next boot finds an empty media folder");
        // Which means `displace_database` reaches its `Rename` arm with
        // `media == None` and records the database as `Alone`. That call is
        // reproduced here verbatim (the only thing between it and this line is
        // the `ALTER DATABASE` that needs a cluster).
        let db2 = format!("{SUPERSEDED_DB_PREFIX}20260731T090000Z");
        let receipt = announce_set_aside(&tmp_res(&dirs), &dirs, "database", &db2, &Companion::Alone, "boot 2");
        assert!(receipt.anything_set_aside());

        // 2. THE SENTENCE THAT USED TO DISOWN HIS LIBRARY. The entry may say
        //    that nothing travelled WITH it — that is true — but it must not
        //    conclude that there is nothing else to restore, because there is,
        //    and it is named right here.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        let entry = note.rsplit("============").next().unwrap_or("");
        let _ = entry;
        assert!(
            note.contains("THAT DOES NOT MEAN THERE ARE NO FILES"),
            "the note must not disown an unaccounted-for media folder:\n{note}"
        );
        assert!(
            note.matches(media[0].as_str()).count() >= 2,
            "the database entry must NAME the unpaired media folder:\n{note}"
        );
        // And the reader is told to check before moving anything — the pairing
        // is likely, not proven, and the note must not overstate it either.
        assert!(note.contains("very probably its files"), "{note}");
        assert!(note.contains("before moving anything"), "{note}");

        // The whole point of the exercise: both halves of his install are on
        // disk and both are named in one file.
        assert!(dirs.data.join(&media[0]).is_dir());
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A `psql` that answers, so `displace_database`'s SUCCESSFUL path can be
    /// driven for real — probe, media rename, terminate, `ALTER DATABASE`,
    /// record, reconcile — with no cluster anywhere.
    ///
    /// It answers off the SQL it is handed, which is the only way one stub can
    /// serve three callers at once: "does `guitar` exist?" must hear 1, "is
    /// `guitar_superseded_<stamp>` taken?" must hear 0, and "how many user
    /// objects are in there?" must hear a non-zero count — so the database holds
    /// data and is RENAMED rather than dropped.
    #[cfg(unix)]
    fn stub_psql(res: &Path) {
        stub_psql_holding(res, 42)
    }

    /// …with the user-object count the probe will read back, so the `Drop` arm
    /// (a database MEASURED to hold nothing) can be driven as well as `Rename`.
    #[cfg(unix)]
    fn stub_psql_holding(res: &Path, relations: u32) {
        use std::os::unix::fs::PermissionsExt;
        let bin = res.join("pg/bin");
        fs::create_dir_all(&bin).expect("bin");
        let psql = bin.join("psql");
        fs::write(
            &psql,
            format!(
                "#!/bin/sh\n\
                 for a in \"$@\"; do sql=\"$a\"; done\n\
                 case \"$sql\" in\n\
                 *\"datname = 'guitar'\"*) echo 1 ;;\n\
                 *pg_database*)           echo 0 ;;\n\
                 *pg_class*)              echo {relations} ;;\n\
                 *)                       : ;;\n\
                 esac\n\
                 exit 0\n"
            ),
        )
        .expect("stub");
        fs::set_permissions(&psql, fs::Permissions::from_mode(0o755)).expect("chmod");
    }

    /// THE WHOLE SUCCESSFUL DISPLACEMENT, end to end, through the real
    /// `displace_database` — and then the file the tutor's son actually reads.
    ///
    /// This is the state the recovery note has to be right about more than any
    /// other: a database and its media both set aside, both recoverable, and
    /// neither of any use without the other.
    #[cfg(unix)]
    #[test]
    fn a_database_and_its_media_are_set_aside_together_and_the_note_says_so() {
        let dirs = tmp_dirs("bothhalves");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "77777777-0000-0000-0000-000000000007");
        // Shaped like the real bundle so the note prints a realistic path, with
        // the stub psql where `pg_command` looks for the real one.
        let res = tmp_res(&dirs);
        stub_psql(&res);

        let (displaced, renamed) = displace_database(&res, &dirs, 5999);
        renamed.expect("displace");

        // Both halves came back in ONE receipt, which is what the dialog needs.
        assert_eq!(displaced.databases.len(), 1);
        assert_eq!(displaced.media.len(), 1);
        let db = displaced.databases[0].clone();
        let media = displaced.media[0].clone();
        assert!(db.starts_with(SUPERSEDED_DB_PREFIX));
        // ONE suffix across both, so a directory listing pairs them by eye.
        assert_eq!(
            db.strip_prefix(SUPERSEDED_DB_PREFIX),
            media.strip_prefix(SUPERSEDED_MEDIA_PREFIX)
        );

        // His only copy is on disk, and `<data>/media` is clean for the sweep.
        assert_eq!(
            fs::read_to_string(
                dirs.data.join(&media).join("77777777-0000-0000-0000-000000000007/source.pdf")
            )
            .expect("kept"),
            "HIS ONLY COPY"
        );
        assert!(media_is_empty(&dirs));

        // MEDIA FIRST, then the database — the crash-safety order — and both
        // records name the other, because both renames really happened.
        let state = read_state(&dirs).expect("marker");
        let kinds: Vec<&str> = state.set_aside.iter().map(|s| s.kind.as_str()).collect();
        assert_eq!(kinds, vec!["media", "database"]);
        assert_eq!(state.set_aside[0].pairing, Companion::Is(db.clone()));
        assert_eq!(state.set_aside[1].pairing, Companion::Is(media.clone()));
        assert!(state.set_aside.iter().all(|s| !s.announced), "still to be said out loud");

        // Nothing is left dangling for a later entry to be warned about.
        assert!(unpaired_media_on_disk(&dirs).is_empty());

        // The note names both, and tells you how to put each one back.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(
            note.contains(&format!(
                "{} -v ON_ERROR_STOP=1 -c 'ALTER DATABASE \"{db}\" RENAME TO guitar'",
                repair_psql(&res, &dirs)
            )),
            "the rename has to be a command, not a fragment of SQL with no way to \
             reach a server:\n{note}"
        );
        assert!(note.contains(&format!("rename \"{media}\" back to \"media\"")), "{note}");
        assert!(note.contains("RESTORING THE DATABASE WITHOUT ITS"), "{note}");
        // The media entry was written at the instant its files moved — before
        // the `ALTER DATABASE` that gives it a partner had run — so it honestly
        // reads NOT DETERMINED. What must then be true is that the file settles
        // it, and that the undetermined block POINTS FORWARD to where: this file
        // is read top to bottom, over the phone, and an unanswered question a
        // hundred lines above its answer is an unanswered question.
        assert!(note.contains("NOT DETERMINED YET"), "{note}");
        assert!(note.contains("FOLLOW-UP NOTE"), "{note}");
        assert!(
            note.find("If a later block headed").expect("the forward pointer")
                < note.rfind("FOLLOW-UP NOTE").expect("the block it points at"),
            "the pointer must come before what it points at:\n{note}"
        );
        assert!(
            note.contains(&format!(
                "The media folder \"{media}\" belongs to \"{db}\"."
            )),
            "and the follow-up must name both halves plainly:\n{note}"
        );

        // Printed in full under `cargo test -- --nocapture`: this file is read
        // over the phone, and the only way to know it reads well is to read it.
        println!("\n{note}");
        println!("\n{}", set_aside_notice(&dirs, &displaced));
        let _ = fs::remove_dir_all(&dirs.data);
    }

    // ---- the note's commands have to RUN ------------------------------------

    /// THE SHAPE OF EVERY COMMAND THE NOTE PRINTS, asserted without a cluster so
    /// it holds on every machine that can compile this crate.
    ///
    /// Each of these is one of the four reasons the old text could not be typed:
    /// `psql` is on nobody's PATH, the cluster answers on a socket rather than a
    /// default TCP port, the role is `guitar` and not the account name libpq
    /// assumes, and the paths involved contain a space on macOS.
    #[test]
    fn every_command_the_note_prints_is_absolute_socketed_and_quoted() {
        let dirs = tmp_dirs("cmdshape");
        let res = tmp_res(&dirs);
        let psql_path = res.join("pg/bin/psql");
        let ctl_path = res.join("pg/bin/pg_ctl");

        for cmd in [
            repair_start_cmd(&res, &dirs),
            repair_stop_cmd(&res, &dirs),
            repair_psql(&res, &dirs),
            repair_evict_cmd(&res, &dirs, "guitar"),
        ] {
            // The BUNDLED binary, by absolute path, in quotes. Nothing is
            // installed on the tutor's machine and nothing is on his PATH.
            let bin = if cmd.contains("pg_ctl") { &ctl_path } else { &psql_path };
            assert!(
                cmd.starts_with(&format!("\"{}\"", bin.display())),
                "must name the bundled binary absolutely, and quote it: {cmd}"
            );
            assert!(cmd.contains(&format!("\"{}\"", dirs.pgdata.display())), "{cmd}");
        }
        // psql reaches the cluster over the UNIX SOCKET in `<data>/pgdata` — the
        // one thing about this install that does not roll — on the port the note
        // itself pins, as the `guitar` role.
        let psql = repair_psql(&res, &dirs);
        assert!(psql.contains(" -h "), "over the socket directory: {psql}");
        assert!(psql.contains(&format!(" -p {REPAIR_PORT} ")), "{psql}");
        assert!(psql.contains(" -U guitar "), "the role initdb made: {psql}");
        assert!(psql.contains(" -d postgres"), "not the database being renamed: {psql}");
        assert!(psql.contains(" -X "), "never read anybody's .psqlrc: {psql}");
        // The repair server binds NO TCP PORT. That is what makes a pinned
        // number safe on a machine that already has a postgres on 5434.
        let start = repair_start_cmd(&res, &dirs);
        assert!(start.contains("-c listen_addresses="), "socket only: {start}");
        assert!(start.contains(" -w start"), "wait for it: {start}");
        assert!(repair_stop_cmd(&res, &dirs).contains(" -w stop"), "and for the stop");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// Stops a repair cluster however this test leaves — assertion, panic or
    /// success. A postmaster left running on a `cargo test` box holds the temp
    /// directory open and quietly breaks the next run.
    #[cfg(unix)]
    struct StopOnDrop {
        res: PathBuf,
        dirs: Dirs,
    }

    #[cfg(unix)]
    impl Drop for StopOnDrop {
        fn drop(&mut self) {
            let _ = Command::new("sh")
                .arg("-c")
                .arg(repair_stop_cmd(&self.res, &self.dirs))
                .output();
        }
    }

    /// Run one line of the note exactly as it is printed, through a shell, with
    /// NO environment of ours — the way somebody typing it would.
    ///
    /// `env_clear` is the assertion, not the setup. The bundled binaries carry
    /// their own runpath (`$ORIGIN/../lib` on Linux, `@loader_path/../lib` on
    /// macOS), so they find their libraries with nothing set; if that ever
    /// stopped being true, a note printing bare commands would stop working on
    /// the tutor's machine while every other test in this file still passed.
    /// `PATH` is kept only so `sh` itself is found.
    ///
    /// The output goes to a FILE rather than a pipe, and that is not tidiness.
    /// `pg_ctl … start` daemonizes a postmaster which inherits whatever stdout
    /// and stderr it was handed and holds them for as long as it lives, so
    /// `Command::output()` — which reads both to EOF — waits for a server this
    /// same test is going to stop four lines later, forever. A terminal is not a
    /// pipe, so the person typing these lines never meets that; a harness
    /// capturing them meets it at once.
    #[cfg(unix)]
    fn run_as_printed(dirs: &Dirs, tag: &str, line: &str) -> (bool, String) {
        let out = dirs.data.join(format!("run-{tag}.out"));
        let f = fs::File::create(&out).expect("capture file");
        let f2 = f.try_clone().expect("dup");
        let status = Command::new("sh")
            .arg("-c")
            .arg(line)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .stdout(std::process::Stdio::from(f))
            .stderr(std::process::Stdio::from(f2))
            .status()
            .expect("sh");
        (status.success(), fs::read_to_string(&out).unwrap_or_default())
    }

    /// Every line in the note's DATABASE entry that is a command, in the order
    /// it is printed. Read back out of the FILE, so what runs below is the text
    /// the tutor's son would be looking at and not a string this test built.
    #[cfg(unix)]
    fn commands_in_the_database_entry(note: &str, res: &Path) -> Vec<String> {
        let from = note.find("This is a DATABASE").expect("the database entry");
        let entry = &note[from..];
        let to = entry.find("\n===========").unwrap_or(entry.len());
        entry[..to]
            .lines()
            .map(str::trim)
            .filter(|l| l.starts_with(&format!("\"{}", res.display())))
            .map(str::to_string)
            .collect()
    }

    /// THE PROOF. The note's commands are run, as printed, against a real
    /// cluster built from the postgres this app actually ships — and the
    /// database really does come back.
    ///
    /// The whole sequence is the one the note describes, in the state the note
    /// is written in: GuitarTutor has displaced the database and QUIT, so there
    /// is no server running at all. Every step below is a line lifted out of
    /// `READ-ME-superseded-data.txt` and handed to `sh` with an empty
    /// environment.
    ///
    /// Nothing here binds a TCP port — not the setup cluster and not the repair
    /// server (`-c listen_addresses=`) — so this cannot collide with a postgres
    /// already on the box, which on a developer's machine there always is.
    ///
    /// Skipped, not failed, where `resources/pg` has not been staged: that is a
    /// checkout without the bundle, not a broken note. The shape assertions
    /// above run everywhere and cover it.
    #[cfg(unix)]
    #[test]
    fn the_notes_commands_really_put_the_database_back() {
        let Some(res) = bundled_res() else {
            println!("resources/pg is not staged in this checkout — skipping the live cluster");
            return;
        };
        let dirs = tmp_dirs("repair");
        fs::create_dir_all(&dirs.logs).expect("logs");
        mark_seeding(&dirs).expect("marker");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&dirs.pgdata, fs::Permissions::from_mode(0o700)).expect("0700");
        }
        let _stop = StopOnDrop { res: res.clone(), dirs: dirs.clone() };

        // ---- what GuitarTutor's own first run leaves behind -----------------
        initdb_once(&res, &dirs, &["--locale=C.UTF-8"]).expect("initdb");
        // Deliberately NOT `REPAIR_PORT`: the app's port rolls, and the note has
        // to work without knowing it. This one is only ever a socket file name —
        // the cluster is started with `listen_addresses=` here too, so the test
        // never opens a port on the machine it runs on.
        let app_port: u16 = 5441;
        append_conf_overrides(&dirs, app_port).expect("conf");
        let start_app = format!(
            "{} -D {} -o \"-p {app_port} -c listen_addresses=\" -w start",
            bundled_bin(&res, "pg_ctl"),
            shell_quoted(&dirs.pgdata)
        );
        let (ok, out) = run_as_printed(&dirs, "setup-start", &start_app);
        assert!(ok, "the setup cluster must start: {out}");

        psql_exec(&res, &dirs, app_port, "postgres", "CREATE DATABASE guitar").expect("createdb");
        // Something of the tutor's in it, so the probe reads `Full` and the
        // database is RENAMED rather than dropped — and so the assertion at the
        // end is about his data and not about a name.
        psql_exec(
            &res,
            &dirs,
            app_port,
            "guitar",
            "CREATE TABLE curriculum (title text); \
             INSERT INTO curriculum VALUES ('Μαθήματα κιθάρας')",
        )
        .expect("his work");
        plant_a_book(&dirs, "99999999-0000-0000-0000-000000000009");

        let (displaced, renamed) = displace_database(&res, &dirs, app_port);
        renamed.expect("the displacement itself must succeed");
        let db = displaced.databases[0].clone();

        // ---- "QUIT GUITARTUTOR" — step 1, and the state the note is read in --
        let (ok, out) = run_as_printed(&dirs, "quit", &repair_stop_cmd(&res, &dirs));
        assert!(ok, "{out}");
        assert!(out.contains("server stopped"), "the note promises this text: {out}");
        assert!(
            !dirs.pgdata.join(format!(".s.PGSQL.{app_port}")).exists(),
            "with GuitarTutor quit there is nothing listening at all — which is \
             exactly why the old note's bare `psql` could not work"
        );

        // ---- now do what the file says, line by line ------------------------
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        let cmds = commands_in_the_database_entry(&note, &res);
        assert_eq!(
            cmds.len(),
            5,
            "start, the two renames, stop, and the eviction line: {cmds:#?}"
        );

        // 2. start the repair server — on the PINNED port, not the app's.
        let (ok, out) = run_as_printed(&dirs, "step2", &cmds[0]);
        assert!(ok, "step 2 must start a server: {out}");
        assert!(out.contains("server started"), "the note promises this text: {out}");
        assert!(
            dirs.pgdata.join(format!(".s.PGSQL.{REPAIR_PORT}")).exists(),
            "and it must be reachable where the psql lines look for it"
        );

        // 3. move the current `guitar` aside. There is none — GuitarTutor
        //    renamed it away — and the note says in as many words that this is
        //    fine and what it will look like.
        let (ok, out) = run_as_printed(&dirs, "step3", &cmds[1]);
        assert!(!ok, "there is no `guitar` here: {out}");
        assert!(
            out.contains("database \"guitar\" does not exist"),
            "the exact text the note tells the reader to expect: {out}"
        );
        assert!(
            note.contains("database \"guitar\" does not exist"),
            "and the note must print that text, or step 3 reads as a failure:\n{note}"
        );

        // 4. put this one back. THE STEP EVERYTHING ELSE EXISTS FOR.
        let (ok, out) = run_as_printed(&dirs, "step4", &cmds[2]);
        assert!(ok, "step 4 must work: {out}");
        assert!(out.contains("ALTER DATABASE"), "the note promises this text: {out}");

        // …and it is HIS database, with his row in it, under the name the app
        // will open on the next launch.
        assert_eq!(
            psql_scalar(&res, &dirs, REPAIR_PORT, "guitar", "SELECT title FROM curriculum")
                .expect("read it back"),
            "Μαθήματα κιθάρας",
            "the whole point: his work is back in the database GuitarTutor opens"
        );
        assert!(
            !database_exists(&res, &dirs, REPAIR_PORT, &db),
            "and it is no longer sitting under the set-aside name"
        );

        // With a `guitar` now present, step 3 does what it says as well — the
        // ordinary case, where an install is replaced rather than repaired.
        let (ok, out) = run_as_printed(&dirs, "step3-again", &cmds[1]);
        assert!(ok, "step 3 with a `guitar` present: {out}");
        assert!(out.contains("ALTER DATABASE"), "{out}");
        run_as_printed(&dirs, "step4-again", &cmds[2]); // put it back so the cluster ends tidy

        // 5. stop it again. Not optional, and the note says why.
        let (ok, out) = run_as_printed(&dirs, "step5", &cmds[3]);
        assert!(ok, "step 5 must stop the server: {out}");
        assert!(out.contains("server stopped"), "the note promises this text: {out}");

        // The remedy for the one failure this repair can hit. Postgres's own
        // wording is what the note tells the reader to look for, so the two have
        // to be the same string.
        assert_eq!(cmds[4], repair_evict_cmd(&res, &dirs, "guitar"));
        assert!(note.contains("is being accessed by other users"), "\n{note}");

        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A script that prints its own argv, one argument per line. Stands in for
    /// `psql` and `pg_ctl` so a printed command can be checked for what a SHELL
    /// makes of it, with no cluster and no postgres involved.
    #[cfg(unix)]
    fn stub_argv_dump(res: &Path, bin: &str) {
        use std::os::unix::fs::PermissionsExt;
        let path = res.join("pg/bin").join(bin);
        fs::create_dir_all(path.parent().expect("bin")).expect("bin");
        fs::write(&path, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n").expect("stub");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).expect("chmod");
    }

    /// THE TUTOR'S MACHINE HAS A SPACE IN EVERY PATH THE NOTE PRINTS.
    ///
    /// macOS puts the data folder in `~/Library/Application Support/GuitarTutor`
    /// and the binaries in `…/GuitarTutor.app/Contents/Resources/…`. An unquoted
    /// path in a file that says "type this exactly" is a command that fails on
    /// HIS computer and on nobody else's — the worst shape a bug can have in a
    /// recovery note, because it works everywhere it is tested.
    ///
    /// So this asserts what a SHELL makes of the printed line, not what the
    /// string looks like: the binaries are replaced by scripts that print their
    /// own argv, and the socket directory has to come back as ONE argument.
    #[cfg(unix)]
    #[test]
    fn the_notes_commands_survive_a_data_folder_with_a_space_in_its_name() {
        // Exactly the macOS shape, under a temp root.
        let root = std::env::temp_dir().join(format!("gt-space-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let data = root.join("Library/Application Support/GuitarTutor");
        let res = root.join("Applications/GuitarTutor.app/Contents/Resources/resources");
        let dirs = Dirs {
            pgdata: data.join("pgdata"),
            media: data.join("media"),
            secrets: data.join("secrets"),
            logs: root.join("Library/Logs/GuitarTutor"),
            data: data.clone(),
        };
        for d in [&dirs.pgdata, &dirs.media, &dirs.logs] {
            fs::create_dir_all(d).expect("dirs");
        }
        assert!(dirs.data.display().to_string().contains(' '), "the whole point");

        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "dddd4444-0000-0000-0000-00000000000d");
        stub_psql(&res);
        let (displaced, ok) = displace_database(&res, &dirs, 5999);
        ok.expect("displace");
        let db = displaced.databases[0].clone();

        // Now make the two binaries report their argv instead of doing anything.
        stub_argv_dump(&res, "psql");
        stub_argv_dump(&res, "pg_ctl");

        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        let cmds = commands_in_the_database_entry(&note, &res);
        assert_eq!(cmds.len(), 5, "{cmds:#?}");
        for cmd in &cmds {
            let (ok, out) = run_as_printed(&dirs, "argv", cmd);
            assert!(ok, "the shell must be able to run it at all: {cmd}\n{out}");
            let argv: Vec<&str> = out.lines().collect();
            // The socket directory — the one with the space in it — has to
            // arrive as a single argument, not as "…/Application" and
            // "Support/GuitarTutor/pgdata".
            assert!(
                argv.contains(&dirs.pgdata.display().to_string().as_str()),
                "the path arrived split: {cmd}\nargv = {argv:#?}"
            );
        }
        // …and the SQL survives its own quoting: the whole statement, database
        // name and all, is one argument to psql.
        let (_, out) = run_as_printed(&dirs, "argv-sql", &cmds[2]);
        assert!(
            out.lines().any(|l| l == format!("ALTER DATABASE \"{db}\" RENAME TO guitar")),
            "the statement must reach psql whole:\n{out}"
        );

        // Printed under `--nocapture`: this is the file, with the paths it has
        // on the machine it was written for.
        println!("\n{note}");
        let _ = fs::remove_dir_all(&root);
    }

    /// "MEASURED EMPTY" AND "ALREADY GONE" ARE NOT THE SAME SENTENCE, and the
    /// note prints the sentence.
    ///
    /// On the `Drop` path the media entry used to be recorded `Alone`, whose
    /// text reads "the database these files belonged to was already gone when
    /// they were found". That is true of a database that was never there
    /// (`DbProbe::Absent`) and false here: this database WAS there, GuitarTutor
    /// counted the user objects in it, found none, and removed it on that count.
    /// The difference matters to the person reading — one sentence describes a
    /// database that vanished on its own, which is alarming and did not happen.
    #[cfg(unix)]
    #[test]
    fn a_database_measured_empty_is_not_described_as_one_that_was_already_gone() {
        let dirs = tmp_dirs("emptydrop");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "88888888-0000-0000-0000-000000000008");
        let res = tmp_res(&dirs);
        stub_psql_holding(&res, 0); // not one table, view or sequence

        let (displaced, ok) = displace_database(&res, &dirs, 5999);
        ok.expect("the drop path");
        assert!(displaced.databases.is_empty(), "it was dropped, not renamed");
        assert_eq!(displaced.media.len(), 1, "his books still travelled");

        let state = read_state(&dirs).expect("marker");
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::AloneEmptyDatabase,
            "the measurement that was actually taken"
        );

        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(
            note.contains("was NOT already") && note.contains("found not one table"),
            "the note must say what was measured:\n{note}"
        );
        // The entry's OWN "to put this one back" block is where the claim is
        // made, so that is where it has to be right. (The standing paragraph at
        // the foot of every entry lists all four ways a media folder can end up
        // unpaired, this one included, and naturally names the others.)
        let media = displaced.media[0].clone();
        let how = restore_instructions(&res, &dirs, "media", &media, &Companion::AloneEmptyDatabase);
        assert!(how.contains("was NOT already"), "{how}");
        assert!(
            !how.contains("was already gone when"),
            "the entry must not print the OTHER measurement's sentence:\n{how}"
        );
        // …and the absent case keeps its own, unchanged.
        assert!(
            restore_instructions(&res, &dirs, "media", &media, &Companion::Alone)
                .contains("was already gone when"),
            "`Alone` keeps the sentence that is true of it"
        );
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// THE REAPER MUST NOT LEAVE THE MARKER DESCRIBING SOMETHING IT DELETED.
    ///
    /// Entries are never removed from `install-state.json` — it is the ledger —
    /// but until now an entry outlived the thing it was about with nothing to
    /// say so, and the recovery note beside it went on explaining how to rename
    /// back a folder that had been gone for a month. Worse, `unannounced` would
    /// still offer it to the tutor as news.
    #[test]
    fn a_reaped_set_aside_is_marked_gone_rather_than_left_standing() {
        let dirs = tmp_dirs("reapmark");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "aaaa1111-0000-0000-0000-00000000000a");

        let moved = displace_media(&tmp_res(&dirs), &dirs, "20200101T000000Z", Companion::Unknown, "a test")
            .expect("displace");
        let media = moved.sole_media().expect("moved").to_string();
        assert!(unannounced(&dirs).media.contains(&media), "news, for now");

        // Released with the database it shares a stamp with.
        reap_companion_media(&dirs, "guitar_superseded_20200101T000000Z", SUPERSEDED_DB_PREFIX);
        assert!(!dirs.data.join(&media).exists(), "really removed");

        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1, "the entry STAYS — this file is a ledger");
        assert!(
            state.set_aside[0].reaped_at.is_some(),
            "…but it has to say that what it describes is gone: {:?}",
            state.set_aside[0]
        );
        assert!(
            !unannounced(&dirs).anything_set_aside(),
            "and it is not announced as data he can still get back"
        );
        // The plain-text note carries the removal too, for whoever reads that.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains(&format!("reaped the set-aside media folder '{media}'")), "{note}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// THE MAIN NOTE HAS TO BE ABLE TO SEE THE API CHILD'S OWN QUARANTINE.
    ///
    /// `app/brain/media.py::sweep_orphaned_media` sets aside every media
    /// directory the database it is connected to has no row for — after a
    /// database that was replaced or renamed, that is the whole library — and it
    /// explains itself only in a README four directories down. Somebody reading
    /// `READ-ME-superseded-data.txt` from the top, which is the file the dialog
    /// names and support asks for, learned nothing about it at all.
    #[test]
    fn the_main_note_describes_the_api_childs_quarantine() {
        let dirs = tmp_dirs("apiquar");
        mark_seeding(&dirs).expect("marker");
        let res = tmp_res(&dirs);

        // Nothing there yet: on the overwhelmingly common launch this is silent.
        assert!(!describe_api_quarantine(&res, &dirs).anything_set_aside());

        // What one sweep of a replaced database leaves: a dated batch holding
        // every book, and the API's own note beside it.
        let batch = dirs.media.join("_superseded/20260715T101112Z");
        fs::create_dir_all(batch.join("bbbb2222-0000-0000-0000-00000000000b")).expect("batch");
        fs::write(
            batch.join("bbbb2222-0000-0000-0000-00000000000b/source.pdf"),
            b"HIS ONLY COPY",
        )
        .expect("pdf");
        fs::write(
            dirs.media.join("_superseded").join(API_QUARANTINE_NOTE),
            b"the API's own explanation",
        )
        .expect("readme");

        let seen = describe_api_quarantine(&res, &dirs);
        assert_eq!(
            seen.media,
            vec!["media/_superseded/20260715T101112Z".to_string()],
            "named the way the reader has to navigate to it: {seen:?}"
        );

        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1);
        assert_eq!(state.set_aside[0].kind, "quarantine");

        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains("media/_superseded/20260715T101112Z"), "{note}");
        assert!(note.contains("THESE ARE FILES OF YOURS"), "{note}");
        // The instruction that is easy to get wrong, and the one fact that
        // separates this from every other entry in the file.
        assert!(note.contains("TWO levels up"), "{note}");
        assert!(
            note.contains("DELETED FOR GOOD 30 days"),
            "nothing else in this file is on a clock, so this has to say it:\n{note}"
        );
        assert!(note.contains(API_QUARANTINE_NOTE), "point at its own note too:\n{note}");
        // It must not inherit the database wording — there is no `ALTER
        // DATABASE` that puts a folder of PDFs back.
        assert!(!note.contains("ALTER DATABASE"), "{note}");

        // …and it is said ONCE. This runs on every launch; a block per launch
        // would bury the entry that matters.
        assert!(
            !describe_api_quarantine(&res, &dirs).anything_set_aside(),
            "already recorded"
        );
        assert_eq!(read_state(&dirs).expect("marker").set_aside.len(), 1);

        // A batch whose contents were moved back BY HAND — which is exactly what
        // the instructions say to do — must stop being announced, and a folder
        // somebody else made in there is not ours to describe at all.
        fs::remove_dir_all(batch.join("bbbb2222-0000-0000-0000-00000000000b")).expect("moved back");
        fs::create_dir_all(dirs.media.join("_superseded/my-own-backup")).expect("theirs");
        fs::write(dirs.media.join("_superseded/my-own-backup/x.pdf"), b"mine").expect("theirs");
        let dirs2 = Dirs { data: dirs.data.join("second-look"), ..dirs.clone() };
        fs::create_dir_all(&dirs2.data).expect("dir");
        mark_seeding(&dirs2).expect("a marker with nothing recorded in it");
        let fresh = describe_api_quarantine(&res, &dirs2);
        assert!(
            !fresh.anything_set_aside(),
            "an emptied batch is not data set aside, and somebody else's folder \
             was never ours: {fresh:?}"
        );
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// The other side of the same coin: once BOTH renames have happened the
    /// pairing IS stated, and stated as certain. `reconcile_pairing` is what
    /// turns the honest `Undetermined` into a fact, and it may only run after
    /// the second rename returned Ok.
    #[test]
    fn a_pairing_is_settled_only_after_both_renames_and_then_said_plainly() {
        let dirs = tmp_dirs("reconcile");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "ffffffff-0000-0000-0000-000000000006");

        let moved = displace_media(&tmp_res(&dirs), &dirs, "20260730T142530Z", Companion::Unknown, "a test")
            .expect("displace");
        let media = moved.sole_media().expect("moved").to_string();
        assert_eq!(read_state(&dirs).expect("marker").set_aside[0].pairing, Companion::Unknown);

        reconcile_pairing(&dirs, &media, "guitar_superseded_20260730T142530Z");

        let state = read_state(&dirs).expect("marker");
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::Is("guitar_superseded_20260730T142530Z".to_string()),
            "settled in the marker"
        );
        // The note is APPEND-ONLY, so the correction arrives as a new block
        // rather than as a silent edit to the one already written.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains("FOLLOW-UP NOTE"), "{note}");
        assert!(note.contains("ΣΥΜΠΛΗΡΩΜΑΤΙΚΗ ΣΗΜΕΙΩΣΗ"), "Greek half too:\n{note}");
        assert!(note.contains("RESTORE THEM TOGETHER"), "{note}");
        assert!(
            note.find("WAS NOT KNOWN WHEN").expect("the first block still says what it said")
                < note.rfind("FOLLOW-UP NOTE").expect("and the correction comes after"),
            "the original entry is never rewritten:\n{note}"
        );
        // …and with the pairing settled, that folder is no longer offered up as
        // an unaccounted-for one to whoever reads a later entry.
        assert!(unpaired_media_on_disk(&dirs).is_empty());
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// EVERY path that can displace media hands back a receipt, and every one
    /// of them records what it did where the tutor's dialog can find it. Three
    /// of these four used to return `()` (or an `Option` nobody looked at) and
    /// show him nothing whatsoever.
    ///
    /// `init_cluster` is the fourth and is not driven here — it needs the
    /// bundled `initdb` — but it is covered by construction: it now returns
    /// `Displaced` and its only displacement is `set_aside_pgdata`, whose
    /// receipt it absorbs.
    #[test]
    fn no_path_can_displace_media_without_handing_back_a_receipt() {
        // 1. the pgdata path (`main`'s postgres-would-not-start recovery, and
        //    the Greek-collation retry).
        let dirs = tmp_dirs("everypath1");
        mark_seeding(&dirs).expect("marker");
        fs::write(dirs.pgdata.join("postgresql.conf"), b"leftovers").expect("cluster");
        plant_a_book(&dirs, "11111111-0000-0000-0000-000000000001");
        let r = set_aside_pgdata(&tmp_res(&dirs), &dirs, "a test").expect("aside");
        assert_eq!(r.directories.len(), 1, "the cluster");
        assert_eq!(r.media.len(), 1, "AND its media — this is the half that was silent");
        assert!(unannounced(&dirs).anything_set_aside(), "durably to-do, too");
        let _ = fs::remove_dir_all(&dirs.data);

        // 2. `clear_unfinished_pgdata`, which used to `.map(|_| ())` it away.
        let dirs = tmp_dirs("everypath2");
        mark_seeding(&dirs).expect("marker");
        fs::create_dir_all(dirs.pgdata.join("base/1")).expect("skeleton");
        fs::write(dirs.pgdata.join("postgresql.conf"), b"half written").expect("conf");
        plant_a_book(&dirs, "22222222-0000-0000-0000-000000000002");
        let r = clear_unfinished_pgdata(&tmp_res(&dirs), &dirs).expect("clear");
        assert_eq!(r.media.len(), 1, "his books moved with the skeleton, silently");
        let _ = fs::remove_dir_all(&dirs.data);

        // 3. `create_and_seed_db`'s choke point — the last thing between his
        //    uploads and a fresh starter library.
        let dirs = tmp_dirs("everypath3");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "33333333-0000-0000-0000-000000000003");
        let r = displace_media_for_a_fresh_database(&tmp_res(&dirs), &dirs).expect("displace");
        assert_eq!(r.media.len(), 1);
        assert!(unannounced(&dirs).anything_set_aside());
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A boot that displaces something and then DIES never reaches the dialog,
    /// and the in-process receipt dies with the process. The record on disk does
    /// not, so the next boot that gets a window on screen says it then — once.
    #[test]
    fn a_displacement_survives_a_boot_that_never_reached_the_dialog() {
        let dirs = tmp_dirs("unannounced");
        mark_seeding(&dirs).expect("marker");
        plant_a_book(&dirs, "44444444-0000-0000-0000-000000000004");

        // The boot that died: it displaced, and nobody was told.
        let _ = displace_media_for_a_fresh_database(&tmp_res(&dirs), &dirs).expect("displace");
        let pending = unannounced(&dirs);
        assert_eq!(pending.media.len(), 1, "the next boot must find this waiting");

        // The next boot tells him, and only then marks it off. Order matters:
        // marking first would swallow the one message that explains where his
        // library went if the dialog never made it to the screen.
        let msg = set_aside_notice(&dirs, &pending);
        assert!(msg.contains(&pending.media[0]), "{msg}");
        mark_announced(&dirs);
        assert!(
            !unannounced(&dirs).anything_set_aside(),
            "and he is not told the same thing on every launch forever"
        );
        // The RECORD itself is untouched by that — it is the recovery, not the
        // notification.
        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1);
        assert!(state.set_aside[0].announced);
        let _ = fs::remove_dir_all(&dirs.data);
    }

    // ---- displacement ------------------------------------------------------

    /// THE RULE, over the whole probe space: a database is DESTROYED only when
    /// it has been measured to hold nothing. Everything else — including a
    /// probe that could not answer — is renamed out of the way.
    ///
    /// Swept rather than listed, and with no wildcard in `displacement_for`, so
    /// a `DbProbe` variant added later cannot quietly inherit `Drop`.
    #[test]
    fn only_a_measured_empty_database_is_ever_destroyed() {
        for probe in [
            DbProbe::Absent,
            DbProbe::Empty,
            DbProbe::Full,
            DbProbe::Unknown,
        ] {
            let what = displacement_for(probe);
            if what == Displacement::Drop {
                assert_eq!(
                    probe,
                    DbProbe::Empty,
                    "{probe:?} must never authorise destroying a database"
                );
            }
        }
        // Spelled out, because these two are the ones that used to be a DROP:
        assert_eq!(displacement_for(DbProbe::Full), Displacement::Rename);
        assert_eq!(
            displacement_for(DbProbe::Unknown),
            Displacement::Rename,
            "a probe that cannot answer is not a measurement of emptiness"
        );
        assert_eq!(displacement_for(DbProbe::Absent), Displacement::Nothing);
    }

    /// Names this app builds get interpolated into SQL and into filenames, so
    /// the check that they are plain identifiers has to actually work.
    #[test]
    fn set_aside_names_are_plain_identifiers() {
        let generated = format!(
            "{SUPERSEDED_DB_PREFIX}{}",
            utc_compact_stamp(SystemTime::now())
        );
        assert!(is_safe_identifier(&generated), "{generated}");
        assert!(is_safe_identifier(&format!("{generated}_2")));
        assert!(generated.len() <= 63, "must fit NAMEDATALEN: {generated}");

        for bad in [
            "",
            "guitar\"; DROP DATABASE guitar --",
            "guitar'; --",
            "guitar superseded",
            "guitar-superseded",
            "guitar\nsuperseded",
        ] {
            assert!(!is_safe_identifier(bad), "{bad:?} must be rejected");
        }
        // 64 characters: one past what Postgres keeps, which would silently
        // truncate and could collide with an existing name.
        assert!(!is_safe_identifier(&"a".repeat(64)));
        assert!(is_safe_identifier(&"a".repeat(63)));
    }

    /// Only names THIS build writes are ever recognised — and therefore only
    /// they can ever be reaped.
    #[test]
    fn stamps_are_only_read_out_of_names_this_build_wrote() {
        assert_eq!(
            stamp_of("guitar_superseded_20260730T142530Z", SUPERSEDED_DB_PREFIX),
            Some("20260730T142530Z")
        );
        // The uniquifier does not hide the stamp.
        assert_eq!(
            stamp_of("guitar_superseded_20260730T142530Z_2", SUPERSEDED_DB_PREFIX),
            Some("20260730T142530Z")
        );
        for foreign in [
            "guitar",
            "guitar_superseded_",
            "guitar_superseded_backup",          // somebody's own name
            "guitar_superseded_2026-07-30",      // not our format
            "guitar_superseded_20260730T14253Z", // one digit short
            "guitar_superseded_20260730X142530Z",
            "pgdata_superseded_20260730T142530Z", // right shape, wrong prefix
        ] {
            assert_eq!(
                stamp_of(foreign, SUPERSEDED_DB_PREFIX),
                None,
                "{foreign} must not be recognised, and so can never be reaped"
            );
        }
    }

    /// THE REAPING POLICY, stated as a table. Removal requires BOTH "not among
    /// the 3 most recent" AND "older than the cutoff" — either one alone must
    /// keep it.
    #[test]
    fn reaping_releases_only_what_is_both_old_and_superseded() {
        let name = |s: &str| format!("{SUPERSEDED_DB_PREFIX}{s}");
        let cutoff = "20260101T000000Z";
        let old = [
            name("20200101T000000Z"),
            name("20210101T000000Z"),
            name("20220101T000000Z"),
            name("20230101T000000Z"),
            name("20240101T000000Z"),
            name("20250101T000000Z"),
        ];

        // Six old ones: the three newest are kept regardless of age, AND the
        // oldest is pinned — so only the two in the middle are released.
        let reaped = reapable(&old, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP);
        assert_eq!(
            reaped,
            vec![old[1].clone(), old[2].clone()],
            "keep the 3 most recent AND the oldest; release only the middle"
        );
        assert!(
            !reaped.contains(&old[0]),
            "the oldest set-aside is the one most likely to hold his own work"
        );

        // Four or fewer: nothing is ever released, however old. Three fill the
        // keep window and the fourth is the pinned oldest.
        for n in 0..=(SUPERSEDED_KEEP + 1) {
            assert!(
                reapable(&old[..n], SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP).is_empty(),
                "{n} set-asides: the keep window plus the pinned oldest covers them all"
            );
        }

        // Young ones are kept even when they are not among the most recent —
        // this is the half that gives anyone a month to notice and ask.
        let young: Vec<String> = (1..=6).map(|i| name(&format!("2026070{i}T000000Z"))).collect();
        assert!(
            reapable(&young, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP).is_empty(),
            "nothing under the age cutoff may be released"
        );

        // A name this build did not write is never released, whatever its age
        // or position.
        let mixed = [
            name("19980101T000000Z"),
            name("19990101T000000Z"),
            name("someone-elses-backup"),
            name("20200101T000000Z"),
            name("20210101T000000Z"),
            name("20220101T000000Z"),
            name("20230101T000000Z"),
        ];
        let reaped = reapable(&mixed, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP);
        assert!(
            !reaped.iter().any(|n| n.contains("someone-elses-backup")),
            "unrecognised names are never removed: {reaped:?}"
        );
        assert!(
            reaped.iter().all(|n| stamp_of(n, SUPERSEDED_DB_PREFIX).is_some()),
            "only names this build wrote may be released: {reaped:?}"
        );
    }

    /// A NAME THIS BUILD DID NOT WRITE MUST NOT MOVE THE POLICY.
    ///
    /// The old `reapable` sorted every name it was handed, parseable or not, and
    /// then applied "pin index 0, keep the last 3" positionally. An alien name is
    /// then not merely ignored — it takes a SLOT, and whichever genuine
    /// set-aside it displaces loses its protection. Both directions are real:
    /// `guitar_superseded_0…` sorts below every timestamp (digits below `1`),
    /// `guitar_superseded_someone-elses-backup` sorts above every timestamp.
    #[test]
    fn an_unrecognised_name_can_neither_be_reaped_nor_shift_the_policy() {
        let name = |s: &str| format!("{SUPERSEDED_DB_PREFIX}{s}");
        let cutoff = "20260101T000000Z";

        // An alien BELOW every stamp used to take the pin, exposing the
        // genuinely oldest set-aside — the one most likely to hold his own work.
        let below = [
            name("0-a-folder-somebody-else-made"),
            name("20200101T000000Z"), // the genuinely oldest: PINNED
            name("20210101T000000Z"),
            name("20220101T000000Z"),
            name("20230101T000000Z"),
            name("20240101T000000Z"),
            name("20250101T000000Z"),
        ];
        let reaped = reapable(&below, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP);
        assert!(
            !reaped.contains(&below[1]),
            "the oldest is pinned by its TIMESTAMP, not by its position: {reaped:?}"
        );
        assert!(!reaped.iter().any(|n| n.contains("somebody-else")), "{reaped:?}");
        assert_eq!(reaped, vec![below[2].clone(), below[3].clone()], "{reaped:?}");

        // An alien ABOVE every stamp used to eat a slot in the keep window,
        // exposing a set-aside that really was among the three most recent.
        let above = [
            name("20200101T000000Z"), // PINNED
            name("20210101T000000Z"),
            name("20220101T000000Z"),
            name("20230101T000000Z"), // …the three most recent, all KEPT
            name("20240101T000000Z"),
            name("20250101T000000Z"),
            name("someone-elses-backup"),
        ];
        let reaped = reapable(&above, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP);
        assert_eq!(reaped, vec![above[1].clone(), above[2].clone()], "{reaped:?}");
        assert!(
            !reaped.contains(&above[3]),
            "an alien must not push a genuine set-aside out of the keep window"
        );

        // Two set-asides made in the SAME SECOND share a stamp and differ only
        // by the uniquifier. "The oldest" is a moment in time, so both are
        // pinned — pinning one and releasing the other would release data from
        // the very moment the pin exists to protect.
        let tied = [
            name("20200101T000000Z"),
            name("20200101T000000Z_2"),
            name("20210101T000000Z"),
            name("20220101T000000Z"),
            name("20230101T000000Z"),
            name("20240101T000000Z"),
            name("20250101T000000Z"),
        ];
        let reaped = reapable(&tied, SUPERSEDED_DB_PREFIX, cutoff, SUPERSEDED_KEEP);
        assert_eq!(reaped, vec![tied[2].clone(), tied[3].clone()], "{reaped:?}");
    }

    /// A database and its media are ONE UNIT on the way out as well as on the
    /// way in. Releasing one and keeping the other leaves either rows nothing
    /// can illustrate or files nothing can name — half a recovery.
    #[test]
    fn the_reaper_never_separates_a_database_from_its_media() {
        // The pairing is derived from the name and from nothing else, so it can
        // never widen what the reaper touches.
        assert_eq!(
            companion_media_name("guitar_superseded_20260730T142530Z", SUPERSEDED_DB_PREFIX)
                .as_deref(),
            Some("media_superseded_20260730T142530Z")
        );
        // The uniquifier is part of the suffix — `free_suffix` uses one tail for
        // both halves, so the pairing has to carry it too.
        assert_eq!(
            companion_media_name("guitar_superseded_20260730T142530Z_2", SUPERSEDED_DB_PREFIX)
                .as_deref(),
            Some("media_superseded_20260730T142530Z_2")
        );
        assert_eq!(
            companion_media_name("pgdata_superseded_20260730T142530Z", SUPERSEDED_DIR_PREFIX)
                .as_deref(),
            Some("media_superseded_20260730T142530Z")
        );
        for foreign in ["guitar_superseded_backup", "guitar", "guitar_superseded_"] {
            assert_eq!(
                companion_media_name(foreign, SUPERSEDED_DB_PREFIX),
                None,
                "{foreign}: a name this build did not write has no companion"
            );
        }

        let dirs = tmp_dirs("reappair");
        let db = "guitar_superseded_20200101T000000Z";
        let media = dirs.data.join("media_superseded_20200101T000000Z");
        fs::create_dir_all(&media).expect("media");
        fs::write(media.join("keep.pdf"), b"x").expect("file");

        // A media folder is NEVER a reap candidate in its own right: the dir
        // pass only ever lists `pgdata_superseded_…`.
        assert!(
            !list_superseded_dirs(&dirs).iter().any(|n| n.starts_with(SUPERSEDED_MEDIA_PREFIX)),
            "media is released only with its partner, never on its own"
        );

        // …and it goes when — and only when — its partner goes.
        reap_companion_media(&dirs, "guitar_superseded_20210101T000000Z", SUPERSEDED_DB_PREFIX);
        assert!(media.is_dir(), "a different stamp is a different unit");
        reap_companion_media(&dirs, "guitar_superseded_not-ours", SUPERSEDED_DB_PREFIX);
        assert!(media.is_dir(), "an unrecognised name releases nothing");
        reap_companion_media(&dirs, db, SUPERSEDED_DB_PREFIX);
        assert!(!media.exists(), "the pair is released together");

        // The removal is written down too, so nobody hunts for it later.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains("media_superseded_20200101T000000Z"), "{note}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A set-aside thing nobody can find is worth no more than a deleted one.
    /// Three independent records, because they fail in different ways: the log,
    /// the marker, and a plain-text note in the data folder the tutor can
    /// stumble across without being told to.
    #[test]
    fn every_set_aside_thing_is_recorded_where_it_can_be_found() {
        let dirs = tmp_dirs("announce");
        touch_pg_version(&dirs);
        mark_seeding(&dirs).expect("mark");

        let receipt = announce_set_aside(
            &tmp_res(&dirs),
            &dirs,
            "database",
            "guitar_superseded_20260730T142530Z",
            &Companion::Is("media_superseded_20260730T142530Z".to_string()),
            "a test",
        );
        // 0. the RECEIPT — the in-process half, which is what carries this to
        // the tutor's dialog and what `#[must_use]` refuses to let anyone drop.
        assert_eq!(receipt.databases, vec!["guitar_superseded_20260730T142530Z"]);
        assert!(receipt.anything_set_aside());

        // 1. the marker — and the PHASE it carries is untouched.
        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.phase, PHASE_SEEDING, "recording must not change the phase");
        assert_eq!(state.set_aside.len(), 1);
        assert_eq!(state.set_aside[0].name, "guitar_superseded_20260730T142530Z");
        assert_eq!(state.set_aside[0].kind, "database");
        // The PAIRING travels in the marker too, not only in the shared name
        // suffix: a rename by hand can break the suffix, and this file is the
        // one support already asks for.
        assert_eq!(
            state.set_aside[0].pairing,
            Companion::Is("media_superseded_20260730T142530Z".to_string()),
            "a database and the media it indexes are never recorded apart"
        );

        // 2. …and it SURVIVES the phase change that follows, which is the whole
        // point of carrying it forward in `write_phase`.
        let _ = finalize_install(&dirs, SeedPlan::Reseed, SeedOutcome::Restored).expect("finalize");
        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.phase, PHASE_COMPLETE);
        assert_eq!(state.set_aside.len(), 1, "the record must not be erased by a phase write");

        // 3. the note in the data folder: bilingual, Greek first, names the
        // thing, and says the one thing that matters.
        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains("guitar_superseded_20260730T142530Z"), "{note}");
        let greek = note.find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c)).expect("Greek");
        let english = note.find("did\nNOT delete it").expect("English");
        assert!(greek < english, "Greek first:\n{note}");
        assert!(note.contains("ALTER DATABASE"), "how to get it back:\n{note}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// `<data>/media` holds the tutor's own uploads as well as the bundled seed
    /// assets, and the seed copy runs again on every `Reseed`. A file of his
    /// that happens to share a name with a bundled one must not be overwritten
    /// — and the identical-asset case must still be a cheap no-op.
    ///
    /// AND HE HAS TO BE TOLD, in all four places. That is the half this test
    /// grew: the rename was real and correct from the day it was written, and
    /// the only record of it was one line in `app.log` — a file the tutor never
    /// opens and the dialog never mentions.
    #[test]
    fn seeding_media_never_overwrites_a_file_that_is_already_there() {
        let dirs = tmp_dirs("media");
        mark_seeding(&dirs).expect("marker"); // so the record can be written
        let src = dirs.data.join("seed-media");
        fs::create_dir_all(src.join("lesson")).expect("src");
        fs::write(src.join("lesson/backing.mp3"), b"BUNDLED-ASSET").expect("bundled");
        fs::write(src.join("lesson/same.txt"), b"identical").expect("bundled 2");

        // His own file under the same name, plus one that really is the same
        // bundled asset from an earlier partial run.
        fs::create_dir_all(dirs.media.join("lesson")).expect("media");
        fs::write(dirs.media.join("lesson/backing.mp3"), b"HIS OWN RECORDING")
            .expect("his file");
        fs::write(dirs.media.join("lesson/same.txt"), b"identical").expect("already copied");

        let receipt = copy_tree(&tmp_res(&dirs), &dirs, &src, &dirs.media).expect("copy");

        // The bundled asset landed…
        assert_eq!(
            fs::read_to_string(dirs.media.join("lesson/backing.mp3")).expect("read"),
            "BUNDLED-ASSET"
        );
        // …and HIS file is still on disk, beside it, under a superseded name.
        let kept: Vec<String> = fs::read_dir(dirs.media.join("lesson"))
            .expect("read")
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.contains(".superseded-"))
            .collect();
        assert_eq!(kept.len(), 1, "his file must be kept: {kept:?}");
        assert_eq!(
            fs::read_to_string(dirs.media.join("lesson").join(&kept[0])).expect("read"),
            "HIS OWN RECORDING"
        );

        // …AND HE IS TOLD, which is the half this path did not have. This was
        // the one displacement in the whole module recorded in `app.log` and
        // nowhere else: no marker entry, no recovery-note block, never a word in
        // the dialog. All four now, like everything else.
        let shown = format!("media/lesson/{}", kept[0]);
        assert_eq!(receipt.files, vec![shown.clone()], "the receipt: {receipt:?}");
        assert!(receipt.anything_set_aside());

        let state = read_state(&dirs).expect("marker");
        assert_eq!(state.set_aside.len(), 1, "{:?}", state.set_aside);
        assert_eq!(state.set_aside[0].kind, "file");
        assert_eq!(state.set_aside[0].name, shown);
        assert!(unannounced(&dirs).files.contains(&shown), "a durable to-do too");

        let note = fs::read_to_string(dirs.data.join(RECOVERY_NOTE)).expect("note");
        assert!(note.contains(&shown), "the note must name it:\n{note}");
        assert!(
            note.contains("This is ONE FILE OF YOURS"),
            "and say what it is and how to put it back:\n{note}"
        );
        // The kind gets its OWN instructions. It must not inherit the database
        // or media wording, which asserts things about a `media` FOLDER that
        // were never true of one file.
        assert!(!note.contains("ALTER DATABASE"), "not a database:\n{note}");
        assert!(
            !note.contains("No media folder was set aside at the same moment"),
            "not a claim about the media folder either:\n{note}"
        );
        // And the dialog says it in his own words, in both languages.
        let msg = set_aside_notice(&dirs, &receipt);
        assert!(msg.contains(&shown), "{msg}");
        assert!(msg.contains("ΕΝΑ ΔΙΚΟ ΣΑΣ ΑΡΧΕΙΟ"), "{msg}");
        assert!(msg.contains("ONE OF YOUR OWN FILES"), "{msg}");

        // The identical file was skipped, not duplicated: a re-run of the seed
        // must not litter the media folder with copies of itself.
        let again = copy_tree(&tmp_res(&dirs), &dirs, &src, &dirs.media).expect("copy again");
        assert!(!again.anything_set_aside(), "nothing moved the second time");
        let superseded = fs::read_dir(dirs.media.join("lesson"))
            .expect("read")
            .flatten()
            .filter(|e| e.file_name().to_string_lossy().contains(".superseded-"))
            .count();
        assert_eq!(superseded, 1, "an identical file must be skipped, not set aside again");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// The dialog the tutor actually reads. It has to name BOTH halves: the
    /// database is where his lessons are and the media folder is where his
    /// uploaded books physically are, and a dialog naming one of the two sends
    /// whoever helps him hunting for a library that is sitting right there.
    #[test]
    fn the_set_aside_notice_is_bilingual_greek_first_and_names_both_halves() {
        let dirs = tmp_dirs("notice");
        let displaced = Displaced {
            databases: vec!["guitar_superseded_20260730T142530Z".to_string()],
            media: vec!["media_superseded_20260730T142530Z".to_string()],
            directories: vec![],
            files: vec![],
        };
        let msg = set_aside_notice(&dirs, &displaced);
        let greek = msg.find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c)).expect("Greek");
        let english = msg.find("GuitarTutor found data from an earlier").expect("English");
        assert!(greek < english, "Greek first:\n{msg}");
        assert!(msg.matches("guitar_superseded_20260730T142530Z").count() >= 2);
        assert!(
            msg.matches("media_superseded_20260730T142530Z").count() >= 2,
            "the media folder must be named in both halves:\n{msg}"
        );
        assert!(msg.contains("NOTHING WAS DELETED"), "{msg}");
        assert!(msg.contains("ΔΕΝ ΔΙΑΓΡΑΦΗΚΕ ΤΙΠΟΤΑ"), "in Greek too:\n{msg}");
        // WHERE it is, and WHICH file explains it — both by name, in both
        // halves. "Show him the file" is useless without the file's name.
        assert!(msg.matches(RECOVERY_NOTE).count() >= 4, "point at the file: {msg}");
        assert!(
            msg.matches(&*dirs.data.display().to_string()).count() >= 2,
            "the folder it is all in must be named in both halves:\n{msg}"
        );
        // The media line has to say, in the tutor's own terms, that this is
        // where his books are — "media_superseded_…" means nothing to him.
        assert!(msg.contains("ΤΑ ΑΡΧΕΙΑ ΣΑΣ"), "{msg}");
        assert!(msg.contains("YOUR FILES"), "{msg}");

        // Media alone — the crash window in `displace_media`, and the case
        // where the database was measured empty and dropped. The dialog must
        // still be true and still name what is on disk.
        let media_only = Displaced {
            databases: vec![],
            media: vec!["media_superseded_20260730T142530Z".to_string()],
            directories: vec![],
            files: vec![],
        };
        let msg = set_aside_notice(&dirs, &media_only);
        assert!(msg.contains("media_superseded_20260730T142530Z"), "{msg}");
        assert!(
            !msg.contains("guitar_superseded"),
            "it must not name a database that was never set aside:\n{msg}"
        );

        // And a boot that displaced more than one thing names ALL of them —
        // one dialog, not one per displacement and not one that stops at the
        // first. An initdb that fails on this machine's locale followed by a
        // fresh-database media displacement produces exactly this.
        let several = Displaced {
            databases: vec![],
            media: vec![
                "media_superseded_20260730T142530Z".to_string(),
                "media_superseded_20260730T150000Z".to_string(),
            ],
            directories: vec!["pgdata_superseded_20260730T142530Z".to_string()],
            files: vec![],
        };
        let msg = set_aside_notice(&dirs, &several);
        for name in [
            "media_superseded_20260730T142530Z",
            "media_superseded_20260730T150000Z",
            "pgdata_superseded_20260730T142530Z",
        ] {
            assert!(msg.contains(name), "{name} must be named:\n{msg}");
        }
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// `absorb` unions and de-duplicates. `main` folds THIS boot's receipt into
    /// what the marker says was never announced, and on the ordinary path those
    /// are the same entries seen twice — naming his media folder twice in one
    /// dialog reads as two folders and two problems.
    #[test]
    fn absorbing_the_same_displacement_twice_names_it_once() {
        let one = Displaced {
            databases: vec!["guitar_superseded_A".into()],
            media: vec!["media_superseded_A".into()],
            directories: vec![],
            files: vec![],
        };
        let mut acc = one.clone();
        acc.absorb(one);
        assert_eq!(acc.databases, vec!["guitar_superseded_A"]);
        assert_eq!(acc.media, vec!["media_superseded_A"]);

        acc.absorb(Displaced {
            databases: vec![],
            media: vec!["media_superseded_B".into()],
            directories: vec!["pgdata_superseded_B".into()],
            files: vec![],
        });
        assert_eq!(acc.media, vec!["media_superseded_A", "media_superseded_B"]);
        assert_eq!(acc.directories, vec!["pgdata_superseded_B"]);
        assert!(!Displaced::nothing().anything_set_aside());
    }

    // ---- durability --------------------------------------------------------

    /// The marker is the sole authority for the one path that moves the tutor's
    /// database, and `fs::write` gives it neither atomicity nor durability. The
    /// observable consequences, as far as a test can reach them: the replacement
    /// is complete or not at all, and no debris is left behind for the next
    /// reader to trip over.
    #[test]
    fn the_install_marker_is_replaced_atomically_and_leaves_no_debris() {
        let dirs = tmp_dirs("durable");
        let path = install_state_path(&dirs);

        mark_seeding(&dirs).expect("first write");
        assert_eq!(read_phase(&dirs).as_deref(), Some(PHASE_SEEDING));
        mark_complete(&dirs, "second write").expect("second write");
        assert_eq!(read_phase(&dirs).as_deref(), Some(PHASE_COMPLETE));

        // Every intermediate state a reader could see is a COMPLETE marker:
        // parse what is on disk, don't just look for a substring.
        let state: InstallState =
            serde_json::from_str(&fs::read_to_string(&path).expect("read")).expect("whole json");
        assert_eq!(state.phase, PHASE_COMPLETE);

        // No `.install-state.json.tmp*` left in the data folder. A temp file
        // that survives is a temp file that fills a disk.
        let debris: Vec<String> = fs::read_dir(&dirs.data)
            .expect("read data dir")
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.contains(".tmp"))
            .collect();
        assert!(debris.is_empty(), "temp files left behind: {debris:?}");

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&path).expect("stat").permissions().mode() & 0o777,
                0o600,
                "the mode is set on the temp file, before the rename"
            );
        }
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// A durable write that cannot land must leave the PREVIOUS file exactly as
    /// it was, and must not leave a temp file behind either. The old
    /// truncate-in-place `fs::write` could not promise the first half at all.
    #[test]
    fn a_failed_durable_write_leaves_the_previous_contents_intact() {
        let dirs = tmp_dirs("durablefail");
        let path = dirs.data.join("state.json");
        write_durably(&path, b"the good contents", None).expect("first write");

        // A directory where the rename's target must go: the temp file writes
        // fine, the rename cannot possibly succeed.
        let blocked = dirs.data.join("blocked.json");
        fs::create_dir_all(&blocked).expect("blocking directory");
        assert!(write_durably(&blocked, b"never lands", None).is_err());

        // A parent that does not exist: the temp write itself fails.
        assert!(write_durably(&dirs.data.join("gone/state.json"), b"x", None).is_err());

        assert_eq!(
            fs::read_to_string(&path).expect("read"),
            "the good contents",
            "an unrelated failed write must not touch it"
        );
        let debris: Vec<String> = fs::read_dir(&dirs.data)
            .expect("read data dir")
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.contains(".tmp"))
            .collect();
        assert!(debris.is_empty(), "a failed write left debris: {debris:?}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// secrets.json gets the same treatment, and for a sharper reason than the
    /// marker: `load_or_create_secrets` treats an existing-but-unparseable file
    /// as a HARD ERROR rather than regenerating it (regenerating
    /// ENCRYPTION_SECRET bricks the tutor's stored Anthropic key). A torn write
    /// therefore produced an install that refuses to boot for good.
    #[test]
    fn secrets_are_written_atomically_and_never_regenerated() {
        let dirs = tmp_dirs("secrets");
        fs::create_dir_all(&dirs.secrets).expect("secrets dir");
        let first = load_or_create_secrets(&dirs.secrets).expect("create");
        assert_eq!(first.app_secret.len(), 64);
        assert_ne!(first.app_secret, first.encryption_secret);

        // Read back, never rewritten.
        let second = load_or_create_secrets(&dirs.secrets).expect("load");
        assert_eq!(second.encryption_secret, first.encryption_secret);

        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(dirs.secrets.join("secrets.json"))
                .expect("stat")
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600, "never briefly world-readable");
        }
        // No debris in the secrets directory either.
        let debris: Vec<String> = fs::read_dir(&dirs.secrets)
            .expect("read")
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n != "secrets.json")
            .collect();
        assert!(debris.is_empty(), "{debris:?}");

        // A corrupt one is refused, LOUDLY, rather than replaced. Matched by
        // hand because `Secrets` deliberately has no `Debug` — an `expect_err`
        // here would be a derive that lets the encryption secret into a panic
        // message, and from there into a log.
        fs::write(dirs.secrets.join("secrets.json"), "{ truncated").expect("corrupt");
        match load_or_create_secrets(&dirs.secrets) {
            Ok(_) => panic!("a corrupt secrets.json must never be silently replaced"),
            // …and it is classified as an EXISTING-file problem, not a write
            // problem. `main` picks the dialog off the variant, and telling the
            // tutor to free up disk space here would send him to do something
            // that cannot possibly help.
            Err(e @ SecretsError::Existing(_)) => {
                assert!(e.detail().contains("Refusing to overwrite"), "{e}")
            }
            Err(e @ SecretsError::NotWritable(_)) => {
                panic!("a corrupt file is not a full disk: {e}")
            }
        }
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// `-X` is not a nicety. Without it psql sources `~/.psqlrc` first, and one
    /// `\timing` line in a developer's — or the tutor's — psqlrc prepends text
    /// to stdout: `greek_collation_ok` then reads something other than `t` and
    /// hands a perfectly good Mac a fatal dialog blaming its text collation,
    /// and `probe_database` reads "unexpected answer" and declines to act.
    #[test]
    fn every_psql_refuses_to_read_a_psqlrc() {
        let dirs = tmp_dirs("psqlargs");
        let cmd = psql_command(Path::new("/res"), &dirs, 5434, "guitar");
        let args: Vec<String> = cmd
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert!(args.contains(&"-X".to_string()), "psql must not read a psqlrc: {args:?}");
        // The rest of the contract the two callers depend on: unaligned,
        // tuples-only output over the unix socket, on OUR port and database.
        assert!(args.contains(&"-tA".to_string()), "{args:?}");
        assert!(args.contains(&"5434".to_string()), "{args:?}");
        assert!(args.contains(&"guitar".to_string()), "{args:?}");
        let _ = fs::remove_dir_all(&dirs.data);
    }

    /// meta.json is the whole memory: what `write_meta` puts down,
    /// `remembered_app_ports` must read back.
    #[test]
    fn meta_round_trips_the_port_pair() {
        let dirs = tmp_dirs("meta");
        let data = dirs.data.clone();
        let ports = AppPorts { web: 8796, api: 8802 };
        write_meta(&dirs, 5437, ports).expect("write meta");
        assert_eq!(remembered_app_ports(&dirs), Some(ports));

        // A meta.json from an older build has no ports at all: no memory, no
        // crash, just a scan.
        fs::write(
            data.join("meta.json"),
            r#"{"app_version":"0.1.0","pg_major":16,"pg_port":5434}"#,
        )
        .expect("write legacy meta");
        assert_eq!(remembered_app_ports(&dirs), None);

        fs::write(data.join("meta.json"), "{ not json").expect("write junk");
        assert_eq!(remembered_app_ports(&dirs), None);
        let _ = fs::remove_dir_all(&data);
    }
}
