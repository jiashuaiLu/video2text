#!/usr/bin/env python3
"""把项目故事排版成 1080x1440 的图文卡片（小红书 3:4）。

生成 HTML → 用无头 Chrome 逐张截图。
用法: python packaging/cards.py <输出目录>
"""
import subprocess
import sys
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ROOT = Path(__file__).resolve().parent.parent
SHOT = (ROOT / "docs/images/after.jpg").as_uri()
W, H = 1080, 1440

CSS = """
*{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}
body{width:%(w)dpx;height:%(h)dpx;overflow:hidden;
  font-family:"PingFang SC","Helvetica Neue",sans-serif;
  background:#F7F8FA;color:#1B1E24;position:relative}
.card{width:%(w)dpx;height:%(h)dpx;padding:92px 84px 168px;display:flex;flex-direction:column}
body.center .card{justify-content:center}
body.cover .card{justify-content:flex-start}
.no{position:absolute;bottom:64px;left:84px;font-size:24px;color:#A7AEBB;letter-spacing:1px}
.brand{position:absolute;bottom:64px;right:84px;font-size:24px;color:#A7AEBB}
.tag{align-self:flex-start;display:inline-block;font-size:26px;color:#0C8A62;background:#EAF7F2;
  padding:8px 24px;border-radius:99px;margin-bottom:38px;font-weight:500}
h1{font-size:88px;line-height:1.24;font-weight:700;letter-spacing:-2px}
h1 .hl{color:#0FA375}
p.lead{font-size:38px;line-height:1.6;color:#5A616E;margin-top:36px}
.big{font-size:150px;font-weight:700;color:#0FA375;line-height:1;letter-spacing:-6px}
.unit{font-size:40px;color:#5A616E;margin-left:8px}
.row{display:flex;align-items:baseline;gap:20px;margin:26px 0}
.box{background:#fff;border:1px solid #ECEEF2;border-radius:22px;padding:34px 38px;
  box-shadow:0 10px 30px -18px rgba(16,24,40,.18);margin-top:28px}
.box .t{font-size:30px;color:#8D96A8;margin-bottom:12px}
.box .c{font-size:38px;line-height:1.55}
.mono{font-family:"SF Mono",Menlo,monospace;font-size:32px;line-height:1.7;color:#3C4351}
.mono .del{color:#C0393F;text-decoration:line-through}
.mono .add{color:#0FA375;font-weight:600}
ul{margin-top:26px}
li{font-size:36px;line-height:1.75;color:#3C4351;list-style:none;padding-left:42px;position:relative}
li::before{content:"";position:absolute;left:6px;top:24px;width:14px;height:14px;
  border-radius:50%%;background:#0FA375}
.dark{background:#12151B;color:#F2F4F8}
.dark .tag{background:#1F3A31;color:#5FD5A8}
.dark p.lead{color:#9AA3B2}
.foot{margin-top:auto;font-size:30px;color:#8D96A8;line-height:1.7}
.shot{margin-top:52px;border-radius:16px;overflow:hidden;
  border:1px solid #2A3140;box-shadow:0 24px 60px -24px rgba(0,0,0,.8)}
.shot img{width:100%%;max-height:520px;object-fit:cover;object-position:top;display:block}
""" % {"w": W, "h": H}


def card_html(idx, total, body, dark=False, layout="center"):
    cls = ("dark " if dark else "") + ("cover" if layout == "cover" else "center")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{CSS}</style></head>
