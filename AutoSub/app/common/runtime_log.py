# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import threading
from collections import deque
from typing import Deque

_MAX_RUNTIME_LOG_ENTRIES = 50000
_runtime_log_lock = threading.Lock()
_runtime_log_entries: Deque[tuple[int, str]] = deque()
_runtime_log_seq = 0
_capture_installed = False


def _append_runtime_log(text: str) -> None:
    """Append a raw stdout/stderr chunk into the in-memory runtime log buffer."""
    global _runtime_log_seq

    if not text:
        return

    with _runtime_log_lock:
        _runtime_log_seq += 1
        _runtime_log_entries.append((_runtime_log_seq, text))
        while len(_runtime_log_entries) > _MAX_RUNTIME_LOG_ENTRIES:
            _runtime_log_entries.popleft()


class _RuntimeLogTee:
    """Proxy stdout/stderr while also mirroring writes into the runtime buffer."""

    def __init__(self, stream):
        self._stream = stream

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._stream, "errors", "replace")

    @property
    def buffer(self):
        return getattr(self._stream, "buffer", None)

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        _append_runtime_log(text)
        if self._stream is None or not hasattr(self._stream, "write"):
            return len(text)
        written = self._stream.write(text)
        return len(text) if written is None else written

    def flush(self) -> None:
        if self._stream is not None and hasattr(self._stream, "flush"):
            self._stream.flush()

    def isatty(self) -> bool:
        return bool(self._stream is not None and hasattr(self._stream, "isatty") and self._stream.isatty())

    def writable(self) -> bool:
        return True

    def fileno(self):
        if self._stream is None or not hasattr(self._stream, "fileno"):
            raise OSError("Runtime log stream has no file descriptor")
        return self._stream.fileno()

    def __getattr__(self, name):
        if self._stream is None:
            raise AttributeError(name)
        return getattr(self._stream, name)


def install_runtime_log_capture() -> None:
    """Install stdout/stderr tees once so packaged GUI runs still retain console output."""
    global _capture_installed

    if _capture_installed:
        return

    if not isinstance(sys.stdout, _RuntimeLogTee):
        sys.stdout = _RuntimeLogTee(sys.stdout)
    if not isinstance(sys.stderr, _RuntimeLogTee):
        sys.stderr = _RuntimeLogTee(sys.stderr)

    _capture_installed = True


def current_runtime_log_seq() -> int:
    with _runtime_log_lock:
        return _runtime_log_seq


def get_runtime_log_snapshot(since_seq: int | None = None) -> str:
    """Return concatenated runtime log chunks after `since_seq`."""
    with _runtime_log_lock:
        entries = list(_runtime_log_entries)

    if since_seq is None:
        return "".join(text for _, text in entries)

    parts: list[str] = []
    if entries and since_seq < entries[0][0] - 1:
        parts.append("[RuntimeLog] Earlier log output was truncated.\n")

    for seq, text in entries:
        if seq > since_seq:
            parts.append(text)

    return "".join(parts)
