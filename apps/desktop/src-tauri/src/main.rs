//! YEMENDEV AI desktop shell: one native window showing the platform's own web UI from 127.0.0.1.
//!
//! The shell starts the local platform (or reuses one already running on the port), shows a local page
//! while it starts or explains why it could not, tells the user if the platform stops unexpectedly, and stops
//! the server it started when the app exits. It adds no native commands: the UI keeps the browser security
//! model (CSP, token in sessionStorage).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

/// A server this shell started: its process group and a signal that its waiting thread saw it exit.
struct Running {
    pid: i32,
    exited: Receiver<()>,
}

static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);

fn show_error(window: &WebviewWindow, reason: &str) {
    eprintln!("yemendev-desktop: {reason}");
    if let Ok(mut page) = window.url() {
        if page.scheme() == "http" && page.host_str() != Some("tauri.localhost") {
            // on the platform page: go back to the shell's own page (same origin as the app assets)
            page = match tauri::Url::parse("tauri://localhost/index.html") {
                Ok(u) => u,
                Err(_) => return,
            };
        }
        page.set_path("/index.html");
        page.query_pairs_mut().clear().append_pair("error", reason);
        let _ = window.navigate(page);
    }
}

fn main() {
    let running: Arc<Mutex<Option<Running>>> = Arc::new(Mutex::new(None));
    let held = running.clone();
    let app = tauri::Builder::default()
        .setup(move |app| {
            let cfg = backend::Config::resolve();
            let port = cfg.as_ref().map(|c| c.port).unwrap_or(backend::DEFAULT_PORT);
            let origin = format!("http://127.0.0.1:{port}");
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("YEMENDEV AI")
                .inner_size(1280.0, 860.0)
                .min_inner_size(420.0, 520.0)
                .on_navigation(move |url| backend::navigation_allowed(url, &origin))
                .build()?;
            let slot = held.clone();
            // This thread lives as long as the server it starts (see Server::take_child).
            std::thread::spawn(move || {
                let cfg = match cfg {
                    Ok(c) => c,
                    Err(reason) => return show_error(&window, &reason),
                };
                let mut server = match backend::ensure(&cfg) {
                    Ok(s) => s,
                    Err(reason) => return show_error(&window, &reason),
                };
                eprintln!(
                    "yemendev-desktop: platform {} on port {} ({})",
                    server.version,
                    server.port,
                    if server.started_here() { "started by the shell" } else { "already running; left running on exit" }
                );
                let url = server.ui_url();
                let child = server.take_child();
                let _ = window.navigate(url);
                if let Some(mut child) = child {
                    let (tx, rx) = channel();
                    *slot.lock().unwrap() = Some(Running { pid: child.id() as i32, exited: rx });
                    let status = child.wait();
                    let _ = tx.send(());
                    if !SHUTTING_DOWN.load(Ordering::SeqCst) {
                        let log = backend::Server::log_path(&cfg.data_dir);
                        show_error(
                            &window,
                            &format!(
                                "the platform stopped unexpectedly ({}). Last log lines from {}:\n{}",
                                status.map(|s| s.to_string()).unwrap_or_else(|e| e.to_string()),
                                log.display(),
                                backend::tail(&log, 15)
                            ),
                        );
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the YEMENDEV desktop shell");
    app.run(move |_, event| {
        if let RunEvent::Exit = event {
            SHUTTING_DOWN.store(true, Ordering::SeqCst);
            if let Some(r) = running.lock().unwrap().take() {
                backend::signal_group(r.pid, false);
                if r.exited.recv_timeout(Duration::from_secs(10)).is_err() {
                    backend::signal_group(r.pid, true);
                    let _ = r.exited.recv_timeout(Duration::from_secs(5));
                }
            }
        }
    });
}
