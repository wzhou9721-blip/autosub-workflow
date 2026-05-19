#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装：brew install python@3.11"
  exit 1
fi

if [ ! -d ".venv-macos" ]; then
  python3 -m venv .venv-macos
fi

source .venv-macos/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-macos.txt

pyinstaller \
  --name AutoSub \
  --windowed \
  --onedir \
  --collect-all qfluentwidgets \
  --collect-all faster_whisper \
  --collect-all ctranslate2 \
  --add-data "logo.ico:." \
  --add-data "glossary_example.txt:." \
  main.py

echo "已生成：dist/AutoSub.app"
