use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use tauri::{Emitter, Manager, Window};

fn project_root() -> PathBuf {
    // 开发期：desktop/src-tauri → 上两级是工程根；可用 V2T_ROOT 覆盖
    if let Ok(v) = std::env::var("V2T_ROOT") {
        return PathBuf::from(v);
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

fn python_bin(root: &Path) -> PathBuf {
    let venv = root.join(".venv/bin/python");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python3")
    }
}

/// 前端加载自检：写标记文件，便于排查白屏/黑屏
#[tauri::command]
fn ui_ready(msg: String) -> Result<(), String> {
    let root = project_root();
    let dir = root.join("work");
    let _ = std::fs::create_dir_all(&dir);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let _ = std::fs::write(dir.join("ui_ready.log"), format!("{now}|{msg}\n"));
    Ok(())
}

/// 调 bin/settings.py，返回其 stdout
fn run_settings(args: &[&str]) -> Result<String, String> {
    let root = project_root();
    let out = Command::new(python_bin(&root))
        .arg(root.join("bin/settings.py"))
        .args(args)
        .current_dir(&root)
        .output()
        .map_err(|e| format!("无法执行 python: {e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
    if stdout.is_empty() && !stderr.is_empty() {
        return Err(stderr.chars().take(400).collect());
    }
    Ok(stdout)
}

#[tauri::command]
fn get_settings() -> Result<String, String> {
    run_settings(&["read"])
}

#[tauri::command]
fn save_settings(payload: String) -> Result<String, String> {
    run_settings(&["write", &payload])
}

#[tauri::command]
fn test_backend() -> Result<String, String> {
    run_settings(&["test"])
}

/// 读取 config.json，返回整理后端的可读描述
#[tauri::command]
fn backend_status() -> Result<String, String> {
    let root = project_root();
    let out = Command::new(python_bin(&root))
        .arg(root.join("bin/v2t.py"))
        .arg("--show-backend")
        .current_dir(&root)
        .output()
        .map_err(|e| format!("无法执行 python: {e}"))?;
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        Ok("未配置（默认本地）".into())
    } else {
        Ok(s)
    }
}

/// 启动转写：后台线程跑 python，逐行把输出推给前端
#[tauri::command]
fn start_run(window: Window, link: String, glossary: String, backend: String, polish: bool) -> Result<(), String> {
    let root = project_root();
    std::thread::spawn(move || {
        let mut cmd = Command::new(python_bin(&root));
        cmd.arg(root.join("bin/v2t.py"))
            .arg(&link)
            .current_dir(&root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        if !glossary.is_empty() {
            cmd.arg("--glossary").arg(&glossary);
        }
        if !backend.is_empty() {
            cmd.arg("--backend").arg(&backend);
        }
        if polish {
            cmd.arg("--polish");
        }

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                let _ = window.emit("v2t-log", format!("启动失败: {e}"));
                let _ = window.emit("v2t-done", serde_json::json!({"code": -1, "md": ""}));
                return;
            }
        };

        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();

        let w2 = window.clone();
        let err_thread = std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().flatten() {
                let _ = w2.emit("v2t-log", line);
            }
        });

        let mut md = String::new();
        for line in BufReader::new(stdout).lines().flatten() {
            if let Some(p) = line.trim().strip_prefix("文稿:") {
                md = p.trim().to_string();
            }
            let _ = window.emit("v2t-log", line);
        }
        let _ = err_thread.join();
        let code = child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
        let _ = window.emit("v2t-done", serde_json::json!({"code": code, "md": md}));
    });
    Ok(())
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    Command::new("open")
        .arg(&path)
        .status()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn open_out_dir() -> Result<(), String> {
    let root = project_root();
    let dir = root.join("out");
    Command::new("open").arg(&dir).status().map_err(|e| e.to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            backend_status,
            ui_ready,
            get_settings,
            save_settings,
            test_backend,
            start_run,
            open_path,
            open_out_dir
        ])
        .setup(|app| {
            let _ = app.get_webview_window("main");
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("运行 Tauri 应用失败");
}
