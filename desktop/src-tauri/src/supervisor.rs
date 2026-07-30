//! Child-process supervision: postgres (via pg_ctl) → api (alembic, uvicorn) →
//! node (Next standalone), log piping with truncation, the readiness gate,
//! restart and ordered shutdown.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use crate::firstrun::{port_free, AppPorts, ChildPids, Secrets};
use crate::paths::{app_log, Dirs};

const LOG_TRUNCATE_BYTES: u64 = 10 * 1024 * 1024;

/// The bundled interpreter that runs alembic and uvicorn — i.e. `argv[0]` of
/// the api child, exactly as the kernel will report it.
///
/// A free function, and public, for one reason: `instance::reap_orphan_children`
/// has to recognise this process on the NEXT launch from its command line, and a
/// second spelling of this path would be a second thing to get wrong. If the
/// bundle layout ever moves, both the spawn and the recognition move with it.
pub fn api_binary(res: &Path) -> PathBuf {
    res.join("python/bin/python3.12")
}

/// The same, for the web child: the bundled node that runs the Next standalone
/// server.
pub fn web_binary(res: &Path) -> PathBuf {
    res.join("node/bin/node")
}

/// libpq reads all of these from the environment, and every one of them can
/// point a bundled binary somewhere we did not intend.
///
/// The tutor's machine is not a clean room: a developer helping him over the
/// phone, a Homebrew postgres, a `.zprofile` from an old job — any of them can
/// leave `PGHOST=db.internal` or `PGDATABASE=postgres` or a `PGSERVICE` entry
/// lying around, and libpq silently prefers the environment over the defaults.
/// Our own invocations always pass `-h`, `-p`, `-U`, `-d` and `-D` explicitly,
/// so there is nothing here we WANT and a great deal we must not inherit:
///
///   * `PGDATABASE`/`PGHOST`/`PGPORT` would aim the install probe at a
///     different server, whose answers we would then act on — `Empty` against
///     someone else's database is a `Reseed` decision about ours;
///   * `PGOPTIONS` can change `search_path` or set a statement timeout, so the
///     probe's table count and the collation guard stop meaning what they say;
///   * `PGDATA` is what `pg_ctl` falls back to, and a stray one aims stop/start
///     at another cluster entirely;
///   * `PGSERVICE`/`PGSERVICEFILE`/`PGSYSCONFDIR` do all of the above at once,
///     out of a file.
///
/// This is the same class of bug as the missing `-X` (see `psql_command`): the
/// tutor's ambient environment must not be able to change what our tooling
/// talks to or what it prints. Cleared on EVERY bundled-postgres invocation
/// rather than at the call sites, so a new one cannot forget.
const LIBPQ_ENV: &[&str] = &[
    "PGHOST",
    "PGHOSTADDR",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGPASSFILE",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGOPTIONS",
    "PGDATA",
    "PGCLIENTENCODING",
    "PGSSLMODE",
    "PGREQUIRESSL",
    "PGCONNECT_TIMEOUT",
    "PGAPPNAME",
    "PGTZ",
    // Not libpq: psql's own startup file. `-X` already refuses to read one,
    // and this makes that true even if a future call site forgets the flag.
    "PSQLRC",
];

/// A `Command` for a bundled postgres binary, with the bundled lib dir on the
/// loader path (the zonky tree carries its own libpq etc.) and the ambient
/// libpq environment stripped. Shared with `firstrun` so initdb/psql/createdb
/// all resolve libraries — and ignore the tutor's environment — the same way.
pub fn pg_command(res: &Path, dirs: &Dirs, bin: &str) -> Command {
    let mut cmd = Command::new(res.join("pg/bin").join(bin));
    let lib = res.join("pg/lib");
    #[cfg(target_os = "macos")]
    cmd.env("DYLD_LIBRARY_PATH", &lib);
    #[cfg(not(target_os = "macos"))]
    cmd.env("LD_LIBRARY_PATH", &lib);
    for var in LIBPQ_ENV {
        cmd.env_remove(var);
    }
    let _ = dirs; // dirs is part of the signature for future use (cwd, logs)
    cmd
}

/// Truncate a log that grew past 10MB, then open it for appending. Each child
/// gets stdout+stderr piped straight into its own file — no pump threads.
fn open_log(path: &Path) -> std::io::Result<fs::File> {
    if let Ok(meta) = fs::metadata(path) {
        if meta.len() > LOG_TRUNCATE_BYTES {
            let _ = fs::write(path, b"[log truncated at startup: >10MB]\n");
        }
    }
    fs::OpenOptions::new().create(true).append(true).open(path)
}

fn attach_log(cmd: &mut Command, path: &Path) -> Result<(), String> {
    let f = open_log(path).map_err(|e| format!("cannot open log {}: {e}", path.display()))?;
    let f2 = f.try_clone().map_err(|e| e.to_string())?;
    cmd.stdout(Stdio::from(f)).stderr(Stdio::from(f2));
    Ok(())
}

