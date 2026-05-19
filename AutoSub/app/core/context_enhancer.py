# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List

from app.common.config import cfg
from app.core.llm import llm_manager


_CACHE: Dict[str, str] = {}
_SEMANTIC_CACHE: Dict[str, str] = {}


def _select_context_llm_config() -> tuple[str, str, str]:
    config_prefix = (
        "context" if cfg.context_api_key.value
        else "optimize" if cfg.optimize_api_key.value
        else "llm"
    )
    effective_key = (
        cfg.context_api_key.value
        or cfg.optimize_api_key.value
        or cfg.llm_api_key.value
    )
    model = cfg.context_model.value or cfg.optimize_model.value or cfg.llm_model.value
    return config_prefix, effective_key, model


def _clean_list(values: Any, limit: int = 16) -> List[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _parse_json_object(text: str) -> Dict[str, Any]:
    content = (text or "").strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


def _format_enhanced_context(raw_context: str, data: Dict[str, Any]) -> str:
    lines = [f"Original context: {raw_context.strip()}"]

    scalar_fields = [
        ("domain", "Domain"),
        ("sport", "Sport"),
        ("content_type", "Content type"),
        ("event", "Event"),
        ("language_style", "Language/style"),
    ]
    for key, label in scalar_fields:
        value = str(data.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")

    list_fields = [
        ("people", "People/person names to preserve"),
        ("teams", "Teams/organizations"),
        ("competitions", "Competitions/leagues/trophies"),
        ("venues", "Venues/locations"),
        ("terms", "Sports terms and tactical phrases"),
        ("asr_hints", "Likely ASR confusion hints"),
        ("translation_style", "Translation style hints"),
    ]
    for key, label in list_fields:
        values = _clean_list(data.get(key), limit=18)
        if values:
            lines.append(f"{label}: {', '.join(values)}")

    return "\n".join(lines)


def enhance_context(raw_context: str, task_scope: str | None = None) -> str:
    raw = (raw_context or "").strip()
    if not raw:
        return ""

    config_prefix, effective_key, model = _select_context_llm_config()
    cache_key = f"{config_prefix}|{model}|{raw}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    if not effective_key:
        _CACHE[cache_key] = raw
        return raw

    prompt = f"""You expand a short user-provided sports video context into a compact production context card for ASR cleanup, subtitle splitting, and translation.

Return JSON object only, no markdown:
{{
  "domain": "sports",
  "sport": "",
  "content_type": "",
  "event": "",
  "people": [],
  "teams": [],
  "competitions": [],
  "venues": [],
  "terms": [],
  "asr_hints": [],
  "translation_style": []
}}

Rules:
1. Infer only likely professional sports context from the user's text.
2. Include concrete original-language terms when useful.
3. Preserve person names as candidates only; downstream tools must not silently change them.
4. Add common competition, venue, rule, ranking, score, tactical, position, and idiom terms for the inferred sport.
5. Keep it compact: max 18 items per list.
6. If unsure, leave fields empty instead of inventing.

User context:
{raw}
"""
    try:
        raw_response = llm_manager.call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.1,
            config_prefix=config_prefix,
            max_tokens=1600,
            expect_json=True,
            task_scope=task_scope,
        )
        parsed = _parse_json_object(raw_response)
        enhanced = _format_enhanced_context(raw, parsed) if parsed else raw
        _CACHE[cache_key] = enhanced
        if enhanced != raw:
            print("[ContextEnhancer] Generated enhanced context card")
        return enhanced
    except Exception as e:
        logging.warning(f"[ContextEnhancer] failed to enhance context: {e}")
        _CACHE[cache_key] = raw
        return raw


def build_semantic_map(
    segments: List[Dict[str, Any]],
    context: str = "",
    task_scope: str | None = None,
    max_chars: int = 12000,
) -> str:
    """Build a compact discourse map for splitting and translation prompts."""
    if not segments:
        return ""

    config_prefix, effective_key, model = _select_context_llm_config()
    if not effective_key:
        return ""

    lines: List[str] = []
    char_count = 0
    for i, seg in enumerate(segments):
        text = str(seg.get("optimized_text") or seg.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        seg_id = seg.get("index", i + 1)
        line = f"[{seg_id}] {text}"
        if char_count + len(line) > max_chars:
            break
        lines.append(line)
        char_count += len(line)

    if not lines:
        return ""

    source_text = "\n".join(lines)
    source_hash = hashlib.sha1(source_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    cache_key = f"{config_prefix}|{model}|{source_hash}|{context.strip()}"
    if cache_key in _SEMANTIC_CACHE:
        return _SEMANTIC_CACHE[cache_key]

    prompt = f"""Create a compact semantic map for a sports subtitle workflow.

Use it to help later steps split and translate subtitles with better discourse understanding.
Output concise plain text only, no markdown table, no JSON.

Include only what is evident or strongly implied:
- Content type: interview, commentary, press conference, analysis, documentary, etc.
- Speaker/turn structure: question, answer, follow-up, explanation, transition.
- Topic flow: 3-8 short bullets with subtitle id ranges when possible.
- References: what "it", "that", "this", "they", "we" likely refer to.
- Translation hints: tone, whether fragments should stay tied to nearby lines.
- Splitting hints: boundaries that should be kept together, and boundaries that are likely turn changes.

Do not invent facts. Preserve person names exactly as candidates; do not correct them.

Video context:
{context or "None"}

Subtitles:
{source_text}
"""
    try:
        result = llm_manager.call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.1,
            config_prefix=config_prefix,
            max_tokens=1600,
            task_scope=task_scope,
        )
        semantic_map = (result or "").strip()
        if len(semantic_map) > 2500:
            semantic_map = semantic_map[:2500].rstrip()
        _SEMANTIC_CACHE[cache_key] = semantic_map
        if semantic_map:
            print("[ContextEnhancer] Generated semantic structure map")
        return semantic_map
    except Exception as e:
        logging.warning(f"[ContextEnhancer] failed to build semantic map: {e}")
        _SEMANTIC_CACHE[cache_key] = ""
        return ""
