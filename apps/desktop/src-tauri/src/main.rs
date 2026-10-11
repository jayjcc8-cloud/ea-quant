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
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                window.app_handle().exit(0);
            }
        })
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
                                if value["status"] == "close_blocked" {
                                    state.exiting.store(false, std::sync::atomic::Ordering::SeqCst);
                                    let _ = window.eval("window.alert('本地模拟停止尚未确认。QUINE 保持打开，请查看运行状态后重试退出。')");
                                }
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
                    // A normal quit is a cooperative request, not parent loss.
                    // Keep the pipe and window until the backend confirms shutdown.
                    let has_child = {
                        let mut owned = state.child.lock().unwrap();
                        if let Some(child) = owned.as_mut() {
                            if let Some(input) = child.stdin.as_mut() {
                                let _ = input.write_all(b"shutdown\n");
                                let _ = input.flush();
                            }
                            true
                        } else {
                            false
                        }
                    };
                    if !has_child {
                        // Backend spawn never happened: this window owns no attempt.
                        state.finished.store(true, std::sync::atomic::Ordering::SeqCst);
                        handle.exit(0);
                        return;
                    }
                    let deadline = Instant::now() + Duration::from_secs(30);
                    loop {
                        let result = state.child.lock().unwrap().as_mut().and_then(|child| child.try_wait().ok().flatten());
                        if let Some(status) = result {
                            let exit_code = status.code().unwrap_or(1);
                            if let Some(log) = state.log.lock().unwrap().as_mut() {
                                let _ = writeln!(log, "desktop_backend_reaped: exit={exit_code}");
                            }
                            if exit_code == 0 {
                                state.finished.store(true, std::sync::atomic::Ordering::SeqCst);
                                handle.exit(0);
                            } else {
                                state.exiting.store(false, std::sync::atomic::Ordering::SeqCst);
                                if let Some(window) = handle.get_webview_window("main") {
                                    let _ = window.eval("window.alert('后端已退出，但本地模拟终态未获确认。请保留工作区证据，重新打开后查看原实例；不会自动续跑。')");
                                }
                            }
                            break;
                        }
                        if !state.exiting.load(std::sync::atomic::Ordering::SeqCst) {
                            break;
                        }
                        if Instant::now() >= deadline {
                            state.exiting.store(false, std::sync::atomic::Ordering::SeqCst);
                            if let Some(window) = handle.get_webview_window("main") {
                                let _ = window.eval("window.alert('退出超时：本地模拟停止尚未确认。窗口保持打开，不会强制结束或另开实例。')");
                            }
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                });
            }
        }
    });
}
