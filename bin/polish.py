#!/usr/bin/env python3
"""polish — 用本地小模型给转写稿补标点、分段、纠错，输出排版好的 Markdown。

全程离线，不联网（首次下载模型除外）。默认 Qwen2.5-7B-Instruct-4bit。

用法:
    polish <转写稿.txt> [-o 输出.md] [--model 模型] [--no-summary]
"""
import argparse
import re
import time
from pathlib import Path

DEFAULT_MODEL = "mlx-community/Qwen2.5-7B-Instruct-4bit"

PUNCT_SYSTEM = (
    "你是中文文字编辑，专门整理语音识别的原始转写稿。你的唯一任务是让文本可读，"
    "不是改写文章。规则：\n"
    "1. 补全标点符号（，。？！、：），按语义断句。\n"
    "2. 把零碎的短句合并成通顺的段落，每段只讲一件事，段间空行。\n"
    "3. 修正明显的语音识别错误：同音错字、专有名词（例如「JPT Cloud」应为「ChatGPT」、"
    "「无资别克斯坦」应为「乌兹别克斯坦」、「任务单」应为「任务栏」）。"
    "拿不准的保持原样，不要瞎猜。\n"
    "4. 严禁增删信息、严禁改写句式、严禁添加标题或总结、严禁编造内容。\n"
    "5. 只输出整理后的正文，不要任何解释或前后缀。"
)

SUMMARY_SYSTEM = (
    "你是内容编辑。只根据给定文稿生成结构化信息，不得编造文稿中没有的内容。\n"
    "输出严格使用下面的格式：\n"
    "## 一句话概括\n"
    "<一句话>\n\n"
    "## 关键要点\n"
    "- <要点1>\n- <要点2>\n- <要点3>\n\n"
    "## 值得记下的数字\n"
    "- <数字和含义>\n"
    "没有数字就写「- 无」。要点控制在 3-6 条。"
)


def chunk_lines(lines, size=50):
    """按行切块，保持原顺序；每块约 size 行。"""
    lines = [l.strip() for l in lines if l.strip()]
    return ["\n".join(lines[i:i + size]) for i in range(0, len(lines), size)]


def build_prompt(system, user):
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--title", default="")
    ap.add_argument("--meta", default="", help="元信息行，如 来源/时长")
    ap.add_argument("--glossary", default="", help="术语表，逗号分隔，强制统一写法")
    args = ap.parse_args()

    src = Path(args.input).expanduser()
    raw = src.read_text(encoding="utf-8").strip()
    title = args.title or src.stem
    out = Path(args.out) if args.out else src.with_suffix(".md")

    import os
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler

    t0 = time.time()
    print(f"[polish] 加载模型 {args.model} …", flush=True)
    model, tok = load(args.model)

    def chat(system, user, max_tokens=1500):
        prompt = tok.apply_chat_template(build_prompt(system, user),
                                         tokenize=False, add_generation_prompt=True)
        return generate(model, tok, prompt=prompt, max_tokens=max_tokens,
                        sampler=make_sampler(temp=0.2), verbose=False).strip()

    system = PUNCT_SYSTEM
    if args.glossary:
        system += ("\n\n【术语表】以下专有名词必须严格使用给定写法，"
                   "原文中发音相近的错写要改成它们：" + args.glossary)
    system += ("\n\n【量词】计量商品用「份」「单」「个」，不要用「道」。")

    # 1) 补标点 + 分段 + 纠错（分块，逐块处理）
    chunks = chunk_lines(raw.splitlines())
    print(f"[polish] 共 {len(chunks)} 块，逐块整理…", flush=True)
    polished = []
    for i, ch in enumerate(chunks, 1):
        print(f"[polish]   第 {i}/{len(chunks)} 块", flush=True)
        polished.append(chat(system, ch))
    body = "\n\n".join(polished)

    # 2) 结构化摘要
    extra = ""
    if not args.no_summary:
        print("[polish] 生成摘要与要点…", flush=True)
        extra = chat(SUMMARY_SYSTEM, body, max_tokens=700) + "\n\n"

    md = f"""# {title}

{args.meta}

{extra}## 正文

{body}
"""
    out.write_text(md, encoding="utf-8")
    n_chars = len(re.sub(r"\s", "", body))
    print(f"[polish] 完成，{time.time() - t0:.0f}s → {out}")
    print(f"[polish] 正文 {n_chars} 字")


if __name__ == "__main__":
    main()
