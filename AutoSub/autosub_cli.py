# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _has_required_modules() -> bool:
    return (
        importlib.util.find_spec("qfluentwidgets") is not None
        and importlib.util.find_spec("PyQt6") is not None
    )


def _find_fallback_python() -> str | None:
    candidates = [
        ROOT.parent / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "Scripts" / "python.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "Python" / "Python312" / "python.exe",
        Path(os.environ.get("LocalAppData", "")) / "Programs" / "Python" / "Python311" / "python.exe",
    ]
    current = Path(sys.executable).resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not resolved.exists() or resolved == current:
            continue
        return str(resolved)
    return None


if not _has_required_modules():
    fallback_python = _find_fallback_python()
    if fallback_python:
        completed = subprocess.run([fallback_python, str(ROOT / "autosub_cli.py"), *sys.argv[1:]])
        raise SystemExit(completed.returncode)

from app.cli.autosub_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
