# AutoSub Workflow

English | [中文](README.zh-CN.md)

AutoSub Workflow is a desktop subtitle workflow tool for transcription, subtitle cleanup, semantic splitting, translation, overflow repair, and export. It is built with PyQt6 and designed around an end-to-end video subtitle production flow.

## Features

- Local or cloud speech transcription
- AI subtitle cleanup and semantic splitting
- Batch translation with contextual prompts
- Subtitle overflow detection and repair
- SRT/VTT/ASS and production report export
- Realtime capture workflow support
- Windows launcher and macOS packaging scripts

## Project Layout

```text
AutoSub/
  app/                    Application source code
  main.py                 Desktop app entry point
  autosub_cli.py          CLI worker entry point
  requirements.txt        Python dependencies for Windows/dev use
  requirements-macos.txt  macOS packaging dependencies
  config.example.json     Safe example config without API keys
  README_MACOS.md         macOS packaging notes
  启动.bat                Windows launcher
```

## Setup

Python 3.11 or 3.12 is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r AutoSub\requirements.txt
```

Create your local config from the example:

```powershell
Copy-Item AutoSub\config.example.json AutoSub\config.json
```

Then edit `AutoSub/config.json` locally with your API keys, model names, language preferences, and workflow switches.

Important: `AutoSub/config.json` is intentionally ignored by Git because it may contain private API keys. Do not commit it.

## Run

From the repository root:

```powershell
.\.venv\Scripts\python.exe AutoSub\main.py
```

Or on Windows, run:

```powershell
AutoSub\启动.bat
```

## Typical Workflow

1. Import a video or audio file.
2. Choose local/cloud transcription settings.
3. Enable or disable cleanup, semantic splitting, translation, and overflow repair.
4. Run the task.
5. Review generated subtitles.
6. Export subtitle files and reports.

## Notes

- Cloud transcription, translation, optimization, context enhancement, and repair features require the corresponding API keys in `AutoSub/config.json`.
- Local transcription requires downloaded models and runtime binaries under ignored local directories such as `AutoSub/models/` and `AutoSub/bin/`.
- Runtime logs, temporary resume files, local configs, model files, and captured session data are ignored by Git.

## Development

Run the regression checks:

```powershell
.\.venv\Scripts\python.exe AutoSub\run_regression.py
```

The app is under active iteration, so check `AutoSub/config.example.json` when new settings are added.
