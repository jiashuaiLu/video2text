use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use tauri::{Emitter, Manager, Window};

/// GUI 启动的 App 只拿到 /usr/bin:/bin:/usr/sbin:/sbin，
/// 这里把 Homebrew 等常见路径补进 PATH，保证 python / ffmpeg 能被找到。
fn ensure_path() {
    const EXTRA: [&str; 3] = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/sbin"];
    let cur = std::env::var("PATH").unwrap_or_default();
    let mut parts: Vec<String> = cur
        .split(':')
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect();
    for e in EXTRA {
        if Path::new(e).exists() && !parts.iter().any(|p| p == e) {
            parts.push(e.to_string());
        }
    }
    std::env::set_var("PATH", parts.join(":"));
}

fn project_root() -> PathBuf {
    // 1) 显式指定优先
    if let Ok(v) = std::env::var("V2T_ROOT") {
        return PathBuf::from(v);
    }
    // 2) 开发模式：工程根目录下有 .venv 就用它
    let dev = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."));
    if dev.join(".venv").exists() {
        return dev;
    }
    // 3) 安装模式：用户数据目录
    data_root()
}

fn data_root() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    let p = PathBuf::from(home).join("Library/Application Support/video2text");
    let _ = std::fs::create_dir_all(&p);
    p
}

fn python_bin(root: &Path) -> PathBuf {
    let venv = root.join(".venv/bin/python");
    if venv.exists() {
        venv
    } else {
        PathBuf::from("python3")
    }
}

/// 递归统计目录大小（MB，跟随符号链接）
fn dir_mb(p: &Path) -> u64 {
    fn walk(p: &Path, acc: &mut u64) {
        let md = match std::fs::metadata(p) { Ok(m) => m, Err(_) => return };
        if md.is_file() { *acc += md.len(); return; }
        if let Ok(rd) = std::fs::read_dir(p) {
            for e in rd.flatten() { walk(&e.path(), acc); }
        }
    }
    let mut n = 0u64;
    walk(p, &mut n);
    n / (1024 * 1024)
}

fn model_dir(root: &Path, slug: &str) -> PathBuf {
    root.join("work/hf/hub").join(format!("models--mlx-community--{slug}"))
}

/// 模型是否已就绪（用 huggingface_hub 校验文件完整性，避免体积误判）
#[tauri::command]
fn models_status() -> serde_json::Value {
    let root = project_root();
    if !root.join(".venv/bin/python").exists() {
        return serde_json::json!({"asr_ready": false, "polish_ready": false, "env_missing": true});
    }
    let script = r#"
import json
from huggingface_hub import snapshot_download
out = {}
for k, repo in (("asr", "mlx-community/Qwen3-ASR-1.7B-8bit"),
                ("polish", "mlx-community/Qwen3.5-9B-4bit")):
    try:
        snapshot_download(repo, local_files_only=True)
        out[k + "_ready"] = True
    except Exception:
        out[k + "_ready"] = False
print(json.dumps(out))
"#;
    let out = Command::new(python_bin(&root))
        .args(["-c", script])
        .env("HF_HOME", root.join("work/hf"))
        .output();
    match out {
        Ok(o) if o.status.success() => {
            let txt = String::from_utf8_lossy(&o.stdout).trim().to_string();
            serde_json::from_str(&txt).unwrap_or(serde_json::json!({"asr_ready": false, "polish_ready": false}))
        }
        _ => serde_json::json!({"asr_ready": false, "polish_ready": false}),
    }
}

