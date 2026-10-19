#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tauri::{Emitter, Manager};
use tokio::sync::oneshot;

type Reply = Result<Value, String>;

struct Bridge {
    child: Mutex<Child>,
    input: Mutex<ChildStdin>,
    pending: Mutex<HashMap<u64, oneshot::Sender<Reply>>>,
    sequence: AtomicU64,
    active: AtomicBool,
    closing: AtomicBool,
    #[cfg(windows)]
    job: isize,
}

impl Bridge {
    fn stop(&self) {
        if self.closing.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Ok(mut input) = self.input.lock() {
            let _ = writeln!(
                input,
                "{}",
                json!({"id":0,"method":"app.shutdown","params":{}})
            );
            let _ = input.flush();
        }
        if let Ok(mut child) = self.child.lock() {
            for _ in 0..30 {
                if child.try_wait().ok().flatten().is_some() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGTERM);
            }
            #[cfg(windows)]
            unsafe {
                windows_sys::Win32::System::JobObjects::TerminateJobObject(self.job as _, 1);
                windows_sys::Win32::Foundation::CloseHandle(self.job as _);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn launch(app: &tauri::AppHandle) -> Result<Arc<Bridge>, String> {
    let mut command;
    if cfg!(debug_assertions) {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .to_owned();
        #[cfg(windows)]
        {
            command = Command::new("cmd");
            command.arg("/c").arg(root.join("run.cmd"));
        }
        #[cfg(not(windows))]
        {
            command = Command::new(root.join("run.sh"));
        }
        command.arg("desktop-server").current_dir(root);
    } else {
        let resources = app
            .path()
            .resource_dir()
            .map_err(|e| e.to_string())?
            .join("resources");
        let executable = if cfg!(windows) {
            "pycut-service.exe"
        } else {
            "pycut-service"
        };
        command = Command::new(resources.join("runtime").join(executable));
        let suffix = if cfg!(windows) { ".exe" } else { "" };
        command
            .env(
                "PYCUT_FFMPEG",
                resources.join("media").join(format!("ffmpeg{suffix}")),
            )
            .env(
                "PYCUT_FFPROBE",
                resources.join("media").join(format!("ffprobe{suffix}")),
            )
            .env("PYCUT_FONT_DIR", resources.join("fonts"));
    }
    let data = if cfg!(debug_assertions) {
        std::env::var_os("PYCUT_DATA_DIR").map(PathBuf::from)
    } else {
        None
    }
    .unwrap_or(app.path().app_data_dir().map_err(|e| e.to_string())?);
    let cache = if cfg!(debug_assertions) {
        std::env::var_os("PYCUT_CACHE_DIR").map(PathBuf::from)
    } else {
        None
    }
    .unwrap_or(app.path().app_cache_dir().map_err(|e| e.to_string())?);
    std::fs::create_dir_all(&data).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&cache).map_err(|e| e.to_string())?;
    command
        .env("PYCUT_DATA_DIR", &data)
        .env("PYCUT_CACHE_DIR", &cache)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("无法启动本地服务：{e}"))?;
    #[cfg(windows)]
    let job = unsafe {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::*;
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if handle.is_null()
            || SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as _,
                std::mem::size_of_val(&limits) as u32,
            ) == 0
            || AssignProcessToJobObject(handle, child.as_raw_handle() as _) == 0
        {
            let _ = child.kill();
            if !handle.is_null() {
                windows_sys::Win32::Foundation::CloseHandle(handle);
            }
            return Err("无法创建安全的后台进程组".into());
        }
        handle as isize
    };
    let output = child.stdout.take().ok_or("缺少服务输出")?;
    let input = child.stdin.take().ok_or("缺少服务输入")?;
    let bridge = Arc::new(Bridge {
        child: Mutex::new(child),
        input: Mutex::new(input),
        pending: Mutex::new(HashMap::new()),
        sequence: AtomicU64::new(1),
        active: AtomicBool::new(false),
        closing: AtomicBool::new(false),
        #[cfg(windows)]
        job,
    });
    let reader = bridge.clone();
    let app = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(output).lines().map_while(Result::ok) {
            let Ok(message) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if let Some(id) = message.get("id").and_then(Value::as_u64) {
                if let Some(reply) = reader.pending.lock().unwrap().remove(&id) {
                    let value = if let Some(error) = message.get("error") {
                        Err(error
                            .get("message")
                            .and_then(Value::as_str)
                            .unwrap_or("本地服务请求失败")
                            .to_owned())
                    } else {
                        Ok(message.get("result").cloned().unwrap_or(Value::Null))
                    };
                    let _ = reply.send(value);
                }
            } else if message.get("event").is_some() {
                if message.get("event").and_then(Value::as_str) == Some("job") {
                    if let Some(status) = message.pointer("/payload/status").and_then(Value::as_str)
                    {
                        reader
                            .active
                            .store(matches!(status, "running" | "cancelling"), Ordering::SeqCst);
                    }
                }
                let _ = app.emit("pycut-event", message);
            }
        }
        for (_, reply) in reader.pending.lock().unwrap().drain() {
            let _ = reply.send(Err("本地服务已退出，请重新启动应用".into()));
        }
        let _ = app.emit(
            "pycut-event",
            json!({"event":"log","payload":{"level":"error","message":"本地服务已停止"}}),
        );
    });
    Ok(bridge)
}

