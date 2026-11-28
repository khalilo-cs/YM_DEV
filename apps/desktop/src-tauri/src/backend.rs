//! Finds, starts, checks and stops the local platform (`yemendev serve`) for the desktop shell.
//!
//! Nothing here creates credentials: a data directory that was never initialised is reported with the
//! command that initialises it (`yemendev init`), instead of minting an admin token silently.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde::Deserialize;
use url::Url;

pub const DEFAULT_PORT: u16 = 8765;
const START_TIMEOUT: Duration = Duration::from_secs(90);
const LOG_NAME: &str = "desktop-server.log";

#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub bin: PathBuf,
    pub data_dir: PathBuf,
    pub port: u16,
}

#[derive(Debug, Default, Deserialize)]
struct FileConfig {
    bin: Option<PathBuf>,
    data_dir: Option<PathBuf>,
    port: Option<u16>,
}

fn config_file() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))?;
    Some(base.join("yemendev").join("desktop.json"))
}

fn find_on_path(name: &str) -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).map(|d| d.join(name)).find(|p| p.is_file())
    })
}

impl Config {
    /// Environment (`YEMENDEV_BIN`, `YEMENDEV_DATA_DIR`, `YEMENDEV_DESKTOP_PORT`) over
    /// `~/.config/yemendev/desktop.json` over defaults (`yemendev` on PATH, `~/.local/share/yemendev`, 8765).
    pub fn resolve() -> Result<Config, String> {
        let file: FileConfig = match config_file() {
            Some(p) if p.is_file() => {
                let text = fs::read_to_string(&p).map_err(|e| format!("cannot read {}: {e}", p.display()))?;
                serde_json::from_str(&text).map_err(|e| format!("invalid {}: {e}", p.display()))?
            }
            _ => FileConfig::default(),
        };
        let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty());
        let bin = env("YEMENDEV_BIN")
            .map(PathBuf::from)
            .or(file.bin)
            .or_else(|| find_on_path("yemendev"))
            .ok_or("the `yemendev` command was not found (set YEMENDEV_BIN or install the platform)")?;
        let data_dir = env("YEMENDEV_DATA_DIR")
            .map(PathBuf::from)
            .or(file.data_dir)
            .or_else(|| {
                std::env::var_os("XDG_DATA_HOME")
                    .map(PathBuf::from)
                    .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")))
                    .map(|d| d.join("yemendev"))
            })
            .ok_or("no data directory (set YEMENDEV_DATA_DIR)")?;
        let port = match env("YEMENDEV_DESKTOP_PORT") {
            Some(v) => v
                .to_str()
                .and_then(|s| s.parse::<u16>().ok())
                .filter(|p| *p > 0)
                .ok_or("YEMENDEV_DESKTOP_PORT must be a port number")?,
            None => file.port.unwrap_or(DEFAULT_PORT),
        };
        Ok(Config { bin, data_dir, port })
    }
}

#[derive(Debug, PartialEq)]
pub enum Probe {
    NotListening,
    Yemendev(String),
    Other(String),
}

/// Parse a raw HTTP response to `GET /api/v1/health`.
pub fn parse_health(raw: &str) -> Probe {
    let (head, body) = raw.split_once("\r\n\r\n").unwrap_or((raw, ""));
    let status_ok = head.lines().next().map(|l| l.split_whitespace().nth(1) == Some("200")).unwrap_or(false);
    let json: Option<serde_json::Value> = serde_json::from_str(body.trim()).ok();
    match (status_ok, json) {
        (true, Some(v)) if v.get("status").and_then(|s| s.as_str()) == Some("ok") => {
            match v.get("version").and_then(|s| s.as_str()) {
                Some(ver) => Probe::Yemendev(ver.to_string()),
                None => Probe::Other("a health endpoint without a version".into()),
            }
        }
        _ => Probe::Other(head.lines().next().unwrap_or("no HTTP response").chars().take(120).collect()),
    }
}

