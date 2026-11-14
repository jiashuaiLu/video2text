#!/usr/bin/env python3
"""llm — 统一的大模型后端：本地 MLX 或任意 OpenAI 兼容 API。

配置优先级（后者覆盖前者）：
    代码默认值 < 配置文件 < 环境变量 < 命令行参数

配置文件查找顺序：
    --config 指定 → $V2T_CONFIG → ./config.json → ~/.config/v2t/config.json

配置文件示例（config.json）：
{
  "llm": {
    "backend": "api",                        // "local" | "api"
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-xxxx",
    "model": "deepseek-chat",
    "temperature": 0.3,
    "max_tokens": 2000,
    "timeout": 180,
    "extra_body": {}                          // 透传给服务商的额外字段
  },
  "local": {
    "model": "mlx-community/Qwen3.5-9B-4bit"  // 本地兜底模型
  }
}

环境变量：V2T_BACKEND / V2T_BASE_URL / V2T_API_KEY / V2T_MODEL / V2T_LOCAL_MODEL
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOCAL = "mlx-community/Qwen3.5-9B-4bit"

# 必须在 import mlx_lm / huggingface_hub 之前设定，否则它们会缓存默认路径
os.environ.setdefault("HF_HOME", str(ROOT / "work/hf"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def _candidate_configs(cli_path: str | None):
    if cli_path:
        yield Path(cli_path).expanduser()
    if os.environ.get("V2T_CONFIG"):
        yield Path(os.environ["V2T_CONFIG"]).expanduser()
    yield ROOT / "config.json"
    yield Path.home() / ".config/v2t/config.json"


def load_config(cli_path: str | None = None, overrides: dict | None = None) -> dict:
    cfg = {
        "llm": {
            "backend": "", "base_url": "", "api_key": "", "model": "",
            "temperature": 0.3, "max_tokens": 2000, "timeout": 180,
            "extra_body": {},
        },
        "local": {"model": os.environ.get("V2T_LOCAL_MODEL", _DEFAULT_LOCAL)},
    }
    for p in _candidate_configs(cli_path):
        if p and p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                raise SystemExit(f"配置文件解析失败 {p}: {e}")
            for k in ("llm", "local"):
                if isinstance(data.get(k), dict):
                    cfg[k].update(data[k])
            cfg["_source"] = str(p)
            break

    # 环境变量
    env_map = {
        "V2T_BACKEND": ("llm", "backend"),
        "V2T_BASE_URL": ("llm", "base_url"),
        "V2T_API_KEY": ("llm", "api_key"),
        "V2T_MODEL": ("llm", "model"),
    }
    for env, (sec, key) in env_map.items():
        if os.environ.get(env):
            cfg[sec][key] = os.environ[env]

    # 命令行覆盖
    for (sec, key), val in (overrides or {}).items():
        if val not in (None, ""):
            cfg[sec][key] = val

    # backend 留空 = 自动：配了 key 就走 API，否则本地
    if cfg["llm"]["backend"] not in ("local", "api"):
        cfg["llm"]["backend"] = ("api" if (cfg["llm"]["base_url"] and cfg["llm"]["api_key"])
                                 else "local")
    if cfg["llm"]["backend"] == "api" and not (cfg["llm"]["base_url"] and cfg["llm"]["api_key"]):
        raise SystemExit(
            "backend=api 但缺少 base_url 或 api_key。\n"
            "可在 config.json 里配置，或用环境变量 V2T_BASE_URL / V2T_API_KEY，\n"
            "或命令行 --base-url / --api-key。"
        )
    return cfg


def mask(secret: str) -> str:
    if not secret:
        return "(空)"
    return secret[:6] + "…" + secret[-4:] if len(secret) > 12 else "***"


def describe(cfg: dict) -> str:
    llm = cfg["llm"]
    if llm["backend"] == "api":
        return (f"API  {llm['base_url']}  模型={llm['model'] or '(未指定)'}  "
                f"key={mask(llm['api_key'])}")
    return f"本地 {cfg['local']['model']}"


# ---------------- API 后端 ----------------

def _api_chat(messages, llm: dict) -> str:
    url = llm["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = {
        "model": llm["model"],
        "messages": messages,
        "temperature": llm.get("temperature", 0.3),
        "max_tokens": llm.get("max_tokens", 2000),
        "stream": False,
    }
    payload.update(llm.get("extra_body") or {})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm['api_key']}",
        "User-Agent": "v2t/2.0",
    }

    last = None
    for attempt in (1, 2, 3):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=llm.get("timeout", 180)) as r:
                data = json.loads(r.read().decode("utf-8"))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                # 有些推理模型把正文放在 reasoning_content，content 为空
                content = (msg.get("reasoning_content") or "").strip()
            if content:
                return content
            last = RuntimeError(f"返回为空: {json.dumps(data, ensure_ascii=False)[:300]}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code in (400, 401, 403, 404):
                break          # 配置类错误，重试没意义
        except Exception as e:
            last = e
        time.sleep(1.5 * attempt)
    raise RuntimeError(f"API 调用失败（{llm['base_url']}）: {last}")


# ---------------- 本地后端 ----------------

_LOCAL_CACHE: dict = {}


def _local_load(model_id: str):
    if model_id not in _LOCAL_CACHE:
        from mlx_lm import load
        _LOCAL_CACHE[model_id] = load(model_id)
    return _LOCAL_CACHE[model_id]


def _local_chat(messages, model_id: str, max_tokens: int, temperature: float) -> str:
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    model, tok = _local_load(model_id)
    kwargs = dict(tokenize=False, add_generation_prompt=True)
    try:
        prompt = tok.apply_chat_template(messages, enable_thinking=False, **kwargs)
    except TypeError:
        prompt = tok.apply_chat_template(messages, **kwargs)
    return generate(model, tok, prompt=prompt, max_tokens=max_tokens,
                    sampler=make_sampler(temp=temperature), verbose=False).strip()


def chat(system: str, user: str, cfg: dict, max_tokens: int | None = None) -> str:
    """统一入口：按配置走本地或 API。"""
    llm = cfg["llm"]
    mt = max_tokens or llm.get("max_tokens", 2000)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    if llm["backend"] == "api":
        return _api_chat(messages, dict(llm, max_tokens=mt))
    return _local_chat(messages, cfg["local"]["model"], mt,
                       llm.get("temperature", 0.3))


def strip_reasoning(text: str) -> str:
    """去掉模型可能带上的思考过程标签。"""
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.S).strip()


def write_template(path: Path):
    tpl = {
        "llm": {
            "backend": "api",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "model": "deepseek-chat",
            "temperature": 0.3,
            "max_tokens": 4000,
            "timeout": 180,
            "extra_body": {},
        },
        "local": {"model": _DEFAULT_LOCAL},
    }
    path.write_text(json.dumps(tpl, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.chmod(path, 0o600)
    return path
