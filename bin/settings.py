#!/usr/bin/env python3
"""settings — 给图形界面用的配置读写与连通性测试（不打印密钥）

用法:
    python settings.py read              → JSON（密钥只返回掩码）
    python settings.py write  '<json>'    → 保存（api_key 为空表示沿用旧值）
    python settings.py test               → 测试当前后端，输出 OK/ERR
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import llm  # noqa: E402

CFG_PATH = ROOT / "config.json"


def read() -> dict:
    cfg = llm.load_config(None)
    key = cfg["llm"].get("api_key", "")
    return {
        "backend": cfg["llm"]["backend"],
        "provider": cfg["llm"].get("provider", ""),
        "base_url": cfg["llm"].get("base_url", ""),
        "model": cfg["llm"].get("model", ""),
        "local_model": cfg["local"]["model"],
        "key_set": bool(key),
        "key_masked": llm.mask(key),
    }


def write(payload: dict) -> dict:
    data = {}
    if CFG_PATH.exists():
        try:
            data = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("llm", {})
    data.setdefault("local", {})

    for k in ("backend", "provider", "base_url", "model"):
        if k in payload and payload[k] is not None:
            data["llm"][k] = payload[k]
    # 空 api_key = 不修改
    if payload.get("api_key"):
        data["llm"]["api_key"] = payload["api_key"]
    if payload.get("clear_key"):
        data["llm"]["api_key"] = ""
    if payload.get("local_model"):
        data["local"]["model"] = payload["local_model"]

    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    CFG_PATH.chmod(0o600)
    return read()


def test() -> str:
    cfg = llm.load_config(None)
    t = time.time()
    try:
        reply = llm.chat("你是一个测试助手。", "回复两个字：可用", cfg, max_tokens=20)
        return f"OK|{time.time() - t:.1f}|{reply[:40]}"
    except Exception as e:
        msg = str(e).replace("\n", " ")[:200]
        return f"ERR|{time.time() - t:.1f}|{msg}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "read":
        print(json.dumps(read(), ensure_ascii=False))
    elif cmd == "write":
        payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(write(payload), ensure_ascii=False))
    elif cmd == "test":
        print(test())
