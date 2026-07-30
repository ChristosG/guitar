//! GuitarTutor desktop shell.
//!
//! Boot: splash → dirs/ports/secrets → (first run: initdb + Greek-collation
//! guard + seed) → postgres → alembic → uvicorn → node → readiness gate →
//! main window at http://localhost:8790 (LITERAL localhost — the web app
//! derives its API base from the origin and the session cookie is host-only).
//!
//! The running app never contacts any host but 127.0.0.1/localhost itself;
//! the API process talks to api.anthropic.com with the tutor's key. Nothing
//! else. No telemetry, no update pings.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod firstrun;
mod paths;
mod supervisor;

use std::path::PathBuf;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use crate::supervisor::{Ready, Supervisor};

static SUPERVISOR: OnceLock<Arc<Supervisor>> = OnceLock::new();

fn main() {
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
                .title("GuitarTutor")
                .inner_size(460.0, 320.0)
                .resizable(false)
                .maximizable(false)
                .center()
                .build()?;
            let handle = app.handle().clone();
            std::thread::spawn(move || boot(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building GuitarTutor")
        .run(|_handle, event| {
            // Fires on Cmd+Q, last-window-close and app.exit() alike. Ordered
            // teardown: node → uvicorn → pg_ctl stop -m fast (idempotent).
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(sup) = SUPERVISOR.get() {
                    sup.shutdown();
                }
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

    // 8790/8791 are FROZEN (localhost origin → API base derivation, host-only
    // cookie). If something else holds one, name it and stop.
    if let Err(port) = firstrun::preflight_frozen_ports() {
        return fatal(
            &handle,
            &format!(
                "Η θύρα {port} χρησιμοποιείται ήδη από άλλο πρόγραμμα.\n\
                 Κλείστε το και ξεκινήστε ξανά το GuitarTutor.\n\n\
                 Port {port} is already in use by another program.\n\
                 Close it and start GuitarTutor again."
            ),
        );
    }

    let secrets = match firstrun::load_or_create_secrets(&dirs.secrets) {
        Ok(s) => s,
        Err(e) => return fatal(&handle, &e),
    };

    let pg_port = match firstrun::pick_pg_port() {
        Some(p) => p,
        None => {
            return fatal(
                &handle,
                "Δεν βρέθηκε ελεύθερη θύρα για τη βάση δεδομένων (5434–5444).\n\
                 No free database port found (5434–5444).",
            )
        }
    };

    let fresh = !firstrun::cluster_exists(&dirs);
    let sup = Arc::new(Supervisor::new(res.clone(), dirs.clone(), secrets, pg_port));
    let _ = SUPERVISOR.set(sup.clone());

    if fresh {
        set_status(&handle, "firstrun");
        if let Err(e) = firstrun::init_cluster(&res, &dirs, pg_port, false) {
            return fatal(&handle, &format!("initdb failed:\n{e}"));
        }
    }

    set_status(&handle, "db");
    if let Err(e) = sup.start_postgres() {
        return fatal(&handle, &format!("Η βάση δεδομένων δεν ξεκίνησε.\nThe database did not start.\n\n{e}"));
    }

    if fresh {
        // Greek collation guard, right after first initdb+start. Retry with ICU
        // (el) when the bundled build supports it; otherwise this install
        // cannot search Greek and must say so instead of limping.
        if !firstrun::greek_collation_ok(&res, &dirs, pg_port) {
            sup.stop_postgres();
            let recovered = firstrun::initdb_supports_icu(&res, &dirs)
                && firstrun::wipe_pgdata(&dirs).is_ok()
                && firstrun::init_cluster(&res, &dirs, pg_port, true).is_ok()
                && sup.start_postgres().is_ok()
                && firstrun::greek_collation_ok(&res, &dirs, pg_port);
            if !recovered {
                return fatal(
                    &handle,
                    "Αυτό το σύστημα δεν υποστηρίζει ελληνική ταξινόμηση κειμένου \
                     (collation) — η αναζήτηση στα ελληνικά δεν θα λειτουργούσε.\n\
                     This system lacks a Greek-capable text collation — Greek \
                     search would not work. Please report this.",
                );
            }
        }
        set_status(&handle, "seeding");
        if let Err(e) = firstrun::create_and_seed_db(&res, &dirs, pg_port) {
            return fatal(&handle, &format!("seed restore failed:\n{e}"));
        }
        if let Err(e) = firstrun::write_meta(&dirs, pg_port) {
            return fatal(&handle, &format!("could not write meta.json: {e}"));
        }
    }

    set_status(&handle, "migrate");
    if let Err(e) = sup.run_migrations() {
        return fatal(&handle, &format!("Η ενημέρωση της βάσης απέτυχε.\nDatabase migration failed.\n\n{e}"));
    }

    let died = {
        let h = handle.clone();
        move || backend_died(h.clone())
    };
    set_status(&handle, "api");
    if let Err(e) = sup.start_api(died.clone()) {
        return fatal(&handle, &e);
    }
    set_status(&handle, "web");
    if let Err(e) = sup.start_node(died.clone()) {
        return fatal(&handle, &e);
    }
    sup.watch_postgres(died);

    set_status(&handle, "waiting");
    match sup.await_ready(Duration::from_secs(180)) {
        Ready::Yes => show_main_window(&handle),
        Ready::No(why) => fatal(
            &handle,
            &format!(
                "Το backend δεν ξεκίνησε μέσα σε 3 λεπτά.\n\
                 The backend did not become ready within 3 minutes.\n\n{why}"
            ),
        ),
    }
}

// ---- windows ---------------------------------------------------------------

fn show_main_window(handle: &AppHandle) {
    let h = handle.clone();
    // Window creation belongs on the main thread (macOS requires it).
    let _ = handle.run_on_main_thread(move || {
        // EXACTLY http://localhost:8790 — literal `localhost`, load-bearing.
        let url: tauri::Url = "http://localhost:8790".parse().expect("static url");
        let built = WebviewWindowBuilder::new(&h, "main", WebviewUrl::External(url))
            .title("GuitarTutor")
            .inner_size(1360.0, 900.0)
            .min_inner_size(980.0, 640.0)
            .on_download(|_webview, event| {
                // Route downloads (backup exports, DOCX) to ~/Downloads under
                // the server-suggested filename. Returning true proceeds.
                if let DownloadEvent::Requested { destination, .. } = event {
                    let name = destination
                        .file_name()
                        .map(|n| n.to_os_string())
                        .unwrap_or_else(|| "download".into());
                    *destination = unique_path(&paths::downloads_dir(), &name);
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
    let app_menu = SubmenuBuilder::new(handle, "GuitarTutor")
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
    let backend = SubmenuBuilder::new(handle, "Backend")
        .text("show-logs", "Show Logs — Αρχεία καταγραφής")
        .text("restart-backend", "Restart Backend — Επανεκκίνηση backend")
        .build()?;
    let menu = MenuBuilder::new(handle)
        .items(&[&app_menu, &edit, &backend])
        .build()?;
    app.set_menu(menu)?;
    app.on_menu_event(|handle, event| match event.id().0.as_str() {
        "show-logs" => open_logs(handle),
        "restart-backend" => menu_restart(handle.clone()),
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

fn reload_main(handle: &AppHandle) {
    if let Some(w) = handle.get_webview_window("main") {
        let _ = w.eval("window.location.reload()");
    }
}

// ---- failure handling ------------------------------------------------------

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
        let restart = handle
            .dialog()
            .message(
                "Το backend σταμάτησε απροσδόκητα.\n\
                 The backend stopped unexpectedly.",
            )
            .title("GuitarTutor")
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
            let show = handle
                .dialog()
                .message(format!("Logs: {}", paths::log_dir().display()))
                .title("GuitarTutor")
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
fn fatal(handle: &AppHandle, msg: &str) {
    eprintln!("FATAL: {msg}");
    let show = handle
        .dialog()
        .message(format!("{msg}\n\nLogs: {}", paths::log_dir().display()))
        .title("GuitarTutor")
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
