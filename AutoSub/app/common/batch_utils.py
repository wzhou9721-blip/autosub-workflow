from __future__ import annotations

import os
import re
from pathlib import Path

from app.common.config import cfg


DEFAULT_RETRYABLE_TRANSLATION_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "connect",
    "network",
    "rate limit",
    "429",
    "temporarily unavailable",
    "503",
    "502",
    "504",
)


def fmt_srt_time(sec: float) -> str:
    if sec is None:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    sc = int(sec % 60)
    ms = int((sec * 1000) % 1000)
    return f"{h:02d}:{m:02d}:{sc:02d},{ms:03d}"


def fmt_vtt_time(sec: float) -> str:
    if sec is None:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    sc = int(sec % 60)
    ms = int((sec * 1000) % 1000)
    return f"{h:02d}:{m:02d}:{sc:02d}.{ms:03d}"


def fmt_ass_time(sec: float) -> str:
    if sec is None:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    sc = int(sec % 60)
    cs = int((sec * 100) % 100)
    return f"{h}:{m:02d}:{sc:02d}.{cs:02d}"


def normalize_text_for_compare(text: str) -> str:
    return re.sub(r"[^\w]", "", (text or "")).lower()


def translated_view_text(seg: dict) -> str:
    return (seg.get("translated_text") or seg.get("text") or "").strip()


def clean_translated_segments(segments: list, min_duration_ms: int = 100) -> list:
    if not segments:
        return segments

    cleaned = []
    n = len(segments)

    for i, seg in enumerate(segments):
        start = float(seg.get("start", 0) or 0)
        end = float(seg.get("end", 0) or 0)
        if (end - start) * 1000 < min_duration_ms:
            continue

        cur_norm = normalize_text_for_compare(translated_view_text(seg))
        if i + 1 < n:
            next_norm = normalize_text_for_compare(translated_view_text(segments[i + 1]))
            if cur_norm and next_norm.startswith(cur_norm) and cur_norm != next_norm:
                continue

        cleaned.append(seg)

    deduped = []
    for seg in cleaned:
        cur_norm = normalize_text_for_compare(translated_view_text(seg))
        if deduped:
            prev = deduped[-1]
            prev_norm = normalize_text_for_compare(translated_view_text(prev))
            if cur_norm and cur_norm == prev_norm:
                prev["end"] = max(float(prev.get("end", 0) or 0), float(seg.get("end", 0) or 0))
                continue
        deduped.append(seg)

    hallu_keywords = [
        "璇蜂笉鍚濈偣璧?",
        "璁㈤槄",
        "杞彂",
        "鎰熻阿瑙傜湅",
        "璋㈣阿澶у",
        "Subscribe",
        "Thanks for watching",
        "Subtitles by",
    ]
    final = []
    for seg in deduped:
        trans = (seg.get("translated_text") or "").strip()
        if not trans:
            final.append(seg)
            continue

        duration = float(seg.get("end", 0) or 0) - float(seg.get("start", 0) or 0)
        if any(k in trans for k in hallu_keywords):
            continue
        if duration > 0 and duration < 2.0:
            max_reasonable_chars = duration * 45
            if len(trans) > max_reasonable_chars:
                continue
        final.append(seg)

    for i, seg in enumerate(final):
        seg["index"] = i + 1
    return final


def get_sub_content(seg: dict, mode: str) -> str:
    orig = seg.get("optimized_text") or seg.get("text", "")
    cn = seg.get("translated_text") or ""
    if mode == "trans_first":
        return f"{cn}\n{orig}" if cn and orig else (cn or orig)
    if mode == "orig_first":
        return f"{orig}\n{cn}" if orig and cn else (orig or cn)
    if mode == "trans_only":
        return cn
    if mode == "orig_only":
        return orig
    return orig


def load_glossary_text() -> str:
    path = cfg.glossaryPath.value
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except Exception:
            pass
    return ""


def normalize_path(p: str) -> str:
    return str(Path(p).resolve())


def build_file_signature(p: str) -> dict:
    normalized = normalize_path(p)
    try:
        stat = Path(normalized).stat()
        return {
            "path": normalized,
            "size": int(stat.st_size),
            "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
        }
    except Exception:
        return {
            "path": normalized,
            "size": None,
            "mtime_ns": None,
        }


def file_signature_equal(a: dict, b: dict) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return (
        (a.get("path") == b.get("path"))
        and (a.get("size") == b.get("size"))
        and (a.get("mtime_ns") == b.get("mtime_ns"))
    )


def is_retryable_translation_error(err: Exception, keywords=None) -> bool:
    msg = str(err).lower()
    candidate_keywords = keywords or DEFAULT_RETRYABLE_TRANSLATION_ERROR_KEYWORDS
    return any(k in msg for k in candidate_keywords)
