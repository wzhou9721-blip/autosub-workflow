from __future__ import annotations

from pathlib import Path

from .transcript_store import SubtitleEntry


def write_srt(entries: list[SubtitleEntry], path: Path) -> None:
    lines: list[str] = []
    for index, entry in enumerate(entries, start=1):
        text = entry.text.strip()
        if not text:
            continue

        lines.append(str(index))
        lines.append(f"{format_srt_timestamp(entry.start)} --> {format_srt_timestamp(entry.end)}")
        lines.append(text)
        lines.append("")

    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
