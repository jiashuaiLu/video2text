#!/usr/bin/env python3
"""glossary — 术语纠错器（确定性，不依赖大模型）

解决 ASR 的专有名词错误。三层策略，从保守到激进：

1. 别名精确替换      "GPT Cloud" → ChatGPT         （用户提供）
2. 拉丁词归一 + 模糊  "Trust MR" → TrustMRR          （距离 ≤1）
3. 中文拼音匹配      「超需求」→「抄需求」           （拼音完全相同）
   跨语种拼音模糊     「路麻吧」→ LumaBar            （lumabar≈lumaba）

用法:
    from glossary import correct
    new_text, fixes = correct(text, {"LumaBar": ["路麻吧"], "ChatGPT": ["GPT Cloud"]})
"""
from __future__ import annotations

import re

try:
    from pypinyin import lazy_pinyin
except ImportError:  # 没装拼音库时退化为前两层
    lazy_pinyin = None

LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:\s+[A-Za-z][A-Za-z0-9]*){0,2}")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 不做模糊替换的常见词，避免误伤
STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "but", "not",
    "app", "apps", "ok", "yes", "no", "ai", "api", "seo", "ios", "mac", "macos",
    "windows", "iphone", "android", "tiktok", "twitter", "dock", "x", "www",
}


def _lev(a: str, b: str, maxd: int = 3) -> int:
    """带提前退出的编辑距离。"""
    if abs(len(a) - len(b)) > maxd:
        return maxd + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            best = min(best, cur[j])
        if best > maxd:
            return maxd + 1
        prev = cur
    return prev[-1]


def _py(s: str) -> str:
    """取拼音字符串（去声调、去空格）；没有拼音库时返回 None。"""
    if lazy_pinyin is None:
        return None
    out = []
    for chunk in lazy_pinyin(s):
        out.append(re.sub(r"[^a-z0-9]", "", chunk.lower()))
    return "".join(out)