pub fn probe(port: u16) -> Probe {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = match TcpStream::connect_timeout(&addr, Duration::from_millis(500)) {
        Ok(s) => s,
        Err(_) => return Probe::NotListening,
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = s.set_write_timeout(Some(Duration::from_secs(3)));
    if s.write_all(b"GET /api/v1/health HTTP/1.0\r\nHost: 127.0.0.1\r\nAccept: application/json\r\n\r\n").is_err() {
        return Probe::Other("connection refused the request".into());
    }
    let mut buf = Vec::new();
    let _ = s.take(64 * 1024).read_to_end(&mut buf);
    parse_health(&String::from_utf8_lossy(&buf))
}

/// The platform UI, or the shell's own page: nothing else may be loaded in the window.
pub fn navigation_allowed(url: &Url, origin: &str) -> bool {
    match url.scheme() {
        "tauri" => true,
        "http" | "https" if url.host_str() == Some("tauri.localhost") => true,
        "http" => url.as_str().starts_with(&format!("{origin}/")),
        _ => false,
    }
}

pub struct Server {
    pub port: u16,
    pub version: String,
    child: Option<Child>,
}

impl Server {
    pub fn ui_url(&self) -> Url {
        Url::parse(&format!("http://127.0.0.1:{}/app/", self.port)).expect("valid url")
    }

    pub fn started_here(&self) -> bool {
        self.child.is_some()
    }

    /// The child process, if this shell started it. Whoever takes it must keep the taking thread alive while
    /// the server runs: on Linux the parent-death signal follows the *thread* that spawned the process.
    pub fn take_child(&mut self) -> Option<Child> {
        self.child.take()
    }

    pub fn log_path(data_dir: &Path) -> PathBuf {
        data_dir.join(LOG_NAME)
    }
}

/// Ask a server process group to stop: SIGTERM now; the caller decides when to escalate.
#[cfg(unix)]
pub fn signal_group(pid: i32, kill: bool) {
    unsafe { libc::kill(-pid, if kill { libc::SIGKILL } else { libc::SIGTERM }) };
}

#[cfg(not(unix))]
pub fn signal_group(_pid: i32, _kill: bool) {}

#[cfg(unix)]
fn terminate(child: &mut Child) {
    let pg = child.id() as i32;
    unsafe { libc::kill(-pg, libc::SIGTERM) };
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    unsafe { libc::kill(-pg, libc::SIGKILL) };
    let _ = child.wait();
}

#[cfg(not(unix))]
fn terminate(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

pub fn tail(path: &Path, lines: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

/// Reuse a running platform on the port, or start `yemendev serve` and wait until it answers.
pub fn ensure(cfg: &Config) -> Result<Server, String> {
    match probe(cfg.port) {
        Probe::Yemendev(version) => return Ok(Server { port: cfg.port, version, child: None }),
        Probe::Other(what) => {
            return Err(format!(
                "port {} on 127.0.0.1 is used by another program ({what}); set YEMENDEV_DESKTOP_PORT",
                cfg.port
            ))
        }
        Probe::NotListening => {}
    }
    if !cfg.data_dir.join("yemendev.sqlite3").is_file() {
        return Err(format!(
            "the data directory {} is not initialised. Create it and the first admin token with:\n  \
             YEMENDEV_DATA_DIR={} {} init\n(or run scripts/install.sh), then reopen the app.",
            cfg.data_dir.display(),
            cfg.data_dir.display(),
            cfg.bin.display()
        ));
    }
    let log_path = cfg.data_dir.join(LOG_NAME);
    let log: File = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|e| format!("cannot open {}: {e}", log_path.display()))?;
    let mut cmd = Command::new(&cfg.bin);
    cmd.args(["serve", "--host", "127.0.0.1", "--port", &cfg.port.to_string()])
        .env("YEMENDEV_DATA_DIR", &cfg.data_dir)
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
        // The server must not outlive the shell, even if the shell is killed.
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                Ok(())
            });
        }
    }
    let mut child = cmd.spawn().map_err(|e| format!("cannot start {}: {e}", cfg.bin.display()))?;
    let deadline = Instant::now() + START_TIMEOUT;
    while Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("`yemendev serve` exited ({status}). Last log lines:\n{}", tail(&log_path, 15)));
        }
        match probe(cfg.port) {
            Probe::Yemendev(version) => return Ok(Server { port: cfg.port, version, child: Some(child) }),
            Probe::Other(what) => {
                terminate(&mut child);
                return Err(format!("unexpected answer on port {}: {what}", cfg.port));
            }
            Probe::NotListening => std::thread::sleep(Duration::from_millis(250)),
        }
    }
    terminate(&mut child);
    Err(format!("the platform did not answer within {}s. Last log lines:\n{}", START_TIMEOUT.as_secs(), tail(&log_path, 15)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_is_recognised_only_with_status_and_version() {
        let ok = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n\r\n{\"status\":\"ok\",\"version\":\"0.1.0\",\"db\":\"ok\"}";
        assert_eq!(parse_health(ok), Probe::Yemendev("0.1.0".into()));
        let other = "HTTP/1.1 200 OK\r\n\r\n{\"status\":\"ok\"}";
        assert!(matches!(parse_health(other), Probe::Other(_)));
        assert!(matches!(parse_health("HTTP/1.1 404 Not Found\r\n\r\n{}"), Probe::Other(_)));
        assert!(matches!(parse_health("garbage"), Probe::Other(_)));
    }

    #[test]
    fn only_the_platform_origin_and_the_shell_page_can_be_shown() {
        let origin = "http://127.0.0.1:8765";
        let ok = |u: &str| navigation_allowed(&Url::parse(u).unwrap(), origin);
        assert!(ok("http://127.0.0.1:8765/app/"));
        assert!(ok("tauri://localhost/index.html?error=x"));
        assert!(!ok("http://127.0.0.1:8766/app/"));
        assert!(!ok("http://127.0.0.1:8765@evil.example/app/")); // userinfo trick: the host is evil.example
        assert!(!ok("http://localhost:8765/app/")); // same server, different origin: not the one the UI uses
        assert!(!ok("http://127.0.0.1.evil.example:8765/app/")); // look-alike host
        assert!(!ok("https://example.com/"));
        assert!(!ok("file:///etc/passwd"));
        assert!(!ok("javascript:alert(1)"));
    }

    #[test]
    fn nothing_listening_is_not_a_platform() {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        assert_eq!(probe(port), Probe::NotListening);
    }
}
