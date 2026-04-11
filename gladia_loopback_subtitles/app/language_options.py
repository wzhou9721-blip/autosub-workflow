from __future__ import annotations

LANGUAGE_OPTIONS: list[tuple[str, str]] = [
    ("自动检测", ""),
    ("英语", "en"),
    ("中文", "zh"),
    ("日语", "ja"),
    ("韩语", "ko"),
    ("西班牙语", "es"),
    ("法语", "fr"),
    ("德语", "de"),
    ("葡萄牙语", "pt"),
    ("意大利语", "it"),
    ("俄语", "ru"),
    ("阿拉伯语", "ar"),
    ("印地语", "hi"),
    ("印尼语", "id"),
    ("泰语", "th"),
    ("越南语", "vi"),
    ("土耳其语", "tr"),
]


def language_label(code: str) -> str:
    for label, value in LANGUAGE_OPTIONS:
        if value == code:
            return label
    return code or "自动检测"
