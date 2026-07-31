//! One-copy-at-a-time guard, and the app's own leftovers. Runs immediately
//! after `paths::ensure_dirs()` — before secrets, before any port scan, before
//! anything touches the cluster.
//!
//! Two DIFFERENT failures used to be caught, by accident, by the frozen-port
//! preflight this branch replaced with a rolling scan:
//!
//! 1. A SECOND copy of Angel OS launched while the first one is running.
//!    `tauri_plugin_single_instance` is not a guarantee: on Linux it wants a
//!    session DBus and fails OPEN when there is none (containers, ssh, some
//!    login setups), and it is a courtesy focus-the-window feature, not a
//!    mutual exclusion primitive.
//!
//! 2. The app's OWN orphans. A force quit — the beachball, then Cmd+Opt+Esc —
//!    SIGKILLs the shell without running the ordered teardown, so ALL THREE of
//!    its children keep running: postgres, uvicorn and node. Under the old
//!    frozen ports the next launch bounced off the busy port with a clear
//!    message. With rolling ports it rolls PAST its own leftovers and then dies
//!    inside pg_ctl, showing the tutor a raw English lock-file dump.
//!
//!    The postgres half of that was fixed first, and it left the other two
//!    behind: an orphaned uvicorn and node are harmless in themselves (their
//!    database has been stopped) and die at logout, but they keep holding their
//!    TCP ports, so every force quit permanently consumes two more candidates
//!    out of a window of about twenty. Ten force quits without a reboot and the
//!    app reaches "no free application port" — precisely the failure this whole
//!    module exists to prevent. So they are reaped here too, by the same rules,
//!    which are worth stating because a wrong kill is unforgivable in a way a
//!    squatted port is not:
//!
//!      * a PID is only a place to LOOK. The operating system reuses PIDs, so
//!        the number a dead launch wrote down may name anything by now; before
//!        we signal, the process's own command line must name the interpreter
//!        inside THIS install (`<res>/python/bin/python3.12`,
//!        `<res>/node/bin/node`) as its `argv[0]`. Anything else — a foreign
//!        process, a system python, an empty command line, no such process — is
//!        left strictly alone, and there is no second, weaker test it can fall
//!        back to;
//!      * and the same flock gate as the cluster: no lock, no signal.
//!
//! These are handled in COMPLETELY different ways, and the difference is the
//! point of this module:
//!
//! * (1) is somebody else's process. We cannot touch it, so we refuse to start
//!   and say so — an flock on `<data>/instance.lock`, which cannot go stale
//!   because the kernel drops it when the holder dies, SIGKILL included.
//!
//! * (2) is OUR process. Once the flock proves no other Angel OS shell is
//!   alive, a postmaster running in OUR data directory belongs to nobody but a
//!   dead copy of this app — so we STOP IT and carry on booting. Sending a
//!   guitar teacher to Activity Monitor to hunt a PID is not a fix, and telling
//!   him to restart his Mac was worse than useless: it did not remove the pid
//!   file, so following the instruction need not even have helped.
//!
//! Read that second bullet twice, because its first four words carry the whole
//! thing: "ONCE THE FLOCK PROVES". `take_lock` deliberately fails OPEN when
//! flock is unavailable — a NAS or FUSE home must not brick the app — and that
//! is sound in itself, but it means "we did not get told no" is not the same
//! answer as "we hold the lock". Collapsing the two (as a `bool` return did)
//! pointed the auto-stop at a cluster nothing had proved was a leftover: on
//! such a filesystem, a genuinely LIVE first copy's postgres would be stopped
//! underneath the tutor mid-lesson by a second launch. Hence `LockState`'s
//! three values, and hence the rule the rest of this module obeys: a running
//! cluster we cannot prove is ours is left strictly alone, and we say so.
//!
//! And we do not guess at liveness with a bare `kill(pid, 0)` on
//! `postmaster.pid` — PID reuse would then block the app permanently. We ask
//! `pg_ctl status`, which is shipped in the bundle, validates the pid file
//! properly, and distinguishes "running" from "stale pid file left by a machine
//! that was rebooted". A stale file needs no dialog and no action: postgres
//! clears it itself at the next start.
//!
//! Residual risk, stated rather than papered over: `pg_ctl` ultimately proves
//! liveness with `kill(pid, 0)` too (it does at least discount EPERM, i.e. a PID
//! owned by another user), so a recycled PID could still read as "running" and
//! we would send it a `pg_ctl stop`. That window is narrow — a pid file only
//! survives an unclean stop, and the PID must be recycled to the same user — and
//! the alternative is worse in both directions: postgres's own startup check has
//! the identical weakness and FATALs on it, so declining to act would leave the
//! app permanently unbootable rather than merely mistaken. The PID we are about
//! to signal goes into app.log first, so a wrong guess leaves evidence.

use std::fs::{File, OpenOptions};
use std::io::ErrorKind;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::firstrun::remembered_child_pids;
use crate::paths::{app_log, Dirs};
use crate::supervisor::{api_binary, pg_command, web_binary};

/// The locked file, parked for the whole process lifetime. An flock lives on
/// the OPEN FILE DESCRIPTION, so closing this `File` — explicitly in
/// `release()`, or by the kernel when the process dies for ANY reason,
/// SIGKILL included — is what releases the lock. That is precisely why a lock
/// file can never go stale here the way a PID file can.
static LOCK: Mutex<Option<File>> = Mutex::new(None);

/// Why this launch must not continue. Deliberately short: everything else this
/// module used to refuse over, it now fixes on its own.
pub enum Blocked {
    /// Someone else holds the instance lock: a live second copy.
    Running,
    /// Our own cluster was left running by a force quit AND `pg_ctl stop`
    /// could not stop it. The only leftover that still reaches the tutor.
    OrphanStuck { pid: Option<i32> },
    /// A cluster is running in our data directory, and flock is unavailable on
    /// this filesystem — so we CANNOT tell whether it belongs to a live first
    /// copy or to a dead one. See `LockState::Unavailable`.
    UnprovableCluster { pid: Option<i32> },
}

