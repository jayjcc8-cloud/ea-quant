use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

// Window-local page zoom uses WKWebView pageZoom on macOS. It does not
// claim to follow the system preferred reading size or change workspace data.
struct PageZoom {
    percent: Mutex<u16>,
    status: tauri::menu::MenuItem<tauri::Wry>,
    increase: tauri::menu::MenuItem<tauri::Wry>,
    decrease: tauri::menu::MenuItem<tauri::Wry>,
    reset: tauri::menu::MenuItem<tauri::Wry>,
}

impl PageZoom {
    fn change(&self, window: &tauri::WebviewWindow, action: &str) {
        let mut percent = self.percent.lock().unwrap();
        let next = match action {
            "page-zoom-in" => (*percent + 25).min(200),
            "page-zoom-out" => percent.saturating_sub(25).max(100),
            "page-zoom-reset" => 100,
            _ => return,
        };
        // Do not advance the menu if the WebView rejects the request.
        if window.set_zoom(f64::from(next) / 100.0).is_ok() {
            *percent = next;
            let _ = self.status.set_text(format!("页面缩放：{next}%（本次窗口）"));
            let _ = self.increase.set_enabled(next < 200);
            let _ = self.decrease.set_enabled(next > 100);
            let _ = self.reset.set_enabled(next != 100);
        }
    }
}

struct Session {
    child: Mutex<Option<Child>>,
    log: Mutex<Option<std::fs::File>>,
    exiting: std::sync::atomic::AtomicBool,
    finished: std::sync::atomic::AtomicBool,
    quit_confirmed: std::sync::atomic::AtomicBool,
    quit_prompt_pending: std::sync::atomic::AtomicBool,
}

impl Session {
    // All three native exit entries share this gate. No browser state is changed
    // on Cancel, and approval never implies that the backend has stopped.
    fn authorize_exit(&self, needs_confirmation: bool, confirm: impl FnOnce() -> bool) -> bool {
        use std::sync::atomic::Ordering::SeqCst;
        if self.exiting.load(SeqCst) {
            return false;
        }
        if needs_confirmation && !self.quit_confirmed.load(SeqCst) {
            // AppKit's modal loop can receive another menu/close event.
            if self.quit_prompt_pending.swap(true, SeqCst) {
                return false;
            }
            let approved = confirm();
            self.quit_prompt_pending.store(false, SeqCst);
            if !approved {
                return false;
            }
            self.quit_confirmed.store(true, SeqCst);
        }
        !self.exiting.swap(true, SeqCst)
    }
}

#[cfg(target_os = "macos")]
fn confirm_quit() -> bool {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSAlert, NSAlertSecondButtonReturn};
    use objc2_foundation::NSString;
    // ExitRequested runs on the app event thread. Never approve on a thread
    // where AppKit cannot present the alert; the next request can retry.
    let Some(mtm) = MainThreadMarker::new() else {
        return false;
    };
    let alert = NSAlert::new(mtm);
    alert.setMessageText(&NSString::from_str("退出 QUINE？"));
    alert.setInformativeText(&NSString::from_str(
        "未发布的 CSV、映射、未保存的设置和未发送消息只在本次会话暂存。需要保留时请取消，先保存或复制到本地文件。已保存的研究与报告保留。确认后仍须等待后端安全停止。",
    ));
    // Cancellation is the first/default choice; Escape also explicitly cancels.
    alert
        .addButtonWithTitle(&NSString::from_str("取消"))
        .setKeyEquivalent(&NSString::from_str("\u{1b}"));
    alert
        .addButtonWithTitle(&NSString::from_str("退出 QUINE"))
        .setKeyEquivalent(&NSString::from_str(""));
    alert.runModal() == NSAlertSecondButtonReturn
}

#[cfg(not(target_os = "macos"))]
fn confirm_quit() -> bool {
    false // This candidate only supports macOS; never invent native approval.
}

fn notify_shutdown_problem(handle: &tauri::AppHandle, message: &'static str) {
    // The same WKUIDelegate also has no JavaScript alert implementation. Keep
    // existing refusal/timeout behavior visible through a native notice.
    let _ = handle.run_on_main_thread(move || {
        #[cfg(target_os = "macos")]
        if let Some(mtm) = objc2::MainThreadMarker::new() {
            let alert = objc2_app_kit::NSAlert::new(mtm);
            alert.setMessageText(&objc2_foundation::NSString::from_str("QUINE 尚未确认正常退出"));
            alert.setInformativeText(&objc2_foundation::NSString::from_str(message));
            alert.addButtonWithTitle(&objc2_foundation::NSString::from_str("保留窗口"));
            alert.runModal();
        }
    });
}