fn do_download(kind: &str, root: PathBuf, emit: &dyn Fn(String)) -> bool {
    let repo = match kind {
        "asr" => "mlx-community/Qwen3-ASR-1.7B-8bit",
        "polish" => "mlx-community/Qwen3.5-9B-4bit",
        _ => return false,
    };
    let dir = model_dir(&root, if kind == "asr" { "Qwen3-ASR-1.7B-8bit" } else { "Qwen3.5-9B-4bit" });
    // 下载依赖 huggingface_hub，必须先完成运行环境初始化
    if !root.join(".venv/bin/python").exists() {
        emit("请先完成「开始准备」，再下载模型".into());
        return false;
    }
    let py = python_bin(&root);
    let script = format!("from huggingface_hub import snapshot_download\nsnapshot_download('{repo}')\n");
    let mut child = match Command::new(&py)
        .args(["-c", &script])
        .env("HF_HOME", root.join("work/hf"))
        .env("HF_ENDPOINT", "https://hf-mirror.com")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => { emit(format!("启动下载失败: {e}")); return false; }
    };
    loop {
        std::thread::sleep(std::time::Duration::from_millis(1500));
        emit(format!("{} MB", dir_mb(&dir)));
        if let Ok(Some(_)) = child.try_wait() {
            break;
        }
    }
    let out = child.wait_with_output();
    match out {
        Ok(o) if o.status.success() => true,
        Ok(o) => {
            let err = String::from_utf8_lossy(&o.stderr);
            let tail: String = err.chars().rev().take(300).collect::<String>().chars().rev().collect();
            emit(format!("下载失败：{}", tail.replace('\n', " ")));
            false
        }
        Err(_) => false,
    }
}

/// 一键下载模型：型如 asr / polish，边下边推进度
#[tauri::command]
fn download_models(kind: String, window: Window) -> Result<(), String> {
    let root = project_root();
    std::thread::spawn(move || {
        let w = window.clone();
        let ok = do_download(&kind, root, &|msg| {
            let _ = w.emit("model-progress", serde_json::json!({"kind": kind, "text": msg}));
        });
        let _ = window.emit("model-done", serde_json::json!({"kind": kind, "ok": ok}));
    });
    Ok(())
}

/// 环境自检：python3 / ffmpeg / Homebrew
#[tauri::command]
fn check_env() -> serde_json::Value {
    let has = |cmd: &str, flag: &str| {
        // 先按名称找，再在 Homebrew 常见目录里找
        for cand in [
            cmd.to_string(),
            format!("/opt/homebrew/bin/{cmd}"),
            format!("/usr/local/bin/{cmd}"),
            format!("/usr/bin/{cmd}"),
        ] {
            if Command::new(&cand)
                .arg(flag)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| s.success())
                .unwrap_or(false)
            {
                return true;
            }
        }
        false
    };
    let brew = Path::new("/opt/homebrew/bin/brew").exists()
        || Path::new("/usr/local/bin/brew").exists();
    serde_json::json!({
        "python3": has("python3", "--version"),
        "ffmpeg": has("ffmpeg", "-version"),
        "brew": brew,
    })
}

/// 复制到剪贴板（WKWebView 里 navigator.clipboard 不一定可用）
#[tauri::command]
fn copy_text(text: String) -> Result<(), String> {
    use std::io::Write;
    let mut child = Command::new("pbcopy")
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
    }
    let _ = child.wait();
    Ok(())
}

/// 是否需要首次初始化
#[tauri::command]
fn bootstrap_needed() -> bool {
    let root = project_root();
    !(root.join(".venv/bin/python").exists() && root.join("bin/v2t.py").exists())
}

