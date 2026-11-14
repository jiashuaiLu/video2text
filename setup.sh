#!/bin/bash
# v2t 一键安装（macOS Apple Silicon）
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> 检查依赖"
command -v ffmpeg >/dev/null 2>&1 || { echo "缺少 ffmpeg，请先执行：brew install ffmpeg"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "缺少 python3"; exit 1; }

echo "==> 创建虚拟环境"
[ -d .venv ] || python3 -m venv .venv

echo "==> 安装 Python 依赖（清华镜像）"
MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -i "$MIRROR" \
  yt-dlp mlx-whisper mlx-lm mlx-audio pypinyin pillow websocket-client

echo "==> 准备配置文件"
[ -f config.json ] || { cp config.example.json config.json && chmod 600 config.json; }

echo "==> 预下载模型（约 6GB，可跳过；首次运行会自动下载）"
if [ "${1:-}" = "--with-models" ]; then
  export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
  export HF_HOME="$ROOT/work/hf"
  .venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
for m in ("mlx-community/Qwen3-ASR-1.7B-8bit", "mlx-community/Qwen3.5-9B-4bit"):
    print("下载", m); snapshot_download(m)
PY
fi

echo
echo "完成。"
echo "  命令行： ./bin/v2t \"<链接>\""
echo "  桌面端： cd desktop && npm install && npx tauri build"
