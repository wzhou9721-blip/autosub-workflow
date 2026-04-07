from __future__ import annotations

from pathlib import Path

from app.common.batch_utils import fmt_ass_time, fmt_srt_time, fmt_vtt_time, get_sub_content


def write_batch_summary(summary_path: str | Path, batch_reports: list[dict]) -> str:
    path = Path(summary_path)
    total = len(batch_reports)
    success = sum(1 for r in batch_reports if r.get("status") == "success")
    failed = sum(1 for r in batch_reports if r.get("status") == "failed")
    cancelled = sum(1 for r in batch_reports if r.get("status") == "cancelled")

    lines = [
        "批量任务摘要",
        f"总文件数: {total}",
        f"成功: {success}",
        f"失败: {failed}",
        f"取消: {cancelled}",
        "",
        "文件明细:",
    ]
    for i, item in enumerate(batch_reports, start=1):
        lines.append(f"{i}. {item.get('file', '')} [{item.get('status', '')}]")
        detail = item.get("detail", "")
        if detail:
            lines.append(f"   详情: {detail}")
        export_path = item.get("export", "")
        if export_path:
            lines.append(f"   导出: {export_path}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return str(path)


def write_translation_summary(
    summary_path: str | Path,
    *,
    file_name: str,
    total_batches: int,
    success_batches: int,
    failed_batches: int,
    skipped_batches: int,
    retryable_retry_count: int,
    non_retryable_skip_count: int,
    elapsed_seconds: int,
    failures: list[dict],
) -> str:
    path = Path(summary_path)
    lines = [
        f"文件: {file_name}",
        f"总批次数: {total_batches}",
        f"成功批次: {success_batches}",
        f"失败批次: {failed_batches}",
        f"已跳过批次(续传): {skipped_batches}",
        f"可重试错误重试次数: {retryable_retry_count}",
        f"不可重试错误跳过次数: {non_retryable_skip_count}",
        f"耗时(秒): {elapsed_seconds}",
        "",
        "失败明细:",
    ]
    for item in failures:
        lines.append(
            f"- 批次#{item['batch']} 范围{item['range'][0]}-{item['range'][1]} "
            f"类型={item['type']} 错误={item['error']}"
        )

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return str(path)


def write_runtime_log(
    runtime_log_path: str | Path,
    *,
    file_name: str,
    log_text: str,
) -> str:
    path = Path(runtime_log_path)
    lines = [
        f"文件: {file_name}",
        "说明: 这里记录的是本次任务运行期间捕获到的控制台输出。",
        "",
    ]
    if log_text.strip():
        lines.append(log_text.rstrip("\n"))
    else:
        lines.append("本次任务未捕获到控制台输出。")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
    return str(path)


def write_quality_report(
    path: str | Path,
    *,
    file_name: str,
    segment_count: int,
    translated: bool,
    has_diarization: bool,
    threshold: int,
    overflow_count: int,
    fallback_count: int,
    translation_summary: dict,
    transcribe_elapsed_sec: float | None,
    optimize_elapsed_sec: float | None,
    translation_elapsed_sec: float | None,
    overflow_fix_elapsed_sec: float | None,
    total_elapsed_sec: float,
    runtime_log_path: str | Path | None = None,
) -> str:
    output_path = Path(path)
    lines = [
        f"文件: {file_name}",
        f"总字幕数: {segment_count}",
        f"是否翻译: {'是' if translated else '否'}",
        f"说话人模式: {'是' if has_diarization else '否'}",
    ]

    timing_parts = []
    if transcribe_elapsed_sec is not None:
        timing_parts.append(f"转录用时: {transcribe_elapsed_sec} 秒")
    if optimize_elapsed_sec is not None:
        timing_parts.append(f"优化与断句用时: {optimize_elapsed_sec} 秒")
    if translation_elapsed_sec is not None:
        timing_parts.append(f"翻译用时: {translation_elapsed_sec} 秒")
    if overflow_fix_elapsed_sec is not None and overflow_fix_elapsed_sec > 0:
        timing_parts.append(f"溢出修复用时: {overflow_fix_elapsed_sec} 秒")
    timing_parts.append(f"总用时: {total_elapsed_sec} 秒")
    if timing_parts:
        lines.extend(timing_parts)
        lines.append("")

    lines.extend([
        f"溢出条数(>{threshold}字): {overflow_count}",
        f"疑似回退条数: {fallback_count}",
    ])
    if translated:
        lines.extend([
            f"翻译总批次: {translation_summary['total_batches']}",
            f"翻译失败批次: {translation_summary['failed_batches']}",
            f"续传跳过批次: {translation_summary['skipped_batches']}",
            f"重试次数: {translation_summary['retryable_retries']}",
        ])
        if translation_summary.get("summary_path"):
            lines.append(f"翻译摘要文件: {Path(translation_summary['summary_path']).name}")
    if runtime_log_path:
        lines.append(f"运行日志文件: {Path(runtime_log_path).name}")

    risk = "低"
    if overflow_count > 0 or fallback_count > 0 or translation_summary["failed_batches"] > 0:
        risk = "中"
    if overflow_count > 5 or fallback_count > 5 or translation_summary["failed_batches"] > 0:
        risk = "高"
    lines.append(f"风险等级: {risk}")

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return str(output_path)


def build_export_targets(
    export_root: str | Path,
    base_name: str,
    *,
    translated: bool,
    has_diarization: bool,
    speakers: list[str] | None = None,
) -> tuple[list[dict], str]:
    root = Path(export_root)
    jobs: list[dict] = []

    if has_diarization:
        for speaker in sorted(speakers or []):
            jobs.append({
                "kind": "srt",
                "path": str(root / f"{base_name}_{speaker}_仅原文.srt"),
                "mode": "orig_only",
                "speaker": speaker,
            })
            if translated:
                jobs.extend([
                    {
                        "kind": "srt",
                        "path": str(root / f"{base_name}_{speaker}_仅译文.srt"),
                        "mode": "trans_only",
                        "speaker": speaker,
                    },
                    {
                        "kind": "srt",
                        "path": str(root / f"{base_name}_{speaker}_双语_译文在上.srt"),
                        "mode": "trans_first",
                        "speaker": speaker,
                    },
                    {
                        "kind": "srt",
                        "path": str(root / f"{base_name}_{speaker}_双语_原文在上.srt"),
                        "mode": "orig_first",
                        "speaker": speaker,
                    },
                ])

        main_export = str(root / f"{base_name}_新闻稿.txt")
        jobs.append({"kind": "news_script", "path": main_export})
        return jobs, main_export

    if translated:
        jobs.extend([
            {"kind": "srt", "path": str(root / f"{base_name}_译文在上.srt"), "mode": "trans_first"},
            {"kind": "srt", "path": str(root / f"{base_name}_原文在上.srt"), "mode": "orig_first"},
            {"kind": "srt", "path": str(root / f"{base_name}_仅译文.srt"), "mode": "trans_only"},
            {"kind": "srt", "path": str(root / f"{base_name}_仅原文.srt"), "mode": "orig_only"},
            {"kind": "vtt", "path": str(root / f"{base_name}_译文在上.vtt"), "mode": "trans_first"},
            {"kind": "vtt", "path": str(root / f"{base_name}_仅译文.vtt"), "mode": "trans_only"},
            {"kind": "ass", "path": str(root / f"{base_name}_仅译文.ass"), "mode": "trans_only"},
        ])
        return jobs, str(root / f"{base_name}_译文在上.srt")

    main_export = str(root / f"{base_name}_仅原文.srt")
    jobs.append({"kind": "srt", "path": main_export, "mode": "orig_only"})
    return jobs, main_export


def write_srt(path: str | Path, segments: list[dict], mode: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i, seg in enumerate(segments):
            start = fmt_srt_time(seg.get("start", 0))
            raw_end = seg.get("end", 0)
            # Keep at least a 40ms gap so Premiere doesn't merge adjacent subtitles.
            if i + 1 < len(segments):
                next_start = segments[i + 1].get("start", 0)
                if raw_end >= next_start:
                    raw_end = max(seg.get("start", 0), next_start - 0.040)
            end = fmt_srt_time(raw_end)
            fh.write(f"{i + 1}\n{start} --> {end}\n{get_sub_content(seg, mode)}\n\n")


def write_vtt(path: str | Path, segments: list[dict], mode: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("WEBVTT\n\n")
        for i, seg in enumerate(segments):
            start = fmt_vtt_time(seg.get("start", 0))
            end = fmt_vtt_time(seg.get("end", 0))
            fh.write(f"{i + 1}\n{start} --> {end}\n{get_sub_content(seg, mode)}\n\n")


def write_ass(path: str | Path, segments: list[dict], mode: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "[Script Info]\nTitle: AutoSub\nScriptType: v4.00+\n"
            "WrapStyle: 0\nPlayResX: 1920\nPlayResY: 1080\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,10,10,30,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        for seg in segments:
            start = fmt_ass_time(seg.get("start", 0))
            end = fmt_ass_time(seg.get("end", 0))
            text = get_sub_content(seg, mode).replace("\n", "\\N")
            fh.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")


def write_news_script(path: str | Path, segments: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for seg in segments:
            ts = fmt_srt_time(seg.get("start", 0))[:8]
            speaker = seg.get("speaker", "Speaker")
            original = seg.get("optimized_text") or seg.get("text", "")
            fh.write(f"[{ts}] [{speaker}] {original}\n")
