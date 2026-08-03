//! Angel OS desktop shell.
//!
//! Boot: splash → dirs → instance guard (which STOPS a leftover cluster of our
//! own — when the instance lock PROVES it is ours — rather than refusing to
//! start) → secrets → pg port → (first run: pgdata guard, then initdb)
//! → postgres (rebuilding a half-built first-run cluster if it will not start)
//! → (first run: Greek-collation guard) → seed plan → (first run: displace the
//! fragment, seed) → alembic → completion marker → reap → APP PORTS → uvicorn
//! → node → readiness gate → main window at http://localhost:<web port>.
//!
//! "First run" means the first run has not COMPLETED — an explicit marker,
//! never `PG_VERSION` (see `firstrun`'s state-machine comment). An interrupted
//! one is resumed here, not skipped, because skipping it hands the tutor an app
//! with no library and nothing on screen to explain why. And a first run whose
//! `initdb` was interrupted is REBUILT here rather than failing identically on
//! every launch forever — `initdb` writes PG_VERSION seconds in, long before
//! the cluster is usable, so "there is a PG_VERSION" was never the same fact as
//! "there is a cluster that starts".
//!
//! THE RULE ABOVE ALL THE OTHERS: this app displaces, it does not destroy. When
//! boot must get a database or a data directory out of its way, it RENAMES it
//! (`guitar_superseded_<stamp>`, `pgdata_superseded_<stamp>`) and builds beside
//! it. The only exception is a database MEASURED, immediately beforehand, to
//! contain zero user relations of any kind. Everything else in this file is an
//! argument that some cell of a truth table cannot be reached wrongly; that rule
//! is what makes those arguments survivable when one of them turns out to be
//! wrong anyway.
//!
//! AND THE MEDIA TRAVELS WITH IT (`media_superseded_<stamp>`, one shared stamp,
//! moved FIRST). Displacement used to stop at the layer boundary: the shell
//! renamed the database aside, and minutes later on the same boot the API child
//! swept `<data>/media` against the NEW database, found no row for any of the
//! tutor's books, and deleted every one — original uploaded PDFs included. A
//! database and the files its rows describe are one recoverable unit or they are
//! not recoverable at all, so `firstrun::displace_media` moves them together and
//! `create_and_seed_db` is the choke point no freshly created database can get
//! past without one. The API side now quarantines instead of deleting, which is
//! defence in depth for the desyncs nobody has thought of.
//!
//! THE INVARIANT THAT PROTECTS HIS WORK: if the main window can appear, the
//! marker reads `complete`. Every plan — `Skip`, `Seed`, `Reseed`, `Adopt` and
//! `Undecided` alike — goes through the single `firstrun::finalize_install`
//! call below, and `show_main_window` will not compile without the receipt that
//! call returns. The marker is what the NEXT launch consults before it decides
//! it may DROP the database, so a boot that finished while the marker still
//! said `seeding` was a boot that armed the next one to delete the tutor's
//! curricula. Nothing here may reintroduce a path that skips it.
//!
//! A panic on this thread is funnelled into the same `fatal()` as every
//! deliberate failure (`run_guarded`): unguarded, it left the splash frozen
//! forever with nothing in app.log and no button to press.
//!
//! The app ports are DERIVED at boot (`firstrun::pick_app_ports`), the same way
//! the postgres port already was — the tutor's machine may well be running
//! something else on 8790/8791. What stays FROZEN is the ORIGIN: LITERAL
//! `localhost`, never 127.0.0.1, never tauri:// (the session cookie is
//! host-only and the web app derives its API base from the origin). Because
//! the port pair is no longer guessable from the origin, the shell injects
//! `window.__GT_API_BASE__` before any page script runs and hands the API child
//! a matching `CORS_ORIGINS`.
//!
//! Note WHERE the app ports are picked: last, immediately before the spawns
//! that bind them. A probe is only a claim about the instant it ran, and on a
//! first run the steps above it take minutes (initdb, a 250MB `pg_restore`, a
//! media copy, alembic). The postgres port is picked earlier because postgres
//! is itself started earlier — same rule, applied to a different child.
//!
//! The running app never contacts any host but 127.0.0.1/localhost itself;
//! the API process talks to api.anthropic.com with the tutor's key. Nothing
//! else. No telemetry, no update pings.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod firstrun;
mod instance;
mod paths;
mod supervisor;

use std::ffi::OsString;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use tauri::menu::{MenuBuilder, MenuItem, MenuItemBuilder, SubmenuBuilder};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder, Wry};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use crate::firstrun::{DiskState, InstallComplete, SeedOutcome, SeedPlan};
use crate::paths::Dirs;
use crate::supervisor::{Ready, Supervisor};

static SUPERVISOR: OnceLock<Arc<Supervisor>> = OnceLock::new();

/// Backend ▸ Restart Backend, kept so boot can enable it. It is built DISABLED:
/// until boot has a backend up there is nothing for it to restart, and letting
/// it run alongside the boot thread means two threads stopping, probing and
/// respawning the same children. `Supervisor::is_booted` enforces the same rule
/// underneath — this is the half the tutor can see.
static RESTART_ITEM: OnceLock<MenuItem<Wry>> = OnceLock::new();

/// What `steer_webkit_renderer` decided, logged by `boot` AFTER the launch
/// banner rather than at the moment it happens. The decision has to be made
/// before `main` does anything else at all, and a line that lands above
/// `--- Angel OS … starting ---` reads as part of the PREVIOUS launch — while
/// the first thing anyone debugging a blank window does is find the last banner
/// and read down from there. Empty on macOS, where nothing is decided.
static RENDERER_NOTE: OnceLock<String> = OnceLock::new();

// ---- linux: which renderer WebKitGTK composites through ---------------------

/// WebKitGTK ≥ 2.42 composites through a DMA-BUF renderer, and on the NVIDIA
/// userspace stack the buffer it exports cannot be imported again. The result is
/// the worst shape a failure can take: the web process is ALIVE — scripts run,
/// `fetch` succeeds, the API answers, the native menu bar works — and it paints
/// nothing. What the tutor sees is the GTK window background, which is
/// indistinguishable from "this app is broken", and there is nothing wrong in
/// any log to report, because nothing IS wrong: the boot genuinely succeeded.
///
/// Two properties make this worth a workaround rather than a note in a README.
/// It is undiagnosable from inside the app (there is no error, no exit code, no
/// failed request), and it is unreportable from outside it (a screenshot of a
/// dark rectangle). The tutor cannot get past it and cannot describe it.
///
/// Scoped as narrowly as the evidence allows, because the OPPOSITE mistake is
/// silent too: disabling this on hardware where it works costs compositing and
/// produces no symptom to notice. So it is applied only where the broken stack
/// is actually present, and never over a decision somebody has already made.
#[cfg(target_os = "linux")]
const DMABUF_VAR: &str = "WEBKIT_DISABLE_DMABUF_RENDERER";

/// The file the NVIDIA kernel module creates, and the reason this check is not
/// "is there NVIDIA hardware". Nouveau drives the same cards through a Mesa
/// userspace and is unaffected; it does not create this file. Both the
/// proprietary driver and NVIDIA's open kernel module DO — and they ship the
/// same userspace EGL/GBM stack, which is where the bug actually lives.
#[cfg(target_os = "linux")]
const NVIDIA_PROC: &str = "/proc/driver/nvidia/version";