pub enum Ready {
    /// 200 with db && embed true — or the honest first-run 409
    /// `llm_not_configured` (API fully up; llm stays false until a key is pasted).
    Yes,
    No(String),
}

pub struct Supervisor {
    pub res: PathBuf,
    pub dirs: Dirs,
    pub secrets: Secrets,
    pub pg_port: u16,
    /// The app port pair — the ONE place it lives. Empty until
    /// `set_ports` is called, which `boot` does immediately before the first
    /// uvicorn/node spawn and never before: a port probed minutes ahead of the
    /// bind (first run does initdb, a 250MB `pg_restore`, a media copy and
    /// alembic in between) is a promise nothing can keep. Everything
    /// downstream — both spawns, both health polls, the restart re-probe and
    /// the main window's URL + injected API base — reads it from here.
    ports: OnceLock<AppPorts>,
    api_pid: Mutex<Option<i32>>,
    node_pid: Mutex<Option<i32>>,
    /// Set for the whole ordered shutdown — monitors stand down.
    shutting_down: AtomicBool,
    /// Set while Restart Backend replaces api+node on purpose. NEVER set
    /// directly: `FlagLatch` owns it, so it cannot be left true by an early
    /// return. See that type for why that matters.
    restarting: AtomicBool,
    /// True while a `watch_postgres` thread is alive. postgres is watched by
    /// POLLING (pg_ctl daemonizes it, so there is no child to wait on) and that
    /// watcher returns for good once it reports a death — which is why
    /// `restart_backend` has to re-arm it, and why arming has to be idempotent:
    /// two watchers would mean two "the backend stopped" dialogs for one crash.
    pg_watch: AtomicBool,
    /// One "backend died" dialog at a time.
    dialog_open: AtomicBool,
    /// Flipped once boot has the backend up and answering. Until then the boot
    /// thread OWNS the children — it is choosing ports, spawning uvicorn and
    /// node, waiting for readiness — and a Restart Backend running alongside it
    /// would be a second thread doing the same things to the same ports.
    booted: AtomicBool,
}

/// Holds a flag true for exactly as long as it is alive.
///
/// Used for two flags, both of which are dangerous when leaked:
///
///   * `restarting` tells the child monitors "this death was on purpose".
///     Leaking it true does not fail loudly — it silently disables crash
///     detection for the rest of the session, the worst shape a bug can have.
///   * `pg_watch` says a postgres watcher is alive. Leaking it true means the
///     watcher can never be re-armed, so the SECOND crash goes unnoticed.
///
/// `Drop` clears it on the happy path, on every `?`, on every early `return`
/// and on a panic, so no future edit can add a return path that forgets.
struct FlagLatch<'a>(&'a AtomicBool);

impl<'a> FlagLatch<'a> {
    fn hold(flag: &'a AtomicBool) -> Self {
        flag.store(true, Ordering::SeqCst);
        Self(flag)
    }
}

impl Drop for FlagLatch<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

impl Supervisor {
    pub fn new(res: PathBuf, dirs: Dirs, secrets: Secrets, pg_port: u16) -> Self {
        Self {
            res,
            dirs,
            secrets,
            pg_port,
            ports: OnceLock::new(),
            api_pid: Mutex::new(None),
            node_pid: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            restarting: AtomicBool::new(false),
            dialog_open: AtomicBool::new(false),
            booted: AtomicBool::new(false),
            pg_watch: AtomicBool::new(false),
        }
    }

    /// Publish the chosen pair. Called exactly once, from `boot`, in the same
    /// breath as the spawns.
    pub fn set_ports(&self, ports: AppPorts) {
        let _ = self.ports.set(ports);
    }

    /// The chosen pair, or `None` while the splash is still up and the scan has
    /// not run yet.
    ///
    /// This returns an `Option` rather than unwrapping because the supervisor is
    /// published to `SUPERVISOR` early in boot — long before the ports are
    /// picked — and the Backend ▸ Restart Backend menu item is live that whole
    /// time. An `expect` here turned one menu click during the splash into a
    /// panic on the boot's own thread.
    pub fn ports(&self) -> Option<AppPorts> {
        self.ports.get().copied()
    }

    /// Boot is finished: the backend is up, ready, and the main window is about
    /// to appear. Only after this may anything else replace the children.
    pub fn mark_booted(&self) {
        self.booted.store(true, Ordering::SeqCst);
    }

    pub fn is_booted(&self) -> bool {
        self.booted.load(Ordering::SeqCst)
    }

    pub fn is_shutting_down(&self) -> bool {
        self.shutting_down.load(Ordering::SeqCst)
    }
    fn expected_exit(&self) -> bool {
        self.is_shutting_down() || self.restarting.load(Ordering::SeqCst)
    }
    pub fn try_open_dialog(&self) -> bool {
        !self.dialog_open.swap(true, Ordering::SeqCst)
    }
    pub fn close_dialog(&self) {
        self.dialog_open.store(false, Ordering::SeqCst);
    }

    // ---- postgres ----------------------------------------------------------

