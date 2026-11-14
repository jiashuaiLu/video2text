#!/usr/bin/env python3
"""术语纠错回归测试：改动 glossary.py 后必须全绿。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from glossary import correct, parse_cli_glossary  # noqa: E402

CASES = [
    # (原文, 期望, 说明)
    ("因为找需求不如超需求", "因为找需求不如抄需求", "同音字纠错，且不误伤「找需求」"),
    ("这是无资别克斯坦开发者的案例", "这是乌兹别克斯坦开发者的案例", "同音专有名词"),
    ("还有 GPT Cloud 这些额度", "还有 ChatGPT 这些额度", "别名映射"),
    ("都能跟它的 Trust MR 对上", "都能跟它的 TrustMRR 对上", "拉丁词归一"),
    ("别从零去想需求，甚至造需求", "别从零去想需求，甚至造需求", "完全不该改动"),
]

GLOSSARY = parse_cli_glossary(
    "LumaBar;ChatGPT=GPT Cloud;TrustMRR=Trust MR;抄需求;乌兹别克斯坦")


def main():
    failed = 0
    for src, want, why in CASES:
        got, _ = correct(src, GLOSSARY)
        ok = got == want
        print(("✅" if ok else "❌"), why)
        if not ok:
            print(f"   原文: {src}\n   期望: {want}\n   实际: {got}")
            failed += 1
    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
