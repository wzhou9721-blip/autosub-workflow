#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

./build_macos_app.command

APP_PATH="dist/AutoSub.app"
DMG_PATH="dist/AutoSub.dmg"
STAGING_DIR="dist/dmg-staging"

if [ ! -d "$APP_PATH" ]; then
  echo "未找到 $APP_PATH，打包失败。"
  exit 1
fi

rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

cat > "$STAGING_DIR/首次打开说明.txt" <<'EOF'
AutoSub 内部版安装说明

1. 把 AutoSub.app 拖到 Applications。
2. 第一次打开如果提示“无法验证开发者”：
   - 打开 Applications
   - 右键 AutoSub.app
   - 选择“打开”
   - 再点“打开”确认

这是未签名内部软件的正常提示，只需要第一次这样操作。
EOF

hdiutil create \
  -volname "AutoSub" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"

echo "已生成：$DMG_PATH"