#[cfg(target_os = "linux")]
enum Dmabuf {
    /// Leave the environment exactly as it is; the reason, for app.log.
    LeaveAlone(&'static str),
    /// Set `DMABUF_VAR=1`; the reason, for app.log.
    Disable(&'static str),
}

/// The decision, as a pure function of the two facts it turns on, so the truth
/// table can be pinned by a test on a machine with no GPU at all.
#[cfg(target_os = "linux")]
fn dmabuf_workaround(preset: Option<&std::ffi::OsStr>, nvidia_userspace: bool) -> Dmabuf {
    // PRESENCE, not value — WebKitGTK itself only tests whether the variable
    // exists, so `=0` and `=` are both already decisions in force. Overriding
    // either would take away the only way to turn the fast path back on when a
    // future driver or WebKit fixes this, and would say nothing about why.
    if preset.is_some() {
        return Dmabuf::LeaveAlone(
            "an explicit setting was already in the environment, and this app does \
             not overrule one",
        );
    }
    if nvidia_userspace {
        return Dmabuf::Disable(
            "the NVIDIA userspace stack is loaded, and WebKitGTK's DMA-BUF renderer \
             paints nothing on it — a live, working, invisible window",
        );
    }
    Dmabuf::LeaveAlone("no NVIDIA userspace stack, so the DMA-BUF renderer is left on")
}

/// Apply that decision. Called as the FIRST statement of `main`: the variable is
/// read when the web content process is launched, and — the reason for the
/// placement rather than merely early — `set_var` is only sound while this
/// program is still single-threaded. Nothing above it has spawned a thread,
/// and GTK and WebKit have not initialised.
#[cfg(target_os = "linux")]
fn steer_webkit_renderer() {
    let preset = std::env::var_os(DMABUF_VAR);
    let nvidia = std::path::Path::new(NVIDIA_PROC).exists();
    let note = match dmabuf_workaround(preset.as_deref(), nvidia) {
        Dmabuf::Disable(why) => {
            std::env::set_var(DMABUF_VAR, "1");
            format!("renderer: {DMABUF_VAR}=1 — {why}")
        }
        Dmabuf::LeaveAlone(why) => format!("renderer: {DMABUF_VAR} untouched — {why}"),
    };
    let _ = RENDERER_NOTE.set(note);
}

// ---- zoom ------------------------------------------------------------------

/// Page zoom, injected into the tutor's window before any page script runs.
///
/// IN THE PAGE RATHER THAN IN RUST, and that is a measured decision, not a
/// shortcut. Tauri's `zoom_hotkeys_enabled` is WINDOWS-ONLY — in wry 0.55 the
/// flag is read by the webview2 backend and by nothing else, so on WebKitGTK
/// and WKWebView (the two this app actually ships) it does nothing at all.
/// `Webview::set_zoom` does work on both, but only Rust can call it, and Rust
/// cannot see a `wheel` event or a trackpad pinch. Routing those back would
/// mean opening IPC to a REMOTE origin — this window loads `http://localhost`,
/// not `tauri://` — which is a capability grant and a much wider blast radius
/// than a zoom control is worth.
///
/// The obvious objection to doing it in CSS is that `zoom` is not browser zoom
/// and would break a viewport-sized layout: the shell is `h-dvh`, and if `dvh`
/// resolved against the UNZOOMED viewport the whole app would overflow its own
/// window at any zoom above 1. That was checked rather than assumed — built,
/// run, and photographed at `zoom: 1.5` in the real WebKitGTK build: the
/// sidebar still ends exactly at the window bottom. WebKit resolves viewport
/// units against the EFFECTIVE zoom, so the objection does not apply. WKWebView
/// is the same engine family.
///
/// PINCH COMES FREE. WebKit delivers a trackpad pinch as a `wheel` event with
/// `ctrlKey` set — the same shape as Ctrl+wheel — so the one listener covers
/// the macOS gesture and the Linux mouse without a line of platform code.
///
/// The level persists in `localStorage`, which is exactly why boot goes to the
/// trouble of REMEMBERING the port pair: localStorage is partitioned by origin
/// including the port, so a stable origin is what lets a zoom set today still
/// be there tomorrow.
const ZOOM_JS: &str = r#"
(function () {
  var KEY = "angelos.zoom";
  // Discrete stops rather than a continuous factor: a trackpad emits dozens of
  // wheel events per gesture, and multiplying by a ratio each time overshoots
  // wildly and lands on values like 1.0700000000000003.
  var STEPS = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  var current = 1;

  function load() {
    try {
      var v = parseFloat(localStorage.getItem(KEY));
      return isFinite(v) && v >= STEPS[0] && v <= STEPS[STEPS.length - 1] ? v : 1;
    } catch (e) { return 1; }  // private mode / storage disabled
  }
  function save() { try { localStorage.setItem(KEY, String(current)); } catch (e) {} }

  // `documentElement` may not exist yet: this runs at document-start, before
  // the parser has produced <html>. Both callers are guarded and the
  // DOMContentLoaded pass below is what actually paints on a cold load.
  function paint() {
    var el = document.documentElement;
    if (!el) return;
    el.style.zoom = current === 1 ? "" : String(current);
  }
  function step(dir) {
    var best = 0, dist = Infinity;
    for (var i = 0; i < STEPS.length; i++) {
      var d = Math.abs(STEPS[i] - current);
      if (d < dist) { dist = d; best = i; }
    }
    var next = best + dir;
    if (next < 0 || next >= STEPS.length) return;   // already at an end stop
    current = STEPS[next]; paint(); save();
  }
  function reset() { current = 1; paint(); save(); }

  current = load();
  paint();
  document.addEventListener("DOMContentLoaded", paint);

  // What the View menu drives. One implementation, two ways in.
  window.__angelZoom = { zoomIn: function () { step(1); },
                         zoomOut: function () { step(-1); },
                         reset: reset };

  // `capture: true` so the app never sees the event first, and
  // `passive: false` because preventDefault on a wheel listener is ignored
  // otherwise — without it the page scrolls WHILE it zooms.
  window.addEventListener("wheel", function (e) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    step(e.deltaY < 0 ? 1 : -1);
  }, { passive: false, capture: true });

  window.addEventListener("keydown", function (e) {
    if (!e.ctrlKey && !e.metaKey) return;
    // "+" and "=" are the same physical key; Shift decides which one arrives,
    // so accepting both is what makes Ctrl+Shift+= and Ctrl++ the same gesture.
    if (e.key === "+" || e.key === "=") { e.preventDefault(); step(1); }
    else if (e.key === "-" || e.key === "_") { e.preventDefault(); step(-1); }
    else if (e.key === "0") { e.preventDefault(); reset(); }
  }, true);
})();
"#;

/// Turn a click on an outbound link into an ordinary navigation, because on
/// WebKitGTK that is the ONLY shape the shell can actually see.
///
/// `on_new_window` below is the obvious home for this and it is not enough —
/// verified by reading wry 0.55's GTK backend after the handler silently never
/// fired. Both routes are closed:
///
///   * `on_new_window` is driven by WebKitGTK's `create` signal, which is only
///     emitted when popups are permitted. wry never calls
///     `set_javascript_can_open_windows_automatically`, so the WebKitGTK
///     default (off) stands and `create` never comes.
///   * `on_navigation` is wired to `decide-policy`, but only for
///     `PolicyDecisionType::NavigationAction`. A `target="_blank"` click raises
///     `NewWindowAction`, and wry's match arm for that is a bare
///     `_ => return false`.
///
/// So the settings page's `<a target="_blank">` to the Anthropic console fell
/// between the two and did nothing whatsoever — which is exactly the bug as the
/// tutor experienced it: a link that says "get a key here" and ignores him.
///
/// Rewriting the click into `location.href` moves it onto `NavigationAction`,
/// which IS routed, where `goes_to_the_browser` sends it to the real browser
/// and returns false — so the navigation is ignored and the cockpit never moves.
/// One path on both platforms, and it lives here in the SHELL rather than in
/// the web app, so the hosted deployment keeps its ordinary `target="_blank"`
/// behaviour where tabs actually exist.
const EXTERNAL_LINKS_JS: &str = r#"
(function () {
  document.addEventListener("click", function (e) {
    // Leave modified clicks alone — they mean something else everywhere else,
    // and a handler that swallows them is worse than no handler.
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var url;
    try { url = new URL(a.href, location.href); } catch (err) { return; }
    // Only the open web. blob:/data:/mailto: and our own origin are somebody
    // else's job — see `goes_to_the_browser`, which decides this again on the
    // Rust side and is the half that actually enforces it.
    if (url.protocol !== "http:" && url.protocol !== "https:") return;
    if (url.hostname === location.hostname) return;
    e.preventDefault();
    location.href = url.href;
  }, true);
})();
"#;

/// Run one of `ZOOM_JS`'s entry points in the tutor's window.
fn zoom_command(handle: &AppHandle, func: &str) {
    if let Some(w) = handle.get_webview_window("main") {
        // Guarded on the JS side too: a menu click can arrive before the page
        // has run the injected script (the window exists first).
        let _ = w.eval(format!("window.__angelZoom && window.__angelZoom.{func}()"));
    }
}

/// Run one of the webview's own history moves (`back`/`forward`) in the
/// tutor's window. Mirrors `zoom_command`, but needs no injected script and no
/// guard: `window.history` exists on every page from the first byte, and a
/// `back()` with nowhere to go is a no-op by spec, not an error. Eval into the
/// remote origin is the same move `reload_main` already makes.
fn history_command(handle: &AppHandle, func: &str) {
    if let Some(w) = handle.get_webview_window("main") {
        let _ = w.eval(format!("window.history.{func}()"));
    }
}

// ---- where a link is allowed to go -----------------------------------------

/// Does this URL belong in the tutor's BROWSER rather than inside the app?
///
/// The app frame is for the app. A link to `console.anthropic.com` opened in it
/// would replace the cockpit with a web page and leave no way back — there is
/// no address bar, no Back button and no tab strip in this window. Worse is
/// what happened before: the settings page marks that link `target="_blank"`,
/// and a webview has no tabs to open, so the click did NOTHING AT ALL. The
/// tutor was told "get a key at console.anthropic.com" and given a link that
/// silently ignored him.
///
/// Deliberately narrow. Only `http`/`https` to a host that is not this machine
/// is treated as the open internet; `blob:`, `data:` and `about:` are how the
/// page does its own work — a DOCX export IS a blob navigation, and sending
/// that to Firefox would break the very feature the save dialog exists for.
///
/// Host, not port. Everything the app links to is either its own origin or the
/// public internet, so pinning the port would buy nothing and would break the
/// moment the port scan rolls to a different pair.
fn goes_to_the_browser(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && !matches!(url.host_str(), Some("localhost") | Some("127.0.0.1") | Some("::1"))
}

/// Hand a URL to whatever the tutor uses to browse, and log it. Never fatal:
/// failing to open a browser is a disappointment, not a reason to lose the app.
fn open_in_browser(handle: &AppHandle, url: &Url) {
    paths::app_log(&format!("opening in the default browser: {url}"));
    if let Err(e) = handle.opener().open_url(url.as_str(), None::<&str>) {
        paths::app_log(&format!("could not open {url} in a browser: {e}"));
    }
}

// ---- downloads -------------------------------------------------------------

/// A download that has been STAGED and not yet given a home.
struct Staged {
    url: String,
    staged_at: PathBuf,
    suggested: OsString,
}

/// THE TWO-STEP EXISTS BECAUSE OF WHICH THREAD WE ARE ON.
///
/// `DownloadEvent::Requested` is where the destination can be chosen, and it is
/// the one place a save dialog would be natural. It is also raised on the GTK
/// main thread, and `blocking_save_file` is documented — in the plugin itself —
/// as "should *NOT* be used when running on the main thread". Asking there is a
/// deadlock, i.e. a frozen app holding a half-finished export.
///
/// So the download is staged into the OS temp directory, and the question is
/// asked on `Finished`, from a thread of our own. The temp directory rather
/// than the data folder is the point of the design: a cancelled export needs no
/// cleanup path of ours, so this feature adds no code anywhere near the tutor's
/// files. Nothing is ever deleted by us — the OS reaps its own temp.
///
/// KEYED BY URL, NOT BY THE REPORTED PATH. `Finished.path` is documented as
/// ALWAYS `None` on macOS, so a design that read it would have worked in every
/// Linux test and shipped broken to the one platform nobody here can try. The
/// URL is always present, and `Requested` is where we chose the path anyway.
static STAGED_DOWNLOADS: Mutex<Vec<Staged>> = Mutex::new(Vec::new());

/// Where to stage `name` while the tutor decides. `<tmp>/angelos-downloads/`,
/// created on demand; the pid keeps two copies of the app from colliding.
fn staging_path(name: &std::ffi::OsStr) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("angelos-downloads-{}", std::process::id()));
    let _ = std::fs::create_dir_all(&dir);
    unique_path(&dir, name)
}