impl Blocked {
    /// Greek first, then English. Names the real situation and the one action
    /// that fixes it; a port number or an errno would be neither.
    pub fn message(&self) -> String {
        match self {
            Blocked::Running => "Το Angel OS εκτελείται ήδη.\n\
                 Χρησιμοποιήστε το παράθυρο που είναι ήδη ανοιχτό — δύο αντίγραφα \
                 δεν μπορούν να τρέχουν ταυτόχρονα.\n\n\
                 Angel OS is already running.\n\
                 Use the window that is already open — two copies cannot run at \
                 the same time."
                .to_string(),
            // Every sentence here has to be TRUE, because the last version of
            // this dialog was not. Restarting the computer really does fix it
            // now: the leftover dies with the machine, and the next launch sees
            // `pg_ctl status` report "no server running" and simply continues —
            // the stale pid file no longer blocks anything.
            Blocked::OrphanStuck { pid } => format!(
                "Η βάση δεδομένων του Angel OS από προηγούμενη χρήση τρέχει ακόμη \
                 και δεν μπόρεσε να σταματήσει{pid_el}.\n\
                 Κάντε επανεκκίνηση του υπολογιστή και ανοίξτε ξανά το Angel OS: \
                 μετά την επανεκκίνηση το Angel OS το τακτοποιεί μόνο του. Τα \
                 δεδομένα σας δεν έχουν χαθεί.\n\n\
                 Angel OS's database from an earlier session is still running and \
                 could not be stopped{pid_en}.\n\
                 Restart the computer and open Angel OS again: after a restart \
                 Angel OS clears this up by itself. None of your data is lost.",
                pid_el = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
                pid_en = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
            ),
            // Every sentence has to be true of BOTH situations, because we
            // genuinely cannot tell them apart: either another copy is open
            // right now, or one died without cleaning up. Both instructions
            // work — using the open window if there is one, restarting the
            // computer if there is not — and neither of them destroys anything.
            Blocked::UnprovableCluster { pid } => format!(
                "Η βάση δεδομένων του Angel OS τρέχει ήδη{pid_el}.\n\
                 Αν το Angel OS είναι ήδη ανοιχτό, χρησιμοποιήστε εκείνο το \
                 παράθυρο — δύο αντίγραφα δεν μπορούν να τρέχουν ταυτόχρονα. Αν δεν \
                 είναι ανοιχτό πουθενά, κάντε επανεκκίνηση του υπολογιστή και ανοίξτε \
                 ξανά το Angel OS. Τα δεδομένα σας δεν έχουν χαθεί.\n\n\
                 Angel OS's database is already running{pid_en}.\n\
                 If Angel OS is already open, use that window — two copies cannot \
                 run at the same time. If it is not open anywhere, restart the \
                 computer and open Angel OS again. None of your data is lost.",
                pid_el = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
                pid_en = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
            ),
        }
    }
}

/// What `pg_ctl status` says about OUR cluster.
#[derive(Debug, PartialEq, Eq)]
enum ClusterStatus {
    /// Exit 0 — a postmaster is alive and it is this data directory's.
    Running,
    /// Exit 3 — no server running. Covers both "no pid file" and "a STALE pid
    /// file", which pg_ctl validates for us and postgres cleans up itself.
    /// Nothing to do, nothing to tell the tutor.
    Stopped,
    /// Exit 4 (`pg_ctl`'s "status unknown": the data directory is absent or
    /// holds no cluster) or pg_ctl could not be run at all. On a first launch
    /// this is the NORMAL answer — pgdata has no PG_VERSION yet.
    Unknown(String),
}

/// Exit code → meaning. Split out from the process launch so the mapping is
/// testable without a cluster, a socket or a `pg_ctl` on the machine.
fn classify_status(code: Option<i32>, detail: &str) -> ClusterStatus {
    match code {
        Some(0) => ClusterStatus::Running,
        Some(3) => ClusterStatus::Stopped,
        // 4 is documented as "program or service status is unknown" and is what
        // an empty pgdata produces. Anything else (a signal, a missing binary)
        // is equally not evidence of a running server.
        other => ClusterStatus::Unknown(format!("pg_ctl status exit {other:?}: {detail}")),
    }
}

/// `pg_ctl -D <pgdata> status`. LC_ALL=C so the line that lands in app.log is
/// the one whoever is helping over the phone can search for.
fn cluster_status(res: &Path, dirs: &Dirs) -> ClusterStatus {
    let output = pg_command(res, dirs, "pg_ctl")
        .env("LC_ALL", "C")
        .arg("-D")
        .arg(&dirs.pgdata)
        .arg("status")
        .output();
    match output {
        Ok(out) => classify_status(out.status.code(), said(&out).trim()),
        // No pg_ctl to run at all — a dev tree without staged resources, or a
        // damaged bundle. Not evidence of anything running.
        Err(e) => ClusterStatus::Unknown(format!("could not run pg_ctl: {e}")),
    }
}

/// Everything a pg_ctl invocation printed, on one line, for the log.
fn said(out: &std::process::Output) -> String {
    format!(
        "{} {}",
        String::from_utf8_lossy(&out.stdout).trim(),
        String::from_utf8_lossy(&out.stderr).trim()
    )
}

/// `pg_ctl stop -m fast`, then CONFIRM. `-w` waits, but a stop that reports
/// success while the postmaster is still there would put us straight into
/// `start_postgres` against a live cluster — so the answer we act on is the
/// status re-check, not the exit code.
fn stop_cluster(res: &Path, dirs: &Dirs) -> Result<(), String> {
    let out = pg_command(res, dirs, "pg_ctl")
        .env("LC_ALL", "C")
        .arg("-w")
        .arg("-t")
        .arg("30")
        .arg("-D")
        .arg(&dirs.pgdata)
        .arg("-m")
        .arg("fast")
        .arg("stop")
        .output()
        .map_err(|e| format!("could not run pg_ctl stop: {e}"))?;
    match cluster_status(res, dirs) {
        ClusterStatus::Running => Err(format!(
            "pg_ctl stop exited with {} but the postmaster is still running ({})",
            out.status,
            said(&out).trim()
        )),
        // Stopped is the goal; Unknown after a stop means pg_ctl can no longer
        // find a server either, which is equally fine to boot on top of.
        _ => Ok(()),
    }
}

/// Take the guard, and clean up after ourselves. `Ok(())` means this process is
/// the only Angel OS and nothing of its own is left running.
pub fn acquire(res: &Path, dirs: &Dirs) -> Result<(), Blocked> {
    let lock = take_lock(&dirs.data.join("instance.lock"));
    if lock == LockState::TakenByAnother {
        return Err(Blocked::Running);
    }
    // The auto-stop below is only sound while we HOLD the lock. That is the
    // whole argument for it: the lock proves no other Angel OS shell is
    // alive, so a postmaster in our pgdata can only belong to a dead copy of
    // us. Without the lock — flock unsupported on this filesystem, see
    // `LockState::Unavailable` — that proof does not exist, and stopping the
    // cluster would be stopping a LIVE first copy's database underneath the
    // tutor, mid-lesson. A running cluster we cannot prove is ours is left
    // strictly alone.
    let holding = lock == LockState::Held;
    // The api and node halves of the same force quit, and FIRST — before
    // anything can fail and take us out of this function, and long before
    // `pick_app_ports` runs. Order matters in one direction only: freeing a port
    // after the scan has already rolled past it would be pointless, so the reap
    // has to come first. Nothing here can fail the boot; the worst case is the
    // status quo, which is a port the scan rolls past.
    reap_orphan_children(res, dirs, holding);
    match cluster_status(res, dirs) {
        ClusterStatus::Stopped => Ok(()),
        ClusterStatus::Unknown(why) => {
            // The first-run answer, and also what a broken pg_ctl looks like.
            // Either way there is nothing to stop; if the cluster really is
            // unusable, `initdb`/`pg_ctl start` says so a few steps later, in a
            // message that names the actual problem.
            app_log(&format!("cluster status: {why} — nothing to stop"));
            Ok(())
        }
        ClusterStatus::Running if !holding => {
            let pid = recorded_pid(&dirs.pgdata);
            app_log(&format!(
                "a database is running in our data directory{}, but flock is unavailable \
                 here so we cannot prove no other copy of Angel OS is using it. \
                 Leaving it alone and refusing to start — stopping a cluster that might \
                 belong to a live first copy would take the app away from the tutor \
                 mid-lesson.",
                pid.map(|p| format!(" (PID {p})")).unwrap_or_default()
            ));
            Err(Blocked::UnprovableCluster { pid })
        }
        ClusterStatus::Running => {
            let pid = recorded_pid(&dirs.pgdata);
            app_log(&format!(
                "a previous Angel OS did not shut down cleanly: its database is \
                 still running{} and we hold the instance lock, so it is ours. \
                 Stopping it and continuing.",
                pid.map(|p| format!(" (PID {p})")).unwrap_or_default()
            ));
            match stop_cluster(res, dirs) {
                Ok(()) => {
                    app_log("leftover database stopped; boot continues normally");
                    Ok(())
                }
                Err(detail) => {
                    app_log(&format!("could not stop the leftover database: {detail}"));
                    Err(Blocked::OrphanStuck { pid })
                }
            }
        }
    }
}