    /// `pg_ctl -w start`, port passed with `-o` so it can change between runs
    /// without editing postgresql.conf.
    pub fn start_postgres(&self) -> Result<(), String> {
        let log = self.dirs.logs.join("postgres.log");
        let _ = open_log(&log); // apply the >10MB truncation before pg_ctl -l appends
        let out = pg_command(&self.res, &self.dirs, "pg_ctl")
            .arg("-w")
            .arg("-t")
            .arg("60")
            .arg("-D")
            .arg(&self.dirs.pgdata)
            .arg("-l")
            .arg(&log)
            .arg("-o")
            .arg(format!("-p {}", self.pg_port))
            .arg("start")
            .output()
            .map_err(|e| format!("pg_ctl start: {e}"))?;
        if out.status.success() {
            Ok(())
        } else {
            Err(format!(
                "pg_ctl start failed: {}",
                String::from_utf8_lossy(&out.stderr)
            ))
        }
    }

    pub fn stop_postgres(&self) {
        let _ = pg_command(&self.res, &self.dirs, "pg_ctl")
            .arg("-w")
            .arg("-t")
            .arg("20")
            .arg("-D")
            .arg(&self.dirs.pgdata)
            .arg("-m")
            .arg("fast")
            .arg("stop")
            .output();
    }

    /// Is the cluster answering? The SAME probe `watch_postgres` declares death
    /// on, so "the watcher says postgres died" and "restart says postgres is
    /// down" can never disagree about what they are measuring.
    fn postgres_alive(&self) -> bool {
        pg_command(&self.res, &self.dirs, "pg_isready")
            .arg("-h")
            .arg("127.0.0.1")
            .arg("-p")
            .arg(self.pg_port.to_string())
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }

    /// Take the postgres-watch slot. `true` means the caller now owns the watch
    /// and must release it when its thread ends; `false` means one is already
    /// running and this caller must do nothing.
    ///
    /// Split out from the thread so the arm/disarm rule can be tested without
    /// waiting on a five-second poll: exactly one watcher at a time, and
    /// re-armable once the previous one has returned.
    fn claim_pg_watch(&self) -> bool {
        !self.pg_watch.swap(true, Ordering::SeqCst)
    }

