//! First-run provisioning: secrets, port choice, initdb (with the Greek
//! collation guard), database creation and optional seed restore.

use std::fs;
use std::io::Write;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};

use crate::paths::Dirs;
use crate::supervisor::pg_command;

/// The web app derives its API base from a literal `localhost` origin and the
/// session cookie is host-only — these two ports are FROZEN.
pub const WEB_PORT: u16 = 8790;
pub const API_PORT: u16 = 8791;

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
}

fn random_hex64() -> String {
    let mut buf = [0u8; 32];
    getrandom::getrandom(&mut buf).expect("OS RNG unavailable");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// Create `secrets/secrets.json` (chmod 600) ONLY if absent. NEVER regenerate:
/// losing ENCRYPTION_SECRET bricks the tutor's stored Anthropic key, so an
/// unreadable existing file is a hard error, not a rewrite.
pub fn load_or_create_secrets(secrets_dir: &Path) -> Result<Secrets, String> {
    let path = secrets_dir.join("secrets.json");
    if path.exists() {
        let raw = fs::read_to_string(&path)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        return serde_json::from_str(&raw).map_err(|e| {
            format!(
                "{} exists but cannot be parsed ({e}). Refusing to overwrite it — \
                 it protects the stored Anthropic key. Fix or restore the file.",
                path.display()
            )
        });
    }
    let secrets = Secrets {
        app_secret: random_hex64(),
        encryption_secret: random_hex64(),
    };
    let body = serde_json::to_string_pretty(&secrets).expect("serialize secrets");
    fs::write(&path, body).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("cannot chmod {}: {e}", path.display()))?;
    }
    Ok(secrets)
}

fn port_free(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// The two frozen app ports must be free — there is no fallback by design.
/// Returns the first taken port on failure.
pub fn preflight_frozen_ports() -> Result<(), u16> {
    for p in [WEB_PORT, API_PORT] {
        if !port_free(p) {
            return Err(p);
        }
    }
    Ok(())
}

/// Postgres port: prefer 5434, scan up to 5444. The chosen port is passed to
/// `pg_ctl -o "-p …"` at every start, so it may differ between runs.
pub fn pick_pg_port() -> Option<u16> {
    (5434..=5444).find(|p| port_free(*p))
}

pub fn cluster_exists(dirs: &Dirs) -> bool {
    dirs.pgdata.join("PG_VERSION").exists()
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
pub fn init_cluster(res: &Path, dirs: &Dirs, port: u16, icu: bool) -> Result<(), String> {
    if icu {
        initdb_once(res, dirs, &["--locale-provider=icu", "--icu-locale=el", "--locale=C.UTF-8"])?;
    } else if let Err(first) = initdb_once(res, dirs, &["--locale=en_US.UTF-8"]) {
        if cfg!(target_os = "macos") {
            return Err(first);
        }
        // Linux without en_US.UTF-8 generated: C.UTF-8 always exists.
        wipe_pgdata(dirs)?;
        initdb_once(res, dirs, &["--locale=C.UTF-8"])
            .map_err(|second| format!("initdb failed twice: {first} / then {second}"))?;
    }
    append_conf_overrides(dirs, port)
}

/// Does the bundled initdb understand ICU? (zonky builds vary by platform.)
pub fn initdb_supports_icu(res: &Path, dirs: &Dirs) -> bool {
    pg_command(res, dirs, "initdb")
        .arg("--help")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).contains("--icu-locale"))
        .unwrap_or(false)
}

pub fn wipe_pgdata(dirs: &Dirs) -> Result<(), String> {
    fs::remove_dir_all(&dirs.pgdata).map_err(|e| e.to_string())?;
    fs::create_dir_all(&dirs.pgdata).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&dirs.pgdata, fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

fn psql_scalar(res: &Path, dirs: &Dirs, port: u16, db: &str, sql: &str) -> Result<String, String> {
    let out = pg_command(res, dirs, "psql")
        .arg("-U")
        .arg("guitar")
        .arg("-h")
        .arg(&dirs.pgdata) // unix socket — auth-local=trust, no password needed
        .arg("-p")
        .arg(port.to_string())
        .arg("-d")
        .arg(db)
        .arg("-tA")
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

/// The app's Greek search is non-negotiable: `lower('ΚΙΘΑΡΑ')` must fold to
/// 'κιθαρα' in this cluster's collation. Run right after first initdb+start.
pub fn greek_collation_ok(res: &Path, dirs: &Dirs, port: u16) -> bool {
    matches!(
        psql_scalar(res, dirs, port, "postgres", "SELECT lower('ΚΙΘΑΡΑ') = 'κιθαρα'").as_deref(),
        Ok("t")
    )
}

/// `createdb guitar`, then restore the bundled seed if the build carries one.
/// Only ever called on a FRESH cluster.
pub fn create_and_seed_db(res: &Path, dirs: &Dirs, port: u16) -> Result<bool, String> {
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
        return Ok(false);
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
        copy_tree(&media_src, &dirs.media)?;
    }
    Ok(true)
}

fn copy_tree(src: &Path, dst: &Path) -> Result<(), String> {
    fs::create_dir_all(dst).map_err(|e| e.to_string())?;
    for entry in fs::read_dir(src).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let to: PathBuf = dst.join(entry.file_name());
        let ty = entry.file_type().map_err(|e| e.to_string())?;
        if ty.is_dir() {
            copy_tree(&entry.path(), &to)?;
        } else if ty.is_file() {
            fs::copy(entry.path(), &to).map_err(|e| {
                format!("copy {} -> {}: {e}", entry.path().display(), to.display())
            })?;
        }
    }
    Ok(())
}

pub fn write_meta(dirs: &Dirs, port: u16) -> Result<(), String> {
    let meta = Meta {
        app_version: env!("CARGO_PKG_VERSION").to_string(),
        pg_major: 16,
        pg_port: port,
    };
    fs::write(
        dirs.data.join("meta.json"),
        serde_json::to_string_pretty(&meta).expect("serialize meta"),
    )
    .map_err(|e| e.to_string())
}
