#!/usr/bin/env python3
"""formatmd — 非 LLM 的排版器：断句、分段、去赘词、识别列举

纯规则 + 统计，零依赖零成本，毫秒级，结果稳定可复现。

分段依据（按优先级）：
1. 话语标记   首先/然后/接下来/所以/但是/另外/最后/第一点…
2. 词汇衔接   相邻句的字二元组 Jaccard 相似度骤降 → 话题切换
3. 长度上限   单段超过 ~230 字强制断开

用法:
    python formatmd.py 转写稿.srt [-o 输出.md] [--timestamps] [--title 标题]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

# 句末标点
SENT_END = "。！？；…"
# 话语标记：出现在句首时视为段落边界
STRONG_MARKERS = (
    "首先", "其次", "接下来", "再说一下", "另外说一下", "同理", "话又说回来",
    "总之", "最后总结", "总结一下", "第一点", "第二点", "第三点",
    "第一个产品", "第二个产品", "第三个产品", "其一", "其二", "先说结论",
)
# 列举标记：句首命中则转列表
ENUM_PAT = re.compile(
    r"^(第[一二三四五六七八九十\d]+[点条个步]|第[一二三四五六七八九十]+|"
    r"[一二三四五六七八九十]+[、.]|\d+[、.．)]|首先|其次|再次|最后)")
# 口语赘词
FILLERS = re.compile(r"[呃嗯]")
AH = re.compile(r"[，。！？]啊(?=[，。！？])")
DOUBLE = re.compile(r"(一一个|一一样|这这|就就|我我|他他|你你|是是|有有|不不|对对)")
ONE_ONE = re.compile(r"一一个")
DUP2 = re.compile(r"([\u4e00-\u9fff]{2,4})\1")


def parse_srt(srt: str):
    """→ [(开始秒, 结束秒, 文本)]"""
    cues = []
    for block in re.split(r"\n\s*\n", srt.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        tline = next((l for l in lines if "-->" in l), None)
        if not tline:
            continue
        idx = lines.index(tline)
        text = "".join(lines[idx + 1:])
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", tline.strip())
        if not m or not text:
            continue
        start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        cues.append((start, text.strip()))
    return cues


def build_sentences(cues):
    """cue 文本 → 句子列表 [(秒, 句子)]，带时间回填。"""
    out = []
    for start, text in cues:
        buf = ""
        for ch in text:
            buf += ch
            if ch in SENT_END:
                s = buf.strip()
                if s:
                    out.append((start, s))
                buf = ""
        if buf.strip():
            out.append((start, buf.strip()))
    return out


# --- 跨字幕断句修复 ---
# 1) 前句以这些字收尾，说明话没说完
DANGLING = re.compile(r"[的个了是在和把被给对从向跟即就也都还呀啊着过的]{1,3}[。！？]?$")
# 2) 后句以这些词开头，说明是同一句的延续
CONT_START = re.compile(
    r"^(就是|而且|但是|所以|然后|并且|因为|如果|虽然|这|那|它|他|她|你|我|"
    r"的|了|个|都|也|还|就|才|又|再|更|最|很|太|比较|其实|反正)")
# 3) 孤立残片开头，如「个，」「的，」
ORPHAN_HEAD = re.compile(r"^[个的了]，\s*")


def merge_split_sentences(sents):
    """把字幕边界劈开的句子接回去。返回 (新句子列表, 合并次数)。"""
    if not sents:
        return sents, 0
    out = [list(sents[0])]
    merged = 0
    for t, s in sents[1:]:
        prev_t, prev = out[-1]
        prev_txt = prev

        orphan = ORPHAN_HEAD.match(s)
        if orphan:
            s = s[orphan.end():].strip()
            if not s:
                continue

        should = False
        if orphan:
            should = True
        elif DANGLING.search(prev_txt.rstrip()) and len(prev_txt) < 70:
            should = True
        elif (CONT_START.match(s) and len(prev_txt) < 16
              and not prev_txt.rstrip().endswith(("？", "！"))):
            should = True

        if should and s and not prev_txt.rstrip().endswith(("？", "！")):
            sep = "" if re.match(r"[A-Za-z0-9]", s) else "，"
            out[-1][1] = prev_txt.rstrip().rstrip("。") + sep + s
            merged += 1
        else:
            out.append([t, s])
    return [tuple(x) for x in out], merged


def bigrams(s: str) -> set:
    s = re.sub(r"[^\u4e00-\u9fff]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else set()


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn2num(text: str) -> int | None:
    """中文数字 → 整数，支持到亿。"""
    if not text or any(c not in CN_DIGIT and c not in CN_UNIT
                       and c not in "万亿" for c in text):
        return None
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in CN_DIGIT:
            num = CN_DIGIT[ch]
        elif ch in CN_UNIT:
            section += (num or 1) * CN_UNIT[ch]
            num = 0
        elif ch == "万":
            total += (section + num) * 10000
            section, num = 0, 0
        elif ch == "亿":
            total = (total + section + num) * 100000000
            section, num = 0, 0
    return total + section + num


NUM_PAT = re.compile(r"(?<![几数上好多至约近])([一二两三四五六七八九十百千万亿零]{1,9})(?=[刀元块个份单条年天月号次倍人])")


def normalize_numbers(s: str) -> str:
    def rep(m):
        v = cn2num(m.group(1))
        return str(v) if v and v >= 10 else m.group(0)
    return NUM_PAT.sub(rep, s)


def clean(s: str, numbers: bool = True) -> str:
    s = FILLERS.sub("", s)
    s = AH.sub("，", s)
    s = ONE_ONE.sub("一个", s)
    s = DOUBLE.sub(lambda m: m.group(1)[0], s)
    s = DUP2.sub(r"\1", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"，。", "。", s)
    s = re.sub(r"^[，、。的]+", "", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    s = re.sub(r"\s+", " ", s)
    if numbers:
        s = normalize_numbers(s)
    return s.strip()


def paragraphize(sents, sim_threshold=0.02, max_chars=260, min_sents=3):
    """按衔接度 + 话语标记分段。返回 [[(秒, 句子), ...]]"""
    if not sents:
        return []
    paras, cur = [], [sents[0]]
    for i in range(1, len(sents)):
        prev_s = sents[i - 1][1]
        cur_s = sents[i][1]
        sim = jaccard(bigrams(prev_s), bigrams(cur_s))
        cur_len = sum(len(s) for _, s in cur)
        starts_marker = any(cur_s.startswith(m) for m in STRONG_MARKERS)

        boundary = False
        if starts_marker and len(cur) >= min_sents:
            boundary = True
        elif sim < sim_threshold and len(cur) >= min_sents:
            boundary = True
        elif cur_len >= max_chars:
            boundary = True

        if boundary:
            paras.append(cur)
            cur = [sents[i]]
        else:
            cur.append(sents[i])
    if cur:
        paras.append(cur)

    # 合并过短的段（< 70 字）到前一段，避免碎片
    merged = []
    for p in paras:
        if merged and sum(len(s) for _, s in p) < 70:
            merged[-1].extend(p)
        else:
            merged.append(p)
    return merged


def render(paras, title, meta, cues_dur=0, timestamps=False, args_no_numbers=False):
    lines = [f"# {title}", ""]
    if meta:
        lines += [meta, ""]
    buf = []
    for p in paras:
        text = clean("".join(s for _, s in p), numbers=not args_no_numbers)
        if not text:
            continue
        first = p[0][0]
        ts = f"**[{first // 60:02d}:{first % 60:02d}]** " if timestamps else ""
        buf.append(ts + text)

    # 列举识别：段内以列举词开头的句子转成列表块
    out_blocks = []
    for para in buf:
        parts = [x for x in re.split(r"(?<=[。！？])", para) if x.strip()]
        enum_items = [x.strip() for x in parts if ENUM_PAT.match(x.strip())]
        if len(enum_items) >= 3 and all(x.endswith(("。", "！", "？")) for x in enum_items):
            head, items, tail = [], [], []
            for x in parts:
                if ENUM_PAT.match(x.strip()):
                    items.append(x.strip())
                elif items:
                    tail.append(x.strip())
                else:
                    head.append(x.strip())
            block = ""
            if head:
                block += "".join(head) + "\n\n"
            block += "\n".join(f"- {i}" for i in items)
            if tail:
                block += "\n\n" + "".join(tail)
            out_blocks.append(block)
        else:
            out_blocks.append(para)

    lines += ["\n\n".join(out_blocks)]
    return "\n".join(lines).strip() + "\n"


def format_md(srt_path, out_path, title="", meta="", timestamps=False,
              numbers=True):
    """供 v2t 调用：SRT → 排版 Markdown。"""
    cues = parse_srt(Path(srt_path).read_text(encoding="utf-8"))
    sents = build_sentences(cues)
    sents, merged = merge_split_sentences(sents)
    paras = paragraphize(sents)
    dur = cues[-1][0] if cues else 0
    if not title:
        title = Path(srt_path).stem
    if not meta:
        meta = f"> 时长 {dur // 60}分{dur % 60}秒 · {len(sents)} 句 · {len(paras)} 段"
    md = render(paras, title, meta, dur, timestamps, args_no_numbers=(not numbers))
    Path(out_path).write_text(md, encoding="utf-8")
    return Path(out_path), len(sents), len(paras), merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("srt")
    ap.add_argument("-o", "--out")
    ap.add_argument("--title", default="")
    ap.add_argument("--meta", default="")
    ap.add_argument("--timestamps", action="store_true")
    ap.add_argument("--stats", action="store_true", help="打印分段统计")
    args = ap.parse_args()

    cues = parse_srt(Path(args.srt).read_text(encoding="utf-8"))
    sents = build_sentences(cues)
    sents, merged = merge_split_sentences(sents)
    paras = paragraphize(sents)
    title = args.title or Path(args.srt).stem
    dur = cues[-1][0] if cues else 0
    meta = args.meta or f"> 时长 {dur // 60}分{dur % 60}秒 · {len(sents)} 句 · {len(paras)} 段"
    md = render(paras, title, meta, dur, args.timestamps)
    out = Path(args.out) if args.out else Path(args.srt).with_suffix(".md")
    out.write_text(md, encoding="utf-8")
    if args.stats:
        lens = [sum(len(s) for _, s in p) for p in paras]
        print(f"句数 {len(sents)}（合并跨句 {merged}）  段数 {len(paras)}  "
              f"段长 {min(lens)}-{max(lens)} (均值 {sum(lens)//len(lens)})")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
