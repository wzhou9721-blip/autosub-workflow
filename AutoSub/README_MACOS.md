# AutoSub macOS 使用说明

这个包支持两种 macOS 使用方式：

- 正式内部交付：在一台 Mac 上生成未签名 `AutoSub.dmg`，发给同事安装使用。
- 开发/排错运行：直接运行源码包里的 `run_macos.command`。

推荐给非技术同事使用第一种。

## 运行

1. 安装 Homebrew（如已安装可跳过）。
2. 安装系统依赖：

```bash
brew install python@3.11 ffmpeg
```

3. 双击 `run_macos.command`，或在终端执行：

```bash
chmod +x run_macos.command
./run_macos.command
```

脚本会自动创建 `.venv-macos` 虚拟环境并安装 Python 依赖。

## 重要限制

- 本包在 Windows 上整理，无法直接产出 macOS 的 `.app` / `.dmg`。如果需要双击版，请在 macOS 上运行 `build_macos_app.command`。
- 实时系统声音捕获目前依赖 Windows loopback，macOS 上可能不可用；文件转写、字幕处理、翻译等主流程可按正常方式使用。
- `config.json` 已脱敏。第一次使用前请在软件设置里填写自己的 API Key。

## 给同事的安装方式

把生成出来的 `AutoSub.dmg` 发给同事。同事只需要：

1. 双击打开 `AutoSub.dmg`。
2. 把 `AutoSub.app` 拖到 `Applications`。
3. 第一次打开如果提示“无法验证开发者”，在 `Applications` 里右键 `AutoSub.app`，选择“打开”，再点“打开”确认。

这是未签名内部软件的正常现象，只需要第一次这样操作。

## 最简单：双击生成内部版 DMG

在 Mac 上解压本包后，直接双击：

```text
一键生成AutoSub安装包.command
```

它会自动检查 Homebrew，安装 `python@3.11` 和 `ffmpeg`，然后生成 `dist/AutoSub.dmg`。

如果提示未安装 Homebrew，脚本会打开 Homebrew 官网。安装完成后，重新双击这个脚本即可。

## 终端方式：生成内部版 DMG

```bash
chmod +x build_macos_dmg_unsigned.command
./build_macos_dmg_unsigned.command
```

成功后安装包会在 `dist/AutoSub.dmg`。

## 在 macOS 上只生成 .app

```bash
chmod +x build_macos_app.command
./build_macos_app.command
```

成功后应用会在 `dist/AutoSub.app`。