def _ascii(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def build_index(glossary: dict) -> dict:
    """{正确写法: [别名...]} → 三张独立索引。

    latin   : 拉丁词归一/模糊（ascii 键）
    cjk     : 中文同音（拼音精确匹配）
    cross   : 拉丁词的拼音形，用于「路麻吧 ↔ LumaBar」这类跨语种匹配
    三张表互不交叉，避免中文拼音误命中拉丁词条。
    """
    idx = {"latin": {}, "cjk": {}, "cross": {}, "alias": []}
    for term, aliases in (glossary or {}).items():
        term = (term or "").strip()
        if not term or "=" in term:
            continue
        aliases = [a for a in (aliases or []) if a and a.strip()]

        for a in aliases:
            if a != term:
                idx["alias"].append((a, term))

        is_latin = bool(re.fullmatch(r"[A-Za-z0-9 .\-]+", term))
        key = _ascii(term)
        py = _py(term)

        if is_latin:
            if len(key) >= 4 and key.lower() not in STOPWORDS:
                idx["latin"][key] = term
            if py and len(py) >= 5:
                idx["cross"][py] = term
        else:
            if py:
                idx["cjk"][py] = term
            # 中文别名也进同音表
            for a in aliases:
                apy = _py(a)
                if apy:
                    idx["cjk"].setdefault(apy, term)
    return idx


def correct(text: str, glossary: dict) -> tuple[str, list[tuple[str, str]]]:
    """返回 (修正后文本, [(原词, 新词), ...])。"""
    if not glossary:
        return text, []
    idx = build_index(glossary)
    fixes: list[tuple[str, str]] = []
    out = text

    # --- 第 1 层：别名精确替换（长别名优先）---
    for alias, term in sorted(idx["alias"], key=lambda x: -len(x[0])):
        pat = re.compile(re.escape(alias), re.IGNORECASE if alias.isascii() else 0)
        if pat.search(out):
            out, n = pat.subn(term, out)
            if n:
                fixes.append((alias, term))

    # --- 第 2 层：拉丁词归一 / 距离 1 模糊 ---
    def fix_latin(m: re.Match) -> str:
        raw = m.group(0)
        key = _ascii(raw)
        if key in idx["latin"]:
            term = idx["latin"][key]
            if raw != term:
                fixes.append((raw, term))
            return term
        if len(key) >= 6:
            for cand, term in idx["latin"].items():
                if cand == key:
                    continue
                if _lev(key, cand, 1) <= 1:
                    fixes.append((raw, term))
                    return term
        return raw

    out = LATIN_RUN.sub(fix_latin, out)

    # --- 第 3 层：中文同音纠错（精确拼音）---
    if idx["cjk"]:
        def fix_cjk(m: re.Match) -> str:
            s_ = m.group(0)
            i, buf = 0, []
            while i < len(s_):
                hit = None
                for size in range(min(6, len(s_) - i), 1, -1):
                    w = s_[i:i + size]
                    wpy = _py(w)
                    if not wpy:
                        continue
                    if wpy in idx["cjk"] and w != idx["cjk"][wpy]:
                        hit = (w, idx["cjk"][wpy])
                        break
                if hit:
                    fixes.append(hit)
                    buf.append(hit[1])
                    i += len(hit[0])
                else:
                    buf.append(s_[i])
                    i += 1
            return "".join(buf)
        out = CJK_RUN.sub(fix_cjk, out)

    # --- 第 4 层：跨语种模糊（只对拉丁词条，距离 ≤1，长度 ≥5）---
    if idx["cross"]:
        def fix_cross(m: re.Match) -> str:
            s_ = m.group(0)
            i, buf = 0, []
            while i < len(s_):
                hit = None
                for size in range(min(5, len(s_) - i), 2, -1):   # 3-5 字窗口
                    w = s_[i:i + size]
                    wpy = _py(w)
                    if not wpy or len(wpy) < 5:
                        continue
                    for k, term in idx["cross"].items():
                        if _lev(wpy, k, 1) <= 1:
                            hit = (w, term)
                            break
                    if hit:
                        break
                if hit:
                    w, term = hit
                    if term.isascii() and term.islower():
                        term = term[0].upper() + term[1:]
                    fixes.append((w, term))
                    buf.append(term)
                    i += len(w)
                else:
                    buf.append(s_[i])
                    i += 1
            return "".join(buf)
        out = CJK_RUN.sub(fix_cross, out)

    # 去重保序
    seen, uniq = set(), []
    for a, b in fixes:
        if (a, b) not in seen:
            seen.add((a, b))
            uniq.append((a, b))
    return out, uniq


def parse_cli_glossary(spec: str) -> dict:
    """严格解析：分号分隔词条，词条内用 = 接别名。

    合法: "LumaBar=路麻吧,luma bar;ChatGPT=GPT Cloud;Grok"
    也支持纯逗号列表（无 = 时）: "LumaBar, Grok, ChatGPT"
    """
    if not spec:
        return {}
    g = {}
    entries = [x for x in re.split(r"[;；]", spec) if x.strip()]
    for part in entries:
        part = part.strip()
        if "=" in part:
            term, alias_str = part.split("=", 1)
            term = term.strip().strip(",，")
            if not term:
                continue
            if "," in term or "，" in term:
                raise SystemExit(
                    f"术语表写法有歧义：'{term}' 里含逗号。\n"
                    f"正确写法用分号分组，例如：v2t --glossary \"LumaBar=路麻吧;ChatGPT=GPT Cloud\"")
            g[term] = [a.strip() for a in re.split(r"[,，]", alias_str) if a.strip()]
        else:
            for k in re.split(r"[,，]", part):
                k = k.strip()
                if k and "=" not in k:
                    g[k] = []
    return g


if __name__ == "__main__":
    import sys
    txt = sys.stdin.read()
    g = parse_cli_glossary(sys.argv[1] if len(sys.argv) > 1 else "")
    new, fx = correct(txt, g)
    print(new)
    print("--- 修正 ---", file=sys.stderr)
    for a, b in fx:
        print(f"  {a} → {b}", file=sys.stderr)
