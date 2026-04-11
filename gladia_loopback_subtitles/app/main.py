from __future__ import annotations

import asyncio
import msvcrt

from .config import load_settings
from .gladia_client import GladiaLiveClient
from .srt_writer import write_srt
from .system_audio_capture import AudioChunkConfig, SystemAudioCapture
from .translation_client import ExternalTranslationClient, RealtimeTranslationCoordinator
from .transcript_store import TranscriptStore
from .utils import append_runtime_log, configure_runtime_log, ensure_directory, log_info, save_json


async def _wait_for_enter(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in ("\r", "\n"):
                return
        await asyncio.sleep(0.1)


async def run() -> None:
    settings = load_settings()
    ensure_directory(settings.output_dir)
    settings.runtime_log_path.write_text("", encoding="utf-8")
    configure_runtime_log(settings.runtime_log_path)

    transcript_store = TranscriptStore(target_language=settings.target_language)
    translation_client = ExternalTranslationClient(settings) if settings.use_external_translation else None
    translation_coordinator = (
        RealtimeTranslationCoordinator(
            client=translation_client,
            store=transcript_store,
            target_language=settings.target_language,
        )
        if translation_client is not None
        else None
    )
    gladia_client = GladiaLiveClient(
        settings=settings,
        transcript_store=transcript_store,
        on_final_transcript=translation_coordinator.submit if translation_coordinator is not None else None,
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
        capture.open()
        log_info(f"capture backend: {capture.backend} ({capture.device_name})")
        session = gladia_client.create_session()
        log_info(f"session started: {session.session_id}")
        log_info("capturing Windows system audio loopback; press Enter to stop")

        stop_event = asyncio.Event()
        stream_task = asyncio.create_task(gladia_client.stream_audio(session, capture, stop_event))
        stop_task = asyncio.create_task(_wait_for_enter(stop_event))

        done, pending = await asyncio.wait(
            {stream_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            stop_event.set()

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
                                "translated_text": translated_texts[index] if index < len(translated_texts) else "",
                            }
                            for index, item in enumerate(source_utterances)
                        ],
                    },
                }

        save_json(settings.output_json_path, result)
        subtitle_entries = transcript_store.build_subtitle_entries(result, translated_texts=translated_texts)
        write_srt(subtitle_entries, settings.output_srt_path)

        line = f"[SAVED] {settings.output_srt_path.as_posix()}"
        print(line)
        append_runtime_log(line)
        line = f"[SAVED] {settings.output_json_path.as_posix()}"
        print(line)
        append_runtime_log(line)
    finally:
        capture.close()
        gladia_client.close()
        if translation_client is not None:
            translation_client.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log_info("stopped by user")


if __name__ == "__main__":
    main()