/// Explicitly drop the lock. The kernel does this at exit anyway; calling it
/// from the ordered teardown makes the release happen AFTER the children are
/// down rather than at some unspecified point during process exit.
pub fn release() {
    let mut slot = LOCK.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(file) = slot.take() {
        // Best effort: the close in `drop` releases it regardless.
        unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    }
    // The lock FILE is never deleted: unlinking it would let a racing launch
    // create and lock a different inode and both copies would think they won.
}

/// The three genuinely different answers `flock` can give. A bool collapsed the
/// last two, and that collapse had teeth: "we hold the lock" and "we could not
/// take one, so anything might be true" both read as `true`, and the auto-stop
/// downstream trusted it. See `acquire`.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
enum LockState {
    /// We hold it. No other Angel OS shell is alive — the kernel guarantees
    /// that, SIGKILLed predecessors included.
    Held,
    /// EWOULDBLOCK: somebody else holds it. A live second copy.
    TakenByAnother,
    /// flock does not work here (NAS, FUSE, some network homes) or the lock
    /// file could not be opened at all. We fail OPEN — refusing to start a
    /// tutor's app because his home directory is on a NAS would be worse than
    /// the risk this guard removes — but we know NOTHING, and callers must not
    /// act as if we knew something.
    Unavailable,
}

/// How many times an EINTR is retried before giving up. `flock` is documented
/// as interruptible by a signal, and this process installs handlers (SIGTERM,
/// SIGINT) and is a GUI app on top of that, so an EINTR here is ordinary rather
/// than exotic. The bound exists so a pathological signal storm cannot spin
/// forever instead of showing the tutor a window.
const EINTR_RETRIES: u32 = 64;

/// Take the lock, retrying while the call is INTERRUPTED.
///
/// EINTR used to fall into the `other` arm below and be classified as
/// `Unavailable` — "this filesystem has no flock". That misclassification had
/// real teeth, and in the worst direction: `Unavailable` makes THIS copy run
/// lockless, so the NEXT launch takes the lock cleanly, concludes that any
/// postgres in the data directory must belong to a dead copy, and stops the
/// database out from under a tutor who is in the middle of a lesson. A signal
/// arriving at the wrong microsecond is not evidence about a filesystem.
///
/// Generic over the attempt so the retry policy is testable without a
/// filesystem that can be made to deliver signals on demand.
fn lock_with_retry<F>(mut attempt: F) -> Result<(), std::io::Error>
where
    F: FnMut() -> Result<(), std::io::Error>,
{
    let mut last = None;
    for _ in 0..EINTR_RETRIES {
        match attempt() {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == ErrorKind::Interrupted => last = Some(e),
            Err(e) => return Err(e),
        }
    }
    // Out of retries: hand back the last EINTR. It is classified as
    // `Unavailable` below, which is the honest answer once we have genuinely
    // stopped being able to ask.
    Err(last.unwrap_or_else(|| std::io::Error::from(ErrorKind::Interrupted)))
}

/// The classification, split out so every errno's meaning is pinned by a test
/// rather than by the shape of a `match` nobody re-reads.
fn classify_lock_error(err: &std::io::Error) -> LockState {
    match err.kind() {
        // EWOULDBLOCK/EAGAIN — someone else holds it. The ONE answer that is
        // evidence of a second copy.
        ErrorKind::WouldBlock => LockState::TakenByAnother,
        // ENOTSUP on some network/FUSE volumes, EBADF, an EINTR that outlasted
        // every retry — a property of the filesystem or of this call, never of
        // another process.
        _ => LockState::Unavailable,
    }
}

fn take_lock(path: &Path) -> LockState {
    let file = match OpenOptions::new().create(true).append(true).open(path) {
        Ok(f) => f,
        Err(e) => {
            app_log(&format!(
                "instance lock: cannot open {} ({e}) — continuing unguarded",
                path.display()
            ));
            return LockState::Unavailable;
        }
    };
    // LOCK_NB: never wait. A blocking flock here would hang the splash forever
    // behind a copy that is itself wedged. LOCK_NB does NOT make the call
    // uninterruptible — hence the retry.
    let fd = file.as_raw_fd();
    let taken = lock_with_retry(|| {
        if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    });
    match taken {
        Ok(()) => {
            *LOCK.lock().unwrap_or_else(|p| p.into_inner()) = Some(file);
            LockState::Held
        }
        Err(e) => {
            let state = classify_lock_error(&e);
            if state == LockState::Unavailable {
                app_log(&format!(
                    "instance lock: flock unavailable on {} ({e}) — continuing unguarded, \
                     and NOT stopping any cluster we find: without the lock we cannot prove \
                     a running one is a leftover rather than a live copy's",
                    path.display()
                ));
            }
            state
        }
    }
}

/// Line 1 of `postmaster.pid`, for the dialog and the log ONLY.
///
/// Nothing branches on this — `pg_ctl status` already decided whether anything
/// is running, and it validates the file in ways a bare `kill(pid, 0)` cannot.
/// This is here so the message can name a number when there is one, and say
/// nothing when there is not.
fn recorded_pid(pgdata: &Path) -> Option<i32> {
    let raw = std::fs::read_to_string(pgdata.join("postmaster.pid")).ok()?;
    let pid: i32 = raw.lines().next()?.trim().parse().ok()?;
    // 0/1/negative are never a postmaster worth naming.
    (pid > 1).then_some(pid)
}

// ---- the api/node half of the same force quit -------------------------------