/// Ask where a finished download should live, then put it there.
///
/// `from` is wherever the download actually landed, and it has TWO sources
/// because the two platforms raise different halves of the event — measured,
/// not assumed:
///
///   * WebKitGTK does not raise `Requested` for a `blob:` download at all (the
///     DOCX export is one), so nothing is staged and the file has already been
///     written to the downloads folder. `Finished` carries its path, and that
///     is what we move.
///   * macOS raises `Requested`, so the file is staged by us — and there
///     `Finished.path` is documented as ALWAYS `None`, which is exactly why the
///     staged path is remembered instead of read back off the event.
///
/// A design that trusted either half alone would have worked perfectly on the
/// platform it was written on and done nothing on the other.
///
/// Runs on its own thread: `blocking_save_file` says, in the plugin's own
/// documentation, that it must not run on the main thread, and the download
/// callbacks all arrive there.
fn place_finished_download(handle: AppHandle, from: PathBuf, suggested: OsString) {
    let chosen = handle
        .dialog()
        .file()
        .set_title("Angel OS")
        .set_file_name(suggested.to_string_lossy())
        .set_directory(paths::downloads_dir())
        .blocking_save_file();

    let Some(target) = chosen.and_then(|p| p.into_path().ok()) else {
        // NOT AN ERROR, AND NOTHING IS REMOVED. The file is already written —
        // in the downloads folder on Linux, in the OS temp directory on macOS
        // — so a dismissed dialog costs the tutor an export he asked for
        // nowhere. Deleting it here to be tidy would be the one way this
        // feature could lose work.
        paths::app_log(&format!(
            "the save dialog was dismissed; the download is left where it landed: {}",
            from.display()
        ));
        return;
    };
    if target == from {
        paths::app_log(&format!("saved to {} (already there)", target.display()));
        return;
    }

    // `rename` first: same-filesystem is the ordinary case and it is atomic.
    // A cross-device move (temp on tmpfs, ~/Downloads on disk — the DEFAULT
    // arrangement on most Linux desktops) fails with EXDEV, and a rename-only
    // implementation would have failed there every single time.
    let moved = std::fs::rename(&from, &target).or_else(|_| {
        std::fs::copy(&from, &target).map(|_| {
            let _ = std::fs::remove_file(&from); // the copy we just made ourselves
        })
    });
    match moved {
        Ok(()) => paths::app_log(&format!("saved to {}", target.display())),
        Err(e) => {
            let msg = format!(
                "Δεν ήταν δυνατή η αποθήκευση του αρχείου εκεί.\n\
                 The file could not be saved there.\n\n{e}"
            );
            paths::app_log(&format!("FAILED to save to {}: {e}", target.display()));
            notice(&handle, &msg);
        }
    }
}

fn main() {
    // FIRST — see `steer_webkit_renderer`: still single-threaded, and before
    // anything can initialise GTK or WebKit.
    #[cfg(target_os = "linux")]
    steer_webkit_renderer();
    install_panic_logger();
    tauri::Builder::default()
        // Must be the FIRST plugin: a second launch focuses the running window.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            for label in ["main", "splash"] {
                if let Some(w) = app.get_webview_window(label) {
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                    break;
                }
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            build_menu(app)?;
            WebviewWindowBuilder::new(app, "splash", WebviewUrl::App("splash.html".into()))
                .title("Angel OS")
                .inner_size(460.0, 320.0)
                .resizable(false)
                .maximizable(false)
                .center()
                .build()?;
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // Every DELIBERATE failure inside `boot` goes through `fatal()`
                // — log, dialog, Show Logs, exit. A panic went through none of
                // it: the thread unwound silently, the splash sat there
                // "checking" forever, nothing reached app.log, and there was no
                // button to press. That is precisely the failure shape round 3
                // existed to eliminate, so a panic has to land in the same
                // place as everything else.
                let reported = handle.clone();
                if let Some(why) = run_guarded(|| boot(handle)) {
                    fatal(&reported, &internal_error_message(&why));
                }
            });
            // SIGTERM/SIGINT must run the same ordered teardown as Quit —
            // otherwise a logout or `kill` orphans postgres/api/node, which
            // then squat the ports and push the next launch further up the
            // scan (or, with the range full, block it outright).
            #[cfg(unix)]
            {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    use signal_hook::consts::{SIGINT, SIGTERM};
                    use signal_hook::iterator::Signals;
                    let mut signals =
                        Signals::new([SIGTERM, SIGINT]).expect("install signal handler");
                    if signals.forever().next().is_some() {
                        handle.exit(0); // -> RunEvent::ExitRequested -> sup.shutdown()
                    }
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Angel OS")
        .run(|_handle, event| {
            // Fires on Cmd+Q, last-window-close and app.exit() alike — which
            // means every deliberate exit path, `fatal()` included, lands here.
            // Ordered teardown: node → uvicorn → pg_ctl stop -m fast
            // (idempotent), and only THEN the instance lock. Releasing it
            // earlier would let a relaunch in while our postgres is still
            // stopping, which is the exact collision the guard exists to stop.
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(sup) = SUPERVISOR.get() {
                    sup.shutdown();
                }
                instance::release();
            }
        });
}

// ---- boot sequence ---------------------------------------------------------