fn main() {
    let session = Arc::new(Session {
        child: Mutex::new(None),
        log: Mutex::new(None),
        exiting: std::sync::atomic::AtomicBool::new(false),
        finished: std::sync::atomic::AtomicBool::new(false),
        quit_confirmed: std::sync::atomic::AtomicBool::new(false),
        quit_prompt_pending: std::sync::atomic::AtomicBool::new(false),
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
            } else if let (Some(window), Some(zoom)) =
                (app.get_webview_window("main"), app.try_state::<PageZoom>())
            {
                zoom.change(&window, event.id().as_ref());
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
            let zoom = PageZoom {
                percent: Mutex::new(100),
                status: MenuItem::with_id(
                    app,
                    "page-zoom-status",
                    "页面缩放：100%（本次窗口）",
                    false,
                    None::<&str>,
                )?,
                increase: MenuItem::with_id(
                    app,
                    "page-zoom-in",
                    "放大页面",
                    true,
                    Some("CmdOrCtrl+Shift+Equal"),
                )?,
                decrease: MenuItem::with_id(
                    app,
                    "page-zoom-out",
                    "缩小页面",
                    false,
                    Some("CmdOrCtrl+-"),
                )?,
                reset: MenuItem::with_id(
                    app,
                    "page-zoom-reset",
                    "恢复 100%",
                    false,
                    Some("CmdOrCtrl+0"),
                )?,
            };
            let view = Submenu::with_items(
                app,
                "显示",
                true,
                &[&zoom.status, &zoom.increase, &zoom.decrease, &zoom.reset],
            )?;
            app.set_menu(Menu::with_items(app, &[&application_menu, &edit, &view])?)?;
            app.manage(zoom);
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
                                state.quit_confirmed.store(false, std::sync::atomic::Ordering::SeqCst);
                                    notify_shutdown_problem(window.app_handle(), "本地模拟停止尚未确认。QUINE 保持打开，请查看运行状态后重试退出。");
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
            // Loading/error pages hold no editor. Product pages ask through
            // AppKit directly; WKWebView's unsupported window.confirm is not used.
            let needs_confirmation = handle
                .get_webview_window("main")
                .is_some_and(|window| window.url().is_ok_and(|url| url.scheme() == "http"));
            if session.authorize_exit(needs_confirmation, confirm_quit) {
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
                                state.quit_confirmed.store(false, std::sync::atomic::Ordering::SeqCst);
                                notify_shutdown_problem(&handle, "后端已退出，但本地模拟终态未获确认。请保留工作区证据，重新打开后查看原实例；不会自动续跑。");
                            }
                            break;
                        }
                        if !state.exiting.load(std::sync::atomic::Ordering::SeqCst) {
                            break;
                        }
                        if Instant::now() >= deadline {
                            state.exiting.store(false, std::sync::atomic::Ordering::SeqCst);
                            state.quit_confirmed.store(false, std::sync::atomic::Ordering::SeqCst);
                            notify_shutdown_problem(&handle, "退出超时：本地模拟停止尚未确认。窗口保持打开，不会强制结束或另开实例。");
                            break;
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                });
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    fn session() -> Session {
        Session {
            child: Mutex::new(None),
            log: Mutex::new(None),
            exiting: AtomicBool::new(false),
            finished: AtomicBool::new(false),
            quit_confirmed: AtomicBool::new(false),
            quit_prompt_pending: AtomicBool::new(false),
        }
    }

    #[test]
    fn cancel_preserves_session_and_next_request_can_confirm() {
        let state = session();
        assert!(!state.authorize_exit(true, || false));
        assert!(!state.exiting.load(Ordering::SeqCst));
        assert!(!state.finished.load(Ordering::SeqCst));
        assert!(!state.quit_prompt_pending.load(Ordering::SeqCst));
        assert!(state.authorize_exit(true, || true));
        assert!(state.exiting.load(Ordering::SeqCst));
        assert!(!state.finished.load(Ordering::SeqCst)); // Backend still must acknowledge.
    }

    #[test]
    fn repeated_exit_during_prompt_or_shutdown_never_dispatches_twice() {
        let state = session();
        assert!(state.authorize_exit(true, || {
            assert!(!state.authorize_exit(true, || panic!("duplicate prompt")));
            true
        }));
        assert!(!state.authorize_exit(true, || panic!("prompt during shutdown")));
    }

    #[test]
    fn unconfirmed_backend_stop_rearms_confirmation_and_retains_ownership() {
        let state = session();
        assert!(state.authorize_exit(true, || true));
        state.exiting.store(false, Ordering::SeqCst);
        state.quit_confirmed.store(false, Ordering::SeqCst);
        assert!(!state.finished.load(Ordering::SeqCst));
        assert!(!state.authorize_exit(true, || false));
        assert!(state.authorize_exit(true, || true));
    }

    #[test]
    fn loading_and_error_pages_keep_cooperative_shutdown_without_a_draft_prompt() {
        let state = session();
        assert!(state.authorize_exit(false, || panic!("no editor on this page")));
        assert!(!state.finished.load(Ordering::SeqCst));
        assert!(!state.authorize_exit(false, || true));
    }
}
