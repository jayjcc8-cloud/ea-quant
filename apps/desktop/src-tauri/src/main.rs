use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Session {
    child: Mutex<Option<Child>>,
    log: Mutex<Option<std::fs::File>>,
    exiting: std::sync::atomic::AtomicBool,
    finished: std::sync::atomic::AtomicBool,
}

fn main() {
    let session = Arc::new(Session {
        child: Mutex::new(None),
        log: Mutex::new(None),
        exiting: std::sync::atomic::AtomicBool::new(false),
        finished: std::sync::atomic::AtomicBool::new(false),
    });
    let setup_session = session.clone();
    let app = tauri::Builder::default()
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "quit-session" {
                app.exit(0);
            }
        })
        .setup(move |app| {
            // macOS's predefined Quit invokes NSApplication terminate: directly.
            // Route our Quit through Tauri's exit request so the backend is reaped.
            use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
            let quit =
                MenuItem::with_id(app, "quit-session", "退出 QUINE", true, Some("CmdOrCtrl+Q"))?;
            let application_menu = Submenu::with_items(app, "QUINE", true, &[&quit])?;
            let edit = Submenu::with_items(
                app,
                "编辑",
                true,
                &[
                    &PredefinedMenuItem::undo(app, None)?,
                    &PredefinedMenuItem::redo(app, None)?,
                    &PredefinedMenuItem::cut(app, None)?,
                    &PredefinedMenuItem::copy(app, None)?,
                    &PredefinedMenuItem::paste(app, None)?,
                    &PredefinedMenuItem::select_all(app, None)?,
                ],
            )?;
            app.set_menu(Menu::with_items(app, &[&application_menu, &edit])?)?;
            let allowed = Arc::new(Mutex::new(None::<String>));
            let navigate_allowed = allowed.clone();
            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("QUINE · 本地研究")
                    .inner_size(1360.0, 900.0)
                    .min_inner_size(850.0, 600.0)
                    .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                    .on_navigation(move |url| {
                        if matches!(url.scheme(), "tauri" | "about") {
                            return true;
                        }
                        navigate_allowed
                            .lock()
                            .unwrap()
                            .as_ref()
                            .is_some_and(|origin| url.origin().ascii_serialization() == *origin)
                    })
                    .build()?;
            let runtime = app.path().resource_dir()?.join("runtime");
            let root = std::env::var_os("QUINE_DATA_ROOT")
                .map(std::path::PathBuf::from)
                .unwrap_or(app.path().app_data_dir()?.parent().unwrap().join("QUINE"));
            // Diagnostics and all mutable data are outside the application bundle.
            // Python validates real paths and takes the exclusive data lock.
            // Reject aliased/relative roots before even diagnostics create a file.
            if !root.is_absolute()
                || root
                    .components()
                    .any(|c| matches!(c, std::path::Component::ParentDir))
                || root.starts_with(&runtime)
                || runtime.starts_with(&root)
                || root
                    .ancestors()
                    .any(|p| std::fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink()))
            {
                window.navigate("tauri://localhost/error.html".parse()?)?;
                return Ok(());
            }
            use std::os::unix::fs::DirBuilderExt;
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(&root)?;
            std::fs::DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(root.join("logs"))?;
            let log = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(root.join("logs/desktop.log"))?;
            *setup_session.log.lock().unwrap() = Some(log.try_clone()?);
            let mut command = Command::new(runtime.join("python/bin/python3.12"));
            command
                .args(["-I", "-B", "-m", "ea.desktop", "--runtime"])
                .arg(&runtime)
                .arg("--data-root")
                .arg(&root)
                .current_dir(&root)
                .env_clear()
                .env("HOME", std::env::var_os("HOME").unwrap_or_default())
                .env("PATH", "/usr/bin:/bin")
                .env("LANG", "en_US.UTF-8")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(log.try_clone()?);
            match command.spawn() {
                Err(error) => {
                    let mut log = log;
                    writeln!(log, "runtime_spawn_failed: {}", error.kind())?;
                    window.navigate("tauri://localhost/error.html".parse()?)?;
                }
                Ok(mut child) => {
                    let output = child.stdout.take().unwrap();
                    *setup_session.child.lock().unwrap() = Some(child);
                    let state = setup_session.clone();
                    std::thread::spawn(move || {
                        let mut log = log;
                        for line in BufReader::new(output).lines().map_while(Result::ok) {
                            let _ = writeln!(log, "{line}");
                            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&line) {
                                if value["status"] == "ready" {
                                    if let Some(url) = value["url"]
                                        .as_str()
                                        .and_then(|s| s.parse::<tauri::Url>().ok())
                                    {
                                        if url.scheme() == "http"
                                            && url.host_str() == Some("127.0.0.1")
                                            && url.port().is_some()
                                        {
                                            *allowed.lock().unwrap() =
                                                Some(url.origin().ascii_serialization());
                                            let _ = window.navigate(url);
                                        }
                                    }
                                }
                            }
                        }
                        if !state.exiting.load(std::sync::atomic::Ordering::SeqCst) {
                            let _ =
                                window.navigate("tauri://localhost/error.html".parse().unwrap());
                        }
                    });
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("desktop window initialization failed");
    app.run(move |handle, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            if session.finished.load(std::sync::atomic::Ordering::SeqCst) {
                return;
            }
            api.prevent_exit();
            if !session
                .exiting
                .swap(true, std::sync::atomic::Ordering::SeqCst)
            {
                let state = session.clone();
                let handle = handle.clone();
                std::thread::spawn(move || {
                    let mut exit_code = 0;
                    if let Some(mut child) = state.child.lock().unwrap().take() {
                        drop(child.stdin.take());
                        let deadline = Instant::now() + Duration::from_secs(25);
                        loop {
                            match child.try_wait() {
                                Ok(Some(status)) => {
                                    exit_code = status.code().unwrap_or(1);
                                    break;
                                }
                                _ if Instant::now() < deadline => {
                                    std::thread::sleep(Duration::from_millis(50))
                                }
                                _ => {
                                    // This kills only our unreaped direct child; the core observes EOF.
                                    let _ = child.kill();
                                    let _ = child.wait();
                                    exit_code = 1;
                                    break;
                                }
                            }
                        }
                    }
                    if let Some(log) = state.log.lock().unwrap().as_mut() {
                        let _ = writeln!(log, "desktop_backend_reaped: exit={exit_code}");
                    }
                    state
                        .finished
                        .store(true, std::sync::atomic::Ordering::SeqCst);
                    handle.exit(exit_code);
                });
            }
        }
    });
}
