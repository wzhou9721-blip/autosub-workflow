# Minimal Windows Loopback -> Gladia Live STT -> SRT

This project captures Windows system playback audio with WASAPI loopback, streams it to Gladia Live STT V2, and writes final subtitle files to `output/output.srt` and `output/output.json`.

The defaults stay close to Gladia's conservative live examples:

- `GLADIA_SOURCE_LANGUAGES=en` for English videos
- `GLADIA_ENDPOINTING=0.05`
- `GLADIA_AUDIO_ENHANCER=false`
- `GLADIA_SAMPLE_RATE=16000`

If you provide an external OpenAI-compatible translation API, the app will switch to:

- Gladia for English transcription only
- External API for final subtitle translation after the session ends

## What this version does

- Captures only Windows system playback audio
- Uses `POST /v2/live` to create a Gladia session
- Connects to the returned WebSocket URL
- Streams raw PCM audio chunks in real time
- Prints partial transcripts and final transcripts in the terminal
- Enables translation and prefers Chinese text in the exported SRT
- Sends `stop_recording` when you end the session
- Polls `GET /v2/live/{id}` for the final result

## What this version does not do

- No GUI
- No OBS integration
- No microphone mode
- No diarization
- No custom vocabulary or football terminology tuning
- No database
- No login system

## Project structure

```text
app/
  __init__.py
  main.py
  config.py
  system_audio_capture.py
  gladia_client.py
  transcript_store.py
  srt_writer.py
  utils.py
requirements.txt
.env.example
README.md
```

## Setup

1. Enter the project folder:

```powershell
cd gladia_loopback_subtitles
```

2. Create or activate a Python 3.11 virtual environment on Windows.
3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in your Gladia API key:

```powershell
Copy-Item .env.example .env
```

5. Edit `.env` and set:

```env
GLADIA_API_KEY=your_real_key
```

Set `GLADIA_SOURCE_LANGUAGES` to match the spoken audio. For example:

- `en` for English commentary
- `zh` for Chinese commentary
- leave it empty only if you really want auto-detection

You can also tune translation quality in `.env`:

- `GLADIA_TRANSLATION_MODEL=enhanced`: better translation quality than `base`
- `GLADIA_TRANSLATION_LIPSYNC=false`: better for subtitles; `true` is more dubbing-oriented
- `GLADIA_TRANSLATION_CONTEXT=`: optional extra context for the topic if you want to improve terminology later

## External translation API

If you want to use your own translation API instead of Gladia's translation, fill all three values in `.env`:

```env
TRANSLATION_API_KEY=your_key
TRANSLATION_BASE_URL=https://your-openai-compatible-base-url/v1
TRANSLATION_MODEL=your_model_name
```

When these are configured, the app will:

- Disable Gladia translation
- Keep Gladia transcription
- Translate each final English utterance through your own OpenAI-compatible API during the session
- Backfill any still-missing translations after the session ends
- Write translated Chinese subtitles into `output/output.srt`

The `output/output.json` file will also include an `external_translation` block with the translated utterances.

## Run

Start playing your local video first so Windows is already outputting sound through your current default speaker or headphones.

Then run:

```powershell
python -m app.main
```

For the desktop UI, run:

```powershell
python -m app.desktop_ui
```

Or just double-click:

```text
启动UI.bat
```

The app will:

- Open Windows loopback capture on the default playback device
- Start a Gladia live session
- Print logs like:

```text
[INFO] session started: ...
[PARTIAL] ...
[FINAL][SRC] ...
[FINAL][ZH] ...
[SAVED] output/output.srt
```

Press `Enter` in the terminal to stop gracefully. The program will send `stop_recording`, poll the final job result, and then write:

- `output/output.srt`
- `output/output.json`

## 桌面 UI

桌面 UI 沿用了你 `AutoSub` 的原生 Python 桌面方向，但保持成单窗口最小实现。

可选项：

- `主语言`：选择主要语音语言，也可以选 `自动检测`
- `开启双语言识别`：可以再选一个第二语言，并启用 Gladia 的 code switching
- `开启降噪增强`：在外放、底噪更明显的场景下开启音频增强

界面会显示：

- 当前运行状态
- 开始 / 停止按钮
- 实时日志，包括 `[PARTIAL]`、`[FINAL][SRC]`、`[FINAL][ZH]`
- 一键打开导出的 SRT 或输出目录

## Notes

- This project is intentionally conservative and minimal. It uses Gladia's documented live flow: create session, connect WebSocket, send audio chunks, stop recording, then fetch the final result.
- `SoundCard` is used first because it supports Windows WASAPI loopback. If loopback fails on your machine, try adjusting `CAPTURE_SAMPLE_RATE` or `CAPTURE_CHANNELS` in `.env`.
- The SRT is generated only from final results. It prefers translated Chinese text and falls back to the source utterance text when a Chinese translation is missing.
- Gladia's supported language code for Chinese translation is `zh`. The docs do not expose a separate `zh-CN` live translation target code in the places used here, so this project uses the official conservative code `zh`.