fn boot(handle: AppHandle) {
    set_status(&handle, "checking");

    let resource_dir = match handle.path().resource_dir() {
        Ok(d) => d,
        Err(e) => return fatal(&handle, &format!("resource dir unavailable: {e}")),
    };
    let res = paths::resource_root(&resource_dir);

    let dirs = match paths::ensure_dirs() {
        Ok(d) => d,
        Err(e) => {
            return fatal(
                &handle,
                &format!(
                    "Δεν ήταν δυνατή η δημιουργία του φακέλου δεδομένων.\n\
                     Could not create the data folder.\n\n{e}"
                ),
            )
        }
    };

    paths::app_log(&format!(
        "--- Angel OS {} starting (pid {}) ---",
        env!("CARGO_PKG_VERSION"),
        std::process::id()
    ));
    // Immediately under the banner, and on every launch — not only when the
    // workaround fires. "Is the blank-window workaround active on this machine"
    // is the first question a blank window raises, and an answer that only
    // appears in one of the two cases cannot distinguish "not applied" from
    // "this build is too old to know about it".
    if let Some(note) = RENDERER_NOTE.get() {
        paths::app_log(note);
    }

    // BEFORE secrets, before any scan, before anything touches the cluster: is
    // this the only copy, and did the last one actually die? Rolling ports
    // removed the frozen-port preflight that used to catch both by accident —
    // a second launch would now roll happily past its own orphaned postgres and
    // die later inside pg_ctl with raw English lock-file output.
    if let Err(blocked) = instance::acquire(&res, &dirs) {
        return fatal(&handle, &blocked.message());
    }

    // The two ways this fails are NOT the same failure and must not read alike.
    // Creating secrets.json is a WRITE, so on a full or read-only data folder it
    // used to hand the tutor a raw English `No space left on device` — the only
    // write-failure path in boot that did not go through the bilingual
    // "free up disk space" dialog every other one uses. An EXISTING file that
    // cannot be read or parsed is the opposite advice: freeing space fixes
    // nothing, and the file must be restored rather than made room for (the app
    // refuses to regenerate it — it protects the stored Anthropic key).
    let secrets = match firstrun::load_or_create_secrets(&dirs.secrets) {
        Ok(s) => s,
        Err(firstrun::SecretsError::NotWritable(e)) => {
            return fatal(&handle, &data_folder_unwritable(&e))
        }
        Err(firstrun::SecretsError::Existing(e)) => return fatal(&handle, &secrets_unreadable(&e)),
    };

    // Picked here because postgres is started a few lines below — the window
    // between probe and bind stays short. The app ports are picked much later,
    // for the same reason.
    let pg_port = match firstrun::pick_pg_port() {
        Some(p) => p,
        None => {
            return fatal(
                &handle,
                "Δεν βρέθηκε ελεύθερη θύρα δικτύου για τη βάση δεδομένων του \
                 Angel OS: άλλα προγράμματα στον υπολογιστή κρατούν ήδη τις \
                 θύρες που χρειάζεται.\n\
                 Κλείστε τα άλλα προγράμματα — ή κάντε επανεκκίνηση του \
                 υπολογιστή — και ξεκινήστε ξανά το Angel OS.\n\n\
                 Angel OS could not find a free network port for its database: \
                 other programs on this computer are already holding the ports it \
                 needs.\n\
                 Close the other programs — or restart the computer — and start \
                 Angel OS again.",
            )
        }
    };

    // What the DISK says. NOT `!cluster_exists()` any more: PG_VERSION appears
    // seconds into a first run, while the seed restore that puts the tutor's
    // library there takes minutes — treating the former as "installed" is how
    // an interrupted first run turned into a permanently empty app. See
    // `firstrun`'s state-machine comment.
    let mut disk = firstrun::disk_state(&dirs);
    let sup = Arc::new(Supervisor::new(res.clone(), dirs.clone(), secrets, pg_port));
    let _ = SUPERVISOR.set(sup.clone());

    // EVERYTHING this boot moves out of the way, from here to the window.
    //
    // Declared this early because displacement starts this early: `init_cluster`
    // below can set a pgdata directory — and with it the tutor's whole media
    // tree — aside before the marker has even been consulted about seeding.
    // Every call that can displace anything hands back a `firstrun::Displaced`,
    // which is `#[must_use]`, so the only way past the compiler is to absorb it
    // here; and every `fatal` from here on carries whatever is in it (see
    // `and_what_was_set_aside`). The one-time dialog at the end names the union
    // of this and whatever the marker says was never announced.
    let mut set_aside = firstrun::Displaced::nothing();

    if disk == DiskState::NoCluster {
        set_status(&handle, "firstrun");
        // BEFORE the marker, and before initdb: is `pgdata` actually free to
        // build in? "No PG_VERSION" is how a first run looks, and it is ALSO
        // how a real cluster that lost one three-byte file looks. Asking first
        // means a refusal changes nothing at all — not even the recorded phase
        // — so whoever helps him is looking at an install in exactly the state
        // the problem left it in.
        if let Err(e) = firstrun::pgdata_is_safe_to_build_in(&dirs) {
            return fatal(&handle, &e);
        }
        // The marker goes down BEFORE initdb, and that ordering is the whole
        // invariant: from the instant a cluster starts being created until the
        // instant the install is complete, the marker reads `seeding`. Write it
        // afterwards instead and there is a window — small, but the one that
        // costs a library — where PG_VERSION exists while the marker still says
        // `complete` from some earlier install whose pgdata has since been
        // removed. An interruption there would leave a cluster that looks
        // finished and is empty.
        if let Err(e) = firstrun::mark_seeding(&dirs) {
            return fatal(&handle, &data_folder_unwritable(&e));
        }
        match firstrun::init_cluster(&res, &dirs, pg_port, false) {
            Ok(displaced) => set_aside.absorb(displaced),
            Err(e) => {
                return fatal(
                    &handle,
                    &and_what_was_set_aside(&dirs, &set_aside, &format!("initdb failed:\n{e}")),
                )
            }
        }
    }

    set_status(&handle, "db");
    if let Err(first) = sup.start_postgres() {
        // THE WINDOW THIS CLOSES: `initdb` writes PG_VERSION EARLY — before it
        // bootstraps the catalogs, seconds into a run that takes much longer.
        // Interrupt a first run just after that and the disk holds a directory
        // that `cluster_exists` calls a cluster and that postgres cannot start.
        // `init_cluster` was reachable only from `DiskState::NoCluster`, so that
        // half-built cluster was never rebuilt: EVERY future launch failed here,
        // identically, forever. An install that can never boot again is a total
        // loss of the tutor's library by a slower route than deleting it.
        //
        // Only `SeedInterrupted` may take this path, and that condition is the
        // whole argument: the marker — now written durably — says this install
        // was part-way through being CREATED and that no boot has ever finished
        // on it. `Unmarked` deliberately does NOT qualify; a pre-marker install
        // whose cluster is merely damaged is very often repairable by somebody
        // who knows how, and never repairable after we have replaced it.
        //
        // And even here nothing is destroyed: the old pgdata is RENAMED aside,
        // so if the marker were somehow wrong the cluster is still sitting in
        // the data folder under another name.
        if disk != DiskState::SeedInterrupted {
            return fatal(
                &handle,
                &and_what_was_set_aside(
                    &dirs,
                    &set_aside,
                    &format!("Η βάση δεδομένων δεν ξεκίνησε.\nThe database did not start.\n\n{first}"),
                ),
            );
        }
        paths::app_log(&format!(
            "postgres would not start ({first}), and the install marker says this \
             first run never finished — so this is a cluster whose initdb was \
             interrupted. Moving it aside and building a new one."
        ));
        set_status(&handle, "firstrun");
        // Idempotent, and not decoration: `pg_ctl -w start` can report failure
        // after a postmaster has come up far enough to hold the directory open.
        // Renaming pgdata out from under a live postmaster is the one way this
        // recovery could itself cause damage, so it is closed off first.
        sup.stop_postgres();
        // The receipt is absorbed, not discarded. This path moves the cluster
        // AND — because a pgdata directory is every database at once — the whole
        // media tree, and it used to say nothing about either: the tutor got a
        // starter library and no explanation anywhere he would look.
        match firstrun::set_aside_pgdata(
            &res,
            &dirs,
            "a first run whose initdb did not finish: postgres could not start from \
             this directory",
        ) {
            Ok(displaced) => set_aside.absorb(displaced),
            Err(e) => {
                return fatal(
                    &handle,
                    &and_what_was_set_aside(&dirs, &set_aside, &data_folder_unwritable(&e)),
                )
            }
        }
        match firstrun::init_cluster(&res, &dirs, pg_port, false) {
            Ok(displaced) => set_aside.absorb(displaced),
            Err(e) => {
                return fatal(
                    &handle,
                    &and_what_was_set_aside(&dirs, &set_aside, &format!("initdb failed:\n{e}")),
                )
            }
        }
        if let Err(second) = sup.start_postgres() {
            return fatal(
                &handle,
                &and_what_was_set_aside(
                    &dirs,
                    &set_aside,
                    &format!(
                        "Η βάση δεδομένων δεν ξεκίνησε.\nThe database did not start.\n\n\
                         {first}\n\nand after rebuilding it:\n{second}"
                    ),
                ),
            );
        }
        // The cluster is brand new. It needs the Greek-collation guard and a
        // full seed, exactly like day one — which is what `NoCluster` means to
        // everything below.
        disk = DiskState::NoCluster;
        paths::app_log("the rebuilt cluster started; continuing as a fresh first run");
    }

    if disk == DiskState::NoCluster {
        // Greek collation guard, right after first initdb+start. Retry with ICU
        // (el) when the bundled build supports it; otherwise this install
        // cannot search Greek and must say so instead of limping.
        if !firstrun::greek_collation_ok(&res, &dirs, pg_port) {
            sup.stop_postgres();
            // `set_aside_pgdata` rather than a wipe. This particular cluster was
            // created by this thread a few seconds ago and contains nothing but
            // an initdb skeleton, so a delete would be defensible — and "this
            // one is definitely worthless" is exactly the reasoning that must
            // stop being load-bearing anywhere near the tutor's data directory.
            // A rename costs one inode operation.
            // Spelled out rather than an `.is_ok()` chain: both steps now hand
            // back a receipt for whatever they moved, and `.is_ok()` is exactly
            // the shape that throws one away. Short-circuiting is preserved.
            let recovered = firstrun::initdb_supports_icu(&res, &dirs)
                && match firstrun::set_aside_pgdata(
                    &res,
                    &dirs,
                    "a first cluster built with a collation that cannot fold Greek",
                ) {
                    Ok(displaced) => {
                        set_aside.absorb(displaced);
                        true
                    }
                    Err(e) => {
                        paths::app_log(&format!("could not set the first cluster aside: {e}"));
                        false
                    }
                }
                && match firstrun::init_cluster(&res, &dirs, pg_port, true) {
                    Ok(displaced) => {
                        set_aside.absorb(displaced);
                        true
                    }
                    Err(e) => {
                        paths::app_log(&format!("the ICU retry's initdb failed: {e}"));
                        false
                    }
                }
                && sup.start_postgres().is_ok()
                && firstrun::greek_collation_ok(&res, &dirs, pg_port);
            if !recovered {
                return fatal(
                    &handle,
                    &and_what_was_set_aside(
                        &dirs,
                        &set_aside,
                        "Αυτό το σύστημα δεν υποστηρίζει ελληνική ταξινόμηση κειμένου \
                         (collation) — η αναζήτηση στα ελληνικά δεν θα λειτουργούσε.\n\
                         This system lacks a Greek-capable text collation — Greek \
                         search would not work. Please report this.",
                    ),
                );
            }
        }
    }

    // The cluster is up, so the one question the disk cannot answer — is an
    // unmarked cluster a complete install from an older build, or a first run
    // that died before it got going? — can finally be put to the cluster itself.
    let plan = firstrun::plan_first_run(&res, &dirs, pg_port, disk);
    paths::app_log(&format!("install: disk={disk:?} → {plan:?}"));
    // WHAT THE SEED STEP DID — the only thing `finalize_install` cannot work
    // out from the plan. Every plan finalizes; this just decides the wording.
    let mut outcome = SeedOutcome::NotAttempted;
    match plan {
        // The ordinary launch; an install older than the marker (its data is
        // already there, so there is nothing to do but record it, which
        // `finalize_install` does below); and the "could not ask" case. All
        // three touch the database not at all.
        SeedPlan::Skip | SeedPlan::Adopt | SeedPlan::Undecided => {}
        SeedPlan::Seed | SeedPlan::Reseed => {
            set_status(&handle, "seeding");
            // Claimed BEFORE anything is written to the database, and fatal.
            // An unrecorded restore that then gets interrupted looks, next
            // launch, exactly like a finished install — a half-filled library
            // with nothing to say it is half-filled. Better to stop here, where
            // nothing has been touched yet, than to leave that behind.
            if let Err(e) = firstrun::mark_seeding(&dirs) {
                return fatal(
                    &handle,
                    &and_what_was_set_aside(&dirs, &set_aside, &data_folder_unwritable(&e)),
                );
            }
            if plan == SeedPlan::Reseed {
                paths::app_log(
                    "a previous first run was interrupted part-way through installing \
                     the starter library — setting aside what it left behind and doing \
                     it again from scratch",
                );
                // NOT a drop. `displace_database` renames the database out of
                // the way whenever it holds anything at all, and drops it only
                // when it has just measured zero user relations in it. That
                // turns this — the one cell of the truth table that could ever
                // cost the tutor anything — from destruction into displacement.
                //
                // AND IT MOVES `<data>/media` WITH IT. The rows and the uploaded
                // PDFs they describe are one thing: leave the media behind and
                // the API child's startup sweep, minutes later on this same
                // boot, finds every book he owns with no row behind it. That is
                // what defeated displacement across the layer boundary, and it
                // is why this is one call and not two.
                // THE RECEIPT COMES BACK WHETHER OR NOT THE RENAME WORKED, and
                // it is absorbed BEFORE the error is looked at. By the time
                // `displace_database` can fail, the media has already been moved
                // — deliberately, media first, so a crash between the two
                // renames leaves the survivable half-state — and the old
                // `Result<Displaced, _>` threw that receipt away on exactly that
                // path. The dialog then said "could not set the database aside"
                // to a tutor whose entire library had silently moved, while
                // app.log, the marker and the READ-ME all named it.
                let (displaced, renamed) = firstrun::displace_database(&res, &dirs, pg_port);
                set_aside.absorb(displaced);
                if let Err(e) = renamed {
                    return fatal(
                        &handle,
                        &and_what_was_set_aside(
                            &dirs,
                            &set_aside,
                            &format!(
                                "Δεν ήταν δυνατή η τακτοποίηση της ημιτελούς βάσης \
                                 δεδομένων. Δεν διαγράφηκε τίποτα.\n\
                                 Angel OS could not set the half-installed database \
                                 aside. Nothing was deleted.\n\n{e}"
                            ),
                        ),
                    );
                }
            }
            match firstrun::create_and_seed_db(&res, &dirs, pg_port) {
                Ok((did, displaced)) => {
                    outcome = did;
                    set_aside.absorb(displaced);
                }
                Err(e) => {
                    return fatal(
                        &handle,
                        &and_what_was_set_aside(
                            &dirs,
                            &set_aside,
                            &format!("seed restore failed:\n{e}"),
                        ),
                    )
                }
            }
        }
    }

    set_status(&handle, "migrate");
    if let Err(e) = sup.run_migrations() {
        return fatal(
            &handle,
            &and_what_was_set_aside(
                &dirs,
                &set_aside,
                &format!("Η ενημέρωση της βάσης απέτυχε.\nDatabase migration failed.\n\n{e}"),
            ),
        );
    }

    // ONLY here is a boot finished: the library is in place (or was already,
    // or was deliberately left alone) AND the schema is current. EVERY plan
    // comes through this one call — `Skip` and `Undecided` included — because
    // the invariant that protects the tutor's library is not "the seed
    // finished" but "`seeding` implies nobody has ever used this install".
    // `Undecided` used to slip past: a boot could complete with the marker
    // still reading `seeding`, and the NEXT launch would read that as an
    // interrupted restore and drop a database by then full of his curricula.
    //
    // Fatal — unlike meta.json below — for exactly that reason. Note that
    // `finalize_install` only fails when the marker does not READ as complete
    // afterwards, so an established install whose disk is full still boots: the
    // marker already says complete, and the failed refresh is logged, not
    // fatal.
    //
    // The receipt is not decoration. `show_main_window` takes one, so there is
    // no path from here to a visible window that skips this call, and the match
    // inside it is exhaustive over `SeedPlan` — a variant added later cannot
    // inherit "do nothing" by default.
    let installed: InstallComplete = match firstrun::finalize_install(&dirs, plan, outcome) {
        Ok(receipt) => receipt,
        Err(e) => {
            return fatal(
                &handle,
                &and_what_was_set_aside(&dirs, &set_aside, &data_folder_unwritable(&e)),
            )
        }
    };

    // Housekeeping, and ONLY from an install that has just been proven complete
    // — `reap_superseded` takes the receipt by reference, so the compiler
    // enforces that. Anything still set aside from a boot that never finished
    // stays exactly where it is. The policy (keep the 3 most recent of each
    // kind; nothing under 30 days old is ever touched) is spelled out and
    // argued in `firstrun`'s reaping section.
    firstrun::reap_superseded(&res, &dirs, pg_port, &installed);

    // LAST possible moment: the very next thing that happens is the uvicorn
    // spawn that binds one of these, and the node spawn that binds the other.
    // Everything expensive is already behind us.
    let remembered = firstrun::remembered_app_ports(&dirs);
    let ports = match firstrun::pick_app_ports(pg_port, remembered) {
        Some(p) => p,
        None => {
            return fatal(
                &handle,
                &and_what_was_set_aside(
                    &dirs,
                    &set_aside,
                // No port range in here on purpose: "8790–8829" is not
                // something the tutor can act on. What he can act on is
                // closing programs or restarting.
                "Δεν βρέθηκε ελεύθερη θύρα δικτύου για το Angel OS: άλλα \
                 προγράμματα στον υπολογιστή κρατούν ήδη τις θύρες που \
                 χρειάζεται.\n\
                 Κλείστε τα άλλα προγράμματα — ή κάντε επανεκκίνηση του \
                 υπολογιστή — και ξεκινήστε ξανά το Angel OS.\n\n\
                 Angel OS could not find a free network port: other programs \
                 on this computer are already holding the ports it needs.\n\
                 Close the other programs — or restart the computer — and start \
                 Angel OS again.",
                ),
            )
        }
    };
    sup.set_ports(ports);
    // Into app.log, not just stderr: a .app launched from Finder has nowhere to
    // print, and "which ports did it pick today" is the first question anyone
    // helping the tutor over the phone has to answer.
    paths::app_log(&format!(
        "ports: web={} api={} postgres={} ({})",
        ports.web,
        ports.api,
        pg_port,
        if remembered == Some(ports) {
            "kept from the last launch — the browser origin, and everything the \
             web app stores under it, stays put"
        } else if ports.are_defaults() {
            "the usual pair"
        } else {
            "the usual pair was taken, so these were chosen instead; the page is \
             told the API base explicitly"
        }
    ));
    // Remembering them is what keeps the origin — and therefore localStorage /
    // IndexedDB, which are partitioned by origin INCLUDING the port — stable
    // across launches. NOT fatal: this file is a convenience, and the worst a
    // failed write can do is make the next launch scan for ports instead of
    // reusing today's pair. It used to be fatal, which was defensible while it
    // was written once on the first run and stopped being defensible the moment
    // it moved to every boot: a full disk or a read-only data directory would
    // then turn a WORKING install into one that refuses to launch.
    if let Err(e) = firstrun::write_meta(&dirs, pg_port, ports) {
        paths::app_log(&format!(
            "could not write meta.json ({e}) — continuing; this launch works \
             normally, but the next one may choose a different port pair"
        ));
    }

    let died = {
        let h = handle.clone();
        move || backend_died(h.clone())
    };
    set_status(&handle, "api");
    if let Err(e) = sup.start_api(died.clone()) {
        return fatal(&handle, &and_what_was_set_aside(&dirs, &set_aside, &e));
    }
    set_status(&handle, "web");
    if let Err(e) = sup.start_node(died.clone()) {
        return fatal(&handle, &and_what_was_set_aside(&dirs, &set_aside, &e));
    }
    sup.watch_postgres(died);

    set_status(&handle, "waiting");
    match sup.await_ready(Duration::from_secs(180)) {
        Ready::Yes => {
            // Boot is over: from here the children belong to the supervisor, not
            // to this thread, and Restart Backend may replace them. Both halves
            // of the gate flip together.
            sup.mark_booted();
            enable_restart_item();
            show_main_window(&handle, &installed);
            // AFTER the window, not before: this is news, not an obstacle, and
            // a modal in front of a splash reads as a failure. Said out loud
            // because a database renamed where nobody looks is worth no more
            // than a database deleted — the dialog names it and points at the
            // file in the data folder that explains how to get it back.
            //
            // THE UNION OF TWO SOURCES, and both are needed:
            //   * `set_aside` — what THIS boot moved. Complete even when the
            //     marker could not be written (`record_set_aside` refuses to
            //     invent a phase in a marker it cannot read).
            //   * `unannounced` — what the MARKER says was never announced.
            //     A boot that displaces something and then dies never reaches
            //     this line, and the in-process receipt dies with the process;
            //     the record on disk does not. This is how the tutor still
            //     hears about a displacement made by a boot that ended in a
            //     fatal dialog days ago.
            // `absorb` de-duplicates, so the ordinary case — both sources
            // holding the same entries — names each folder once.
            //
            // AND A THIRD, which is not this process's own doing at all. The
            // API child's startup sweep sets aside every media directory the
            // database it connected to has no row for, into
            // `<data>/media/_superseded/<stamp>/` — after a database that was
            // replaced or renamed, that is the tutor's whole library — and it
            // explains itself only inside that folder. This is the first moment
            // it can be seen: the backend has answered, so its lifespan (and
            // therefore that sweep) has run. Describing it here puts it in the
            // one file support actually asks for, and in this dialog.
            let mut news = firstrun::describe_api_quarantine(&res, &dirs);
            news.absorb(firstrun::unannounced(&dirs));
            news.absorb(set_aside);
            if news.anything_set_aside() {
                notice(&handle, &firstrun::set_aside_notice(&dirs, &news));
                // AFTER the dialog was dismissed, never before: a crash while
                // it is on screen must re-announce on the next launch rather
                // than swallow the one message that explains where his library
                // went.
                firstrun::mark_announced(&dirs);
            }
        }
        Ready::No(why) => fatal(
            &handle,
            &and_what_was_set_aside(
                &dirs,
                &set_aside,
                &format!(
                    "Το backend δεν ξεκίνησε μέσα σε 3 λεπτά.\n\
                     The backend did not become ready within 3 minutes.\n\n{why}"
                ),
            ),
        ),
    }
}