    /// pg_ctl daemonizes postgres, so it is not a waitable child — watch it
    /// with a pg_isready poll instead. `on_died` fires once on unexpected loss.
    ///
    /// Idempotent and RE-ARMABLE. Both matter: the watcher returns for good
    /// after reporting a death, so `restart_backend` calls this again to cover
    /// the next one, and calling it while a watcher is already alive (boot
    /// armed it; the restart was for a dead uvicorn, not a dead postgres) must
    /// not leave two threads polling the same cluster and raising two dialogs
    /// for one crash.
    pub fn watch_postgres<F: Fn() + Send + 'static>(self: &std::sync::Arc<Self>, on_died: F) {
        if !self.claim_pg_watch() {
            return; // a watcher is already on it
        }
        let sup = self.clone();
        std::thread::spawn(move || {
            // Declared AFTER `sup` so it is dropped BEFORE it: the flag is
            // cleared on every way out of this thread — the two returns below
            // and a panic — which is what makes the watch re-armable at all.
            let _armed = FlagLatch::hold(&sup.pg_watch);
            let mut misses = 0u32;
            loop {
                std::thread::sleep(Duration::from_secs(5));
                if sup.expected_exit() {
                    if sup.is_shutting_down() {
                        return;
                    }
                    continue; // restarting: postgres stays up, keep watching
                }
                // Two consecutive misses before declaring death: a single poll
                // can lose to a checkpoint or a briefly saturated CPU.
                misses = if sup.postgres_alive() { 0 } else { misses + 1 };
                if misses >= 2 {
                    on_died();
                    return;
                }
            }
        });
    }

    /// Make sure postgres is up before a restart tries to migrate against it.
    ///
    /// This is the half Restart Backend was missing. `restart_backend` replaces
    /// api+node only — so the crash that most often OPENS the dialog, postgres
    /// dying, was the one crash the single button on offer could not fix: the
    /// restart would run `alembic` against a cluster that is not there, fail,
    /// and report "Restart failed" to a tutor who had done nothing wrong.
    ///
    /// `pg_ctl stop` first, and unconditionally, because "not answering" covers
    /// more than "not there": a postmaster wedged past the point of accepting
    /// connections, or a `postmaster.pid` left by a crash, both make `start`
    /// refuse. `stop` is idempotent — on an already-dead cluster it reports
    /// "no server running" and we ignore it, which is exactly the shutdown path
    /// already relies on.
    fn ensure_postgres(&self) -> Result<(), String> {
        if self.postgres_alive() {
            return Ok(());
        }
        crate::paths::app_log(
            "restart: the database is not answering — stopping and starting it before the \
             migrations (a restart that skipped this could only ever fail)",
        );
        self.stop_postgres();
        self.start_postgres().map_err(|e| {
            format!(
                "Η βάση δεδομένων δεν ξαναξεκίνησε.\n\
                 The database did not start again.\n\n{e}"
            )
        })?;
        crate::paths::app_log("restart: the database is up again");
        Ok(())
    }

    // ---- api + node ---------------------------------------------------------

    fn python(&self) -> PathBuf {
        api_binary(&self.res)
    }

    /// Env for every python invocation — the exact names `app/config.py` reads.
    fn api_env(&self, cmd: &mut Command) {
        cmd.env(
            "DATABASE_URL",
            format!(
                "postgresql+psycopg://guitar:guitar@127.0.0.1:{}/guitar",
                self.pg_port
            ),
        )
        .env("MEDIA_DIR", &self.dirs.media)
        .env("EMBED_BACKEND", "local-e5")
        .env("EMBED_MODEL_DIR", self.res.join("models/e5-small"))
        .env("HF_HUB_OFFLINE", "1")
        // The backup endpoints shell out to pg_dump/pg_restore — point them
        // at the bundled Postgres tree (routers/backup.py reads PG_BIN_DIR).
        .env("PG_BIN_DIR", self.res.join("pg/bin"))
        .env("LLM_PROVIDER", "claude")
        .env("AUTH_ENABLED", "0")
        .env("APP_SECRET", &self.secrets.app_secret)
        .env("ENCRYPTION_SECRET", &self.secrets.encryption_secret)
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .current_dir(self.res.join("api"));

        // config.py's default hardcodes :8790; with the web port rolled the
        // browser would be rejected at preflight with an opaque
        // `TypeError: Failed to fetch`. Name the origin we actually serve.
        //
        // Conditional because `run_migrations` shares this env and runs BEFORE
        // the ports are picked — alembic has no HTTP surface, so leaving
        // CORS_ORIGINS at its default for that one invocation is correct. Every
        // uvicorn spawn happens after `set_ports` and always gets it.
        if let Some(ports) = self.ports.get() {
            cmd.env("CORS_ORIGINS", ports.cors_origins());
        }
    }

    /// `alembic upgrade head`, blocking. Idempotent — runs on every boot, same
    /// as the Docker image's CMD.
    pub fn run_migrations(&self) -> Result<(), String> {
        let mut cmd = Command::new(self.python());
        cmd.arg("-m").arg("alembic").arg("upgrade").arg("head");
        self.api_env(&mut cmd);
        attach_log(&mut cmd, &self.dirs.logs.join("api.log"))?;
        let status = cmd.status().map_err(|e| format!("alembic: {e}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("alembic upgrade head exited with {status}"))
        }
    }

    fn spawn_monitored<F: Fn() + Send + 'static>(
        &self,
        mut cmd: Command,
        pid_slot: &Mutex<Option<i32>>,
        log: &Path,
        on_died: F,
        sup: std::sync::Arc<Supervisor>,
    ) -> Result<(), String> {
        attach_log(&mut cmd, log)?;
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("failed to spawn {:?}: {e}", cmd.get_program()))?;
        let pid = child.id() as i32;
        *pid_slot.lock().unwrap() = Some(pid);
        // Before the monitor thread, and on every spawn including a Restart
        // Backend's: from the instant this process exists, a force quit can
        // orphan it, and the only thing that will ever find it again is the
        // number written here. See `record_children`.
        self.record_children();
        std::thread::spawn(move || {
            let _ = child.wait(); // reap, whatever the outcome
            if !sup.expected_exit() {
                on_died();
            }
        });
        Ok(())
    }

    /// Publish the current child PIDs to meta.json.
    ///
    /// This is the note the NEXT launch reads after a force quit — a SIGKILL of
    /// the shell (the beachball, then Cmd+Opt+Esc) runs no teardown at all, so
    /// uvicorn and node survive it, keep their ports, and push every subsequent
    /// launch further up the scan. Called on every spawn and once more at the
    /// end of the ordered shutdown, where it writes `None` and the note is
    /// retracted.
    ///
    /// Best effort by design, and logged rather than surfaced: the file is a
    /// convenience (see `firstrun::ChildPids`), and a boot must not fail because
    /// a disk was full while we were writing a hint.
    fn record_children(&self) {
        let pids = ChildPids {
            api: *self.api_pid.lock().unwrap(),
            web: *self.node_pid.lock().unwrap(),
        };
        if let Err(e) = crate::firstrun::record_child_pids(&self.dirs, pids) {
            app_log(&format!(
                "could not record the child PIDs in meta.json ({e}) — this launch works \
                 normally; only the cleanup after a force quit is affected, and the port \
                 scan already copes with that by rolling"
            ));
        }
    }

    /// The uvicorn invocation. Split from the spawn so a test can look at the
    /// program it names WITHOUT starting a python: `argv[0]` is the whole basis
    /// on which the next launch decides an orphan of this process is safe to
    /// kill, so "we spawn what the reaper looks for" has to be pinned by
    /// something other than two people spelling a path the same way.
    fn api_command(&self, ports: AppPorts) -> Command {
        let mut cmd = Command::new(self.python());
        cmd.arg("-m")
            .arg("uvicorn")
            .arg("app.main:app")
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(ports.api.to_string());
        self.api_env(&mut cmd);
        cmd
    }

    /// The node invocation, split out for the same reason.
    fn node_command(&self, ports: AppPorts) -> Command {
        let mut cmd = Command::new(web_binary(&self.res));
        cmd.arg("server.js")
            .current_dir(self.res.join("web"))
            .env("PORT", ports.web.to_string())
            .env("HOSTNAME", "127.0.0.1")
            .env("NODE_ENV", "production");
        cmd
    }

    pub fn start_api<F: Fn() + Send + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        let ports = self.ports().ok_or(NO_PORTS_YET)?;
        let cmd = self.api_command(ports);
        let log = self.dirs.logs.join("api.log");
        self.spawn_monitored(cmd, &self.api_pid, &log, on_died, self.clone())
    }

    pub fn start_node<F: Fn() + Send + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        let ports = self.ports().ok_or(NO_PORTS_YET)?;
        let cmd = self.node_command(ports);
        let log = self.dirs.logs.join("web.log");
        self.spawn_monitored(cmd, &self.node_pid, &log, on_died, self.clone())
    }

    // ---- readiness ----------------------------------------------------------

    /// Poll /health/ready until `db && embed` (llm is false until the tutor
    /// pastes a key — deliberately NOT gated on), then require the web port to
    /// answer. Both ports are the DERIVED ones (`self.ports`), not 8790/8791.
    /// The polls themselves go to 127.0.0.1: no cookie, no origin, no CORS —
    /// only the browser needs the literal `localhost` spelling.
    ///
    /// First-run nuance: with LLM_PROVIDER=claude and no key stored,
    /// `get_provider()` raises inside the endpoint and the API answers
    /// 409 {"detail":{"code":"llm_not_configured"}} INSTEAD of the JSON body.
    /// That 409 still proves uvicorn is up and serving requests (and alembic
    /// already proved the DB), so it passes the gate.
    pub fn await_ready(&self, timeout: Duration) -> Ready {
        let deadline = Instant::now() + timeout;
        let Some(ports) = self.ports() else {
            return Ready::No(NO_PORTS_YET.into());
        };
        let url = format!("http://127.0.0.1:{}/health/ready", ports.api);
        let mut last = String::from("no response yet");
        let mut api_ok = false;
        while Instant::now() < deadline {
            if self.is_shutting_down() {
                return Ready::No("shutdown during boot".into());
            }
            match ureq::get(&url).timeout(Duration::from_secs(5)).call() {
                Ok(resp) => match resp.into_json::<serde_json::Value>() {
                    Ok(v) => {
                        let db = v["db"].as_bool().unwrap_or(false);
                        let embed = v["embed"].as_bool().unwrap_or(false);
                        if db && embed {
                            api_ok = true;
                        } else {
                            last = format!("api answers but db={db} embed={embed}");
                        }
                    }
                    Err(e) => last = format!("unparseable /health/ready body: {e}"),
                },
                Err(ureq::Error::Status(409, resp)) => {
                    let body = resp.into_json::<serde_json::Value>().unwrap_or_default();
                    if body["detail"]["code"] == "llm_not_configured" {
                        api_ok = true; // fresh install, no key pasted yet
                    } else {
                        last = format!("409 from /health/ready: {body}");
                    }
                }
                Err(e) => last = format!("api not answering yet: {e}"),
            }
            if api_ok {
                match ureq::get(&format!("http://127.0.0.1:{}/", ports.web))
                    .timeout(Duration::from_secs(5))
                    .call()
                {
                    Ok(_) => return Ready::Yes,
                    Err(e) => last = format!("api ready, web not yet: {e}"),
                }
            }
            std::thread::sleep(Duration::from_millis(700));
        }
        Ready::No(last)
    }

    // ---- teardown / restart -------------------------------------------------

    /// SIGTERM, wait for the process to vanish, SIGKILL after `grace`.
    fn term_and_reap(pid: i32, grace: Duration) {
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            // Signal 0 = existence probe. ESRCH after the monitor thread reaps.
            if unsafe { libc::kill(pid, 0) } != 0 {
                return;
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        unsafe {
            libc::kill(pid, libc::SIGKILL);
        }
    }

    fn stop_api_and_node(&self) {
        if let Some(pid) = self.node_pid.lock().unwrap().take() {
            Self::term_and_reap(pid, Duration::from_secs(10));
        }
        if let Some(pid) = self.api_pid.lock().unwrap().take() {
            Self::term_and_reap(pid, Duration::from_secs(10));
        }
        // Both slots are empty now, so this RETRACTS the note the next launch
        // would act on. Here rather than only in `shutdown` because Restart
        // Backend comes through this too: between the kill and the respawn the
        // recorded PIDs name processes that no longer exist, and a number that
        // names nothing is a number the operating system may hand to somebody
        // else. (Even then nothing could go wrong — the next launch proves
        // ownership from the command line before it signals — but the shortest
        // honest record is the one to keep.)
        self.record_children();
    }

    /// Ordered full shutdown: node → uvicorn → `pg_ctl stop -m fast`.
    /// Idempotent — RunEvent::ExitRequested can fire more than once.
    pub fn shutdown(&self) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        // `stop_api_and_node` clears the recorded PIDs as part of stopping them,
        // so an ordered exit leaves nothing behind for the next launch to reap —
        // which is the whole difference between this path and a force quit.
        self.stop_api_and_node();
        self.stop_postgres();
    }

    /// Backend menu / crash dialog: replace api+node, make sure postgres is
    /// there to replace them against, re-run migrations, wait for readiness
    /// again.
    ///
    /// postgres is not one of the children this replaces — it is left alone
    /// when it is healthy, because dropping every connection and replaying WAL
    /// is not something to do to a working cluster. But it IS probed, because
    /// the commonest reason this dialog is on screen at all is that postgres
    /// died: `watch_postgres` reports that and returns, `backend_died` offers
    /// exactly one button, and until round 4 that button was guaranteed to
    /// fail. See `ensure_postgres`.
    ///
    /// Deliberately reuses the SAME ports, never a fresh scan: the live main
    /// window was told the API base at creation and cannot be told a different
    /// one, so silently re-picking behind its back would leave the page talking
    /// to a port with nothing on it. But reusing them BLINDLY is how this turns
    /// into a dead end — if something took the port while the backend was down,
    /// uvicorn fails to bind, and the tutor waits out a two-minute readiness
    /// timeout to be shown a raw `ureq` error. So: re-probe first, and if the
    /// port is gone say the one true thing, which is that only a relaunch can
    /// pick new ports.
    ///
    /// "Not booted yet" is an ORDINARY answer here, not an error to be reported
    /// as a failure: the menu item is live from the moment the splash appears,
    /// and there is nothing to restart until boot has finished starting it.
    pub fn restart_backend<F: Fn() + Send + Clone + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        // Everything below stops children, re-probes ports and respawns — all
        // of which the boot thread is still doing for the first time. Two
        // threads doing it at once is two uvicorns fighting over one port.
        // `ports()` being empty is the earliest, sharpest form of this.
        if !self.is_booted() || self.ports().is_none() {
            return Err(still_starting_message());
        }
        // From here the latch is held by a value, not by a pair of stores. Every
        // way out of this function — `?`, the early `return` below, a panic —
        // clears it, because clearing it is `Drop`.
        let latch = FlagLatch::hold(&self.restarting);
        // Our own children hold these ports; they must go first or the probe
        // below would only ever find them busy.
        self.stop_api_and_node();
        let ports = self.ports().ok_or_else(still_starting_message)?;
        let stolen = [ports.api, ports.web]
            .into_iter()
            .find(|p| !wait_port_free(*p, Duration::from_secs(3)));
        if let Some(port) = stolen {
            return Err(port_taken_message(port));
        }
        let rearm = on_died.clone();
        let result: Result<(), String> = (|| {
            // BEFORE the migrations: alembic against a cluster that is not
            // there is the failure this whole probe exists to prevent.
            self.ensure_postgres()?;
            self.run_migrations()?;
            self.start_api(on_died.clone())?;
            self.start_node(on_died)?;
            Ok(())
        })();
        // Released HERE on purpose, before the readiness wait: from this point a
        // child dying is a real crash again and must reach the dialog.
        drop(latch);
        result?;
        match self.await_ready(Duration::from_secs(120)) {
            Ready::Yes => {
                // Re-arm. If postgres is what died, its watcher reported that
                // and returned; without this the cluster would go unwatched for
                // the rest of the session and a SECOND crash would show the
                // tutor nothing at all — just a page that stops working. A
                // no-op when the existing watcher is still alive.
                self.watch_postgres(rearm);
                Ok(())
            }
            Ready::No(why) => Err(why),
        }
    }
}

