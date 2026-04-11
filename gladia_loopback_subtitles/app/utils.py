from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_LOG_FILE_PATH: Path | None = None


def configure_runtime_log(path: Path) -> None:
    global _LOG_FILE_PATH
    _LOG_FILE_PATH = path


def append_runtime_log(message: str) -> None:
    if _LOG_FILE_PATH is None:
        return
    _LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_FILE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{message}\n")


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip()
    return normalize_cjk_spacing(text)


def normalize_cjk_spacing(text: str) -> str:
    # Remove artificial spaces between CJK characters and around Chinese punctuation.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s*([，。！？；：、])\s*", r"\1", text)
    text = re.sub(r"([（《“])\s+", r"\1", text)
    text = re.sub(r"\s+([）》”])", r"\1", text)
    return text.strip()


def log_info(message: str) -> None:
    line = f"[INFO] {message}"
    print(line)
    append_runtime_log(line)


def pick_result_root(result_payload: dict[str, Any]) -> dict[str, Any]:
    result_root = result_payload.get("result")
    if isinstance(result_root, dict):
        return result_root
    return result_payload
