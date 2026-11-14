#!/usr/bin/env python3
"""排版规则回归测试：断句、分段、数字规整、跨字幕合并。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import formatmd as fm  # noqa: E402

SRT = """1
00:00:00,000 --> 00:00:05,000
新手最头疼的是找需求。我当年也是。

2
00:00:05,000 --> 00:00:10,000
不过今天告诉你，别从零去想。

3
00:00:10,000 --> 00:00:15,000
给Mac专门做的一个。Dock，第二个Dock栏。

4
00:00:15,000 --> 00:00:20,000
三十天只卖了六十四份，还有一百八十份。
"""


def main():
    cues = fm.parse_srt(SRT)
    sents = fm.build_sentences(cues)
    merged, n = fm.merge_split_sentences(sents)

    checks = [
        ("解析出 4 个 cue", len(cues) == 4),
        ("句子数合理", 5 <= len(sents) <= 8),
        ("跨字幕合并生效", n >= 1),
        ("合并后含完整 Dock 句", any("一个Dock" in s for _, s in merged)),
    ]

    # 数字规整
    txt = fm.clean("三十天只卖了六十四份，还有一百八十份。")
    checks.append(("中文数字转阿拉伯", "30天" in txt and "64份" in txt))

    failed = 0
    for name, ok in checks:
        print(("✅" if ok else "❌"), name)
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
