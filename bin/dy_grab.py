#!/usr/bin/env python3
"""用无头 Chrome (CDP) 打开抖音视频页，抓取真实播放地址。

抖音播放器常用 MSE，<video>.src 会是 blob: 地址，所以这里同时监听
Network 事件，从实际请求里捞 douyinvod 的媒体地址。

用法: python dy_grab.py <视频URL> [cookies.txt]
输出: JSON {title, author, duration, media: [...], srcs: [...]}
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MEDIA_PAT = ("douyinvod", "mime_type=video_mp4", "/video/tos/")
# 页面自身的 UI 素材，不是视频流
IGNORE_PAT = ("douyinstatic.com", "/obj/douyin-pc-web/", "uuu_265")


class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=30, max_size=64 * 1024 * 1024)
        self.n = 0
        self.events = []

    def _recv(self, timeout=None):
        if timeout is not None:
            self.ws.settimeout(timeout)
        msg = json.loads(self.ws.recv())
        if "method" in msg:
            self.events.append(msg)
        return msg

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = self._recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def pump(self, seconds):
        """持续读取事件流，把 Network 事件收集起来。"""
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._recv(timeout=max(0.2, end - time.time()))
            except Exception:
                break

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        return r.get("result", {}).get("value")

    def media_urls(self):
        """从 Network 事件里提取媒体地址，带 mime_type=video_mp4 的优先。"""
        found = []
        for ev in self.events:
            if ev.get("method") not in ("Network.requestWillBeSent", "Network.responseReceived"):
                continue
            p = ev["params"]
            req = p.get("request") or {}
            url = req.get("url") or (p.get("response") or {}).get("url") or ""
            rtype = p.get("type") or ""
            if any(k in url for k in IGNORE_PAT):
                continue
            if rtype == "Media" or any(k in url for k in MEDIA_PAT):
                if url.startswith("http") and url not in found:
                    found.append(url)
        # mime_type=video_mp4 的是完整视频流，优先
        found.sort(key=lambda u: (0 if "mime_type=video_mp4" in u else 1, len(u)))
        return found


def load_cookies(path):
    out = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, cpath, secure, expiry, name, value = parts[:7]
            if not domain.endswith("douyin.com"):
                continue
            try:
                exp = float(expiry)
            except ValueError:
                exp = -1
            out.append({"name": name, "value": value, "domain": domain,
                        "path": cpath or "/", "secure": secure == "TRUE",
                        "httpOnly": http_only,
                        "expires": exp if exp > 0 else -1})
    return out


PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "work", "chrome-profile")


def launch_chrome(port, profile=None):
    """复用持久化 profile：多次抓取视为同一设备，显著降低风控概率。"""
    profile = profile or PROFILE_DIR
    os.makedirs(profile, exist_ok=True)
    headful = os.environ.get("V2T_HEADFUL") == "1"
    args = [CHROME]
    if not headful:
        args.append("--headless=new")
    args += [f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--mute-audio", "--window-size=1280,900",
         "--disable-blink-features=AutomationControlled",
         "--disable-features=AutomationControlled,Translate",
         "--lang=zh-CN",
         "--autoplay-policy=no-user-gesture-required", "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    return proc, profile
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Chrome 启动失败")


def new_target(port):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]


def grab(url, cookies, port=9333, quiet=True):
    proc, profile = launch_chrome(port)
    try:
        cdp = CDP(new_target(port))
        cdp.call("Page.enable")
        cdp.call("Network.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setUserAgentOverride", userAgent=UA,
                 acceptLanguage="zh-CN,zh;q=0.9", platform="MacIntel")
        if cookies:
            cdp.call("Network.setCookies", cookies=cookies)
        cdp.call("Page.navigate", url=url)

        # 边等边收事件：首屏先把媒体请求发出来
        media, page = [], {}
        deadline = time.time() + 60
        nudged = False
        while time.time() < deadline:
            cdp.pump(5)
            if not nudged:
                cdp.eval("(() => { const v = document.querySelector('video'); "
                         "if (v) { v.muted = true; v.play?.().catch(() => {}); } return 1; })()")
                nudged = True
            media = cdp.media_urls()
            page = cdp.eval("""(() => ({
                title: document.title,
                desc: (document.querySelector('meta[name="description"]')||{}).content || '',
                srcs: [...document.querySelectorAll('video')].map(v => v.currentSrc || v.src || '').filter(s => s && !s.startsWith('blob:')),
                ready: document.readyState
            }))()""") or {}
            if media:
                break
        srcs = page.get("srcs") or []
        title = (page.get("title") or "").strip()
        challenged = any(k in title for k in ("验证码", "验证", "安全")) or "captcha" in str(page).lower()
        return {
            "challenged": challenged,
            "title": title.replace(" - 抖音", "").strip(),
            "desc": page.get("desc", ""),
            "media": media or srcs,
            "srcs": srcs,
            "blob_only": not media and not srcs,
        }
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    url = sys.argv[1]
    ck = sys.argv[2] if len(sys.argv) > 2 else None
    cookies = load_cookies(ck) if ck and os.path.exists(ck) else []
    print(json.dumps(grab(url, cookies), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
