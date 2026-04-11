from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import requests

from .config import Settings
from .transcript_store import TranscriptStore
from .utils import clean_text, emit_runtime_message


class ExternalTranslationClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = requests.Session()
        self.http.headers.update(
            {
                "Authorization": f"Bearer {self.settings.translation_api_key}",
                "Content-Type": "application/json",
            }
        )

    def translate_utterances(self, utterances: list[dict[str, Any]]) -> list[str]:
        if not utterances:
            return []

        translations: list[str] = []
        batch_size = max(1, self.settings.translation_batch_size)
        for start in range(0, len(utterances), batch_size):
            batch = utterances[start : start + batch_size]
            translations.extend(self._translate_batch(batch))

        return translations

    def _translate_batch(self, utterances: list[dict[str, Any]]) -> list[str]:
        payload_items = [
            {"index": index, "text": clean_text(item.get("text"))}
            for index, item in enumerate(utterances)
            if clean_text(item.get("text"))
        ]
        if not payload_items:
            return ["" for _ in utterances]

        system_prompt = (
            "You are a professional subtitle translator. Translate subtitle lines into natural, concise "
            "Simplified Chinese for on-screen captions. Preserve meaning, names, numbers, URLs, and competition "
            "titles accurately. Do not add explanations. Return JSON only."
        )
        user_prompt = (
            "Translate the following subtitle lines into Simplified Chinese.\n"
            "Return a JSON object with a top-level key named translations.\n"
            "Each item must be an object with index and text.\n"
            "Keep the same number of items and the same indexes.\n\n"
            f"{json.dumps({'items': payload_items}, ensure_ascii=False)}"
        )

        body = {
            "model": self.settings.translation_api_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }

        response = self.http.post(
            self._chat_completions_url(),
            json=body,
            timeout=self.settings.translation_request_timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"Translation API request failed: {response.status_code} {response.text}"
            )

        data = response.json()
        content = self._extract_content(data)
        parsed = self._extract_json_object(content)
        translated_items = parsed.get("translations")
        if not isinstance(translated_items, list):
            raise RuntimeError(f"Unexpected translation response format: {content}")

        translated_by_index: dict[int, str] = {}
        for item in translated_items:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            translated_by_index[index] = clean_text(item.get("text"))

        results: list[str] = []
        for index, source in enumerate(utterances):
            translated = translated_by_index.get(index)
            if translated:
                results.append(translated)
            else:
                results.append(clean_text(source.get("text")))
        return results

    def close(self) -> None:
        self.http.close()

    def _chat_completions_url(self) -> str:
        base = self.settings.translation_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @staticmethod
    def _extract_content(response_json: dict[str, Any]) -> str:
        choices = response_json.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(f"Unexpected translation API response: {response_json}")

        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            joined = "".join(text_parts).strip()
            if joined:
                return joined

        raise RuntimeError(f"Unexpected translation content: {response_json}")

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                stripped = "\n".join(lines[1:-1]).strip()

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Translation API did not return JSON: {content}")

        return json.loads(stripped[start : end + 1])


class RealtimeTranslationCoordinator:
    def __init__(
        self,
        client: ExternalTranslationClient,
        store: TranscriptStore,
        target_language: str,
        on_translation: Callable[[str, str], None] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.target_language = target_language
        self.on_translation = on_translation
        self._tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(1)
        self._pending_batch: list[tuple[str, dict[str, Any]]] = []
        self._batch_lock = asyncio.Lock()
        self._batch_size = max(1, self.client.settings.translation_frequency)

    def submit(self, utterance_id: str, utterance: dict[str, Any]) -> None:
        if not clean_text(utterance.get("text")):
            return
        if utterance_id in self.store.translation_by_utterance_id:
            return

        task = asyncio.create_task(self._enqueue_and_maybe_translate(utterance_id, utterance))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for_pending(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def backfill_missing(self, utterances: list[dict[str, Any]]) -> list[str]:
        await self.wait_for_pending()
        await self._flush_pending_batch()

        missing_indices: list[int] = []
        missing_utterances: list[dict[str, Any]] = []
        final_texts: list[str] = []

        for index, utterance in enumerate(utterances):
            utterance_id = self._utterance_id(utterance)
            stored = self.store.translation_by_utterance_id.get(utterance_id, {})
            translated = clean_text(stored.get("text"))
            if translated:
                final_texts.append(translated)
                continue

            missing_indices.append(index)
            missing_utterances.append(utterance)
            final_texts.append("")

        if missing_utterances:
            translated_batch = await asyncio.to_thread(self.client.translate_utterances, missing_utterances)
            for batch_index, translated in enumerate(translated_batch):
                utterance = missing_utterances[batch_index]
                utterance_id = self._utterance_id(utterance)
                translated_text = clean_text(translated) or clean_text(utterance.get("text"))
                self.store.store_translation(
                    utterance_id,
                    {
                        "text": translated_text,
                        "language": self.target_language,
                        "start": utterance.get("start"),
                        "end": utterance.get("end"),
                    },
                )
                if self.on_translation is not None:
                    self.on_translation(utterance_id, translated_text)
                final_texts[missing_indices[batch_index]] = translated_text

        return final_texts

    async def _enqueue_and_maybe_translate(self, utterance_id: str, utterance: dict[str, Any]) -> None:
        should_flush = False
        async with self._batch_lock:
            self._pending_batch.append((utterance_id, utterance))
            should_flush = len(self._pending_batch) >= self._batch_size

        if should_flush:
            await self._flush_pending_batch()

    async def _flush_pending_batch(self) -> None:
        async with self._batch_lock:
            if not self._pending_batch:
                return
            batch = self._pending_batch[:]
            self._pending_batch.clear()

        async with self._semaphore:
            translated_list = await asyncio.to_thread(
                self.client.translate_utterances,
                [utterance for _, utterance in batch],
            )

        for index, (utterance_id, utterance) in enumerate(batch):
            translated_text = clean_text(translated_list[index] if index < len(translated_list) else "") or clean_text(
                utterance.get("text")
            )
            self.store.store_translation(
                utterance_id,
                {
                    "text": translated_text,
                    "language": self.target_language,
                    "start": utterance.get("start"),
                    "end": utterance.get("end"),
                },
            )
            emit_runtime_message(f"[FINAL][ZH] {translated_text}")
            if self.on_translation is not None:
                self.on_translation(utterance_id, translated_text)

    @staticmethod
    def _utterance_id(utterance: dict[str, Any]) -> str:
        return str(
            utterance.get("id")
            or utterance.get("utterance_id")
            or f"{utterance.get('start', '')}-{utterance.get('end', '')}"
        )
