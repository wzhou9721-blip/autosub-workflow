# AutoSub Workflow

[English](README.md) | 中文

AutoSub Workflow 是一个桌面端字幕工作流工具，用于视频/音频转录、字幕清理、智能断句、翻译、溢出修复和导出。项目基于 PyQt6，目标是把字幕生产流程串成一个可操作的完整工具。

## 主要功能

- 本地或云端语音转录
- AI 字幕清理与语义断句
- 带上下文的批量翻译
- 字幕溢出检测与修复
- 导出 SRT/VTT/ASS 和质量报告
- 实时捕获工作流支持
- Windows 启动脚本和 macOS 打包脚本

## 项目结构

```text
AutoSub/
  app/                    应用源码
  main.py                 桌面应用入口
  autosub_cli.py          CLI 工作器入口
  requirements.txt        Windows/开发环境依赖
  requirements-macos.txt  macOS 打包依赖
  config.example.json     不含密钥的示例配置
  README_MACOS.md         macOS 打包说明
  启动.bat                Windows 一键启动脚本
```

## 安装

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

然后在本地编辑 `AutoSub/config.json`，填写 API Key、模型名称、语言偏好和工作流开关。

注意：`AutoSub/config.json` 可能包含私人 API Key，所以已经被 Git 忽略。不要提交这个文件。

## 启动

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe AutoSub\main.py
```

Windows 也可以直接运行：

```powershell
AutoSub\启动.bat
```

## 常用流程

1. 导入视频或音频文件。
2. 选择本地/云端转录配置。
3. 按需开启或关闭清理、智能断句、翻译、溢出修复。
4. 开始任务。
5. 检查生成的字幕。
6. 导出字幕文件和质量报告。

## 配置说明

- 云端转录、翻译、优化、语境增强、溢出修复等功能需要在 `AutoSub/config.json` 中配置对应 API Key。
- 本地转录需要本地模型和运行时二进制文件，这些通常位于 `AutoSub/models/`、`AutoSub/bin/`，并且不会上传到 Git。
- 运行日志、临时断点文件、本地配置、模型文件和实时捕获数据都被 Git 忽略。

## 开发与检查

运行回归检查：

```powershell
.\.venv\Scripts\python.exe AutoSub\run_regression.py
```

项目仍在持续迭代；如果新增配置项，可以参考并更新 `AutoSub/config.example.json`。
