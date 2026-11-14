//! Self-Reference Engine 的桌面壳。
//!
//! 壳只做三件事：
//! 1. 以子进程方式拉起仓库里的启动器（python launch.py --backend-only --no-window --no-browser），
//!    它自己会找到 `backend/.venv` 并起 uvicorn；
//! 2. 等 /api/health 就绪，把 http://127.0.0.1:8000 注入页面（window.__LUNAR_API_BASE__），
//!    让前端知道该把 /api 打到哪（页面来自 tauri://localhost，相对路径会打到壳自己身上）；
//! 3. 窗口关闭时杀掉后端进程树，不留孤儿 uvicorn。
//!
//! 不重写任何界面：web/ 目录（由 scripts/stage-web.mjs 从 frontend/dist 复制而来）就是
//! 原来的 React 应用，壳只负责“像桌面软件一样双击就能开”。

use std::io::Read;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent};

/// 后端进程句柄：退出时统一杀掉。
struct Backend(Mutex<Option<Child>>);

/// 默认后端端口；被占用时由启动器自己报错，方便用户排查（而不是悄悄换端口导致配置失联）。
const DEFAULT_PORT: u16 = 8000;

/// 仓库根目录：开发时是 `desktop/` 的上一级；打包后是 exe 所在目录（或 LUNAR_REPO_ROOT）。
fn repo_root() -> PathBuf {
    if let Ok(dir) = std::env::var("LUNAR_REPO_ROOT") {
        let p = PathBuf::from(dir);
        if p.join("launch.py").is_file() {
            return p;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let deep = dir.join("..").join("..").join("..").join("..");
            for candidate in [dir.to_path_buf(), deep] {
                if candidate.join("launch.py").is_file() {
                    return candidate.canonicalize().unwrap_or(candidate);
                }
            }
        }
    }
    // 开发期从 target/debug 往上找。
    let mut cur = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    loop {
        if cur.join("launch.py").is_file() {
            return cur;
        }
        if !cur.pop() {
            break;
        }
    }
    PathBuf::from(".")
}

fn python_exe(root: &PathBuf) -> PathBuf {
    if let Ok(custom) = std::env::var("LUNAR_PYTHON") {
        if !custom.trim().is_empty() {
            return PathBuf::from(custom);
        }
    }
    for rel in [
        ["backend", ".venv", "Scripts", "python.exe"],
        ["backend", ".venv", "bin", "python"],
        ["backend", "venv", "Scripts", "python.exe"],
    ] {
        let c = rel.iter().fold(root.clone(), |acc, part| acc.join(part));
        if c.is_file() {
            return c;
        }
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

/// 极简 HTTP GET：只关心 /api/health 是否返回 200，不值得为此拉一个 HTTP 客户端依赖。
fn http_ok(url: &str) -> bool {
    let stripped = url.trim_start_matches("http://");
    let (hostport, path) = match stripped.split_once('/') {
        Some((h, p)) => (h, format!("/{}", p)),
        None => (stripped, "/".to_string()),
    };
    let Ok(mut stream) = TcpStream::connect(hostport) else {
        return false;
    };
    stream.set_read_timeout(Some(Duration::from_secs(3))).ok();
    let req = format!("GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n", path, hostport);
    if std::io::Write::write_all(&mut stream, req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    stream.read_to_string(&mut buf).ok();
    buf.starts_with("HTTP/1.1 200") || buf.starts_with("HTTP/1.0 200")
}

fn wait_health(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let url = format!("http://127.0.0.1:{}/api/health", port);
    while Instant::now() < deadline {
        if http_ok(&url) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

fn start_backend(root: &PathBuf, port: u16) -> Option<Child> {
    let py = python_exe(root);
    let mut cmd = Command::new(py);
    cmd.current_dir(root)
        .arg("launch.py")
        .arg("--backend-only")
        .arg("--no-window")
        .arg("--no-browser")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .env("LUNAR_BACKEND_PORT", port.to_string())
        .env(
            "LUNAR_CORS_ORIGINS",
            "tauri://localhost,http://tauri.localhost,https://tauri.localhost",
        )
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW：不弹黑框
    }
    cmd.spawn().ok()
}

/// 把 Box<dyn Error> 的整条 source 链打到 stderr：WebView2/Windows 的真实原因常在被包的
/// 内层，只打印最外层会得到一句没有信息量的 "拒绝访问 (os error 5)"。
fn log_error_chain(what: &str, err: &dyn std::error::Error) {
    eprintln!("[self-engine] {}: {}", what, err);
    let mut src = err.source();
    while let Some(e) = src {
        eprintln!("[self-engine]   caused by: {}", e);
        src = e.source();
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let root = repo_root();
            let port = DEFAULT_PORT;
            let child = start_backend(&root, port);
            eprintln!(
                "[self-engine] repo_root={} python={} backend_spawned={}",
                root.display(),
                python_exe(&root).display(),
                child.is_some()
            );
            *app.state::<Backend>().0.lock().unwrap() = child;

            let api_base = format!("http://127.0.0.1:{}", port);
            let injected = format!(
                "window.__LUNAR_API_BASE__ = {};",
                serde_json::to_string(&api_base).unwrap_or_else(|_| "''".into())
            );
            if let Some(win) = app.get_webview_window("main") {
                let win2 = win.clone();
                std::thread::spawn(move || {
                    // 页面先渲染出来（此时后端可能还没起），就绪后再注入地址并通知前端重试。
                    let script = format!(
                        "{} window.dispatchEvent(new Event('lunar:backend-ready'));",
                        injected
                    );
                    if wait_health(port, Duration::from_secs(180)) {
                        let _ = win2.eval(&script);
                    } else {
                        let msg = format!(
                            "{} console.error('[self-engine] backend not healthy on {}');",
                            injected, api_base
                        );
                        let _ = win2.eval(&msg);
                    }
                    let _ = win2;
                });
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                if let Some(state) = app_handle.try_state::<Backend>() {
                    if let Ok(mut guard) = state.0.lock() {
                        if let Some(mut child) = guard.take() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}
