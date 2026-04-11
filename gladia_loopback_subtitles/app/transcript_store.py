from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .utils import clean_text, pick_result_root


@dataclass(slots=True)
class SubtitleEntry:
    start: float
    end: float
    text: str


class TranscriptStore:
    def __init__(self, target_language: str) -> None:
        self.target_language = target_language
        self.partial_text = ""
        self.final_source_by_id: dict[str, dict[str, Any]] = {}
        self.translation_by_utterance_id: dict[str, dict[str, Any]] = {}

    def store_partial(self, text: str) -> str:
        self.partial_text = clean_text(text)
        return self.partial_text

    def store_final_source(self, utterance_id: str, utterance: dict[str, Any]) -> str:
        utterance = {**utterance, "id": utterance_id}
        self.final_source_by_id[utterance_id] = utterance
        return clean_text(utterance.get("text"))

    def store_translation(self, utterance_id: str, translated_utterance: dict[str, Any]) -> str:
        self.translation_by_utterance_id[utterance_id] = translated_utterance
        return clean_text(translated_utterance.get("text"))

    def get_source_utterances(self, result_payload: dict[str, Any]) -> list[dict[str, Any]]:
        result_root = pick_result_root(result_payload)
        transcription = result_root.get("transcription") or {}
        utterances = transcription.get("utterances") or []
        return [item for item in utterances if isinstance(item, dict)]

    def build_subtitle_entries(
        self,
        result_payload: dict[str, Any],
        translated_texts: list[str] | None = None,
    ) -> list[SubtitleEntry]:
        result_root = pick_result_root(result_payload)
        transcription = result_root.get("transcription") or {}
        translation = result_root.get("translation") or {}
        source_utterances = self.get_source_utterances(result_payload)

        translation_utterances = self._pick_translation_utterances(translation)
        time_based_translation = {
            self._time_key(item): item for item in translation_utterances if isinstance(item, dict)
        }

        entries: list[SubtitleEntry] = []
        for index, source in enumerate(source_utterances):
            if not isinstance(source, dict):
                continue

            translated = None
            if translated_texts is not None and index < len(translated_texts):
                text = clean_text(translated_texts[index]) or clean_text(source.get("text"))
            else:
                if index < len(translation_utterances) and isinstance(translation_utterances[index], dict):
                    translated = translation_utterances[index]
                translated = time_based_translation.get(self._time_key(source), translated)
                text = clean_text((translated or {}).get("text")) or clean_text(source.get("text"))
            if not text:
                continue

            start = self._normalize_seconds(source.get("start"))
            end = self._normalize_seconds(source.get("end"))
            if end <= start:
                end = start + 0.5

            entries.append(SubtitleEntry(start=start, end=end, text=text))

        if entries:
            return entries

        return self._fallback_entries_from_runtime_store()

    def _fallback_entries_from_runtime_store(self) -> list[SubtitleEntry]:
        entries: list[SubtitleEntry] = []
        for utterance_id, source in sorted(self.final_source_by_id.items()):
            translated = self.translation_by_utterance_id.get(utterance_id, {})
            text = clean_text(translated.get("text")) or clean_text(source.get("text"))
            if not text:
                continue

            start = self._normalize_seconds(source.get("start"))
            end = self._normalize_seconds(source.get("end"))
            if end <= start:
                end = start + 0.5

            entries.append(SubtitleEntry(start=start, end=end, text=text))
        return entries

    def _pick_translation_utterances(self, translation_block: dict[str, Any]) -> list[dict[str, Any]]:
        results = translation_block.get("results") or []
        if not isinstance(results, list):
            return []

        preferred = None
        for item in results:
            if not isinstance(item, dict):
                continue
            languages = item.get("languages") or []
            if self.target_language in languages:
                preferred = item
                break
            subtitles = item.get("subtitles") or []
            if any(self.target_language in str(subtitle) for subtitle in subtitles):
                preferred = item
                break

        if preferred is None and results:
            first = results[0]
            preferred = first if isinstance(first, dict) else None

        if preferred is None:
            return []
        return preferred.get("utterances") or []

    @staticmethod
    def _time_key(utterance: dict[str, Any]) -> tuple[int, int]:
        start = TranscriptStore._normalize_seconds(utterance.get("start"))
        end = TranscriptStore._normalize_seconds(utterance.get("end"))
        return (int(round(start * 1000)), int(round(end * 1000)))

    @staticmethod
    def _normalize_seconds(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0

        if numeric > 10000:
            return numeric / 1000.0
        return numeric
