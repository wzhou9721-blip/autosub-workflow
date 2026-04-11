from __future__ import annotations

import asyncio
import msvcrt
import threading

from .config import load_settings
from .session_runner import RuntimeOptions, SessionRunner
from .utils import log_info


async def _wait_for_enter(stop_signal: threading.Event) -> None:
    while not stop_signal.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in ("\r", "\n"):
                stop_signal.set()
                return
        await asyncio.sleep(0.1)


def main() -> None:
    settings = load_settings()
    runner = SessionRunner(settings)
    stop_signal = threading.Event()
    source_languages = settings.source_languages or [""]
    runtime_options = RuntimeOptions(
        primary_language=source_languages[0],
        dual_language_enabled=len(source_languages) > 1,
        secondary_language=source_languages[1] if len(source_languages) > 1 else "zh",
        denoise_enabled=settings.audio_enhancer,
        translation_frequency=settings.translation_frequency,
    )

    async def _run_cli() -> None:
        wait_task = asyncio.create_task(_wait_for_enter(stop_signal))
        try:
            await asyncio.to_thread(runner.run, stop_signal, runtime_options)
        finally:
            stop_signal.set()
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)

    try:
        log_info("capturing Windows system audio loopback; press Enter to stop")
        asyncio.run(_run_cli())
    except KeyboardInterrupt:
        log_info("stopped by user")


if __name__ == "__main__":
    main()
