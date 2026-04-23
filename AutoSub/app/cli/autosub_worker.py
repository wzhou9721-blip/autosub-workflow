# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.common.config import APP_ROOT, cfg
from app.common.runtime_log import install_runtime_log_capture
from app.common.runtime_state import (
    BATCH_SESSION_STATE_FILE,
    BATCH_TRANSLATE_RESUME_FILE,
    overflow_resume_path,
    safe_unlink,
    translate_resume_path,
)
from app.common.thread import BatchTranscriptionThread


def ensure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def file_signature_hash(file_path: str | Path) -> str:
    normalized = os.path.normcase(os.path.abspath(str(file_path)))
    import hashlib

    return hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]


def expected_export_dir(input_path: str | Path) -> Path:
    src = Path(input_path)
    return src.parent / f"{src.stem}_导出"


def collect_output_files(root: str | Path) -> list[str]:
    base = Path(root)
    if not base.exists():
        return []
    result: list[str] = []
    for path in sorted(base.rglob("*")):
        if path.is_file():
            result.append(str(path))
    return result


def copy_tree_contents(src_dir: str | Path, dst_dir: str | Path) -> list[str]:
    src = Path(src_dir)
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(src)
        target = dst / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(str(target))
    return copied


def is_under_path(path: str | Path, parent: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def should_cleanup_native_export(input_file: Path, native_export_dir: Path, requested_output_dir: Path) -> bool:
    download_root = Path(
        os.environ.get(
            "BOT4000_DOWNLOAD_DIR",
            r"C:\Users\Zhou\Desktop\3000\data\downloads",
        )
    )
    if requested_output_dir.resolve() == native_export_dir.resolve():
        return False
    return is_under_path(input_file, download_root) and is_under_path(native_export_dir, download_root)


def resolve_task_value(task: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in task and task[key] is not None:
            return task[key]
    options = task.get("options") or {}
    for key in keys:
        if key in options and options[key] is not None:
            return options[key]
    return default


def resolve_translation_mode(task: dict[str, Any]) -> str | None:
    style = resolve_task_value(task, "translation_style")
    if style == "stable":
        return "稳健"
    if style == "balanced":
        return "折中"
    if style == "fast":
        return "极速"
    return None


def build_cfg_overrides(task: dict[str, Any]) -> dict[str, Any]:
    options = task.get("options") or {}
    workflow = options.get("workflow") or {}
    transcribe_mode = resolve_task_value(task, "transcribe_mode")
    if transcribe_mode == "cloud":
        resolved_transcribe_mode = "云端"
    elif transcribe_mode == "local":
        resolved_transcribe_mode = "本地"
    else:
        resolved_transcribe_mode = None

    asr_provider = resolve_task_value(task, "asr_provider")
    provider_override = asr_provider if asr_provider in {"Whisper", "Gladia"} else None

    overrides: dict[str, Any] = {}

    if resolved_transcribe_mode:
        overrides["transcribeMode"] = resolved_transcribe_mode
    if provider_override:
        overrides["asr_provider"] = provider_override

    for cfg_name, task_name in (
        ("sourceLanguage", "source_language"),
        ("targetLanguage", "target_language"),
        ("videoContext", "video_context"),
        ("glossaryPath", "glossary_path"),
        ("secondaryLanguage", "secondary_language"),
    ):
        value = resolve_task_value(task, task_name)
        if value not in (None, ""):
            overrides[cfg_name] = value

    if "optimize" in workflow:
        overrides["workflow_optimize"] = bool(workflow["optimize"])
    if "split" in workflow:
        overrides["workflow_split"] = bool(workflow["split"])
    if "translate" in workflow:
        overrides["workflow_translate"] = bool(workflow["translate"])
    if "overflow_fix" in workflow:
        overrides["workflow_overflow_fix"] = bool(workflow["overflow_fix"])

    for cfg_name, task_name in (
        ("gap_fill_enabled", "gap_fill_enabled"),
        ("multilingualMode", "multilingual_mode"),
        ("wordLevelTimestamps", "word_timestamps"),
        ("gladia_local_denoise", "local_denoise"),
        ("llm_reflect", "llm_reflect"),
        ("remove_punctuation", "remove_punctuation"),
    ):
        value = resolve_task_value(task, task_name)
        if value is not None:
            overrides[cfg_name] = bool(value)

    translation_mode = resolve_translation_mode(task)
    if translation_mode:
        overrides["llm_translation_mode"] = translation_mode

    for cfg_name, task_name in (
        ("multilingual_gap_threshold_sec", "multilingual_gap_threshold_sec"),
        ("cloud_multilingual_gap_threshold_sec", "cloud_multilingual_gap_threshold_sec"),
        ("llm_batch_size", "llm_batch_size"),
        ("llm_max_workers", "llm_max_workers"),
        ("fix_reflect_rounds", "fix_reflect_rounds"),
        ("font_size", "font_size"),
        ("max_line_count", "max_line_count"),
    ):
        value = resolve_task_value(task, task_name)
        if value is not None:
            overrides[cfg_name] = value

    return overrides


def build_batch_config(task: dict[str, Any]) -> dict[str, Any]:
    transcribe_mode = resolve_task_value(task, "transcribe_mode")
    if transcribe_mode == "cloud":
        resolved_transcribe_mode = "云端"
    elif transcribe_mode == "local":
        resolved_transcribe_mode = "本地"
    else:
        resolved_transcribe_mode = cfg.transcribeMode.value

    options = task.get("options") or {}
    workflow = options.get("workflow") or {}

    multilingual_mode = bool(resolve_task_value(task, "multilingual_mode", default=False))
    secondary_language = resolve_task_value(task, "secondary_language")
    return {
        "transcribeMode": resolved_transcribe_mode,
        "asrModel": resolve_task_value(task, "asr_model", default=cfg.asrModel.value),
        "sourceLanguage": resolve_task_value(task, "source_language", default=cfg.sourceLanguage.value),
        "targetLanguage": resolve_task_value(task, "target_language", default=cfg.targetLanguage.value),
        "videoContext": resolve_task_value(task, "video_context", default=cfg.videoContext.value),
        "wordLevelTimestamps": bool(resolve_task_value(task, "word_timestamps", default=cfg.wordLevelTimestamps.value)),
        "workflowOptimize": bool(workflow.get("optimize", cfg.workflow_optimize.value)),
        "workflowSplit": bool(workflow.get("split", cfg.workflow_split.value)),
        "workflowTranslate": bool(workflow.get("translate", cfg.workflow_translate.value)),
        "workflowOverflowFix": bool(workflow.get("overflow_fix", cfg.workflow_overflow_fix.value)),
        "gapFillEnabled": bool(resolve_task_value(task, "gap_fill_enabled", default=cfg.gap_fill_enabled.value)),
        "multilingualMode": multilingual_mode,
        "secondaryLanguage": secondary_language if multilingual_mode else None,
        "multilingualGapThresholdSec": resolve_task_value(
            task,
            "multilingual_gap_threshold_sec",
            default=cfg.multilingual_gap_threshold_sec.value,
        ),
        "CloudMultilingualGapThresholdSec": resolve_task_value(
            task,
            "cloud_multilingual_gap_threshold_sec",
            default=cfg.cloud_multilingual_gap_threshold_sec.value,
        ),
    }


@contextmanager
def temporary_cfg_overrides(overrides: dict[str, Any]):
    snapshot: dict[str, Any] = {}
    changed_items: list[tuple[Any, Any]] = []
    for attr_name, value in overrides.items():
        config_item = getattr(cfg, attr_name, None)
        if config_item is None:
            continue
        snapshot[attr_name] = config_item.value
        changed_items.append((config_item, value))
    try:
        for config_item, value in changed_items:
            config_item.value = value
        yield
    finally:
        for attr_name, value in snapshot.items():
            getattr(cfg, attr_name).value = value


def cleanup_resume_artifacts(input_path: str | Path) -> None:
    paths = [
        BATCH_SESSION_STATE_FILE,
        BATCH_TRANSLATE_RESUME_FILE,
        translate_resume_path(file_signature_hash(input_path)),
        overflow_resume_path(file_signature_hash(input_path)),
    ]
    for path in paths:
        try:
            safe_unlink(path)
        except Exception:
            pass


class CliBatchObserver:
    def __init__(self) -> None:
        self.progress_messages: list[str] = []
        self.file_done_events: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.success_count = 0
        self.total_count = 0

    def on_progress(self, current: int, total: int, message: str) -> None:
        line = f"[{current}/{total}] {message}"
        self.progress_messages.append(line)
        print(line)

    def on_file_done(self, current: int, total: int, src_path: str, export_path: str) -> None:
        event = {
            "current": current,
            "total": total,
            "src_path": src_path,
            "export_path": export_path,
        }
        self.file_done_events.append(event)
        print(f"[CLI] File done: {src_path} -> {export_path}")

    def on_all_done(self, success: int, total: int) -> None:
        self.success_count = success
        self.total_count = total
        print(f"[CLI] Batch finished: {success}/{total}")

    def on_error(self, message: str) -> None:
        self.errors.append(message)
        print(f"[CLI][ERROR] {message}")


def run_task(task: dict[str, Any], task_path: str | Path, output_dir: str | Path | None = None, input_override: str | None = None) -> dict[str, Any]:
    input_path = input_override or task.get("input_path") or ""
    if not input_path:
        return {
            "ok": False,
            "status": "blocked",
            "error_code": "missing_input_path",
            "error": "Task JSON does not contain input_path, and no --input override was provided.",
            "task_path": str(task_path),
        }

    input_file = Path(input_path)
    if not input_file.exists():
        return {
            "ok": False,
            "status": "blocked",
            "error_code": "input_not_found",
            "error": f"Input file does not exist: {input_file}",
            "task_path": str(task_path),
            "input_path": str(input_file),
        }

    requested_output_dir = Path(output_dir or task.get("output_dir") or (Path(task_path).parent / "output"))
    requested_output_dir.mkdir(parents=True, exist_ok=True)
    native_export_dir = expected_export_dir(input_file)

    overrides = build_cfg_overrides(task)
    batch_config = build_batch_config(task)
    file_entries = [{
        "path": str(input_file),
        "sourceLanguage": batch_config["sourceLanguage"],
        "targetLanguage": batch_config["targetLanguage"],
        "multilingualMode": batch_config["multilingualMode"],
        "secondaryLanguage": batch_config["secondaryLanguage"],
    }]

    observer = CliBatchObserver()
    cleanup_resume_artifacts(input_file)

    worker = BatchTranscriptionThread(file_entries, batch_config)
    worker.file_progress.connect(observer.on_progress)
    worker.file_done.connect(observer.on_file_done)
    worker.all_done.connect(observer.on_all_done)
    worker.error.connect(observer.on_error)

    with temporary_cfg_overrides(overrides):
        worker.run()

    cleanup_resume_artifacts(input_file)

    native_output_files = collect_output_files(native_export_dir)
    mirrored_output_files: list[str] = []
    if native_export_dir.exists():
        if requested_output_dir.resolve() != native_export_dir.resolve():
            mirrored_output_files = copy_tree_contents(native_export_dir, requested_output_dir)
            if mirrored_output_files and should_cleanup_native_export(input_file, native_export_dir, requested_output_dir):
                shutil.rmtree(native_export_dir, ignore_errors=True)
        else:
            mirrored_output_files = native_output_files

    summary = {
        "ok": observer.success_count == len(file_entries) and bool(native_output_files or mirrored_output_files),
        "status": "completed" if observer.success_count == len(file_entries) else "failed",
        "task_path": str(task_path),
        "request_text": task.get("request_text", ""),
        "input_path": str(input_file),
        "native_export_dir": str(native_export_dir),
        "output_dir": str(requested_output_dir),
        "native_output_files": native_output_files,
        "output_files": mirrored_output_files or native_output_files,
        "success_count": observer.success_count,
        "total_count": len(file_entries),
        "errors": observer.errors,
        "batch_summary_path": getattr(worker, "batch_summary_path", ""),
    }

    summary_path = requested_output_dir / "summary.json"
    dump_json(summary_path, summary)
    summary["summary_path"] = str(summary_path)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoSub headless CLI worker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a subtitle task JSON")
    run_parser.add_argument("--config", required=True, help="Path to task.json")
    run_parser.add_argument("--output-dir", default="", help="Optional output directory override")
    run_parser.add_argument("--input", default="", help="Optional input file override")

    return parser.parse_args()


def main() -> int:
    ensure_utf8_stdio()
    install_runtime_log_capture()
    args = parse_args()

    try:
        if args.command != "run":
            return emit({
                "ok": False,
                "error_code": "unsupported_command",
                "error": f"Unsupported command: {args.command}",
            })

        task_path = Path(args.config).resolve()
        task = load_json(task_path)
        result = run_task(
            task,
            task_path=task_path,
            output_dir=args.output_dir or None,
            input_override=args.input or None,
        )
        return emit(result)
    except Exception as exc:
        payload = {
            "ok": False,
            "status": "failed",
            "error_code": exc.__class__.__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "app_root": str(APP_ROOT),
        }
        return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