/// A failure message with the displacement notice appended, when anything of
/// the tutor's has been moved aside and not yet said out loud.
///
/// A boot that dies after a displacement never reaches the one-time dialog, and
/// "Angel OS could not start" plus a library that has silently moved is
/// exactly the experience this round exists to end. The record is durable either
/// way — the next boot that survives drains it — but the next boot may be days
/// away, and the person reading THIS dialog is the one who can act now.
///
/// THE UNION OF THE SAME TWO SOURCES the success path uses, and that is the
/// correction. It used to name only the in-process receipt, which is empty in
/// precisely the window it matters most: `displace_database` moves the media
/// FIRST, on purpose, and a failed `ALTER DATABASE` after it is fatal. The
/// durable record was written the instant those files moved — before anything
/// else was allowed to fail — so reading the marker here is what lets this
/// dialog say where his books went. (The receipt is still folded in: it is
/// complete even when the marker could not be written at all.)
///
/// Nothing is marked announced from here. A fatal dialog is not proof he read
/// it, and being told once more on a launch that works is the harmless side.
fn and_what_was_set_aside(dirs: &Dirs, set_aside: &firstrun::Displaced, msg: &str) -> String {
    let mut all = firstrun::unannounced(dirs);
    all.absorb(set_aside.clone());
    if !all.anything_set_aside() {
        return msg.to_string();
    }
    format!(
        "{msg}\n\n------------------------------------------------------------\n\n{}",
        firstrun::set_aside_notice(dirs, &all)
    )
}

/// The data folder could not be written at a point where continuing would risk
/// the tutor's library. Greek first, then English, and no path or errno in the
/// part he reads — `fatal` appends the log directory anyway.
fn data_folder_unwritable(detail: &str) -> String {
    format!(
        "Δεν ήταν δυνατή η εγγραφή στον φάκελο δεδομένων, οπότε η εγκατάσταση δεν \
         μπορεί να συνεχίσει με ασφάλεια.\n\
         Ελευθερώστε χώρο στον δίσκο και ξεκινήστε ξανά το Angel OS.\n\n\
         The data folder could not be written to, so the installation cannot \
         safely continue.\n\
         Free up disk space and start Angel OS again.\n\n{detail}"
    )
}

