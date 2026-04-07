from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.common.config import APP_ROOT


BATCH_SESSION_STATE_FILE = APP_ROOT / "temp_batch_session_state.json"
BATCH_TRANSLATE_RESUME_FILE = APP_ROOT / "temp_batch_translate_resume.json"
OPTIMIZE_RESUME_FILE = APP_ROOT / "temp_optimize_resume.json"
SPLIT_RESUME_FILE = APP_ROOT / "temp_split_resume.json"


def translate_resume_path(file_hash: str) -> Path:
    return APP_ROOT / f"temp_translate_resume_{file_hash}.json"


def overflow_resume_path(file_hash: str) -> Path:
    return APP_ROOT / f"temp_overflow_resume_{file_hash}.json"


def load_json_file(path: str | Path, default: Any = None) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        return default
    with open(file_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json_atomic(path: str | Path, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{file_path.stem}.",
        suffix=".tmp",
        dir=file_path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, file_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def safe_unlink(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)
