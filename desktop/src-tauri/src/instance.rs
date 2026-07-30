//! One-copy-at-a-time guard, and the app's own leftovers. Runs immediately
//! after `paths::ensure_dirs()` — before secrets, before any port scan, before
//! anything touches the cluster.
//!
//! Two DIFFERENT failures used to be caught, by accident, by the frozen-port
//! preflight this branch replaced with a rolling scan:
//!
//! 1. A SECOND copy of GuitarTutor launched while the first one is running.
//!    `tauri_plugin_single_instance` is not a guarantee: on Linux it wants a
//!    session DBus and fails OPEN when there is none (containers, ssh, some
//!    login setups), and it is a courtesy focus-the-window feature, not a
//!    mutual exclusion primitive.
//!
//! 2. The app's OWN orphans. A force quit — the beachball, then Cmd+Opt+Esc —
//!    SIGKILLs the shell without running the ordered teardown, so its postgres
//!    keeps running. Under the old frozen ports the next launch bounced off the
//!    busy port with a clear message. With rolling ports it rolls PAST its own
//!    leftovers and then dies inside pg_ctl, showing the tutor a raw English
//!    lock-file dump.
//!
//! These are handled in COMPLETELY different ways, and the difference is the
//! point of this module:
//!
//! * (1) is somebody else's process. We cannot touch it, so we refuse to start
//!   and say so — an flock on `<data>/instance.lock`, which cannot go stale
//!   because the kernel drops it when the holder dies, SIGKILL included.
//!
//! * (2) is OUR process. Once the flock proves no other GuitarTutor shell is
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
use std::path::Path;
use std::sync::Mutex;

use crate::paths::{app_log, Dirs};
use crate::supervisor::pg_command;

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
            Blocked::Running => "Το GuitarTutor εκτελείται ήδη.\n\
                 Χρησιμοποιήστε το παράθυρο που είναι ήδη ανοιχτό — δύο αντίγραφα \
                 δεν μπορούν να τρέχουν ταυτόχρονα.\n\n\
                 GuitarTutor is already running.\n\
                 Use the window that is already open — two copies cannot run at \
                 the same time."
                .to_string(),
            // Every sentence here has to be TRUE, because the last version of
            // this dialog was not. Restarting the computer really does fix it
            // now: the leftover dies with the machine, and the next launch sees
            // `pg_ctl status` report "no server running" and simply continues —
            // the stale pid file no longer blocks anything.
            Blocked::OrphanStuck { pid } => format!(
                "Η βάση δεδομένων του GuitarTutor από προηγούμενη χρήση τρέχει ακόμη \
                 και δεν μπόρεσε να σταματήσει{pid_el}.\n\
                 Κάντε επανεκκίνηση του υπολογιστή και ανοίξτε ξανά το GuitarTutor: \
                 μετά την επανεκκίνηση το GuitarTutor το τακτοποιεί μόνο του. Τα \
                 δεδομένα σας δεν έχουν χαθεί.\n\n\
                 GuitarTutor's database from an earlier session is still running and \
                 could not be stopped{pid_en}.\n\
                 Restart the computer and open GuitarTutor again: after a restart \
                 GuitarTutor clears this up by itself. None of your data is lost.",
                pid_el = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
                pid_en = pid.map(|p| format!(" (PID {p})")).unwrap_or_default(),
            ),
            // Every sentence has to be true of BOTH situations, because we
            // genuinely cannot tell them apart: either another copy is open
            // right now, or one died without cleaning up. Both instructions
            // work — using the open window if there is one, restarting the
            // computer if there is not — and neither of them destroys anything.
            Blocked::UnprovableCluster { pid } => format!(
                "Η βάση δεδομένων του GuitarTutor τρέχει ήδη{pid_el}.\n\
                 Αν το GuitarTutor είναι ήδη ανοιχτό, χρησιμοποιήστε εκείνο το \
                 παράθυρο — δύο αντίγραφα δεν μπορούν να τρέχουν ταυτόχρονα. Αν δεν \
                 είναι ανοιχτό πουθενά, κάντε επανεκκίνηση του υπολογιστή και ανοίξτε \
                 ξανά το GuitarTutor. Τα δεδομένα σας δεν έχουν χαθεί.\n\n\
                 GuitarTutor's database is already running{pid_en}.\n\
                 If GuitarTutor is already open, use that window — two copies cannot \
                 run at the same time. If it is not open anywhere, restart the \
                 computer and open GuitarTutor again. None of your data is lost.",
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
/// the only GuitarTutor and nothing of its own is left running.
pub fn acquire(res: &Path, dirs: &Dirs) -> Result<(), Blocked> {
    let lock = take_lock(&dirs.data.join("instance.lock"));
    if lock == LockState::TakenByAnother {
        return Err(Blocked::Running);
    }
    // The auto-stop below is only sound while we HOLD the lock. That is the
    // whole argument for it: the lock proves no other GuitarTutor shell is
    // alive, so a postmaster in our pgdata can only belong to a dead copy of
    // us. Without the lock — flock unsupported on this filesystem, see
    // `LockState::Unavailable` — that proof does not exist, and stopping the
    // cluster would be stopping a LIVE first copy's database underneath the
    // tutor, mid-lesson. A running cluster we cannot prove is ours is left
    // strictly alone.
    let holding = lock == LockState::Held;
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
                 here so we cannot prove no other copy of GuitarTutor is using it. \
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
                "a previous GuitarTutor did not shut down cleanly: its database is \
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
    /// We hold it. No other GuitarTutor shell is alive — the kernel guarantees
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
                .find("GuitarTutor is already running")
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
}
