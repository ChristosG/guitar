//! Child-process supervision: postgres (via pg_ctl) → api (alembic, uvicorn) →
//! node (Next standalone), log piping with truncation, the readiness gate,
//! restart and ordered shutdown.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::firstrun::{Secrets, API_PORT, WEB_PORT};
use crate::paths::Dirs;

const LOG_TRUNCATE_BYTES: u64 = 10 * 1024 * 1024;

/// A `Command` for a bundled postgres binary, with the bundled lib dir on the
/// loader path (the zonky tree carries its own libpq etc.). Shared with
/// `firstrun` so initdb/psql/createdb all resolve libraries the same way.
pub fn pg_command(res: &Path, dirs: &Dirs, bin: &str) -> Command {
    let mut cmd = Command::new(res.join("pg/bin").join(bin));
    let lib = res.join("pg/lib");
    #[cfg(target_os = "macos")]
    cmd.env("DYLD_LIBRARY_PATH", &lib);
    #[cfg(not(target_os = "macos"))]
    cmd.env("LD_LIBRARY_PATH", &lib);
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
    api_pid: Mutex<Option<i32>>,
    node_pid: Mutex<Option<i32>>,
    /// Set for the whole ordered shutdown — monitors stand down.
    shutting_down: AtomicBool,
    /// Set while Restart Backend replaces api+node on purpose.
    restarting: AtomicBool,
    /// One "backend died" dialog at a time.
    dialog_open: AtomicBool,
}

impl Supervisor {
    pub fn new(res: PathBuf, dirs: Dirs, secrets: Secrets, pg_port: u16) -> Self {
        Self {
            res,
            dirs,
            secrets,
            pg_port,
            api_pid: Mutex::new(None),
            node_pid: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            restarting: AtomicBool::new(false),
            dialog_open: AtomicBool::new(false),
        }
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

    /// pg_ctl daemonizes postgres, so it is not a waitable child — watch it
    /// with a pg_isready poll instead. `on_died` fires once on unexpected loss.
    pub fn watch_postgres<F: Fn() + Send + 'static>(self: &std::sync::Arc<Self>, on_died: F) {
        let sup = self.clone();
        std::thread::spawn(move || {
            let mut misses = 0u32;
            loop {
                std::thread::sleep(Duration::from_secs(5));
                if sup.expected_exit() {
                    if sup.is_shutting_down() {
                        return;
                    }
                    continue; // restarting: postgres stays up, keep watching
                }
                let up = pg_command(&sup.res, &sup.dirs, "pg_isready")
                    .arg("-h")
                    .arg("127.0.0.1")
                    .arg("-p")
                    .arg(sup.pg_port.to_string())
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false);
                // Two consecutive misses before declaring death: a single poll
                // can lose to a checkpoint or a briefly saturated CPU.
                misses = if up { 0 } else { misses + 1 };
                if misses >= 2 {
                    on_died();
                    return;
                }
            }
        });
    }

    // ---- api + node ---------------------------------------------------------

    fn python(&self) -> PathBuf {
        self.res.join("python/bin/python3.12")
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
        .env("LLM_PROVIDER", "claude")
        .env("AUTH_ENABLED", "0")
        .env("APP_SECRET", &self.secrets.app_secret)
        .env("ENCRYPTION_SECRET", &self.secrets.encryption_secret)
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .current_dir(self.res.join("api"));
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
        std::thread::spawn(move || {
            let _ = child.wait(); // reap, whatever the outcome
            if !sup.expected_exit() {
                on_died();
            }
        });
        Ok(())
    }

    pub fn start_api<F: Fn() + Send + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        let mut cmd = Command::new(self.python());
        cmd.arg("-m")
            .arg("uvicorn")
            .arg("app.main:app")
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(API_PORT.to_string());
        self.api_env(&mut cmd);
        let log = self.dirs.logs.join("api.log");
        self.spawn_monitored(cmd, &self.api_pid, &log, on_died, self.clone())
    }

    pub fn start_node<F: Fn() + Send + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        let mut cmd = Command::new(self.res.join("node/bin/node"));
        cmd.arg("server.js")
            .current_dir(self.res.join("web"))
            .env("PORT", WEB_PORT.to_string())
            .env("HOSTNAME", "127.0.0.1")
            .env("NODE_ENV", "production");
        let log = self.dirs.logs.join("web.log");
        self.spawn_monitored(cmd, &self.node_pid, &log, on_died, self.clone())
    }

    // ---- readiness ----------------------------------------------------------

    /// Poll /health/ready until `db && embed` (llm is false until the tutor
    /// pastes a key — deliberately NOT gated on), then require :8790 to answer.
    ///
    /// First-run nuance: with LLM_PROVIDER=claude and no key stored,
    /// `get_provider()` raises inside the endpoint and the API answers
    /// 409 {"detail":{"code":"llm_not_configured"}} INSTEAD of the JSON body.
    /// That 409 still proves uvicorn is up and serving requests (and alembic
    /// already proved the DB), so it passes the gate.
    pub fn await_ready(&self, timeout: Duration) -> Ready {
        let deadline = Instant::now() + timeout;
        let url = format!("http://127.0.0.1:{API_PORT}/health/ready");
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
                match ureq::get(&format!("http://127.0.0.1:{WEB_PORT}/"))
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
    }

    /// Ordered full shutdown: node → uvicorn → `pg_ctl stop -m fast`.
    /// Idempotent — RunEvent::ExitRequested can fire more than once.
    pub fn shutdown(&self) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        self.stop_api_and_node();
        self.stop_postgres();
    }

    /// Backend menu / crash dialog: replace api+node (postgres stays up),
    /// re-run migrations, wait for readiness again.
    pub fn restart_backend<F: Fn() + Send + Clone + 'static>(
        self: &std::sync::Arc<Self>,
        on_died: F,
    ) -> Result<(), String> {
        self.restarting.store(true, Ordering::SeqCst);
        self.stop_api_and_node();
        let result: Result<(), String> = (|| {
            self.run_migrations()?;
            self.start_api(on_died.clone())?;
            self.start_node(on_died)?;
            Ok(())
        })();
        self.restarting.store(false, Ordering::SeqCst);
        result?;
        match self.await_ready(Duration::from_secs(120)) {
            Ready::Yes => Ok(()),
            Ready::No(why) => Err(why),
        }
    }
}
