from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings, build_runtime_settings
from .gladia_client import GladiaLiveClient
from .language_options import language_label
from .srt_writer import write_srt
from .system_audio_capture import AudioChunkConfig, SystemAudioCapture
from .transcript_store import TranscriptStore
from .translation_client import ExternalTranslationClient, RealtimeTranslationCoordinator
from .utils import (
    configure_runtime_log,
    emit_runtime_message,
    ensure_directory,
    log_info,
    save_json,
    set_runtime_message_listener,
)


@dataclass(slots=True)
class RuntimeOptions:
    primary_language: str = "en"
    dual_language_enabled: bool = False
    secondary_language: str = "zh"
    denoise_enabled: bool = False
    translation_frequency: int = 1

    def build_source_languages(self) -> list[str]:
        codes: list[str] = []
        if self.primary_language:
            codes.append(self.primary_language)
        if self.dual_language_enabled and self.secondary_language and self.secondary_language not in codes:
            codes.append(self.secondary_language)
        return codes

    def describe(self) -> str:
        codes = self.build_source_languages()
        if not codes:
            language_text = "自动检测"
        else:
            language_text = ", ".join(language_label(code) for code in codes)
        return (
            f"语言={language_text}; "
            f"双语言={'开启' if self.dual_language_enabled else '关闭'}; "
            f"降噪={'开启' if self.denoise_enabled else '关闭'}; "
            f"翻译频率={max(1, self.translation_frequency)}"
        )


@dataclass(slots=True)
class SessionCallbacks:
    on_log: Callable[[str], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_finished: Callable[[Path, Path], None] | None = None
    on_error: Callable[[str], None] | None = None
    on_partial: Callable[[str], None] | None = None
    on_final_source: Callable[[str, str], None] | None = None
    on_final_translation: Callable[[str, str], None] | None = None
    on_audio_level: Callable[[float], None] | None = None


class SessionRunner:
    def __init__(self, base_settings: Settings, callbacks: SessionCallbacks | None = None) -> None:
        self.base_settings = base_settings
        self.callbacks = callbacks or SessionCallbacks()

    def run(self, stop_signal: threading.Event, runtime_options: RuntimeOptions) -> None:
        settings = build_runtime_settings(
            self.base_settings,
            source_languages=runtime_options.build_source_languages(),
            code_switching=runtime_options.dual_language_enabled,
            audio_enhancer=runtime_options.denoise_enabled,
            translation_frequency=max(1, runtime_options.translation_frequency),
        )
        try:
            asyncio.run(self._run_async(settings, stop_signal, runtime_options))
        except Exception as exc:
            if self.callbacks.on_error is not None:
                self.callbacks.on_error(str(exc))
            raise

    async def _run_async(
        self,
        settings: Settings,
        stop_signal: threading.Event,
        runtime_options: RuntimeOptions,
    ) -> None:
        ensure_directory(settings.output_dir)
        settings.runtime_log_path.write_text("", encoding="utf-8")
        configure_runtime_log(settings.runtime_log_path)
        set_runtime_message_listener(self.callbacks.on_log)

        transcript_store = TranscriptStore(target_language=settings.target_language)
        translation_client = ExternalTranslationClient(settings) if settings.use_external_translation else None
        translation_coordinator = (
            RealtimeTranslationCoordinator(
                client=translation_client,
                store=transcript_store,
                target_language=settings.target_language,
                on_translation=self.callbacks.on_final_translation,
            )
            if translation_client is not None
            else None
        )
        gladia_client = GladiaLiveClient(
            settings=settings,
            transcript_store=transcript_store,
            on_final_transcript=translation_coordinator.submit if translation_coordinator is not None else None,
            on_partial_transcript=self.callbacks.on_partial,
            on_final_source=self.callbacks.on_final_source,
            on_translation=self.callbacks.on_final_translation if translation_coordinator is None else None,
        )
        capture = SystemAudioCapture(
            AudioChunkConfig(
                capture_sample_rate=settings.capture_sample_rate,
                capture_channels=settings.capture_channels,
                target_sample_rate=settings.sample_rate,
                chunk_duration_ms=settings.chunk_duration_ms,
            )
        )

        try:
            self._status("starting")
            capture.open()
            log_info(f"capture backend: {capture.backend} ({capture.device_name})")
            log_info(f"runtime options: {runtime_options.describe()}")
            session = gladia_client.create_session()
            log_info(f"session started: {session.session_id}")
            self._status("running")

            stream_task = asyncio.create_task(
                gladia_client.stream_audio(
                    session,
                    capture,
                    stop_signal,
                    on_audio_level=self.callbacks.on_audio_level,
                )
            )
            stop_task = asyncio.create_task(asyncio.to_thread(stop_signal.wait))

            done, pending = await asyncio.wait(
                {stream_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_task in done:
                stop_signal.set()
                self._status("stopping")

            if stop_task in pending:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)

            await stream_task

            log_info("waiting for final result")
            result = await asyncio.to_thread(gladia_client.wait_for_final_result, session.session_id)

            translated_texts: list[str] | None = None
            if translation_coordinator is not None:
                source_utterances = transcript_store.get_source_utterances(result)
                if source_utterances:
                    log_info("finalizing external subtitle translations")
                    translated_texts = await translation_coordinator.backfill_missing(source_utterances)

                    result = {
                        **result,
                        "external_translation": {
                            "provider": "openai_compatible",
                            "model": settings.translation_api_model,
                            "target_language": settings.target_language,
                            "utterances": [
                                {
                                    "start": item.get("start"),
                                    "end": item.get("end"),
                                    "source_text": item.get("text"),
                                    "translated_text": translated_texts[index]
                                    if index < len(translated_texts)
                                    else "",
                                }
                                for index, item in enumerate(source_utterances)
                            ],
                        },
                    }

            save_json(settings.output_json_path, result)
            subtitle_entries = transcript_store.build_subtitle_entries(result, translated_texts=translated_texts)
            write_srt(subtitle_entries, settings.output_srt_path)
            emit_runtime_message(f"[SAVED] {settings.output_srt_path.as_posix()}")
            emit_runtime_message(f"[SAVED] {settings.output_json_path.as_posix()}")
            log_info(f"saved {len(subtitle_entries)} subtitle lines")
            self._status("completed")
            if self.callbacks.on_finished is not None:
                self.callbacks.on_finished(settings.output_srt_path, settings.output_json_path)
        finally:
            if self.callbacks.on_audio_level is not None:
                self.callbacks.on_audio_level(0.0)
            set_runtime_message_listener(None)
            capture.close()
            gladia_client.close()
            if translation_client is not None:
                translation_client.close()

    def _status(self, status: str) -> None:
        if self.callbacks.on_status is not None:
            self.callbacks.on_status(status)
