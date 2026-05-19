#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "AutoSub macOS 内部版打包助手"
echo "--------------------------------"
echo "这个脚本会在当前 Mac 上生成 dist/AutoSub.dmg。"
echo

if ! command -v brew >/dev/null 2>&1; then
  echo "未检测到 Homebrew。"
  echo "请先在打开的网页中安装 Homebrew，然后重新双击本脚本。"
  open "https://brew.sh"
  echo
  echo "按回车退出。"
  read -r _
  exit 1
fi

echo "正在准备系统依赖：python@3.11、ffmpeg"
brew list python@3.11 >/dev/null 2>&1 || brew install python@3.11
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg

echo
echo "正在生成 AutoSub.dmg，这一步第一次运行会下载 Python 依赖，可能需要几分钟。"
chmod +x build_macos_dmg_unsigned.command build_macos_app.command run_macos.command
./build_macos_dmg_unsigned.command

echo
echo "完成：$(pwd)/dist/AutoSub.dmg"
echo "这个 AutoSub.dmg 就是发给同事的最终安装包。"
echo
echo "按回车退出。"
read -r _
