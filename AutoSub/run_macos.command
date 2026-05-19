#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装：brew install python@3.11"
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "未找到 ffmpeg。请先安装：brew install ffmpeg"
  exit 1
fi

if [ ! -d ".venv-macos" ]; then
  python3 -m venv .venv-macos
fi

source .venv-macos/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-macos.txt
python main.py