/// `secrets.json` is there and unusable. Deliberately NOT
/// `data_folder_unwritable`: telling the tutor to free up disk space here would
/// send him to do something that cannot possibly help, and the app will not
/// regenerate the file (losing `ENCRYPTION_SECRET` bricks his stored Anthropic
/// key), so the only real action is to put the file back.
fn secrets_unreadable(detail: &str) -> String {
    format!(
        "Ένα αρχείο ρυθμίσεων του Angel OS υπάρχει αλλά δεν διαβάζεται. Το \
         Angel OS ΔΕΝ το αντικατέστησε — προστατεύει το αποθηκευμένο κλειδί \
         σας.\n\
         Μην διαγράψετε τίποτα: δείξτε αυτό το μήνυμα σε όποιον σας υποστηρίζει.\n\n\
         A Angel OS settings file exists but cannot be read. Nothing was \
         overwritten — that file protects the stored Anthropic key, so it is \
         restored, never regenerated.\n\n{detail}"
    )
}

fn enable_restart_item() {
    if let Some(item) = RESTART_ITEM.get() {
        if let Err(e) = item.set_enabled(true) {
            // Not fatal: `Supervisor::is_booted` is the real gate, and a menu
            // item that stays greyed out is a nuisance, not a failure.
            paths::app_log(&format!("could not enable the Restart Backend item: {e}"));
        }
    }
}

// ---- windows ---------------------------------------------------------------

/// The ONE place the tutor's window comes into existence.
///
/// `_installed` is never read, and that is the entire point of it: the only way
/// to obtain an `InstallComplete` is `firstrun::finalize_install`, which mints
/// one solely when `<data>/install-state.json` READS BACK as `complete`. So the
/// compiler — not a comment, not a code review — is what guarantees that no
/// boot can put a window on screen while the marker still says `seeding`. That
/// marker is what the next launch consults before deciding it is entitled to
/// DROP the database, so the two facts must never be able to drift apart.
fn show_main_window(handle: &AppHandle, _installed: &InstallComplete) {
    // The SAME source the spawns read, not a copy threaded down from boot:
    // "the URL the window loads" and "the port node was told to bind" must be
    // the same fact, and there is exactly one place that fact lives.
    let Some(ports) = SUPERVISOR.get().and_then(|sup| sup.ports()) else {
        return;
    };
    let h = handle.clone();
    // Window creation belongs on the main thread (macOS requires it).
    let _ = handle.run_on_main_thread(move || {
        // EXACTLY http://localhost:<web> — literal `localhost`, load-bearing:
        // the session cookie is host-only, and 127.0.0.1 would be a different
        // host to the browser. Only the port is derived.
        let url: tauri::Url = ports.web_origin().parse().expect("derived localhost url");
        // Tell the page where the API actually landed, BEFORE any page script
        // runs — with the ports rolling, deriving :api from the origin is no
        // longer possible. This is the shared contract: a plain string global
        // named exactly `__GT_API_BASE__`, no trailing slash. serde_json emits
        // a properly escaped JS string literal (naive quoting would not).
        // Main frame only, which is all this app has.
        // Two globals and a zoom engine, before any page script runs. `{}` is
        // not a format placeholder in `ZOOM_JS` — it is substituted as a value,
        // so its braces need no escaping.
        let init = format!(
            "window.__GT_API_BASE__ = {};\n{}\n{}",
            serde_json::to_string(&ports.api_base()).expect("serialize api base"),
            ZOOM_JS,
            EXTERNAL_LINKS_JS
        );
        let nav_handle = h.clone();
        let win_handle = h.clone();
        let dl_handle = h.clone();
        let built = WebviewWindowBuilder::new(&h, "main", WebviewUrl::External(url))
            .initialization_script(init)
            .title("Angel OS")
            .inner_size(1360.0, 900.0)
            .min_inner_size(980.0, 640.0)
            // A same-tab click onto the open internet. Returning false stops
            // the app frame from being replaced by a web page it has no way
            // back from — there is no Back button in this window.
            .on_navigation(move |url| {
                if !goes_to_the_browser(url) {
                    return true;
                }
                open_in_browser(&nav_handle, url);
                false
            })
            // `target="_blank"` — which is what the settings page uses for the
            // Anthropic console link, and which did NOTHING before this: a
            // webview has no tabs, so the click was swallowed in silence.
            .on_new_window(move |url, _features| {
                if goes_to_the_browser(&url) {
                    open_in_browser(&win_handle, &url);
                }
                // Denied either way. Even a local URL must not open a second,
                // chromeless window with no menu and no supervisor behind it.
                NewWindowResponse::Deny
            })
            .on_download(move |_webview, event| {
                // Unconditional, and it earns its place: when this handler does
                // not fire there is NOTHING anywhere to distinguish "the webview
                // never raised a download" from "we mishandled one". That
                // ambiguity cost a long debugging session once already.
                paths::app_log(&format!(
                    "download event: {}",
                    match &event {
                        DownloadEvent::Requested { url, .. } => format!("requested {url}"),
                        DownloadEvent::Finished { url, success, .. } =>
                            format!("finished {url} (success={success})"),
                        _ => "other".to_string(),
                    }
                ));
                match event {
                    // Stage it. We cannot ask here — this is the GTK main
                    // thread and the dialog is documented as unusable on it.
                    DownloadEvent::Requested { url, destination } => {
                        let suggested = destination
                            .file_name()
                            .map(|n| n.to_os_string())
                            .unwrap_or_else(|| "download".into());
                        *destination = staging_path(&suggested);
                        if let Ok(mut q) = STAGED_DOWNLOADS.lock() {
                            q.push(Staged {
                                url: url.to_string(),
                                staged_at: destination.clone(),
                                suggested,
                            });
                        }
                    }
                    // Now we may ask, on a thread of our own.
                    DownloadEvent::Finished { url, path, success } => {
                        let staged = STAGED_DOWNLOADS.lock().ok().and_then(|mut q| {
                            // The FIRST match, not a keyed lookup: the backup
                            // URL never varies, so two exports in a row share a
                            // key and a map would lose one of them.
                            let i = q.iter().position(|s| s.url == url.as_str())?;
                            Some(q.remove(i))
                        });
                        if !success {
                            paths::app_log(&format!("download failed: {url}"));
                            return true;
                        }
                        // Whichever half this platform gave us. See
                        // `place_finished_download` — WebKitGTK skips
                        // `Requested` entirely for blob downloads, macOS never
                        // reports a path.
                        let landed = match staged {
                            Some(s) => Some((s.staged_at, s.suggested)),
                            None => path.map(|p| {
                                let name = p
                                    .file_name()
                                    .map(|n| n.to_os_string())
                                    .unwrap_or_else(|| "download".into());
                                (p, name)
                            }),
                        };
                        match landed {
                            Some((from, suggested)) => {
                                let h = dl_handle.clone();
                                std::thread::spawn(move || {
                                    place_finished_download(h, from, suggested)
                                });
                            }
                            // Neither half. Nothing is lost — the file is
                            // wherever the webview put it — but we cannot offer
                            // to move something we cannot name, and saying so
                            // beats a dialog that would move the wrong file.
                            None => paths::app_log(&format!(
                                "download finished but neither a staged path nor a \
                                 reported one is available, so it was left where the \
                                 webview placed it: {url}"
                            )),
                        }
                    }
                    _ => {}
                }
                true
            })
            .build();
        match built {
            Ok(_) => {
                if let Some(splash) = h.get_webview_window("splash") {
                    let _ = splash.close();
                }
            }
            Err(e) => {
                let h2 = h.clone();
                let msg = format!("could not create the main window: {e}");
                std::thread::spawn(move || fatal(&h2, &msg));
            }
        }
    });
}

/// `name` in `dir`, appending " (1)", " (2)"… before the last extension when
/// the file already exists.
fn unique_path(dir: &std::path::Path, name: &std::ffi::OsStr) -> PathBuf {
    let candidate = dir.join(name);
    if !candidate.exists() {
        return candidate;
    }
    let stem = candidate
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "download".into());
    let ext = candidate
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy()))
        .unwrap_or_default();
    for i in 1.. {
        let next = dir.join(format!("{stem} ({i}){ext}"));
        if !next.exists() {
            return next;
        }
    }
    unreachable!()
}

fn set_status(handle: &AppHandle, key: &str) {
    if let Some(splash) = handle.get_webview_window("splash") {
        let _ = splash.eval(format!("window.setStatus && window.setStatus('{key}')"));
    }
}

// ---- menu ------------------------------------------------------------------

