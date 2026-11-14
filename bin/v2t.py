#!/usr/bin/env python3
"""v2t — 视频链接 → 中文文稿（默认纯规则排版，可选大模型深度整理）

链路: 抓流 → 音频 → Qwen3-ASR 转写(自带标点) → 术语纠错 → 规则排版 → Markdown

三档:
    v2t "链接"            默认：转写 + 术语纠错 + 规则排版（约 40 秒）
    v2t "链接" --no-format 极速：只要转写和字幕（约 31 秒）
    v2t "链接" --polish    深度：再用大模型润色、分段、摘要（约 4 分钟）

用法:
    v2t --init-config     生成 config.json 模板
    v2t --show-backend    打印当前整理后端
    v2t --polish-only 某.txt
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import llm  # noqa: E402
import glossary as gl  # noqa: E402
import formatmd  # noqa: E402

PY = ROOT / ".venv/bin/python"
WORK = ROOT / "work"
COOKIE_FILE = WORK / "dy_cookies.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ASR_MODEL = "mlx-community/Qwen3-ASR-1.7B-8bit"

PUNCT_SYSTEM = (
    "你是中文编辑，任务是把语音识别稿整理成一篇可以直接阅读、直接发出去的文章。\n"
    "\n【要做的】\n"
    "1. 排版：碎句合并成通顺段落，每段只讲一件事、3-8 句，段间空行；"
    "多用句号断句，少用一逗到底。\n"
    "2. 纠错：修掉语音识别造成的错别字、同音词、专有名词错误"
    "（产品名、人名、公司名、技术名词），拿不准的保持原样，不要编造。\n"
    "3. 清理：删掉「呃」「嗯」「就是说」这类口头禅和明显重复的啰嗦话。\n"
    "\n【禁止的】\n"
    "1. 不得增删信息、不得改写观点、不得扩写或缩写原意。\n"
    "2. 不得添加标题、评论、总结、过渡句。\n"
    "3. 只输出整理后的正文，不要任何前后缀说明。"
)

SUMMARY_SYSTEM = (
    "你是内容编辑。只根据给定文稿生成结构化信息，不得编造。格式严格如下：\n"
    "## 一句话概括\n<一句话>\n\n## 关键要点\n- <要点>\n- <要点>\n- <要点>\n\n"
    "## 值得记下的数字\n- <数字+含义，没有就写「- 无」>\n"
    "要点 3-6 条，每条一行。"
)


def log(m):
    print(f"[v2t] {m}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def clean_title(t):
    t = re.split(r"\s*简介[：:]", t or "")[0]
    t = re.split(r"[，,]", t)[0]
    t = re.sub(r"\s*#\S+", "", t)
    return re.sub(r"\s+", " ", t).strip(" 。，、|")


TERM_STOP = {
    "the", "and", "for", "with", "you", "your", "app", "apps", "http", "https",
    "www", "com", "cn", "net", "ios", "mac", "macos", "iphone", "ipad",
    "android", "windows", "tiktok", "douyin", "video", "new", "how", "why",
}


def extract_terms(*texts, limit=18):
    """从标题/简介里自动抽取产品名、专有名词。"""
    blob = " ".join(t for t in texts if t)
    cands = []
    # 英文/中英混排的产品名
    for m in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", blob):
        if m.lower() in TERM_STOP or m.isdigit():
            continue
        if m not in cands:
            cands.append(m)
    # 书名号 / 引号里的中文名字
    for m in re.findall(r"[《「『\"]([^》」』\"]{2,14})[》」』\"]", blob):
        m = m.strip()
        if m and m not in cands:
            cands.append(m)
    # 话题标签：#砼核 #阈限空间 —— 视频主题词通常就藏在这里
    for m in re.findall(r"#([\u4e00-\u9fff]{2,10})", blob):
        if m not in cands:
            cands.insert(0, m)   # 标签更可信，排在前面
    return cands[:limit]


def safe_name(s, limit=60):
    s = re.sub(r'[\\/:*?"<>|#\n\r\t]', " ", s or "")
    return (re.sub(r"\s+", " ", s).strip()[:limit] or "untitled")


# ---------------- 抓流 ----------------

def douyin_id(url):
    for pat in (r"modal_id=(\d{15,})", r"/video/(\d{15,})", r"/note/(\d{15,})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    if "v.douyin.com" in url:
        r = run(["curl", "-sL", "-m", "20", "-A", UA, "-o", "/dev/null",
                 "-w", "%{url_effective}", url])
        m = re.search(r"/video/(\d{15,})", r.stdout)
        if m:
            return m.group(1)
    return None


def refresh_cookies():
    if COOKIE_FILE.exists() and time.time() - COOKIE_FILE.stat().st_mtime < 86400:
        return COOKIE_FILE
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    run([str(ROOT / ".venv/bin/yt-dlp"), "--cookies-from-browser", "chrome",
         "--cookies", str(COOKIE_FILE), "--simulate", "--skip-download",
         "https://www.douyin.com/"], timeout=180)
    return COOKIE_FILE if COOKIE_FILE.exists() else None


def grab_douyin(video_id):
    url = f"https://www.douyin.com/video/{video_id}"
    challenged = 0
    for attempt, wait in ((1, 0), (2, 6), (3, 15)):
        if wait:
            time.sleep(wait)
        # 第 1 次无头；被风控后改用有头（真实窗口），通过率显著提高
        env = dict(os.environ, V2T_HEADFUL="1" if attempt >= 2 else "0")
        if attempt == 2:
            log("改用真实浏览器窗口抓取（可能会闪出一个 Chrome 窗口）")
        r = run([str(PY), str(ROOT / "bin/dy_grab.py"), url, str(refresh_cookies() or "")],
                timeout=300, env=env)
        try:
            info = json.loads(r.stdout)
        except Exception:
            log(f"第 {attempt} 次抓取出错")
            continue
        media = [u for u in (info.get("media") or info.get("srcs") or [])
                 if not any(k in u for k in ("douyinstatic.com", "douyin-pc-web", "uuu_265"))]
        if media:
            return {"title": clean_title(info.get("title", "")) or f"douyin_{video_id}",
                    "desc": info.get("desc", ""), "media": media}
        if info.get("challenged"):
            challenged += 1
            log(f"第 {attempt} 次被抖音风控拦截（验证码），稍后重试…")
        else:
            log(f"第 {attempt} 次未取到地址，重试…")
    if challenged:
        log("抖音持续要求验证。可先在本机 Chrome 打开该视频看一次，再回来重试。")
    return None


def ytdlp_meta(url):
    """用 yt-dlp 取标题和简介（B站/YouTube/小红书等通用）。"""
    r = run([str(ROOT / ".venv/bin/yt-dlp"), "--no-warnings", "--skip-download",
             "--socket-timeout", "25", "-J", url], timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        return "", ""
    try:
        data = json.loads(r.stdout)
    except Exception:
        return "", ""
    return data.get("title", "") or "", data.get("description", "") or ""


def download(url, dest):
    r = run(["curl", "-sL", "-m", "300", "-A", UA,
             "-H", "Referer: https://www.douyin.com/",
             "-o", str(dest), "-w", "%{http_code}", url])
    return (r.stdout or "").strip() == "200" and dest.exists() and dest.stat().st_size > 10000


def has_audio(p):
    r = run(["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(p)])
    return "audio" in (r.stdout or "")


def download_best(cands, dest):
    for i, u in enumerate(cands[:4], 1):
        log(f"下载候选 {i}/{min(len(cands), 4)}…")
        if download(u, dest) and has_audio(dest):
            return dest
        log("该地址不可用或无音轨，换下一个")
    raise RuntimeError("所有候选地址都失败")


def extract_wav(src, dest):
    r = run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {r.stderr[-300:]}")
    return dest


# ---------------- 转写 ----------------

def srt_to_text(srt: str) -> str:
    lines = []
    for block in re.split(r"\n\s*\n", srt.strip()):
        parts = [l for l in block.splitlines() if l.strip()]
        if not parts:
            continue
        if parts[0].strip().isdigit():
            parts = parts[1:]
        if parts and "-->" in parts[0]:
            parts = parts[1:]
        lines.append("".join(p.strip() for p in parts))
    text = ""
    for line in lines:
        if text and not re.search(r"[，。！？、；：,.!?;:]$", text):
            text += "，"
        text += line
    return text


def asr_qwen3(wav, out_dir, stem, context):
    log("Qwen3-ASR 转写中…")
    t = time.time()
    r = run([str(PY), "-m", "mlx_audio.stt.generate", "--model", ASR_MODEL,
             "--audio", str(wav), "--output-path", str(out_dir / f"{stem}.srt"),
             "--format", "srt", "--context", context or ""],
            env=dict(os.environ, HF_HOME=str(WORK / "hf"),
                     HF_ENDPOINT="https://hf-mirror.com"), timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"Qwen3-ASR 失败: {r.stderr[-400:]}")
    srt = out_dir / f"{stem}.srt.srt"
    if not srt.exists():
        raise RuntimeError("ASR 没有产出字幕")
    srt.replace(out_dir / f"{stem}.srt")
    txt = out_dir / f"{stem}.txt"
    txt.write_text(srt_to_text((out_dir / f"{stem}.srt").read_text(encoding="utf-8")),
                   encoding="utf-8")
    log(f"转写完成 {time.time() - t:.0f}s")
    return txt


def asr_whisper(wav, out_dir, stem, initial_prompt):
    log("Whisper large-v3 转写中（较慢）…")
    r = run([str(ROOT / ".venv/bin/mlx_whisper"), str(wav), "--model",
             "mlx-community/whisper-large-v3-mlx", "--language", "zh",
             "--task", "transcribe", "--output-dir", str(out_dir),
             "--output-format", "all", "--output-name", stem,
             "--condition-on-previous-text", "False",
             "--initial-prompt", initial_prompt or ""],
            env=dict(os.environ, HF_HOME=str(WORK / "hf"),
                     HF_ENDPOINT="https://hf-mirror.com"), timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"whisper 失败: {r.stderr[-400:]}")
    return out_dir / f"{stem}.txt"


# ---------------- 深度整理（可选） ----------------

def split_blocks(raw_text, size=700):
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
    if len(paras) <= 1:
        sents = [s for s in re.split(r"(?<=[。！？])", raw_text) if s.strip()]
        return ["".join(sents[i:i + 8]) for i in range(0, len(sents), 8)]
    blocks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) > size:
            blocks.append(buf)
            buf = p
        else:
            buf += ("\n" + p if buf else p)
    if buf:
        blocks.append(buf)
    return blocks


def polish(raw_text, cfg, glossary, with_summary=True):
    system = PUNCT_SYSTEM
    if glossary:
        system += f"\n\n【术语表】必须严格使用这些写法：{glossary}"
    system += "\n\n【量词】商品计量用「份」「单」「个」，不要用「道」。"

    blocks = split_blocks(raw_text)
    log(f"深度整理 {len(blocks)} 块…")
    body = []
    for i, b in enumerate(blocks, 1):
        log(f"  第 {i}/{len(blocks)} 块")
        body.append(llm.strip_reasoning(llm.chat(system, b, cfg)))
    body = "\n\n".join(body)

    summary = ""
    if with_summary:
        log("生成摘要…")
        summary = llm.strip_reasoning(llm.chat(SUMMARY_SYSTEM, body, cfg, max_tokens=800))
        summary += "\n\n"
    return summary, body


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="视频链接 → 中文文稿")
    ap.add_argument("source", nargs="?", help="链接或本地文件")
    ap.add_argument("-o", "--out", default=str(ROOT / "out"))
    ap.add_argument("--asr", choices=["qwen3", "whisper"], default="qwen3")
    ap.add_argument("--polish", action="store_true", help="深度整理成稿（慢，默认关闭）")
    ap.add_argument("--no-format", action="store_true", help="只转写，不做规则排版")
    ap.add_argument("--polish-only", metavar="TXT", help="只整理已有转写稿")
    ap.add_argument("--glossary", default="", help="术语表，分号分隔，如 A=B,C;D")
    ap.add_argument("--title", default="")
    ap.add_argument("--backend", choices=["local", "api"])
    ap.add_argument("--base-url")
    ap.add_argument("--api-key")
    ap.add_argument("--model")
    ap.add_argument("--config")
    ap.add_argument("--init-config", action="store_true", help="生成 config.json 模板")
    ap.add_argument("--show-backend", action="store_true", help="打印整理后端")
    args = ap.parse_args()

    if args.show_backend:
        try:
            print(llm.describe(llm.load_config(args.config, overrides={})))
        except SystemExit as e:
            print(f"配置异常: {e}")
        return

    if args.init_config:
        p = llm.write_template(ROOT / "config.json")
        print(f"已生成配置模板: {p}（权限 600）")
        return

    cfg = llm.load_config(args.config, overrides={
        ("llm", "backend"): args.backend,
        ("llm", "base_url"): args.base_url,
        ("llm", "api_key"): args.api_key,
        ("llm", "model"): args.model,
        ("local", "model"): args.model if args.backend == "local" else None,
    })

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 只整理模式
    if args.polish_only:
        src_txt = Path(args.polish_only).expanduser()
        if not src_txt.exists():
            sys.exit(f"文件不存在: {src_txt}")
        log(f"整理后端: {llm.describe(cfg)}")
        raw = src_txt.read_text(encoding="utf-8").strip()
        title = args.title or src_txt.stem
        summary, body = polish(raw, cfg, ", ".join(auto_terms))
        md = out_dir / f"{safe_name(title)}.md"
        md.write_text(
            f"# {title}\n\n- 来源: {src_txt}\n"
            f"- 字数: {len(re.sub(r'\\s', '', body))}\n- 整理: {llm.describe(cfg)}\n\n"
            f"{summary}## 正文\n\n{body}\n", encoding="utf-8")
        print(f"\n完成 {time.time() - t0:.0f}s → {md}")
        return

    if not args.source:
        ap.error("需要提供链接/文件，或使用 --polish-only / --init-config")

    src = args.source
    auto_terms = []
    vid = douyin_id(src) if "douyin.com" in src else None
    if vid:
        log(f"抖音 ID {vid}")
        info = grab_douyin(vid)
        if not info:
            sys.exit("没能取到播放地址（私密/已删除/风控）")
        title = args.title or info["title"]
        auto_terms = extract_terms(title, info.get("desc", ""))
        if auto_terms:
            log("自动识别术语: " + ", ".join(auto_terms))
        local = WORK / f"{vid}.mp4"
        log(f"下载《{title}》")
        download_best(info["media"], local)
    elif src.startswith(("http://", "https://")):
        meta_title, meta_desc = ytdlp_meta(src)
        title = args.title or clean_title(meta_title) or Path(src.split("?")[0]).stem
        auto_terms = extract_terms(meta_title, meta_desc)
        if auto_terms:
            log("自动识别术语: " + ", ".join(auto_terms))
        local = WORK / f"dl_{int(time.time())}.mp4"
        log(f"下载《{title}》")
        base = [str(ROOT / ".venv/bin/yt-dlp"), "-f", "bv*+ba/b", "--no-playlist",
                "-o", str(local), src]
        r = run(base, timeout=3600)
        if not local.exists():
            # B 站高清等需要登录：带本机 Chrome 的 Cookie 重试一次
            log("首次下载失败，尝试用 Chrome 登录状态重试…")
            r = run([str(ROOT / ".venv/bin/yt-dlp"), "-f", "bv*+ba/b", "--no-playlist",
                     "--cookies-from-browser", "chrome", "-o", str(local), src],
                    timeout=3600)
        if not local.exists():
            sys.exit(f"下载失败: {r.stderr[-300:]}")
    else:
        local = Path(src).expanduser()
        if not local.exists():
            sys.exit(f"文件不存在: {local}")
        title = args.title or local.stem

    stem = safe_name(title)
    wav = WORK / f"{stem}.wav"
    extract_wav(local, wav)

    # 术语来源：自动抽取 + 命令行补充（命令行仅作调试用，界面已不暴露）
    ctx = ", ".join(auto_terms + ([args.glossary] if args.glossary else []))
    txt = (asr_qwen3(wav, out_dir, stem, ctx) if args.asr == "qwen3"
           else asr_whisper(wav, out_dir, stem, ctx))

    dur = float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(wav)]).stdout.strip() or 0)
    raw = txt.read_text(encoding="utf-8").strip()

    # 术语纠错：txt 与 srt 同步修正
    srt_path = out_dir / f"{stem}.srt"
    terms = gl.parse_cli_glossary(args.glossary)
    for t in auto_terms:                      # 自动术语也进纠错词典
        terms.setdefault(t, [])
    if terms:
        fixed, fx = gl.correct(raw, terms)
        if fx:
            log("术语纠错: " + ", ".join(f"{a}→{b}" for a, b in fx))
            txt.write_text(fixed, encoding="utf-8")
            raw = fixed
            if srt_path.exists():
                srt_fixed, _ = gl.correct(srt_path.read_text(encoding="utf-8"), terms)
                srt_path.write_text(srt_fixed, encoding="utf-8")

    # 规则排版（非 LLM，0.1 秒）
    md = out_dir / f"{stem}.md"
    if srt_path.exists() and not args.no_format:
        formatmd.format_md(
            srt_path, md, title=title,
            meta=f"> 时长 {int(dur // 60)}分{int(dur % 60)}秒 · 来源 {src}")

    if not args.polish:
        print("\n" + "=" * 56)
        tail = " + 规则排版" if (md.exists() and not args.no_format) else ""
        print(f"完成 {time.time() - t0:.0f}s（转写 + 术语纠错{tail}）")
        if md.exists() and not args.no_format:
            print(f"文稿: {md}")
        print(f"原文: {txt}")
        print(f"字幕: {srt_path}")
        print("=" * 56)
        print(raw[:400] + "…")
        return

    # 深度整理
    log(f"整理后端: {llm.describe(cfg)}")
    summary, body = polish(raw, cfg, ", ".join(auto_terms))
    md.write_text(
        f"# {title}\n\n- 来源: {src}\n- 时长: {int(dur // 60)}分{int(dur % 60)}秒\n"
        f"- 字数: {len(re.sub(r'\\s', '', body))}\n- 链路: Qwen3-ASR + {llm.describe(cfg)}\n\n"
        f"{summary}## 正文\n\n{body}\n", encoding="utf-8")
    print("\n" + "=" * 56)
    print(f"完成 {time.time() - t0:.0f}s（深度整理）\n文稿: {md}")
    print("=" * 56)
    print(body[:400] + "…")


if __name__ == "__main__":
    main()
