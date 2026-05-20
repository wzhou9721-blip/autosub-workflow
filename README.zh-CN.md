# AutoSub Workflow

[English](README.md) | 中文

AutoSub Workflow 是一款面向短视频、体育剪辑、访谈和多语言内容生产的桌面字幕工作台。它把“转录、清理、断句、翻译、溢出修复、导出”串成一个完整流程，让字幕不再散落在脚本、网页和临时文件之间。

## 为什么做它

很多字幕工具只解决其中一步：转录很快，但断句粗糙；翻译能跑，但字幕会溢出；导出有文件，却缺少过程记录。AutoSub 的目标是把字幕生产变成一个可复用的工作流：导入素材，配置语言和模型，运行任务，检查结果，最后导出能直接交付的字幕文件和报告。

## 主要能力

- **云端 / 本地转录**：支持 Gladia、Whisper 兼容接口，以及本地 faster-whisper 工作流。
- **AI 初始优化**：清理转录噪声，减少幻觉片段、重复片段和不自然标点。
- **智能语义断句**：按语义、停顿和长度限制重组字幕，让观众更容易读。
- **上下文翻译**：结合视频语境、术语表和语义结构，尽量保留人名、队名、赛事名和上下文关系。
- **溢出修复**：面向剪辑软件字幕框，自动检查并修复过长字幕。
- **一站式导出**：导出 SRT、VTT、ASS、新闻稿和质量报告。
- **实时捕获工作流**：支持系统音频捕获场景，方便直播、会议或长素材拆段处理。

## 下载哪个版本

项目 Release 提供两个 Windows 版本：

- **Cloud Lite 版**：体积更小，适合主要使用云端转录、翻译和断句的用户。内置 ffmpeg，但不包含本地 Whisper 推理组件。
- **Full 版**：功能完整，适合需要本地 Whisper / faster-whisper 转录能力的用户。体积更大，但解压后组件更齐。

发布页：

- [Cloud Lite 版](https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0-cloud-lite)
- [Full 版](https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0)

## 典型流程

1. 导入视频或音频文件。
2. 选择云端或本地转录方式。
3. 填写视频语境、源语言、目标语言和术语表。
4. 按需开启 AI 优化、智能断句、翻译和溢出修复。
5. 运行任务并检查结果。
6. 导出字幕文件、质量报告和运行日志。

## 项目结构

```text
AutoSub/
  app/                    应用源码
  main.py                 桌面应用入口
  autosub_cli.py          CLI 工作器入口
  AutoSub.spec            Full 版打包配置
  AutoSub_cloud_lite.spec Cloud Lite 版打包配置
  requirements.txt        Windows / 开发环境依赖
  config.example.json     不含密钥的示例配置
  启动.bat                Windows 一键启动脚本
```

## 本地开发

建议使用 Python 3.11 或 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r AutoSub\requirements.txt
```

从示例配置创建本地配置：

```powershell
Copy-Item AutoSub\config.example.json AutoSub\config.json
```

然后在 `AutoSub/config.json` 中填写自己的 API Key、模型名称、语言偏好和工作流开关。这个文件可能包含私密密钥，已经被 Git 忽略，请不要提交。

启动开发版：

```powershell
.\.venv\Scripts\python.exe AutoSub\main.py
```

运行回归检查：

```powershell
.\.venv\Scripts\python.exe AutoSub\run_regression.py
```

## 打包

Full 版：

```powershell
cd AutoSub
..\.venv\Scripts\python.exe -m PyInstaller AutoSub.spec --noconfirm --clean
```

Cloud Lite 版：

```powershell
cd AutoSub
..\.venv\Scripts\python.exe -m PyInstaller AutoSub_cloud_lite.spec --noconfirm --clean
```

## 说明

- 云端转录、翻译、优化、语境增强和溢出修复需要配置对应 API Key。
- Cloud Lite 版不包含本地 Whisper 组件，选择本地转录时会提示下载 Full 版或安装本地组件。
- 本地模型、运行日志、临时断点文件、用户配置和实时捕获数据默认不会提交到 Git。