fn build_menu(app: &mut tauri::App) -> tauri::Result<()> {
    let handle = app.handle();
    // On macOS the first submenu becomes the application menu. The Edit roles
    // are REQUIRED: without them Cmd+C/Cmd+V do nothing in the webview and the
    // tutor cannot paste an API key.
    let app_menu = SubmenuBuilder::new(handle, "Angel OS")
        .about(None)
        .separator()
        .quit()
        .build()?;
    let edit = SubmenuBuilder::new(handle, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;
    // ZOOM IS NOT ONLY DISCOVERABILITY HERE — IT IS THE KEYBOARD.
    //
    // The injected listener in `ZOOM_JS` already handles Ctrl/Cmd +/-/0 while
    // the page has focus, and these items would then be a duplicate. They are
    // not: focus is not always in the page (the menu bar, a native dialog, a
    // window that has just opened), and a tutor who does not know the shortcut
    // exists will never press it. An accelerator on a menu item is the only
    // version of this feature that can be FOUND.
    //
    // `CmdOrCtrl` maps to Cmd on macOS and Ctrl elsewhere, which is what makes
    // one definition correct on both.
    let zoom_in = MenuItemBuilder::with_id("zoom-in", "Zoom In — Μεγέθυνση")
        .accelerator("CmdOrCtrl+Plus")
        .build(handle)?;
    let zoom_out = MenuItemBuilder::with_id("zoom-out", "Zoom Out — Σμίκρυνση")
        .accelerator("CmdOrCtrl+-")
        .build(handle)?;
    let zoom_reset = MenuItemBuilder::with_id("zoom-reset", "Actual Size — Κανονικό μέγεθος")
        .accelerator("CmdOrCtrl+0")
        .build(handle)?;
    let view = SubmenuBuilder::new(handle, "View")
        .item(&zoom_in)
        .item(&zoom_out)
        .item(&zoom_reset)
        .build()?;
    // HISTORY IS THE SAME ARGUMENT AS ZOOM: THE MENU ITEM IS THE KEYBOARD.
    //
    // wry already routes mouse buttons 8/9 — the thumb buttons — to
    // history.back/forward natively, so a tutor with such a mouse has always
    // had this. A tutor without one had NOTHING: this window has no address
    // bar, no toolbar and no Back button, so a wrong click's only way home was
    // restarting the app. And a shortcut that appears nowhere on screen might
    // as well not exist — the accelerator on a menu item is the only version
    // of this feature that can be FOUND.
    //
    // Two spellings on purpose, not one `CmdOrCtrl`: each platform's browsers
    // trained the hands we are serving. Alt+Left/Right is Back/Forward
    // everywhere except macOS — where that chord is a text-caret motion and
    // the browsers use Cmd+[ / Cmd+] instead.
    let (back_accel, forward_accel) = if cfg!(target_os = "macos") {
        ("CmdOrCtrl+[", "CmdOrCtrl+]")
    } else {
        ("Alt+Left", "Alt+Right")
    };
    let back = MenuItemBuilder::with_id("history-back", "Back — Πίσω")
        .accelerator(back_accel)
        .build(handle)?;
    let forward = MenuItemBuilder::with_id("history-forward", "Forward — Εμπρός")
        .accelerator(forward_accel)
        .build(handle)?;
    let history = SubmenuBuilder::new(handle, "History")
        .item(&back)
        .item(&forward)
        .build()?;
    // Built DISABLED. This menu exists from the instant the splash appears,
    // minutes before there is a backend to restart on a first run; a click in
    // that window used to reach `Supervisor::ports()` and panic. Greying it out
    // is the honest version of the same rule `Supervisor::is_booted` enforces —
    // `boot` enables it once the app is actually up.
    let restart = MenuItemBuilder::with_id(
        "restart-backend",
        "Restart Backend — Επανεκκίνηση backend",
    )
    .enabled(false)
    .build(handle)?;
    let backend = SubmenuBuilder::new(handle, "Backend")
        .text("show-logs", "Show Logs — Αρχεία καταγραφής")
        .item(&restart)
        .build()?;
    let _ = RESTART_ITEM.set(restart);
    let menu = MenuBuilder::new(handle)
        .items(&[&app_menu, &edit, &view, &history, &backend])
        .build()?;
    app.set_menu(menu)?;
    app.on_menu_event(|handle, event| match event.id().0.as_str() {
        "show-logs" => open_logs(handle),
        "restart-backend" => menu_restart(handle.clone()),
        "zoom-in" => zoom_command(handle, "zoomIn"),
        "zoom-out" => zoom_command(handle, "zoomOut"),
        "zoom-reset" => zoom_command(handle, "reset"),
        "history-back" => history_command(handle, "back"),
        "history-forward" => history_command(handle, "forward"),
        _ => {}
    });
    Ok(())
}

fn open_logs(handle: &AppHandle) {
    let _ = handle
        .opener()
        .open_path(paths::log_dir().to_string_lossy(), None::<String>);
}

fn menu_restart(handle: AppHandle) {
    std::thread::spawn(move || {
        let Some(sup) = SUPERVISOR.get() else { return };
        if sup.is_shutting_down() || !sup.try_open_dialog() {
            return; // already restarting / dialog already up
        }
        // Belt to the greyed-out menu item's braces: the item is only enabled
        // once boot finishes, but a menu can be driven by more than a mouse
        // (accessibility, AppleScript, a stale keyboard event), and this path
        // used to end in a panic. Say the true thing and change nothing.
        if !sup.is_booted() {
            notice(&handle, &supervisor::still_starting_message());
            sup.close_dialog(); // after, so a second click cannot stack a second one
            return;
        }
        set_status(&handle, "restart");
        let died = {
            let h = handle.clone();
            move || backend_died(h.clone())
        };
        let result = sup.restart_backend(died);
        sup.close_dialog();
        match result {
            Ok(()) => reload_main(&handle),
            Err(why) => fatal(&handle, &format!("Η επανεκκίνηση απέτυχε.\nRestart failed.\n\n{why}")),
        }
    });
}

/// Tell the tutor something and carry on — no logs button, no exit. The only
/// dialog in the app that is not a failure.
fn notice(handle: &AppHandle, msg: &str) {
    handle
        .dialog()
        .message(msg)
        .title("Angel OS")
        .buttons(MessageDialogButtons::Ok)
        .blocking_show();
}

fn reload_main(handle: &AppHandle) {
    if let Some(w) = handle.get_webview_window("main") {
        let _ = w.eval("window.location.reload()");
    }
}

// ---- failure handling ------------------------------------------------------

/// Mirror every panic, on ANY thread, into app.log before the default hook
/// prints it to a stderr that a Finder-launched `.app` does not have.
///
/// `catch_unwind` below covers the boot thread's panic with a dialog, but the
/// hook is what records WHERE it happened: `PanicHookInfo`'s `Display` carries
/// the file and line, and by the time `catch_unwind` hands back a payload that
/// is gone. Chained rather than replaced, so `cargo run` still prints normally.
fn install_panic_logger() {
    let default = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        paths::app_log(&format!("PANIC: {info}"));
        default(info);
    }));
}

/// Run `body`, returning `Some(message)` if it panicked.
///
/// `AssertUnwindSafe` is the honest choice here rather than a shrug: the only
/// thing `body` closes over is the `AppHandle`, whose interior state belongs to
/// tauri and is not ours to leave half-updated. There is no partially-mutated
/// value of ours on the other side of this boundary — the caller's single
/// reaction is to show a dialog and exit.
fn run_guarded<F: FnOnce()>(body: F) -> Option<String> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(body))
        .err()
        .map(|payload| panic_text(&payload))
}

/// The message out of a panic payload. `panic!("…")` with arguments produces a
/// `String`, a literal produces a `&'static str`, and `panic_any` can produce
/// anything at all — the last of which must still say something rather than
/// leaving the dialog blank.
fn panic_text(payload: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = payload.downcast_ref::<&'static str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "panic with a non-text payload".to_string()
    }
}

/// A panic is a bug in this app, not something the tutor did or can fix — so
/// the Greek half says so plainly and points at the one useful action (send the
/// logs), and the detail is kept for whoever reads them. `fatal` appends the
/// log directory and offers Show Logs.
fn internal_error_message(detail: &str) -> String {
    format!(
        "Παρουσιάστηκε εσωτερικό σφάλμα κατά την εκκίνηση του Angel OS.\n\
         Δεν φταίει κάτι που κάνατε. Στείλτε τα αρχεία καταγραφής και δοκιμάστε \
         να ανοίξετε ξανά το Angel OS.\n\n\
         Angel OS hit an internal error while starting.\n\
         This is a bug in the app, not something you did. Please send the logs.\n\n\
         Internal error: {detail}"
    )
}

/// A supervised process died outside shutdown/restart. Two-step dialog because
/// the dialog plugin offers at most two buttons:
/// [Restart backend] [More…] → [Show logs] [Quit].
fn backend_died(handle: AppHandle) {
    let Some(sup) = SUPERVISOR.get() else { return };
    if sup.is_shutting_down() || !sup.try_open_dialog() {
        return;
    }
    std::thread::spawn(move || {
        let sup = SUPERVISOR.get().expect("checked above");
        // Offering a restart before boot has finished would put this thread and
        // the boot thread through the same stop/probe/respawn at the same time.
        // A child that dies DURING boot is a boot that failed — the log is the
        // answer, and the boot thread is on its way to saying so too.
        let restart = sup.is_booted()
            && handle
                .dialog()
                .message(
                    "Το backend σταμάτησε απροσδόκητα.\n\
                     The backend stopped unexpectedly.",
                )
                .title("Angel OS")
                .kind(MessageDialogKind::Error)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Restart backend — Επανεκκίνηση".into(),
                    "More… — Περισσότερα".into(),
                ))
                .blocking_show();
        if restart {
            let died = {
                let h = handle.clone();
                move || backend_died(h.clone())
            };
            let result = sup.restart_backend(died);
            sup.close_dialog();
            match result {
                Ok(()) => reload_main(&handle),
                Err(why) => {
                    fatal(&handle, &format!("Η επανεκκίνηση απέτυχε.\nRestart failed.\n\n{why}"))
                }
            }
        } else {
            // Reached either from More… or — when the backend died before boot
            // finished — directly, so this dialog has to stand on its own and
            // say WHAT happened, not just where the logs are.
            let show = handle
                .dialog()
                .message(format!(
                    "Το backend σταμάτησε απροσδόκητα.\n\
                     The backend stopped unexpectedly.\n\n\
                     Logs: {}",
                    paths::log_dir().display()
                ))
                .title("Angel OS")
                .kind(MessageDialogKind::Error)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Show logs — Αρχεία καταγραφής".into(),
                    "Quit — Έξοδος".into(),
                ))
                .blocking_show();
            if show {
                open_logs(&handle);
                std::thread::sleep(Duration::from_millis(800)); // let the opener launch
            }
            handle.exit(1);
        }
    });
}

/// Localized fatal dialog (always offers Show Logs), then exit. Never call on
/// the main thread — `blocking_show` would deadlock there; every caller is a
/// worker thread.
///
/// The reason goes into app.log first: the dialog is gone the moment it is
/// dismissed, and Show Logs opens a directory, so a fatal nobody wrote down is
/// a support call with no evidence. `handle.exit(1)` then routes through
/// `RunEvent::ExitRequested`, which runs the ordered teardown and releases the
/// instance lock — after the children are down, not before.
fn fatal(handle: &AppHandle, msg: &str) {
    paths::app_log(&format!("FATAL: {msg}"));
    let show = handle
        .dialog()
        .message(format!("{msg}\n\nLogs: {}", paths::log_dir().display()))
        .title("Angel OS")
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Show Logs — Αρχεία καταγραφής".into(),
            "Quit — Έξοδος".into(),
        ))
        .blocking_show();
    if show {
        open_logs(handle);
        std::thread::sleep(Duration::from_millis(800));
    }
    handle.exit(1);
}

#[cfg(test)]
mod tests {
    use super::*;