/// 首次初始化：拷贝脚本 → 建 venv → 装依赖，逐行把进度推给前端
fn do_bootstrap(root: PathBuf, res_dir: PathBuf, emit: &dyn Fn(String)) -> Result<(), String> {
    emit("[初始化] 准备脚本…".into());
    let dst_bin = root.join("bin");
    let _ = std::fs::create_dir_all(&dst_bin);
    let src_bin = res_dir.join("bin");
    if src_bin.exists() {
        if let Ok(entries) = std::fs::read_dir(&src_bin) {
            for e in entries.flatten() {
                let p = e.path();
                if p.extension().map(|x| x == "py").unwrap_or(false) {
                    let _ = std::fs::copy(&p, dst_bin.join(p.file_name().unwrap()));
                }
            }
        }
    }
    if !dst_bin.join("v2t.py").exists() {
        emit("[初始化-失败] 缺少内置脚本，请重新安装".into());
        return Err("缺少内置脚本".into());
    }

    let venv = root.join(".venv");
    if !venv.join("bin/python").exists() {
        emit("[初始化] 创建 Python 环境…".into());
        let out = Command::new("python3")
            .args(["-m", "venv", venv.to_str().unwrap_or(".venv")])
            .output();
        match out {
            Ok(o) if o.status.success() => {}
            _ => {
                emit("[初始化-失败] 找不到 python3。请先安装：brew install python".into());
                return Err("缺少 python3".into());
            }
        }
    }

    emit("[初始化] 安装依赖（约 2-3 分钟）…".into());
    let pip = venv.join("bin/pip");
    let mirror = "https://pypi.tuna.tsinghua.edu.cn/simple";
    let pkgs = ["yt-dlp", "mlx-whisper", "mlx-lm", "mlx-audio",
                "pypinyin", "pillow", "websocket-client"];
    let mut cmd = Command::new(&pip);
    cmd.arg("install").arg("-q").arg("-i").arg(mirror);
    for p in pkgs {
        cmd.arg(p);
    }
    match cmd.output() {
        Ok(o) if o.status.success() => emit("[初始化] 依赖安装完成".into()),
        Ok(o) => {
            let msg = String::from_utf8_lossy(&o.stderr).chars().take(300).collect::<String>();
            emit(format!("[初始化-失败] 依赖安装出错：{msg}"));
            return Err("pip 安装失败".into());
        }
        Err(e) => {
            emit(format!("[初始化-失败] 无法执行 pip：{e}"));
            return Err(format!("pip: {e}"));
        }
    }

    if Command::new("ffmpeg").arg("-version").output()
        .map(|o| o.status.success()).unwrap_or(false) {
        emit("[初始化] ffmpeg 已就绪".into());
    } else {
        emit("[提示] 未检测到 ffmpeg，转写前请执行：brew install ffmpeg".into());
    }
    emit("[初始化] 完成，可以开始使用了".into());
    Ok(())
}

#[tauri::command]
fn bootstrap(window: Window) -> Result<(), String> {
    let root = project_root();
    let res_dir = window
        .path()
        .resource_dir()
        .map_err(|e| format!("找不到资源目录: {e}"))?;
    std::thread::spawn(move || {
        let w = window.clone();
        let res = do_bootstrap(root, res_dir, &|s| {
            let _ = w.emit("v2t-log", s);
        });
        let code = if res.is_ok() { 0 } else { -1 };
        let _ = window.emit("v2t-done", serde_json::json!({"code": code, "md": ""}));
    });
    Ok(())
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
    // GUI 启动拿不到 shell 的 PATH，先补齐再干别的
    ensure_path();

    // 命令行入口：--models-status / --download-model asr
    if std::env::args().any(|a| a == "--models-status") {
        println!("{}", models_status());
        return;
    }
    if let Some(pos) = std::env::args().position(|a| a == "--download-model") {
        if let Some(kind) = std::env::args().nth(pos + 1) {
            let root = project_root();
            let ok = do_download(&kind, root, &|m| println!("  {m}"));
            println!("结果: {}", if ok { "OK" } else { "失败" });
            return;
        }
    }

    // 命令行自检入口：video2text --check-env
    if std::env::args().any(|a| a == "--check-env") {
        println!("{}", check_env());
        return;
    }

    // 命令行自检入口：video2text --bootstrap
    if std::env::args().any(|a| a == "--bootstrap") {
        let root = project_root();
        let exe = std::env::current_exe().unwrap_or_default();
        let res_dir = exe.parent().map(|p| p.join("../Resources").canonicalize().unwrap_or_else(|_| p.join("../Resources"))).unwrap_or_default();
        println!("数据目录: {}", root.display());
        println!("资源目录: {}", res_dir.display());
        match do_bootstrap(root, res_dir, &|s| println!("{s}")) {
            Ok(_) => println!("结果: OK"),
            Err(e) => println!("结果: 失败（{e}）"),
        }
        return;
    }

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            backend_status,
            ui_ready,
            bootstrap_needed,
            check_env,
            models_status,
            download_models,
            copy_text,
            bootstrap,
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