/// The internal reason a spawn cannot proceed. Never shown to the tutor — the
/// paths a human can reach are gated by `is_booted` and answer with
/// `still_starting_message` instead.
const NO_PORTS_YET: &str = "internal: the app ports have not been chosen yet";

/// Poll until `port` binds or `grace` runs out.
///
/// Not just `port_free`: `term_and_reap` returns the instant a SIGKILLed child
/// stops existing, and the kernel can still be tearing its listener down. A
/// single probe there would report a port we just released as stolen.
fn wait_port_free(port: u16, grace: Duration) -> bool {
    let deadline = Instant::now() + grace;
    loop {
        if port_free(port) {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
}

/// Greek first, then English. Restart Backend was asked for while the app was
/// still starting — the splash is up, the backend it would replace does not
/// exist yet. Nothing is wrong, so this says nothing is wrong: wait.
pub fn still_starting_message() -> String {
    "Το GuitarTutor ξεκινάει ακόμη.\n\
     Περιμένετε να ανοίξει το κύριο παράθυρο και δοκιμάστε ξανά — δεν υπάρχει \
     ακόμη backend για επανεκκίνηση.\n\n\
     GuitarTutor is still starting.\n\
     Wait for the main window to open and try again — there is no backend to \
     restart yet."
        .to_string()
}

/// Greek first, then English. The only action that can help is a relaunch —
/// the port scan runs at startup and nowhere else — so that is what it says,
/// in place of the raw bind/`ureq` error this replaces.
fn port_taken_message(port: u16) -> String {
    format!(
        "Η θύρα {port}, που χρησιμοποιεί το GuitarTutor, δεν είναι πλέον διαθέσιμη \
         — την πήρε άλλο πρόγραμμα.\n\
         Κλείστε εντελώς το GuitarTutor και ανοίξτε το ξανά· μόνο κατά την \
         εκκίνηση μπορεί να επιλέξει νέες θύρες.\n\n\
         Port {port}, which GuitarTutor is using, is no longer available — \
         another program took it.\n\
         Quit GuitarTutor completely and open it again; it can only pick new \
         ports while starting up."
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener};
    use std::sync::Arc;

    /// A supervisor with nothing started: `new` only stores plain data, so this
    /// is safe to build in a unit test. Its ports are deliberately UNSET — the
    /// state the whole splash lasts.
    fn unbooted() -> Arc<Supervisor> {
        let root = std::env::temp_dir().join(format!("gt-sup-{}", std::process::id()));
        Arc::new(Supervisor::new(
            root.join("res"),
            Dirs {
                pgdata: root.join("pgdata"),
                media: root.join("media"),
                secrets: root.join("secrets"),
                logs: root.join("logs"),
                data: root,
            },
            Secrets {
                app_secret: "a".into(),
                encryption_secret: "b".into(),
            },
            5434,
        ))
    }

    /// THE regression. `SUPERVISOR` is published while the splash is still up,
    /// and Backend ▸ Restart Backend is clickable that whole time; `ports()`
    /// used to `expect` and took the click as a panic. It must now be an
    /// ordinary, bilingual "wait a moment" — and it must NOT touch anything.
    #[test]
    fn restart_during_boot_is_a_polite_refusal_not_a_panic() {
        let sup = unbooted();
        assert!(sup.ports().is_none(), "ports are unset while the splash is up");
        let err = sup.restart_backend(|| {}).expect_err("must refuse");
        assert!(err.contains("still starting"), "{err}");
        let greek = err
            .find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c))
            .expect("Greek half");
        assert!(greek < err.find("still starting").expect("English half"));
    }

    /// The second half of the same bug: `restarting` was latched true BEFORE the
    /// early return, so a refused restart left crash detection dead for the rest
    /// of the session. Refusing must leave the supervisor exactly as it was.
    #[test]
    fn a_refused_restart_leaves_no_latch_behind() {
        let sup = unbooted();
        assert!(sup.restart_backend(|| {}).is_err());
        assert!(
            !sup.expected_exit(),
            "a refused restart must not leave `restarting` set — crash detection \
             depends on it being false"
        );

        // …and the same once the ports exist but boot has not finished, which is
        // the window where the boot thread is mid-spawn.
        sup.set_ports(AppPorts { web: 8790, api: 8791 });
        assert!(sup.restart_backend(|| {}).is_err());
        assert!(!sup.expected_exit());
        assert!(!sup.is_booted());
    }

    /// The latch is a value now, so the compiler enforces what a pair of manual
    /// stores could not: it clears on the happy path, on an early return, and on
    /// a panic unwinding through it.
    #[test]
    fn the_restart_latch_cannot_leak() {
        let flag = AtomicBool::new(false);
        {
            let _held = FlagLatch::hold(&flag);
            assert!(flag.load(Ordering::SeqCst), "held while alive");
        }
        assert!(!flag.load(Ordering::SeqCst), "cleared by scope exit");

        // An early return out of the middle of a function.
        fn bail(flag: &AtomicBool) -> Result<(), ()> {
            let _held = FlagLatch::hold(flag);
            Err(())
        }
        assert!(bail(&flag).is_err());
        assert!(!flag.load(Ordering::SeqCst), "cleared by an early return");

        // A panic inside the guarded section — the case a manual
        // `store(false)` at the end of the function could never cover.
        let result = std::panic::catch_unwind(|| {
            let _held = FlagLatch::hold(&flag);
            assert!(flag.load(Ordering::SeqCst), "held before the panic");
            panic!("boom");
        });
        assert!(result.is_err(), "the panic must have happened");
        assert!(!flag.load(Ordering::SeqCst), "cleared while unwinding");
    }

    /// The stolen-port message must name the port, say what happened, and give
    /// the one instruction that works — in Greek before English.
    #[test]
    fn stolen_port_message_is_bilingual_greek_first() {
        let msg = port_taken_message(8791);
        let greek = msg
            .find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c))
            .expect("Greek half");
        let english = msg.find("no longer available").expect("English half");
        assert!(greek < english, "Greek must come first:\n{msg}");
        assert!(msg.matches("8791").count() >= 2, "both halves name the port");
        assert!(msg.contains("open it again"));
    }

    /// The tutor's shell environment must not be able to point our bundled
    /// postgres tooling at somebody else's server, or reconfigure the session
    /// the install probe runs in. `env_remove` shows up in `get_envs` as a
    /// `None` value, which is exactly what is asserted here.
    #[test]
    fn the_ambient_libpq_environment_is_stripped() {
        let sup = unbooted();
        let cmd = pg_command(&sup.res, &sup.dirs, "psql");
        let envs: Vec<(String, Option<String>)> = cmd
            .get_envs()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.map(|v| v.to_string_lossy().into_owned()),
                )
            })
            .collect();
        for var in LIBPQ_ENV {
            let entry = envs
                .iter()
                .find(|(k, _)| k == var)
                .unwrap_or_else(|| panic!("{var} is not cleared for bundled postgres binaries"));
            assert_eq!(entry.1, None, "{var} must be REMOVED, not set");
        }
        // The one thing that must still be set: the bundled loader path, or
        // none of these binaries find their own libpq.
        let loader = if cfg!(target_os = "macos") {
            "DYLD_LIBRARY_PATH"
        } else {
            "LD_LIBRARY_PATH"
        };
        assert!(
            envs.iter().any(|(k, v)| k == loader && v.is_some()),
            "the bundled lib dir must still be on the loader path"
        );
        // The names that actually redirect a connection, spelled out so a
        // future trim of the list has to argue with this test.
        for critical in ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE", "PGOPTIONS"] {
            assert!(LIBPQ_ENV.contains(&critical), "{critical} must stay on the list");
        }
    }

    /// Exactly one postgres watcher at a time, and re-armable once it has gone.
    ///
    /// Both halves are load-bearing. `watch_postgres` returns for good after it
    /// reports a death, so `restart_backend` re-arms it — without which the
    /// cluster is unwatched for the rest of the session and a second crash
    /// shows the tutor nothing. And re-arming while a watcher is still alive
    /// (the restart was for a dead uvicorn, not a dead postgres) must NOT leave
    /// two threads polling, or one crash raises two dialogs.
    #[test]
    fn the_postgres_watch_is_single_and_re_armable() {
        let sup = unbooted();
        assert!(sup.claim_pg_watch(), "a fresh supervisor is unwatched");
        assert!(!sup.claim_pg_watch(), "a second watcher must not start");
        assert!(!sup.claim_pg_watch(), "…however many times it is asked");

        // What the watcher thread's `FlagLatch` does when it returns.
        sup.pg_watch.store(false, Ordering::SeqCst);
        assert!(sup.claim_pg_watch(), "must be re-armable after the watcher returns");
    }

    /// What we SPAWN has to be what the next launch LOOKS FOR.
    ///
    /// `instance::reap_orphan_children` proves an orphan is ours by matching
    /// `argv[0]` — the program named here — against `api_binary`/`web_binary`.
    /// If a spawn ever names the interpreter by a different route (a `python3`
    /// symlink, a versionless path, a different bundle layout) the match stops
    /// being exact, and the reaper silently stops reaping: no error, no log, just
    /// two ports quietly consumed by every force quit again. The two spellings
    /// must be one spelling, and this is the test that says so.
    #[test]
    fn the_children_we_spawn_are_the_ones_the_reaper_recognises() {
        let sup = unbooted();
        let ports = AppPorts { web: 8790, api: 8791 };
        assert_eq!(
            sup.api_command(ports).get_program(),
            api_binary(&sup.res).as_os_str(),
            "the api child's argv[0] must be exactly what the reaper matches"
        );
        assert_eq!(
            sup.node_command(ports).get_program(),
            web_binary(&sup.res).as_os_str(),
            "…and the web child's"
        );
        // Absolute, and inside this install's resource root — the reaper refuses
        // to act on a relative path, because prefix-matching one against a
        // command line proves nothing.
        for bin in [api_binary(&sup.res), web_binary(&sup.res)] {
            assert!(bin.is_absolute(), "{}", bin.display());
            assert!(bin.starts_with(&sup.res), "{}", bin.display());
        }
        // The two children are never the same binary; a PID recorded as the api
        // can therefore never be reaped by matching the node's path.
        assert_ne!(api_binary(&sup.res), web_binary(&sup.res));
    }

    /// A port we are holding never comes free inside the grace window, and the
    /// wait must actually return false rather than spin forever.
    #[test]
    fn wait_port_free_gives_up_on_a_held_port() {
        let held = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("ephemeral port");
        let port = held.local_addr().expect("addr").port();
        let started = Instant::now();
        assert!(!wait_port_free(port, Duration::from_millis(400)));
        assert!(started.elapsed() >= Duration::from_millis(400));
        assert!(started.elapsed() < Duration::from_secs(5), "bounded wait");
    }
}