    /// WHAT LEAVES THE APP FRAME, AND WHAT MUST NOT.
    ///
    /// Both directions of this are a real bug, which is why it is a table and
    /// not an `if`. Send too little and the tutor gets what he had before: a
    /// link that says "get a key at console.anthropic.com" and does NOTHING
    /// when clicked, because a webview has no tab to open. Send too much and a
    /// DOCX export — which is a `blob:` navigation, that is genuinely how the
    /// feature works — gets handed to Firefox instead of being saved, breaking
    /// the export the save dialog exists to serve.
    #[test]
    fn only_the_open_internet_leaves_the_app_frame() {
        let browser = |u: &str| goes_to_the_browser(&u.parse().expect("url"));

        // The open internet. This is the whole point of the feature.
        assert!(browser("https://console.anthropic.com/settings/keys"));
        assert!(browser("http://example.org/"));

        // Our own web app, whatever port the scan landed on today. The port is
        // deliberately not part of the test because it is deliberately not part
        // of the rule — pinning it would break the moment the pair rolls.
        assert!(!browser("http://localhost:8790/el/curricula"));
        assert!(!browser("http://localhost:9999/el/settings"));
        assert!(!browser("http://127.0.0.1:8791/health/ready"));

        // How the PAGE does its own work. A DOCX export is the first one, and
        // treating it as an outbound link would break exporting entirely.
        assert!(!browser("blob:http://localhost:8790/9f0c-4a1e"));
        assert!(!browser("data:text/plain,hello"));
        assert!(!browser("about:blank"));
    }

    /// THE WORKAROUND APPLIES TO THE STACK IT FIXES, AND TO NOTHING ELSE.
    ///
    /// Pinned as a truth table because the failure it prevents is invisible and
    /// the failure it could CAUSE is invisible too. Disabling the DMA-BUF
    /// renderer on a machine where it works costs compositing for nothing and
    /// there is no symptom to notice; leaving it on where it is broken hands the
    /// tutor a black window with a working menu bar, which reads as "the app is
    /// broken" and is unreportable — every log says the boot succeeded, because
    /// it did.
    #[test]
    #[cfg(target_os = "linux")]
    fn the_dmabuf_workaround_applies_only_to_the_stack_it_fixes() {
        use std::ffi::OsStr;

        // The NVIDIA userspace stack with nothing preset: THIS is the black
        // window, and it is the only cell that may be touched.
        let Dmabuf::Disable(why) = dmabuf_workaround(None, true) else {
            panic!("an NVIDIA stack must get the workaround");
        };
        assert!(why.contains("NVIDIA"), "the log line must name the stack: {why}");

        // Mesa — Intel, AMD, and nouveau, which is NVIDIA hardware on a Mesa
        // userspace and therefore NOT affected. `/proc/driver/nvidia/version`
        // is what distinguishes them: nouveau does not create it.
        assert!(matches!(dmabuf_workaround(None, false), Dmabuf::LeaveAlone(_)));

        // AN EXPLICIT SETTING IS NEVER OVERRIDDEN, IN EITHER DIRECTION. `0` on
        // an NVIDIA box is somebody deliberately putting the fast path back
        // (a fixed driver, a fixed WebKit) — forcing it to 1 anyway would make
        // that impossible and leave them nothing in the log to explain why.
        assert!(matches!(
            dmabuf_workaround(Some(OsStr::new("0")), true),
            Dmabuf::LeaveAlone(_)
        ));
        assert!(matches!(
            dmabuf_workaround(Some(OsStr::new("1")), false),
            Dmabuf::LeaveAlone(_)
        ));
        // Empty counts as set: WebKitGTK tests the variable's PRESENCE, so
        // `WEBKIT_DISABLE_DMABUF_RENDERER=` already disables it. Reading that
        // as "unset" would have us write over a decision already in force.
        assert!(matches!(
            dmabuf_workaround(Some(OsStr::new("")), true),
            Dmabuf::LeaveAlone(_)
        ));
    }

    /// THE FATAL DIALOG IN THE FAILED-`ALTER DATABASE` WINDOW HAS TO NAME THE
    /// MEDIA THAT ALREADY MOVED.
    ///
    /// That window is not hypothetical and it is not rare enough to ignore:
    /// `displace_database` moves `<data>/media` FIRST, deliberately, and the
    /// `ALTER DATABASE` after it has no FORCE — one connected backend refuses
    /// it. The boot is then fatal with the tutor's entire library sitting under
    /// `media_superseded_…`. app.log, `install-state.json` and the READ-ME all
    /// said so; the dialog — the only one of the four he actually sees — said
    /// "Angel OS could not set the half-installed database aside" and stopped
    /// there, which reads as "and your books are gone".
    ///
    /// Both halves of the fix are asserted: the receipt now survives the failure,
    /// AND the dialog folds in what the marker says was never announced, so it
    /// is right even when the receipt is lost with the process.
    #[test]
    fn the_failed_alter_dialog_names_the_media_that_already_moved() {
        let data = std::env::temp_dir().join(format!("gt-fataldlg-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        let dirs = Dirs {
            pgdata: data.join("pgdata"),
            media: data.join("media"),
            secrets: data.join("secrets"),
            logs: data.join("logs"),
            data: data.clone(),
        };
        for d in [&dirs.pgdata, &dirs.media, &dirs.logs] {
            std::fs::create_dir_all(d).expect("dirs");
        }
        firstrun::mark_seeding(&dirs).expect("marker");
        // One uploaded book, laid out the way the API child writes it.
        let book = dirs.media.join("cccc3333-0000-0000-0000-00000000000c");
        std::fs::create_dir_all(&book).expect("book");
        std::fs::write(book.join("source.pdf"), b"HIS ONLY COPY").expect("pdf");

        // A resource root with no psql in it, so the probe cannot answer
        // (`Unknown` → rename, the safe reading), the media moves for real, and
        // the `ALTER DATABASE` fails. That is the window, with no cluster.
        let no_psql = data.join("no-such-resource-root");
        let (displaced, renamed) = firstrun::displace_database(&no_psql, &dirs, 5999);
        assert!(renamed.is_err(), "there is no psql here");

        let moved: Vec<String> = std::fs::read_dir(&data)
            .expect("read data dir")
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with("media_superseded_"))
            .collect();
        assert_eq!(moved.len(), 1, "his library really moved: {moved:?}");

        let failure = "Angel OS could not set the half-installed database aside.";
        let msg = and_what_was_set_aside(&dirs, &displaced, failure);
        assert!(msg.starts_with(failure), "the failure still comes first:\n{msg}");
        assert!(
            msg.contains(&moved[0]),
            "the dialog must name where his books went:\n{msg}"
        );
        // BY FILENAME, not just as a path buried in a sentence — that file is
        // the whole recovery and somebody has to be able to ask for it by name.
        assert!(
            msg.contains("READ-ME-superseded-data.txt"),
            "and point at the file that explains it:\n{msg}"
        );
        assert!(msg.contains("ΔΕΝ ΔΙΑΓΡΑΦΗΚΕ ΤΙΠΟΤΑ"), "Greek half too:\n{msg}");

        // …and it is still right when the in-process receipt is gone — a boot
        // that displaced something and then died carries nothing forward, which
        // is exactly the case the durable record exists for.
        let msg = and_what_was_set_aside(&dirs, &firstrun::Displaced::nothing(), failure);
        assert!(
            msg.contains(&moved[0]) && msg.contains("READ-ME-superseded-data.txt"),
            "the marker alone must be enough:\n{msg}"
        );

        // And an ordinary failure on an install where nothing moved says
        // nothing about set-aside data — the notice must not become noise.
        let clean = std::env::temp_dir().join(format!("gt-fataldlg2-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&clean);
        std::fs::create_dir_all(&clean).expect("dir");
        let quiet = Dirs { data: clean.clone(), ..dirs.clone() };
        assert_eq!(
            and_what_was_set_aside(&quiet, &firstrun::Displaced::nothing(), failure),
            failure
        );

        let _ = std::fs::remove_dir_all(&data);
        let _ = std::fs::remove_dir_all(&clean);
    }

    /// The boot thread's panic guard. `boot` routes every DELIBERATE failure
    /// through `fatal()` — log, dialog, Show Logs, exit — but a panic went
    /// through none of it: the thread unwound silently, the splash stayed on
    /// "checking" forever, app.log said nothing, and there was no button to
    /// press. That is the exact failure shape round 3 existed to remove.
    #[test]
    fn a_panicking_boot_is_caught_and_carries_its_message() {
        assert_eq!(run_guarded(|| {}), None, "a normal boot reports nothing");

        // `panic!` with arguments produces a String payload…
        let why = run_guarded(|| panic!("pgdata went {}", "missing")).expect("a panic");
        assert!(why.contains("pgdata went missing"), "{why}");

        // …a bare literal produces a &'static str, which is a DIFFERENT
        // downcast and the one a naive implementation misses.
        let why = run_guarded(|| panic!("bare literal")).expect("a panic");
        assert_eq!(why, "bare literal");

        // An `expect` on None — the shape an actual bug takes, and the exact
        // one that used to freeze the splash when a menu click reached
        // `Supervisor::ports()` before boot had chosen any.
        fn no_ports_yet() -> Option<u16> {
            None
        }
        let why = run_guarded(|| {
            no_ports_yet().expect("ports were chosen");
        })
        .expect("a panic");
        assert!(why.contains("ports were chosen"), "{why}");
    }

    /// A payload that is neither string must still say SOMETHING: an empty
    /// dialog is indistinguishable from the frozen splash this replaces.
    #[test]
    fn an_exotic_panic_payload_still_produces_a_message() {
        let why = run_guarded(|| std::panic::panic_any(42u32)).expect("a panic");
        assert!(!why.trim().is_empty(), "the dialog must not be blank");
        assert!(why.contains("non-text"), "{why}");
    }

    /// What the tutor reads. A panic is a bug in the app, so the message must
    /// say so — Greek first, since he reads Greek and only whoever is helping
    /// him reads the English half — and it must still carry the detail for the
    /// log. `fatal` adds the log directory and the Show Logs button.
    #[test]
    fn the_internal_error_dialog_is_bilingual_greek_first_and_keeps_the_detail() {
        let msg = internal_error_message("called `Option::unwrap()` on a `None` value");
        let greek = msg
            .find(|c: char| ('\u{0370}'..='\u{03ff}').contains(&c))
            .expect("Greek half");
        let english = msg.find("internal error while starting").expect("English half");
        assert!(greek < english, "Greek must come first:\n{msg}");
        assert!(msg.contains("not something you did"), "{msg}");
        assert!(msg.contains("Option::unwrap"), "the detail must survive:\n{msg}");
    }
}