<body class="{cls}">
<div class="card">{body}</div>
<div class="no">{idx:02d} / {total:02d}</div>
<div class="brand">video2text</div>
</body></html>"""


CARDS = [
    # 1 封面
    (dict(dark=True, layout="cover"), """
      <div class="tag">开源 · 本地运行</div>
      <h1>我把抖音视频<br>变成了<span class="hl">能读的文章</span></h1>
      <p class="lead">贴个链接，等它十分之一的时间<br>出来一份带标点、分好段的文稿</p>
      <div class="shot"><img src="%(shot)s"></div>
    """),
    # 2 痛点
    ({}, """
      <div class="tag">问题</div>
      <h1>7 分钟的视频<br>想找<span class="hl">一句话</span><br>得重听半小时</h1>
      <div class="box"><div class="t">现有的办法</div>
        <div class="c">字幕不给导出 · 自动字幕错字多<br>在线工具要你把整段音频上传</div></div>
    """),
    # 3 速度
    ({}, """
      <div class="tag">速度</div>
      <h1>视频多长<br>就等它<span class="hl">十分之一</span>的时间</h1>
      <div class="row"><div class="big">10×</div><div class="unit">实时速度</div></div>
      <div class="box"><div class="t">实测（M4 Pro）</div>
        <div class="c">7 分 42 秒的视频 → 约 40 秒出稿<br>只算转写更快，12-20 倍实时</div></div>
      <p class="lead">全程不上传，断网也能用</p>
    """),
    # 4 效果对比
    ({}, """
      <div class="tag">区别在哪</div>
      <h1>别人给字幕<br>我给<span class="hl">能读的文章</span></h1>
      <div class="box"><div class="t">普通工具</div>
        <div class="c" style="color:#8D96A8">新手独立开发者最头疼的莫过于找需求<br>我当年也有这样的痛点</div></div>
      <div class="box" style="border-color:#BFE6D8"><div class="t">这个工具</div>
        <div class="c">新手独立开发者最头疼的莫过于找需求。我当年也有这样的痛点。</div></div>
    """),
    # 5 技术：拿流
    (dict(dark=True), """
      <div class="tag">最难的一关</div>
      <h1>抖音的接口<br>拒绝了一切<span class="hl">直连</span></h1>
      <div class="mono" style="margin-top:30px">
        yt-dlp 直连<br><span class="del">→ 403 Forbidden</span><br><br>
        带上 Cookie<br><span class="del">→ 403 Blocked by Argus</span><br><br>
        真实浏览器跑一遍<br><span class="add">→ 拿到播放地址 ✓</span>
      </div>
      <p class="lead">签名是页面里的 JS 现算的<br>那就让页面自己算</p>
    """),
    # 6 技术：拼音纠错
    ({}, """
      <div class="tag">第二难</div>
      <h1>它把 LumaBar<br>听成<span class="hl">「撸妈爸」</span></h1>
      <div class="mono" style="margin-top:34px">
        撸妈爸 → lu ma ba<br>
        LumaBar → luma bar<br>
        <span class="add">编辑距离 1 → 自动纠正</span><br><br>
        超需求 → chao xu qiu<br>
        抄需求 → chao xu qiu<br>
        <span class="add">拼音相同 → 直接替换</span>
      </div>
      <p class="lead">不用大模型，0.3 秒跑完</p>
    """),
    # 7 两档速度
    ({}, """
      <div class="tag">设计取舍</div>
      <h1>默认<span class="hl">不用</span>大模型</h1>
      <div class="box"><div class="t">提取文字 · 默认</div>
        <div class="c">40 秒 · 转写 + 术语纠错 + 规则排版</div></div>
      <div class="box"><div class="t">AI 整理 · 可选</div>
        <div class="c">3 分钟 · 再加摘要、要点、润色</div></div>
      <p class="lead">大多数人只想「把话拿出来」<br>不是每篇都要变成文章</p>
    """),
    # 8 结尾
    (dict(dark=True), """
      <div class="tag">开源</div>
      <h1>代码在 GitHub<br><span class="hl">jiashuaiLu/video2text</span></h1>
      <p class="lead">macOS · 开源免费<br>支持抖音 / B站 / 本地文件</p>
      <div class="box" style="background:#1A1E26;border-color:#2A3140">
        <div class="t" style="color:#6F7A8C">它能做的</div>
        <div class="c" style="color:#DDE3EE">贴链接 → 带标点的文稿 + 字幕文件<br>可选择用你自己的大模型再润色</div>
      </div>
    """),
]


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "cards").resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_html"
    tmp.mkdir(exist_ok=True)

    total = len(CARDS)
    for i, (opts, body) in enumerate(CARDS, 1):
        body = body % {"shot": SHOT} if "%(shot)s" in body else body
        html = card_html(i, total, body, dark=opts.get("dark", False),
                         layout=opts.get("layout", "center"))
        f = tmp / f"card{i:02d}.html"
        f.write_text(html, encoding="utf-8")
        png = out / f"card{i:02d}.png"
        r = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=1", f"--window-size={W},{H}",
             f"--screenshot={png}", f.as_uri()],
            capture_output=True, text=True, timeout=120)
        ok = png.exists()
        print(("✅" if ok else "❌"), png.name, f"{png.stat().st_size // 1024}KB" if ok else r.stderr[:200])

    print(f"\n共 {total} 张，输出目录 {out}")


if __name__ == "__main__":
    main()
