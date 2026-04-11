from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
import websockets

from .config import Settings
from .transcript_store import TranscriptStore
from .utils import clean_text, emit_runtime_message


@dataclass(slots=True)
class LiveSessionInfo:
    session_id: str
    websocket_url: str


class GladiaLiveClient:
    def __init__(
        self,
        settings: Settings,
        transcript_store: TranscriptStore,
        on_final_transcript: Callable[[str, dict[str, Any]], None] | None = None,
        on_partial_transcript: Callable[[str], None] | None = None,
        on_final_source: Callable[[str, str], None] | None = None,
        on_translation: Callable[[str, str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.transcript_store = transcript_store
        self.on_final_transcript = on_final_transcript
        self.on_partial_transcript = on_partial_transcript
        self.on_final_source = on_final_source
        self.on_translation = on_translation
        self.http = requests.Session()
        self.http.headers.update(
            {
                "x-gladia-key": self.settings.gladia_api_key,
                "Content-Type": "application/json",
            }
        )

    def create_session(self) -> LiveSessionInfo:
        payload = {
            "encoding": self.settings.encoding,
            "bit_depth": self.settings.bit_depth,
            "sample_rate": self.settings.sample_rate,
            "channels": self.settings.channels,
            "model": self.settings.gladia_model,
            "endpointing": self.settings.endpointing,
            "maximum_duration_without_endpointing": self.settings.maximum_duration_without_endpointing,
            "pre_processing": {
                "audio_enhancer": self.settings.audio_enhancer,
                "speech_threshold": self.settings.speech_threshold,
            },
            "language_config": {
                "languages": self.settings.source_languages,
                "code_switching": self.settings.code_switching,
            },
            "realtime_processing": {
                "translation": not self.settings.use_external_translation,
                "translation_config": {
                    "target_languages": [self.settings.target_language],
                    "model": self.settings.gladia_translation_model,
                    "match_original_utterances": self.settings.translation_match_original_utterances,
                    "lipsync": self.settings.translation_lipsync,
                    "context_adaptation": self.settings.translation_context_adaptation,
                    "context": self.settings.translation_context,
                    "informal": self.settings.translation_informal,
                },
                "custom_vocabulary": False,
                "custom_spelling": False,
                "named_entity_recognition": False,
                "sentiment_analysis": False,
            },
            "messages_config": {
                "receive_partial_transcripts": True,
                "receive_final_transcripts": True,
                "receive_speech_events": False,
                "receive_pre_processing_events": False,
                "receive_realtime_processing_events": True,
                "receive_post_processing_events": True,
                "receive_acknowledgments": True,
                "receive_errors": True,
                "receive_lifecycle_events": False,
            },
            "callback": False,
        }

        response = self.http.post(f"{self.settings.gladia_base_url}/v2/live", json=payload, timeout=30)
        if not response.ok:
            raise RuntimeError(
                f"Failed to create Gladia live session: {response.status_code} {response.text}"
            )

        body = response.json()
        session_id = body.get("id")
        websocket_url = body.get("url")
        if not session_id or not websocket_url:
            raise RuntimeError(f"Unexpected session response: {body}")

        return LiveSessionInfo(session_id=session_id, websocket_url=websocket_url)

    async def stream_audio(
        self,
        session: LiveSessionInfo,
        capture: Any,
        stop_event: asyncio.Event,
        on_audio_level: Callable[[float], None] | None = None,
    ) -> None:
        async with websockets.connect(
            session.websocket_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            receiver_task = asyncio.create_task(self._receive_messages(websocket))

            try:
                while not stop_event.is_set():
                    if receiver_task.done():
                        receiver_task.result()

                    chunk = await asyncio.to_thread(capture.read_chunk)
                    if chunk:
                        if on_audio_level is not None:
                            on_audio_level(float(getattr(capture, "last_level", 0.0)))
                        await websocket.send(chunk)
            finally:
                await websocket.send(json.dumps({"type": "stop_recording"}))
                await receiver_task

    async def _receive_messages(self, websocket: Any) -> None:
        try:
            async for raw_message in websocket:
                if isinstance(raw_message, bytes):
                    continue

                message = json.loads(raw_message)
                self._handle_message(message)
        except websockets.ConnectionClosedOK:
            return

    def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        data = message.get("data") or {}

        if message_type == "transcript":
            utterance_id = str(
                data.get("utterance_id")
                or data.get("id")
                or f"{data.get('start', '')}-{data.get('end', '')}-{len(self.transcript_store.final_source_by_id)}"
            )
            utterance = data.get("utterance") or {}
            text = clean_text(utterance.get("text"))
            if not text:
                return

            if data.get("is_final"):
                self.transcript_store.store_final_source(utterance_id, utterance)
                emit_runtime_message(f"[FINAL][SRC] {text}")
                if self.on_final_source is not None:
                    self.on_final_source(utterance_id, text)
                if self.on_final_transcript is not None:
                    self.on_final_transcript(utterance_id, utterance)
            else:
                self.transcript_store.store_partial(text)
                emit_runtime_message(f"[PARTIAL] {text}")
                if self.on_partial_transcript is not None:
                    self.on_partial_transcript(text)
            return

        if message_type == "translation":
            utterance_id = str(data.get("utterance_id", ""))
            translated_utterance = data.get("translated_utterance") or {}
            translated_text = clean_text(translated_utterance.get("text"))
            if not translated_text:
                return

            self.transcript_store.store_translation(utterance_id, translated_utterance)
            target_language = translated_utterance.get("language") or data.get("target_language")
            if target_language == self.settings.target_language:
                emit_runtime_message(f"[FINAL][ZH] {translated_text}")
                if self.on_translation is not None:
                    self.on_translation(utterance_id, translated_text)
            return

        if message_type == "error":
            raise RuntimeError(f"Gladia websocket error: {message}")

    def wait_for_final_result(self, session_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.result_poll_timeout_seconds

        while time.monotonic() < deadline:
            result = self.fetch_result(session_id)
            status = result.get("status")

            if status == "done":
                return result
            if status == "error":
                raise RuntimeError(f"Gladia result finished with error: {json.dumps(result, ensure_ascii=False)}")

            time.sleep(self.settings.result_poll_interval_seconds)

        raise TimeoutError(
            f"Timed out while waiting for Gladia result after {self.settings.result_poll_timeout_seconds} seconds."
        )

    def fetch_result(self, session_id: str) -> dict[str, Any]:
        response = self.http.get(f"{self.settings.gladia_base_url}/v2/live/{session_id}", timeout=30)
        if not response.ok:
            raise RuntimeError(f"Failed to fetch Gladia result: {response.status_code} {response.text}")
        return response.json()

    def close(self) -> None:
        self.http.close()
