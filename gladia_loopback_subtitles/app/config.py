from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    gladia_api_key: str
    gladia_base_url: str
    gladia_model: str
    source_languages: list[str]
    code_switching: bool
    target_language: str
    use_external_translation: bool
    gladia_translation_model: str
    translation_match_original_utterances: bool
    translation_lipsync: bool
    translation_context_adaptation: bool
    translation_context: str
    translation_informal: bool
    translation_api_key: str
    translation_base_url: str
    translation_api_model: str
    translation_frequency: int
    translation_batch_size: int
    translation_request_timeout_seconds: float
    encoding: str
    sample_rate: int
    bit_depth: int
    channels: int
    capture_sample_rate: int
    capture_channels: int
    chunk_duration_ms: int
    endpointing: float
    maximum_duration_without_endpointing: float
    audio_enhancer: bool
    speech_threshold: float
    output_dir: Path
    output_srt_path: Path
    output_json_path: Path
    runtime_log_path: Path
    result_poll_interval_seconds: float
    result_poll_timeout_seconds: float


def _parse_languages(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    api_key = os.getenv("GLADIA_API_KEY", "").strip()
    if not api_key or api_key == "your_gladia_api_key_here":
        raise RuntimeError("GLADIA_API_KEY is missing. Please set it in your .env file.")

    output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    translation_api_key = os.getenv("TRANSLATION_API_KEY", "").strip()
    translation_base_url = os.getenv("TRANSLATION_BASE_URL", "").strip()
    translation_api_model = os.getenv("TRANSLATION_MODEL", "").strip()
    use_external_translation = bool(
        translation_api_key
        and translation_base_url
        and translation_api_model
        and translation_api_key != "your_translation_api_key_here"
        and translation_base_url != "https://your-openai-compatible-base-url/v1"
        and translation_api_model != "your_translation_model"
    )

    sample_rate = int(os.getenv("GLADIA_SAMPLE_RATE", "16000"))
    bit_depth = int(os.getenv("GLADIA_BIT_DEPTH", "16"))
    channels = int(os.getenv("GLADIA_CHANNELS", "1"))

    return Settings(
        gladia_api_key=api_key,
        gladia_base_url=os.getenv("GLADIA_BASE_URL", "https://api.gladia.io").rstrip("/"),
        gladia_model=os.getenv("GLADIA_MODEL", "solaria-1"),
        source_languages=_parse_languages(os.getenv("GLADIA_SOURCE_LANGUAGES")),
        code_switching=_parse_bool(os.getenv("GLADIA_CODE_SWITCHING"), False),
        target_language=os.getenv("GLADIA_TARGET_LANGUAGE", "zh").strip() or "zh",
        use_external_translation=use_external_translation,
        gladia_translation_model=os.getenv("GLADIA_TRANSLATION_MODEL", "enhanced").strip() or "enhanced",
        translation_match_original_utterances=_parse_bool(
            os.getenv("GLADIA_TRANSLATION_MATCH_ORIGINAL_UTTERANCES"), True
        ),
        translation_lipsync=_parse_bool(os.getenv("GLADIA_TRANSLATION_LIPSYNC"), False),
        translation_context_adaptation=_parse_bool(
            os.getenv("GLADIA_TRANSLATION_CONTEXT_ADAPTATION"), True
        ),
        translation_context=os.getenv("GLADIA_TRANSLATION_CONTEXT", "").strip(),
        translation_informal=_parse_bool(os.getenv("GLADIA_TRANSLATION_INFORMAL"), False),
        translation_api_key=translation_api_key,
        translation_base_url=translation_base_url,
        translation_api_model=translation_api_model,
        translation_frequency=max(1, int(os.getenv("TRANSLATION_FREQUENCY", "1"))),
        translation_batch_size=int(os.getenv("TRANSLATION_BATCH_SIZE", "20")),
        translation_request_timeout_seconds=float(
            os.getenv("TRANSLATION_REQUEST_TIMEOUT_SECONDS", "60")
        ),
        encoding=os.getenv("GLADIA_ENCODING", "wav/pcm"),
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
        capture_sample_rate=int(os.getenv("CAPTURE_SAMPLE_RATE", "48000")),
        capture_channels=int(os.getenv("CAPTURE_CHANNELS", "2")),
        chunk_duration_ms=int(os.getenv("CHUNK_DURATION_MS", "100")),
        endpointing=float(os.getenv("GLADIA_ENDPOINTING", "0.05")),
        maximum_duration_without_endpointing=float(
            os.getenv("GLADIA_MAX_WITHOUT_ENDPOINTING", "5")
        ),
        audio_enhancer=_parse_bool(os.getenv("GLADIA_AUDIO_ENHANCER"), False),
        speech_threshold=float(os.getenv("GLADIA_SPEECH_THRESHOLD", "0.6")),
        output_dir=output_dir,
        output_srt_path=output_dir / "output.srt",
        output_json_path=output_dir / "output.json",
        runtime_log_path=output_dir / "runtime.log",
        result_poll_interval_seconds=float(os.getenv("RESULT_POLL_INTERVAL_SECONDS", "2")),
        result_poll_timeout_seconds=float(os.getenv("RESULT_POLL_TIMEOUT_SECONDS", "120")),
    )


def build_runtime_settings(
    settings: Settings,
    *,
    source_languages: list[str] | None = None,
    code_switching: bool | None = None,
    audio_enhancer: bool | None = None,
    translation_frequency: int | None = None,
) -> Settings:
    return replace(
        settings,
        source_languages=source_languages if source_languages is not None else settings.source_languages,
        code_switching=code_switching if code_switching is not None else settings.code_switching,
        audio_enhancer=audio_enhancer if audio_enhancer is not None else settings.audio_enhancer,
        translation_frequency=translation_frequency if translation_frequency is not None else settings.translation_frequency,
    )