/// One child the previous launch recorded in meta.json, and the bundled binary
/// its command line has to name before we will signal it.
#[derive(Debug, PartialEq, Eq)]
struct Recorded {
    /// What it is, in the tutor's log. Not an identifier — a noun.
    role: &'static str,
    pid: i32,
    binary: PathBuf,
}

/// How long the leftovers get to exit on SIGTERM before they are SIGKILLed.
///
/// Both are signalled TOGETHER and waited on together, so this is the entire
/// cost of a reap, not the cost per child — and it is only ever paid on a launch
/// that follows a force quit. Two seconds is generous for a uvicorn and a node
/// whose only remaining work is closing a listening socket, and short enough
/// that nobody watching the splash notices.
const REAP_GRACE: Duration = Duration::from_secs(2);

/// Stop the api/node children a force-quit predecessor left running.
///
/// Everything that makes this safe is in `reapable`; this is the part that talks
/// to the machine, and it is deliberately incapable of failing the boot.
fn reap_orphan_children(res: &Path, dirs: &Dirs, holding: bool) {
    let pids = remembered_child_pids(dirs);
    let recorded: Vec<Recorded> = [
        ("API server", pids.api, api_binary(res)),
        ("web server", pids.web, web_binary(res)),
    ]
    .into_iter()
    .filter_map(|(role, pid, binary)| pid.map(|pid| Recorded { role, pid, binary }))
    .collect();
    if recorded.is_empty() {
        return; // the ordinary launch: the last one shut down in order
    }
    let ours = reapable(recorded, holding, command_line_of);
    if ours.is_empty() {
        return;
    }
    for child in &ours {
        // Same shape as the cluster line below, and for the same reason: it has
        // to say what was found, why acting on it was safe, and that the boot
        // carried on. Every PID is named BEFORE anything is signalled, so even a
        // wrong guess leaves evidence.
        app_log(&format!(
            "a previous Angel OS did not shut down cleanly: its {} is still running \
             (PID {}), its command line is the {} inside this install and we hold the \
             instance lock, so it is ours. Stopping it and continuing.",
            child.role,
            child.pid,
            child
                .binary
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| "runtime".into()),
        ));
    }
    let pids: Vec<i32> = ours.iter().map(|c| c.pid).collect();
    for (child, how) in ours.iter().zip(stop_orphans(&pids)) {
        match how {
            Some(how) => app_log(&format!(
                "leftover {} stopped ({how}); the port it was holding is free again and \
                 boot continues normally",
                child.role
            )),
            None => app_log(&format!(
                "could not stop the leftover {} (PID {}) — it is still there after SIGKILL. \
                 Boot continues anyway: the port scan simply rolls past it, exactly as it \
                 did before.",
                child.role, child.pid
            )),
        }
    }
}

/// Which recorded children may be signalled — the whole decision, with the two
/// things that touch the machine (reading a command line, sending a signal)
/// kept out of it.
///
/// Two gates, and neither is a formality:
///
///   * `holding` is the SAME gate the cluster auto-stop is under, and it is
///     checked first and on its own. Without the flock we have no evidence that
///     a first copy is not alive right now, and its api and node are exactly the
///     processes this function would otherwise go and kill — mid-lesson, from a
///     second launch, out of a NAS home directory. When we are not holding it we
///     do not even LOOK, because there is no answer a command line could give
///     that would make signalling somebody else's live children acceptable;
///   * the command line, which is the answer to PID reuse. The number came out
///     of a file written by a process that is long dead; the only thing that can
///     turn it back into an identity is asking the operating system what is
///     running under it NOW. `argv[0]` must be the interpreter inside THIS
///     install — see `command_line_is_ours`.
fn reapable<F>(recorded: Vec<Recorded>, holding: bool, command_line: F) -> Vec<Recorded>
where
    F: Fn(i32) -> Option<String>,
{
    if !holding {
        app_log(
            "a previous Angel OS recorded child processes, but flock is unavailable here \
             so we cannot prove no other copy is running them right now. Leaving them \
             strictly alone — the port scan will roll past anything still holding a port.",
        );
        return Vec::new();
    }
    recorded
        .into_iter()
        .filter(|child| match command_line(child.pid) {
            Some(line) if command_line_is_ours(&line, &child.binary) => true,
            // The process exists and is somebody else's. This is PID reuse
            // caught in the act, and the one case worth a line of its own: it is
            // the difference between the app being careful and the app being
            // lucky.
            Some(line) => {
                app_log(&format!(
                    "PID {} was our {} in an earlier session, but the process running under \
                     that number now is not ours ({line}) — the operating system reuses PIDs. \
                     Leaving it strictly alone.",
                    child.pid, child.role
                ));
                false
            }
            // No such process: it already died, at a logout or a reboot. The
            // ordinary outcome, and nothing to say about it.
            None => false,
        })
        .collect()
}

/// Does this command line prove the process is one of OUR children?
///
/// The test is `argv[0]`, and nothing weaker. A substring search would accept
/// any process that merely MENTIONS the path — `grep`, an editor, a helper
/// script, the tutor's own terminal — and "it was probably ours" is not a
/// standard anything gets killed on. `Command::new` was given this exact
/// absolute path, so the kernel reports this exact absolute path, and prefix
/// equality is both the strictest and the most faithful check available.
///
/// The prefix must be followed by a space or by nothing at all, so a sibling
/// binary (`…/python3.12-config`) cannot pass; and the path must be absolute,
/// so a relative resource root — a dev tree, a bundle we could not resolve —
/// cannot produce a match that means nothing.
///
/// A consequence worth naming: an orphan spawned from a DIFFERENT copy of the
/// install (the app was dragged from Downloads to Applications between the two
/// launches) does not match, and is left running. That is the right way round.
/// It keeps its port, the scan rolls past it, and it dies at logout.
fn command_line_is_ours(command_line: &str, binary: &Path) -> bool {
    let Some(bin) = binary.to_str() else {
        return false;
    };
    if bin.is_empty() || !binary.is_absolute() {
        return false;
    }
    match command_line.trim_start().strip_prefix(bin) {
        Some(rest) => rest.is_empty() || rest.starts_with(' '),
        None => false,
    }
}

/// The command line of a running process, or `None` when there is no such
/// process. Two implementations because there is no portable one.
///
/// Linux: `/proc/<pid>/cmdline`, whose arguments are NUL-separated. A process
/// that exists but has an empty command line — a kernel thread, a zombie — comes
/// back as an empty string, which matches nothing and is therefore left alone.
#[cfg(target_os = "linux")]
fn command_line_of(pid: i32) -> Option<String> {
    let raw = std::fs::read(format!("/proc/{pid}/cmdline")).ok()?;
    Some(
        String::from_utf8_lossy(&raw)
            .replace('\0', " ")
            .trim()
            .to_string(),
    )
}

