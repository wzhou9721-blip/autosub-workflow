<p align="center">
  <img src="AutoSub/logo.ico" alt="AutoSub Logo" width="96">
</p>

<h1 align="center">AutoSub Workflow</h1>

<p align="center">
  A desktop subtitle workflow for transcription, semantic splitting, translation, overflow repair, and export.
</p>

<p align="center">
  English · <a href="README.zh-CN.md">中文</a> ·
  <a href="https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0-cloud-lite">Cloud Lite</a> ·
  <a href="https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0">Full</a>
</p>

![AutoSub cover](AutoSub/docs/screenshots/cover.png)

## Overview

AutoSub Workflow is a desktop subtitle production tool for short videos, sports clips, interviews, meeting recordings, and multilingual media. It is designed for the whole subtitle pipeline, not just speech-to-text: transcribe, clean up, split by meaning, translate, repair overflow, and export subtitle files and reports.

It is especially useful when:

- Multilingual videos cause missing or skipped transcript sections.
- Raw cloud transcription still needs cleanup, splitting, and review.
- Translated subtitles become too long for editing software.
- Different editors require different subtitle line-length behavior.
- You want transcription, translation, overflow repair, and export in one repeatable desktop flow.

## Core Features

- **Transcription** with cloud providers or local faster-whisper in the Full build.
- **AI cleanup** for repeated fragments, suspicious segments, punctuation issues, and rough transcript output.
- **Semantic splitting** using meaning, pauses, and length limits to produce more readable subtitles.
- **Context-aware translation** with video context, glossary hints, and semantic structure.
- **Overflow repair** based on subtitle style and maximum no-wrap length.
- **Realtime capture** for system audio workflows such as meetings, livestreams, and long media monitoring.
- **Export** to SRT, VTT, ASS, production notes, quality reports, and runtime logs.

## What Makes It Different

### 1. Built for multilingual missing-audio recovery

AutoSub includes multilingual mode and blank-interval gap filling. For bilingual interviews, mixed-language commentary, or sports clips with sudden language changes, it can revisit suspicious silent gaps and reduce missed speech.

![Multilingual mode and gap filling](AutoSub/docs/screenshots/multilingual-gap-fill.png)

### 2. Designed around editing-software subtitle limits

Many tools stop after translation. AutoSub goes one step further: PR mode / Jianying mode, font size, maximum no-wrap length, and automatic overflow repair help reduce unwanted line breaks after subtitles are imported into editing software.

![Subtitle style and overflow repair](AutoSub/docs/screenshots/overflow-workflow.png)

### 3. Realtime system-audio capture

AutoSub can capture system audio, split it into segments, then continue through transcription, translation, and export. This workflow requires manual segment submission and has some latency, but it is practical for livestreams, meetings, and monitoring long-running media.

![Realtime system-audio capture](AutoSub/docs/screenshots/realtime-capture.png)

### 4. Centralized global settings

Cloud ASR, translation, context enhancement, optimization, semantic splitting, web knowledge enhancement, overflow repair, local faster-whisper, and config management are organized in one global settings area.

![Global settings](AutoSub/docs/screenshots/global-settings.png)

## Releases

Two Windows builds are available:

- **Cloud Lite**: smaller package for users who mainly rely on cloud transcription, translation, and splitting. It includes ffmpeg for media extraction, but excludes local Whisper inference components.
- **Full**: complete package for users who need local Whisper / faster-whisper transcription support. Larger download, more bundled runtime pieces.

Download:

- [Cloud Lite](https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0-cloud-lite)
- [Full](https://github.com/wzhou9721-blip/autosub-workflow/releases/tag/v1.0.0)

## Typical Workflow

1. Import a video, audio, or subtitle file.
2. Choose cloud or local transcription.
3. Add video context, source language, target language, and glossary hints.
4. Enable AI cleanup, semantic splitting, translation, and overflow repair as needed.
5. Run the task and review the result.
6. Export subtitle files, quality reports, and runtime logs.

## Development Setup

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

Then edit `AutoSub/config.json` locally with your API keys, model names, language preferences, and workflow switches. This file may contain private credentials and is intentionally ignored by Git.

Run the development app:

```powershell
.\.venv\Scripts\python.exe AutoSub\main.py
```

## Packaging

Full build:

```powershell
cd AutoSub
..\.venv\Scripts\python.exe -m PyInstaller AutoSub.spec --noconfirm --clean
```

Cloud Lite build:

```powershell
cd AutoSub
..\.venv\Scripts\python.exe -m PyInstaller AutoSub_cloud_lite.spec --noconfirm --clean
```

## Notes

- Cloud transcription, translation, optimization, context enhancement, and overflow repair require the corresponding API keys in `AutoSub/config.json`.
- Cloud Lite does not bundle local Whisper inference components. Choose the Full build if local transcription is required.
- Runtime logs, temporary resume files, local configs, model files, and captured session data are ignored by Git.