#[tauri::command]
async fn rpc(
    app: tauri::AppHandle,
    state: tauri::State<'_, Arc<Bridge>>,
    method: String,
    params: Value,
) -> Reply {
    if state.closing.load(Ordering::SeqCst) {
        return Err("应用正在退出".into());
    }
    let id = state.sequence.fetch_add(1, Ordering::SeqCst);
    let (sender, receiver) = oneshot::channel();
    state.pending.lock().unwrap().insert(id, sender);
    let write = (|| {
        let mut input = state
            .input
            .lock()
            .map_err(|_| "本地服务输入已关闭".to_string())?;
        writeln!(
            input,
            "{}",
            json!({"id":id,"method":method,"params":params})
        )
        .map_err(|e| e.to_string())?;
        input.flush().map_err(|e| e.to_string())
    })();
    if let Err(error) = write {
        state.pending.lock().unwrap().remove(&id);
        return Err(error);
    }
    let timeout = if method == "libraries.scan" { 3600 } else { 120 };
    let result = match tokio::time::timeout(Duration::from_secs(timeout), receiver).await {
        Ok(Ok(result)) => result,
        _ => {
            state.pending.lock().unwrap().remove(&id);
            Err("本地服务响应超时，请检查任务状态".into())
        }
    }?;
    if matches!(
        method.as_str(),
        "media.thumbnail" | "media.preview" | "media.output_preview"
    ) {
        if let Some(path) = result.get("path").and_then(Value::as_str) {
            app.asset_protocol_scope()
                .allow_file(path)
                .map_err(|e| e.to_string())?;
        }
    }
    Ok(result)
}

fn existing_path(path: &str) -> Result<PathBuf, String> {
    let path = Path::new(path);
    if !path.is_absolute() {
        return Err("路径必须是绝对路径".into());
    }
    path.canonicalize().map_err(|e| e.to_string())
}

#[tauri::command]
fn reveal_path(path: String) -> Result<(), String> {
    let path = existing_path(&path)?;
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut c = Command::new("open");
        c.arg("-R").arg(path);
        c
    };
    #[cfg(windows)]
    let mut command = {
        let mut c = Command::new("explorer.exe");
        c.arg(format!("/select,{}", path.display()));
        c
    };
    #[cfg(not(any(windows, target_os = "macos")))]
    let mut command = {
        let mut c = Command::new("xdg-open");
        c.arg(path.parent().unwrap_or(&path));
        c
    };
    command.spawn().map(|_| ()).map_err(|e| e.to_string())
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    let path = existing_path(&path)?;
    #[cfg(target_os = "macos")]
    let mut command = {
        let mut c = Command::new("open");
        c.arg(path);
        c
    };
    #[cfg(windows)]
    let mut command = {
        let mut c = Command::new("explorer.exe");
        c.arg(path);
        c
    };
    #[cfg(not(any(windows, target_os = "macos")))]
    let mut command = {
        let mut c = Command::new("xdg-open");
        c.arg(path);
        c
    };
    command.spawn().map(|_| ()).map_err(|e| e.to_string())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let bridge = launch(app.handle()).map_err(std::io::Error::other)?;
            app.manage(bridge);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![rpc, reveal_path, open_path])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let bridge = window.state::<Arc<Bridge>>();
                if bridge.active.load(Ordering::SeqCst) {
                    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
                    api.prevent_close();
                    let window = window.clone();
                    window
                        .app_handle()
                        .dialog()
                        .message("正在处理视频。退出将停止任务并保留已完成成片。")
                        .title("退出 pycut？")
                        .kind(MessageDialogKind::Warning)
                        .buttons(MessageDialogButtons::OkCancel)
                        .show(move |confirmed| {
                            if confirmed {
                                window.state::<Arc<Bridge>>().stop();
                                window.app_handle().exit(0);
                            }
                        });
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("无法启动 pycut 桌面应用");
    app.run(|handle, event| {
        if let tauri::RunEvent::Exit = event {
            handle.state::<Arc<Bridge>>().stop();
        }
    });
}