/// macOS has no `/proc`. `ps -o command=` prints the argument vector and, with
/// the `=` suffixing the format, no header line; `-ww` stops BSD ps truncating
/// it. A pid nothing is running under exits non-zero and prints nothing.
///
/// `LC_ALL=C` for the same reason every other invocation in this module sets it:
/// whatever ends up in app.log has to be the string whoever is helping over the
/// phone can search for.
#[cfg(not(target_os = "linux"))]
fn command_line_of(pid: i32) -> Option<String> {
    let out = std::process::Command::new("/bin/ps")
        .env("LC_ALL", "C")
        .arg("-ww")
        .arg("-o")
        .arg("command=")
        .arg("-p")
        .arg(pid.to_string())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let line = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!line.is_empty()).then_some(line)
}

/// SIGTERM everything, wait ONCE, then SIGKILL whatever is still there. Returns
/// one answer per pid, in the order given: the signal that worked, or `None` if
/// it survived both — which is not a boot failure, it is the situation we were
/// already in.
///
/// Signalling the whole set together, rather than a child at a time, is what
/// keeps the cost of a reap at one grace period instead of one per orphan. The
/// splash is on screen while this runs.
///
/// The wait is a `kill(pid, 0)` poll rather than a `wait()`: an orphan is not
/// our child (init or launchd adopted it when its parent was killed), so there
/// is no status for us to reap, and whoever adopted it is who will make it
/// vanish from the process table.
fn stop_orphans(pids: &[i32]) -> Vec<Option<&'static str>> {
    let mut outcome: Vec<Option<&'static str>> = vec![None; pids.len()];
    for pid in pids {
        unsafe { libc::kill(*pid, libc::SIGTERM) };
    }
    wait_for_all(pids, &mut outcome, "SIGTERM", REAP_GRACE);

    let survivors: Vec<i32> = pids
        .iter()
        .zip(&outcome)
        .filter_map(|(pid, done)| done.is_none().then_some(*pid))
        .collect();
    if survivors.is_empty() {
        return outcome;
    }
    for pid in &survivors {
        unsafe { libc::kill(*pid, libc::SIGKILL) };
    }
    // SIGKILL cannot be caught or ignored, so the only thing left to wait for is
    // the kernel tearing the process down and its adopter reaping it. Short on
    // purpose: this is a confirmation, not a negotiation, and it is the
    // difference between a log line that is true and one that is hopeful.
    wait_for_all(pids, &mut outcome, "SIGKILL", Duration::from_millis(500));
    outcome
}

/// Poll until every pid still marked unfinished has gone, or `grace` runs out,
/// filling in `how` for each one that goes.
fn wait_for_all(
    pids: &[i32],
    outcome: &mut [Option<&'static str>],
    how: &'static str,
    grace: Duration,
) {
    let deadline = Instant::now() + grace;
    loop {
        let mut waiting = false;
        for (pid, done) in pids.iter().zip(outcome.iter_mut()) {
            if done.is_some() {
                continue;
            }
            if alive(*pid) {
                waiting = true;
            } else {
                *done = Some(how);
            }
        }
        if !waiting || Instant::now() >= deadline {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// Signal 0: existence only, no signal delivered. `EPERM` is an existence
/// answer too — the process is there and belongs to another user — and reading
/// it as "gone" would let us log that we stopped something we did not.
fn alive(pid: i32) -> bool {
    // A ZOMBIE is not alive. It has exited and released every port and file it
    // held; all that remains is an exit status its parent has not collected. On
    // a Mac launchd reaps orphans at once so this is rarely observed — but when
    // whatever inherited our children does NOT reap (a container whose PID 1 is
    // not an init, a wedged shell), `kill(pid, 0)` keeps succeeding forever and
    // a reap that actually worked reports "could not stop it" instead. Seen in
    // the E2E; the boot was fine, the log was alarming for no reason.
    if is_zombie(pid) {
        return false;
    }
    if unsafe { libc::kill(pid, 0) } == 0 {
        return true;
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// `true` only when the process is provably in the zombie state. Anything we
/// cannot read (permission, a platform whose output we do not recognise) is
/// reported as NOT a zombie, so an unreadable process is still treated as
/// alive and left alone.
fn is_zombie(pid: i32) -> bool {
    #[cfg(target_os = "linux")]
    {
        // /proc/<pid>/stat: "<pid> (comm) <state> ...". `comm` can itself hold
        // spaces and parentheses, so the state is the field after the LAST ')'.
        if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            if let Some(rest) = stat.rsplit_once(')') {
                return rest.1.trim_start().starts_with('Z');
            }
        }
        false
    }
    #[cfg(not(target_os = "linux"))]
    {
        std::process::Command::new("/bin/ps")
            .args(["-o", "state=", "-p", &pid.to_string()])
            .env("LC_ALL", "C")
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim_start().starts_with('Z'))
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(tag: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("gt-instance-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).expect("tmpdir");
        d
    }

    /// The three answers pg_ctl can give, and what each one means for the boot.
    /// Getting exit 3 wrong is what used to send the tutor to Activity Monitor
    /// over a pid file a reboot had already made meaningless.
    #[test]
    fn pg_ctl_status_codes_map_to_the_right_decision() {
        assert_eq!(classify_status(Some(0), "server is running"), ClusterStatus::Running);
        // "no server running" — includes a stale pid file, which pg_ctl checks
        // for us. No dialog, no action: postgres clears the file itself.
        assert_eq!(classify_status(Some(3), "no server running"), ClusterStatus::Stopped);
        // First run: pgdata exists but holds no cluster yet.
        assert!(matches!(
            classify_status(Some(4), "directory is not a database cluster directory"),
            ClusterStatus::Unknown(_)
        ));
        // Killed by a signal, or a pg_ctl that is not there at all.
        assert!(matches!(classify_status(None, "signal"), ClusterStatus::Unknown(_)));
        assert!(matches!(classify_status(Some(1), "?"), ClusterStatus::Unknown(_)));
    }

    /// Neither "no server" nor "cannot tell" may ever be treated as a running
    /// orphan — those two are how a stale pid file and a first run present, and
    /// blocking on either is the bug this replaced.
    #[test]
    fn only_exit_zero_counts_as_a_running_cluster() {
        for code in [None, Some(1), Some(2), Some(3), Some(4), Some(127)] {
            assert_ne!(
                classify_status(code, "whatever"),
                ClusterStatus::Running,
                "exit {code:?} must not read as a running cluster"
            );
        }
    }

    /// The PID is display-only, so parsing must be forgiving and must never be
    /// what decides anything.
    #[test]
    fn recorded_pid_is_parsed_for_display_only() {
        let d = tmpdir("pidparse");
        assert_eq!(recorded_pid(&d), None, "no file at all");

        std::fs::write(
            d.join("postmaster.pid"),
            format!("4242\n{}\n1700000000\n5434\n", d.display()),
        )
        .expect("write");
        assert_eq!(recorded_pid(&d), Some(4242));

        for junk in ["not-a-pid\n", "0\n", "1\n", "-9\n", ""] {
            std::fs::write(d.join("postmaster.pid"), junk).expect("write");
            assert_eq!(recorded_pid(&d), None, "for {junk:?}");
        }
    }

    /// Both dialogs must be bilingual, Greek FIRST — the tutor reads Greek and
    /// only the person helping him reads the English half.
    #[test]
    fn messages_are_bilingual_greek_first() {
        for msg in [
            Blocked::Running.message(),
            Blocked::OrphanStuck { pid: Some(4242) }.message(),
            Blocked::OrphanStuck { pid: None }.message(),
            Blocked::UnprovableCluster { pid: Some(4242) }.message(),
            Blocked::UnprovableCluster { pid: None }.message(),
        ] {
            let greek = msg.find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c));
            let english = msg
                .find("Angel OS is already running")
                .or_else(|| msg.find("is still running and"))
                .or_else(|| msg.find("database is already running"));
            assert!(greek.is_some(), "no Greek in: {msg}");
            assert!(english.is_some(), "no English in: {msg}");
            assert!(greek < english, "English must come after Greek: {msg}");
        }
    }

    /// A real flock, taken twice. The FIRST holder gets `Held`; a second
    /// attempt on the same file from the same process gets... `Held` again,
    /// because flock is per open-file-description and a second `open` of the
    /// same path is a new description that the kernel lets the same process
    /// re-take. So the assertion that carries weight here is the one that can
    /// be made honestly: taking the lock reports `Held`, and `Held` is the ONLY
    /// state that authorises the auto-stop.
    #[test]
    fn taking_the_lock_reports_held() {
        let d = tmpdir("lockheld");
        assert_eq!(take_lock(&d.join("instance.lock")), LockState::Held);
        release();
    }

    /// EINTR is not a filesystem property, and must never be reported as one.
    ///
    /// The chain it used to start: a signal lands mid-`flock` → EINTR → the old
    /// `other` arm called that "flock unavailable" → THIS copy runs lockless →
    /// the next launch takes the lock cleanly, sees a postgres in the data
    /// directory, and — correctly, by its own rules — concludes it must belong
    /// to a dead copy and stops it. The tutor is mid-lesson in the copy that
    /// lost the coin toss.
    #[test]
    fn an_interrupted_flock_is_retried_and_not_mistaken_for_a_missing_flock() {
        // Interrupted twice, then it works: the lock is TAKEN, which is the
        // whole point — not merely "not misclassified".
        let mut calls = 0;
        let got = lock_with_retry(|| {
            calls += 1;
            if calls <= 2 {
                Err(std::io::Error::from_raw_os_error(libc::EINTR))
            } else {
                Ok(())
            }
        });
        assert!(got.is_ok(), "an interrupted flock must be retried, not surrendered");
        assert_eq!(calls, 3);

        // A REAL answer is returned immediately — a retry loop that swallowed
        // EWOULDBLOCK would turn a live second copy into "no lock here".
        let mut calls = 0;
        let got = lock_with_retry(|| {
            calls += 1;
            Err(std::io::Error::from_raw_os_error(libc::EWOULDBLOCK))
        });
        assert_eq!(calls, 1, "EWOULDBLOCK is an answer, not an interruption");
        assert_eq!(
            classify_lock_error(&got.expect_err("would block")),
            LockState::TakenByAnother
        );

        // And the retry is BOUNDED: an endless signal storm must not hang the
        // splash forever. Giving up reports `Unavailable`, which is honest once
        // we have stopped being able to ask at all.
        let mut calls = 0;
        let got = lock_with_retry(|| {
            calls += 1;
            Err(std::io::Error::from_raw_os_error(libc::EINTR))
        });
        assert_eq!(calls, EINTR_RETRIES as i32);
        assert_eq!(
            classify_lock_error(&got.expect_err("still interrupted")),
            LockState::Unavailable
        );
    }

    /// Every errno's meaning, pinned. Only EWOULDBLOCK/EAGAIN is evidence of
    /// another copy; everything else is evidence of nothing, and `Unavailable`
    /// is the state that says so.
    #[test]
    fn only_would_block_is_evidence_of_a_second_copy() {
        assert_eq!(
            classify_lock_error(&std::io::Error::from_raw_os_error(libc::EWOULDBLOCK)),
            LockState::TakenByAnother
        );
        for benign in [libc::ENOTSUP, libc::EINVAL, libc::EBADF, libc::ENOLCK, libc::EINTR] {
            assert_eq!(
                classify_lock_error(&std::io::Error::from_raw_os_error(benign)),
                LockState::Unavailable,
                "errno {benign} must not read as another copy holding the lock"
            );
        }
    }

    /// The distinction the bool could not make, stated as the rule the caller
    /// applies: ONLY `Held` may stop a running cluster.
    ///
    /// `take_lock` fails OPEN when flock is unsupported (a NAS or FUSE home
    /// must not brick the app) — but "we could not take a lock" is not
    /// "nothing else is running", and the auto-stop that round 3 added would
    /// have read it that way and stopped a LIVE first copy's database out from
    /// under the tutor. `Unavailable` and `Held` must never be collapsed again.
    #[test]
    fn only_holding_the_lock_authorises_stopping_a_cluster() {
        let may_stop = |s: LockState| s == LockState::Held;
        assert!(may_stop(LockState::Held));
        assert!(!may_stop(LockState::Unavailable), "cannot prove the cluster is ours");
        assert!(!may_stop(LockState::TakenByAnother), "somebody else's, plainly");
        // …and the three are genuinely distinct, which a bool made impossible.
        assert_ne!(LockState::Held, LockState::Unavailable);
        assert_ne!(LockState::TakenByAnother, LockState::Unavailable);
    }

    /// The dialog for "a cluster is running and we cannot prove whose it is"
    /// has to be true of BOTH possibilities at once, because we genuinely
    /// cannot tell them apart — and it must offer an action that works in
    /// either case.
    #[test]
    fn the_unprovable_cluster_dialog_is_true_either_way() {
        let msg = Blocked::UnprovableCluster { pid: Some(4242) }.message();
        assert!(msg.matches("4242").count() >= 2, "both halves name the PID");
        // If another copy IS open: use it. If it is NOT: restart. Both stated.
        assert!(msg.contains("use that window"), "{msg}");
        assert!(msg.contains("restart the computer"), "{msg}");
        assert!(msg.contains("None of your data is lost"), "{msg}");
        // Never claim a PID we do not have.
        let anon = Blocked::UnprovableCluster { pid: None }.message();
        assert!(!anon.contains("PID"), "no PID to name, so name none: {anon}");
    }

    /// A resource root shaped like the real one, so the command lines the
    /// ownership test is fed in these tests look exactly like the ones the
    /// kernel reports on the tutor's machine.
    fn res_root(tag: &str) -> PathBuf {
        std::path::PathBuf::from(format!(
            "/Applications/Angel OS-{tag}.app/Contents/Resources/resources"
        ))
    }

    /// What our api child's command line actually is, spelled the way
    /// `supervisor::start_api` spells it.
    fn our_api_command_line(res: &Path) -> String {
        format!(
            "{} -m uvicorn app.main:app --host 127.0.0.1 --port 8791",
            api_binary(res).display()
        )
    }

    /// THE trap this whole path is built around: PID reuse. The number was
    /// written down by a process that is dead; whatever is running under it now
    /// is a stranger until it proves otherwise, and the proof is `argv[0]`.
    #[test]
    fn ownership_is_argv_zero_inside_this_install_and_nothing_weaker() {
        let res = res_root("live");
        let python = api_binary(&res);
        let node = web_binary(&res);

        // Ours: the exact binary this install spawns, with its arguments.
        assert!(command_line_is_ours(&our_api_command_line(&res), &python));
        assert!(command_line_is_ours(
            &format!("{} server.js", node.display()),
            &node
        ));
        // Bare, no arguments at all.
        assert!(command_line_is_ours(&python.display().to_string(), &python));

        // Not ours, in every way a real machine produces:
        for foreign in [
            // A system interpreter. The commonest process on the box.
            "/usr/bin/python3.12 -m uvicorn app.main:app --host 127.0.0.1 --port 8791",
            "/opt/homebrew/bin/node server.js",
            // ANOTHER install of this very app — a second copy in ~/Downloads.
            // Its children are not ours to kill.
            "/Applications/Angel OS-other.app/Contents/Resources/resources/python/bin/python3.12 -m uvicorn",
            // Merely NAMING our path. This is what a substring test would have
            // killed: a helper's grep, an editor, a tail on the log.
            "grep -r /Applications/Angel OS-live.app/Contents/Resources/resources/python/bin/python3.12 .",
            "/bin/sh -c /Applications/Angel OS-live.app/Contents/Resources/resources/node/bin/node",
            // A sibling binary that shares our prefix.
            "/Applications/Angel OS-live.app/Contents/Resources/resources/python/bin/python3.12-config --libs",
            // A zombie or a kernel thread: exists, says nothing.
            "",
            "   ",
        ] {
            assert!(
                !command_line_is_ours(foreign, &python) && !command_line_is_ours(foreign, &node),
                "must not be treated as ours: {foreign:?}"
            );
        }

        // The api PID must match the API binary and the web PID the node one:
        // recording which is which is not decoration.
        assert!(!command_line_is_ours(&our_api_command_line(&res), &node));

        // A relative resource root — a dev tree, or a bundle whose path we could
        // not resolve — can never authorise a kill: prefix-matching a relative
        // path against a command line is a coincidence, not evidence.
        let relative = Path::new("resources/python/bin/python3.12");
        assert!(!command_line_is_ours(
            "resources/python/bin/python3.12 -m uvicorn",
            relative
        ));

        // And the case this whole path was written for, in the words the
        // operating system actually used: these two lines are copied verbatim
        // out of `ps` on the machine where an E2E force-quit left them behind,
        // with the .deb's resource root rather than the .app's.
        let deb = Path::new("/usr/lib/Angel OS/resources");
        assert!(command_line_is_ours(
            "/usr/lib/Angel OS/resources/python/bin/python3.12 -m uvicorn app.main:app \
             --host 127.0.0.1 --port 8793",
            &api_binary(deb)
        ));
        assert!(command_line_is_ours(
            "/usr/lib/Angel OS/resources/node/bin/node server.js",
            &web_binary(deb)
        ));
        // The postgres left by the same force quit is NOT one of these. It is
        // not our child, it is not signalled here, and `pg_ctl` deals with it.
        assert!(!command_line_is_ours(
            "/usr/lib/Angel OS/resources/pg/bin/postgres -D /home/tester/.local/share/\
             guitar-tutor/pgdata -p 5434",
            &api_binary(deb)
        ));
    }

    /// The filter, over the three answers the operating system can give about a
    /// recorded PID. Only the first one may ever be signalled.
    #[test]
    fn only_a_process_that_proves_it_is_ours_is_reaped() {
        let res = res_root("live");
        let recorded = || {
            vec![
                Recorded { role: "API server", pid: 8101, binary: api_binary(&res) },
                Recorded { role: "web server", pid: 8102, binary: web_binary(&res) },
            ]
        };

        // 1. Both are ours: both are reaped.
        let ours = reapable(recorded(), true, |pid| match pid {
            8101 => Some(our_api_command_line(&res)),
            8102 => Some(format!("{} server.js", web_binary(&res).display())),
            _ => None,
        });
        assert_eq!(
            ours.iter().map(|c| c.pid).collect::<Vec<_>>(),
            vec![8101, 8102]
        );

        // 2. The PID was recycled: something else entirely is running under it.
        // Nothing is signalled — this is the case where a mistake would be
        // inexcusable.
        let stranger = reapable(recorded(), true, |_| {
            Some("/usr/lib/firefox/firefox --contentproc".to_string())
        });
        assert!(stranger.is_empty(), "a recycled PID must never be signalled");

        // 3. No such process: it died at the last logout. A no-op, and the
        // ordinary outcome of a force quit followed by a reboot.
        assert!(reapable(recorded(), true, |_| None).is_empty());

        // …and the mixed case, which is what a real machine hands over: one
        // orphan still there, one already gone.
        let mixed = reapable(recorded(), true, |pid| {
            (pid == 8102).then(|| format!("{} server.js", web_binary(&res).display()))
        });
        assert_eq!(mixed.iter().map(|c| c.pid).collect::<Vec<_>>(), vec![8102]);
    }

    /// The same gate as the cluster auto-stop, and for a sharper reason: without
    /// the flock, a genuinely LIVE first copy may be running right now, and its
    /// uvicorn and node are precisely the processes named in the file we just
    /// read. Killing them takes the app away from the tutor mid-lesson.
    ///
    /// Not holding the lock does not merely mean "signal nothing" — we do not
    /// even ASK what the PIDs are running, because there is no answer that could
    /// make it acceptable.
    #[test]
    fn no_lock_means_no_signal_and_not_even_a_look() {
        let res = res_root("live");
        let recorded = vec![
            Recorded { role: "API server", pid: 8101, binary: api_binary(&res) },
            Recorded { role: "web server", pid: 8102, binary: web_binary(&res) },
        ];
        let looked = std::cell::Cell::new(0);
        let left_alone = reapable(recorded, false, |_| {
            looked.set(looked.get() + 1);
            // Even a command line that IS ours must not get anything killed
            // here: on a filesystem without flock it is exactly what a live
            // first copy's child looks like.
            Some(our_api_command_line(&res))
        });
        assert!(left_alone.is_empty(), "nothing may be signalled without the lock");
        assert_eq!(looked.get(), 0, "we do not even inspect them");

        // The rule, stated the same way the cluster's is: only `Held`.
        let may_reap = |s: LockState| s == LockState::Held;
        assert!(may_reap(LockState::Held));
        assert!(!may_reap(LockState::Unavailable));
        assert!(!may_reap(LockState::TakenByAnother));
    }

    /// The real reader, against the two processes a test can be certain about:
    /// this one, and one that cannot exist.
    #[test]
    fn reading_a_command_line_is_honest_about_dead_pids() {
        // Above every `pid_max` any Unix uses (Linux caps at 2^22, macOS far
        // lower), so nothing can ever be running here.
        assert_eq!(command_line_of(i32::MAX), None, "a pid that cannot exist");

        // And the process we are certain about: ourselves. It exists, so a
        // command line comes back — and it is emphatically NOT one of our
        // bundled children, which is the answer that keeps the reaper's hands
        // off the test runner.
        let mine = command_line_of(std::process::id() as i32).expect("our own command line");
        assert!(!mine.trim().is_empty());
        let res = res_root("live");
        assert!(!command_line_is_ours(&mine, &api_binary(&res)));
        assert!(!command_line_is_ours(&mine, &web_binary(&res)));

        // And the signalling half, on that same impossible pid: `kill` answers
        // ESRCH, so it reads as already gone and costs the splash NO wall clock
        // at all. A launch after a reboot — where the orphans died with the
        // machine — must not sit through a grace period for nothing.
        let started = Instant::now();
        assert_eq!(stop_orphans(&[i32::MAX]), vec![Some("SIGTERM")]);
        assert!(started.elapsed() < REAP_GRACE, "a dead pid is not waited on");
        assert!(stop_orphans(&[]).is_empty());
    }

    /// End to end, against processes that genuinely exist: the record in
    /// meta.json, the command line the operating system really reports, the
    /// signal, and the survival of everything that is not ours.
    ///
    /// The stand-in for each bundled runtime is a SYMLINK to `/bin/sleep` placed
    /// where that runtime lives in the bundle. `argv[0]` is whatever was handed
    /// to `exec`, not the resolved target, so the kernel reports exactly the path
    /// the reaper is looking for — while the thing actually executed is a system
    /// binary, so nothing here depends on being able to run a copied one.
    #[test]
    fn a_real_orphan_is_stopped_and_a_process_from_another_install_is_not() {
        use crate::firstrun::{record_child_pids, write_meta, AppPorts, ChildPids};
        use std::process::Command;

        let root = tmpdir("reaplive");
        let stand_in = |res: &Path, rel: &str| -> PathBuf {
            let path = res.join(rel);
            std::fs::create_dir_all(path.parent().expect("bin dir")).expect("mkdir");
            std::os::unix::fs::symlink("/bin/sleep", &path).expect("symlink");
            path
        };

        // THIS install, and a second copy of the app somewhere else — the exact
        // situation in which a PID from meta.json must not be trusted on its
        // number alone.
        let res = root.join("Angel OS.app/Contents/Resources/resources");
        let other = root.join("Downloads/Angel OS.app/Contents/Resources/resources");
        let node = stand_in(&res, "node/bin/node");
        let their_python = stand_in(&other, "python/bin/python3.12");

        // Our orphaned web server: spawned from THIS install's node.
        let mut orphan = Command::new(&node).arg("300").spawn().expect("orphan");
        let orphan_pid = orphan.id() as i32;
        // Stands in for init/launchd. An orphan is adopted when its parent is
        // killed, and it is the ADOPTER that reaps it — which is what makes the
        // process vanish from the table after a signal, and therefore what
        // `stop_orphans` is polling for. Without something playing that part a
        // signalled child would linger as a zombie, which is a property of this
        // test being its own parent and not of the code under test.
        let adopter = std::thread::spawn(move || {
            let _ = orphan.wait();
        });

        // The other copy's api child. Recorded under OUR api PID — i.e. the PID
        // was recycled, or meta.json was copied between installs — so the only
        // thing standing between it and a kill is the command-line check.
        let mut stranger = Command::new(&their_python).arg("300").spawn().expect("stranger");
        let stranger_pid = stranger.id() as i32;

        let dirs = Dirs {
            pgdata: root.join("pgdata"),
            media: root.join("media"),
            secrets: root.join("secrets"),
            logs: root.join("logs"),
            data: root.clone(),
        };
        write_meta(&dirs, 5434, AppPorts { web: 8790, api: 8791 }).expect("meta");
        record_child_pids(
            &dirs,
            ChildPids { api: Some(stranger_pid), web: Some(orphan_pid) },
        )
        .expect("record");

        assert!(alive(orphan_pid) && alive(stranger_pid), "both are running");
        reap_orphan_children(&res, &dirs, true);
        adopter.join().expect("adopter");

        assert!(!alive(orphan_pid), "our own leftover web server must be stopped");
        assert!(
            alive(stranger_pid),
            "a process from ANOTHER install must be left strictly alone, whatever \
             meta.json says its PID is"
        );

        // …and with the lock unproven, the same orphan would have survived.
        let mut second = Command::new(&node).arg("300").spawn().expect("second orphan");
        let second_pid = second.id() as i32;
        record_child_pids(&dirs, ChildPids { api: None, web: Some(second_pid) }).expect("record");
        reap_orphan_children(&res, &dirs, false);
        assert!(alive(second_pid), "no lock, no signal — even for a process that IS ours");

        let _ = second.kill();
        let _ = second.wait();
        let _ = stranger.kill();
        let _ = stranger.wait();
        let _ = std::fs::remove_dir_all(&root);
    }

    /// The orphan dialog is the last resort, so what it says has to be true and
    /// actionable: name the PID when there is one, promise nothing when there is
    /// not, and give the one instruction that now genuinely works.
    #[test]
    fn the_stuck_orphan_dialog_says_something_true() {
        let named = Blocked::OrphanStuck { pid: Some(4242) }.message();
        assert!(named.matches("4242").count() >= 2, "both halves name the PID");
        assert!(named.contains("Restart the computer"));
        // Reassurance is part of being true: nothing was lost, and the tutor has
        // no way of knowing that.
        assert!(named.contains("None of your data is lost"));
        // Never invent a PID we do not have — and never leave "(PID )" behind.
        let anon = Blocked::OrphanStuck { pid: None }.message();
        assert!(!anon.contains("PID"), "no PID to name, so name none: {anon}");
    }

    /// A process that has exited but whose parent has not collected it still
    /// answers `kill(pid, 0)`. Treating it as alive made a reap that worked
    /// report failure (seen in the container E2E, where PID 1 never reaps).
    #[test]
    fn a_zombie_is_not_alive() {
        let mut child = std::process::Command::new("/bin/sh")
            .args(["-c", "exit 0"])
            .spawn()
            .expect("spawn");
        let pid = child.id() as i32;
        // Do NOT wait(): the child stays a zombie for as long as we hold it.
        for _ in 0..200 {
            if is_zombie(pid) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert!(is_zombie(pid), "the exited child should be a zombie");
        assert_eq!(unsafe { libc::kill(pid, 0) }, 0, "a zombie still answers kill(pid,0)");
        assert!(!alive(pid), "and alive() must nonetheless report it as gone");
        let _ = child.wait();
    }

    #[test]
    fn a_live_process_is_not_mistaken_for_a_zombie() {
        assert!(!is_zombie(std::process::id() as i32));
        assert!(alive(std::process::id() as i32));
    }

}
