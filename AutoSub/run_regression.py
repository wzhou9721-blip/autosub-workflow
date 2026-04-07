import os
import re
import sys
import traceback
import tempfile

# Ensure project root in path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.common.text_utils import TextSplitter, clean_punctuation_text
from app.common.config import cfg
from app.common.export_utils import (
    build_export_targets,
    write_quality_report,
    write_runtime_log,
    write_translation_summary,
)
from app.core.splitter import SubtitleSplitter
from app.core.llm import LLMTranslator, llm_manager
from app.core.project import project_manager
from app.common.thread import BatchTranscriptionThread
from app.common.runtime_state import load_json_file, save_json_atomic, safe_unlink


def test_file_signature_compare() -> None:
    sig = {"path": "a", "size": 1, "mtime_ns": 2}
    sig2 = {"path": "a", "size": 1, "mtime_ns": 2}
    sig3 = {"path": "a", "size": 2, "mtime_ns": 2}
    assert BatchTranscriptionThread._file_signature_equal(sig, sig2) is True
    assert BatchTranscriptionThread._file_signature_equal(sig, sig3) is False


def test_retryable_error_classifier() -> None:
    assert BatchTranscriptionThread._is_retryable_translation_error(Exception("timeout")) is True
    assert BatchTranscriptionThread._is_retryable_translation_error(Exception("429 rate limit")) is True
    assert BatchTranscriptionThread._is_retryable_translation_error(Exception("unauthorized api key")) is False


def test_get_sub_content_modes() -> None:
    seg = {
        "text": "hello",
        "optimized_text": "hello optimized",
        "translated_text": "你好",
    }
    assert BatchTranscriptionThread._get_sub_content(seg, "trans_first") == "你好\nhello optimized"
    assert BatchTranscriptionThread._get_sub_content(seg, "orig_first") == "hello optimized\n你好"
    assert BatchTranscriptionThread._get_sub_content(seg, "trans_only") == "你好"
    assert BatchTranscriptionThread._get_sub_content(seg, "orig_only") == "hello optimized"


def test_build_translation_contexts() -> None:
    segments = [
        {"text": "a1"},
        {"text": "a2", "translated_text": "已译a2"},
        {"text": "a3"},
        {"text": "a4"},
        {"text": "a5"},
        {"text": "a6"},
        {"text": "a7"},
    ]
    prev_ctx, next_ctx = BatchTranscriptionThread._build_translation_contexts(segments, 3, 2)
    assert prev_ctx == "a1 | 已译a2 | a3"
    assert next_ctx == "a6 | a7"


def test_build_overflow_contexts() -> None:
    segments = [
        {"text": "s1"},
        {"text": "s2", "translated_text": "上一条"},
        {"text": "current"},
        {"optimized_text": "next optimized"},
        {"text": "s5", "translated_text": "下二条"},
    ]
    prev_ctx, next_ctx = LLMTranslator.build_overflow_contexts(segments, 2)
    assert prev_ctx == "s1 | 上一条"
    assert next_ctx == "next optimized | 下二条"


def test_detect_weak_overflow_boundaries() -> None:
    assert LLMTranslator._has_weak_overflow_boundaries(
        [
            {"text": "I'm not going to", "cn": "我不会去"},
            {"text": "do that", "cn": "做那件事"},
        ]
    ) is True
    assert LLMTranslator._has_weak_overflow_boundaries(
        [
            {"text": "Look at this.", "cn": "你看这个。"},
            {"text": "I'm not doing that.", "cn": "我不做那个。"},
        ]
    ) is False
    assert LLMTranslator._has_weak_overflow_boundaries(
        [
            {"text": "Look at this in", "cn": "看看这个 在"},
            {"text": "the mirror.", "cn": "镜子里。"},
        ]
    ) is False
    assert LLMTranslator._has_weak_overflow_boundaries(
        [
            {"text": "Son in and Solanke's there and so is Kulezevsk", "cn": "孙兴慜而球交入 索兰克在那里 库卢塞夫斯基也"},
            {"text": "iy.", "cn": "在"},
        ]
    ) is True


def test_detect_cross_subtitle_translation_fragments() -> None:
    assert LLMTranslator._needs_continuation_reflection(
        [
            {"id": 136, "text": "now the outright top assist maker"},
            {"id": 137, "text": "in Tottenham Hotspur's Premier League history."},
        ],
        [
            {"id": 136, "cn": "现在成为了助攻王"},
            {"id": 137, "cn": "托特纳姆热刺英超历史上的"},
        ],
    ) is True
    assert LLMTranslator._needs_continuation_reflection(
        [
            {"id": 136, "text": "now the outright top assist maker"},
            {"id": 137, "text": "in Tottenham Hotspur's Premier League history."},
        ],
        [
            {"id": 136, "cn": "如今已是热刺队史英超"},
            {"id": 137, "cn": "头号助攻王。"},
        ],
    ) is False


def test_prepare_export_segments_applies_remove_punctuation() -> None:
    prepared = BatchTranscriptionThread._prepare_export_segments(
        [
            {
                "index": 9,
                "start": 0.0,
                "end": 1.0,
                "text": "hello",
                "translated_text": "Hello, world.",
            }
        ],
        translated=True,
        remove_punctuation=True,
        hallucination_filter=False,
    )
    assert "," not in prepared[0]["translated_text"]
    assert "." not in prepared[0]["translated_text"]
    assert "Hello" in prepared[0]["translated_text"]
    assert "world" in prepared[0]["translated_text"]
    assert prepared[0]["index"] == 1


def test_cleanup_temp_audio_files() -> None:
    original_paths = list(project_manager.temp_audio_paths)
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "demo.wav")
        with open(wav_path, "w", encoding="utf-8") as fh:
            fh.write("temp")
        try:
            project_manager.temp_audio_paths = [wav_path]
            BatchTranscriptionThread._cleanup_temp_audio_files()
            assert not os.path.exists(wav_path)
            assert project_manager.temp_audio_paths == []
        finally:
            project_manager.temp_audio_paths = original_paths


def test_repair_overflow_reflects_invalid_first_attempt() -> None:
    translator = LLMTranslator()
    original_call_llm = llm_manager.call_llm
    original_rounds = cfg.fix_reflect_rounds.value
    prompts = []
    responses = [
        '[{"text":"Yesterday","cn":"昨天他已经到了这里"},{"text":"he arrived.","cn":"。"}]',
        '[{"text":"Yesterday","cn":"昨天"},{"text":"he arrived.","cn":"他到了。"}]',
    ]

    def fake_call_llm(messages, model, temperature=0.3, config_prefix="llm", max_tokens=None, expect_json=False, task_scope=None):
        prompts.append(messages[0]["content"])
        return responses[len(prompts) - 1]

    try:
        cfg.fix_reflect_rounds.value = 1
        llm_manager.call_llm = fake_call_llm
        repaired = translator.repair_overflow(
            "Yesterday he arrived.",
            "昨天他到了。",
            max_chars=6,
            prev_context="Before this subtitle.",
            next_context="After this subtitle.",
        )
    finally:
        llm_manager.call_llm = original_call_llm
        cfg.fix_reflect_rounds.value = original_rounds

    assert len(prompts) == 2
    assert "Previous attempt feedback" in prompts[1]
    assert "exceeds the 6-character limit" in prompts[1]
    assert repaired == [
        {"text": "Yesterday", "cn": "昨天"},
        {"text": "he arrived.", "cn": "他到了。"},
    ]


def test_repair_overflow_honors_reflection_limit() -> None:
    translator = LLMTranslator()
    original_call_llm = llm_manager.call_llm
    original_rounds = cfg.fix_reflect_rounds.value
    prompts = []

    def fake_call_llm(messages, model, temperature=0.3, config_prefix="llm", max_tokens=None, expect_json=False, task_scope=None):
        prompts.append(messages[0]["content"])
        return '[{"text":"He arrived","cn":"他昨天已经到了这里"},{"text":"yesterday.","cn":"昨天。"}]'

    try:
        cfg.fix_reflect_rounds.value = 0
        llm_manager.call_llm = fake_call_llm
        try:
            translator.repair_overflow(
                "He arrived yesterday.",
                "他昨天已经到了。",
                max_chars=6,
            )
            raise AssertionError("Expected overflow repair to fail when reflection is disabled.")
        except Exception as e:
            assert "6-character limit" in str(e)
    finally:
        llm_manager.call_llm = original_call_llm
        cfg.fix_reflect_rounds.value = original_rounds

    assert len(prompts) == 1


def test_repair_overflow_batch_filters_invalid_items() -> None:
    translator = LLMTranslator()
    original_call_llm = llm_manager.call_llm

    def fake_call_llm(messages, model, temperature=0.3, config_prefix="llm", max_tokens=None, expect_json=False, task_scope=None):
        return (
            '[{"id":1,"segments":[{"text":"Yesterday","cn":"昨天"},{"text":"he arrived.","cn":"他到了。"}]},'
            '{"id":2,"segments":[{"text":"Too long","cn":"这段译文长度明显超限"},{"text":"tail","cn":"尾巴"}]}]'
        )

    try:
        llm_manager.call_llm = fake_call_llm
        repaired = translator.repair_overflow_batch(
            [
                {
                    "id": 1,
                    "text": "Yesterday he arrived.",
                    "translated_text": "昨天他到了。",
                    "prev_context": "Before",
                    "next_context": "After",
                },
                {
                    "id": 2,
                    "text": "Too long tail.",
                    "translated_text": "这段译文超限。",
                    "prev_context": "Before",
                    "next_context": "After",
                },
            ],
            max_chars=6,
        )
    finally:
        llm_manager.call_llm = original_call_llm

    assert repaired == {
        1: [
            {"text": "Yesterday", "cn": "昨天"},
            {"text": "he arrived.", "cn": "他到了。"},
        ]
    }
def test_splitter_enforces_strict_english_max_words() -> None:
    splitter = SubtitleSplitter()
    text = " ".join(f"w{i}" for i in range(1, 22))
    parts = splitter._split_by_max_words(text, 20)

    assert len(parts) == 2
    assert max(len(re.findall(r"\S+", part)) for part in parts) <= 20


def test_splitter_skips_pause_split_for_tiny_fragments() -> None:
    texts = ["It was a challenging list of fixtures which Roberto De Zerbi had"]
    source_segments = [
        {"start": 0.0, "end": 0.6, "text": "It was a challenging list"},
        {"start": 1.3, "end": 2.2, "text": "of fixtures which Roberto De Zerbi had"},
    ]

    enforced = SubtitleSplitter._enforce_long_pause_boundaries(
        texts,
        source_segments,
        pause_split_sec=0.5,
    )

    assert enforced == texts


def test_splitter_heuristic_merge_does_not_exceed_max_words() -> None:
    splitter = SubtitleSplitter()
    segments = [
        {
            "start": 0.0,
            "end": 0.7,
            "text": "right then now",
            "optimized_text": "right then now",
            "words": [],
        },
        {
            "start": 0.75,
            "end": 2.6,
            "text": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen",
            "optimized_text": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen",
            "words": [],
        },
    ]

    merged = splitter._heuristic_merge(segments, max_cjk=20, max_en=20)

    assert len(merged) == 2


def test_redistribute_origin_text_avoids_mid_word_splits() -> None:
    segments = [
        {"text": "Son in and Solanke's there and so is Kulezevsk", "cn": "孙兴慜而球交入 索兰克在那里 库卢塞夫斯基也"},
        {"text": "iy.", "cn": "在"},
    ]
    TextSplitter.redistribute_segment_texts_by_weights(
        "Son in and Solanke's there and so is Kulezevskiy.",
        segments,
        weight_key="cn",
    )
    assert segments[0]["text"] == "Son in and Solanke's there and so is"
    assert segments[1]["text"] == "Kulezevskiy."


def test_apply_translation_result() -> None:
    batch = [
        {"index": 1, "text": "hello"},
        {"index": 2, "text": "world"},
        {"index": 3, "text": "bye"},
    ]
    BatchTranscriptionThread._apply_translation_result(
        batch,
        [
            {"id": 2, "cn": "世界"},
            {"cn": "你好"},
        ],
    )
    assert batch[0]["translated_text"] == "你好"
    assert batch[1]["translated_text"] == "世界"
    assert batch[2]["translated_text"] == "bye"


def test_build_translation_failure() -> None:
    failure, is_non_retryable = BatchTranscriptionThread._build_translation_failure(
        10,
        5,
        17,
        Exception("timeout"),
    )
    assert is_non_retryable is False
    assert failure["batch"] == 3
    assert failure["range"] == [11, 15]
    assert failure["type"] == "retryable_exhausted"

    failure, is_non_retryable = BatchTranscriptionThread._build_translation_failure(
        15,
        5,
        17,
        Exception("unauthorized"),
    )
    assert is_non_retryable is True
    assert failure["range"] == [16, 17]
    assert failure["type"] == "non_retryable"


def test_update_translation_summary_counts() -> None:
    summary = {
        "failed_batches": 0,
        "skipped_batches": 0,
        "retryable_retries": 0,
        "non_retryable_skips": 0,
    }
    BatchTranscriptionThread._update_translation_summary_counts(
        summary,
        file_failures=[{"batch": 1}, {"batch": 2}],
        skipped_batches=3,
        retryable_retry_count=4,
        non_retryable_skip_count=1,
    )
    assert summary["failed_batches"] == 2
    assert summary["skipped_batches"] == 3
    assert summary["retryable_retries"] == 4
    assert summary["non_retryable_skips"] == 1


def test_clean_punctuation_text() -> None:
    text = "你好，世界。 这是：测试；"
    cleaned = clean_punctuation_text(text)
    assert "，" not in cleaned and "。" not in cleaned and "：" not in cleaned and "；" not in cleaned
    assert "你好" in cleaned and "世界" in cleaned


def test_text_splitter_constraint() -> None:
    cn = "这是一个很长的句子需要被拆分用于验证最大字数约束"
    segs = TextSplitter.split_subtitle(cn, "This is a long sentence for split", 0.0, 10.0, max_chars=8)
    assert len(segs) >= 2
    assert all(len((s.get("translated_text") or "")) <= 8 for s in segs)


def test_join_segment_texts_with_long_pause_boundaries() -> None:
    source_text, raw_boundaries = SubtitleSplitter._join_segment_texts_with_boundaries(
        [
            {"start": 0.0, "end": 0.5, "text": "Look at this."},
            {"start": 1.7, "end": 2.5, "text": "I'm not doing that with them."},
            {"start": 2.8, "end": 3.3, "text": "Okay."},
        ],
        pause_split_sec=0.9,
    )
    assert source_text == "Look at this I'm not doing that with them Okay"
    assert raw_boundaries == [12]


def test_enforce_long_pause_boundaries() -> None:
    texts = ["Look at this I'm not doing that with them", "Okay"]
    source_segments = [
        {"start": 0.0, "end": 0.5, "text": "Look at this."},
        {"start": 1.7, "end": 2.5, "text": "I'm not doing that with them."},
        {"start": 2.8, "end": 3.3, "text": "Okay."},
    ]
    fixed = SubtitleSplitter._enforce_long_pause_boundaries(
        texts,
        source_segments,
        pause_split_sec=0.9,
    )
    assert fixed == ["Look at this", "I'm not doing that with them", "Okay"]


def test_runtime_state_atomic_json() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = os.path.join(tmpdir, "state.json")
        payload = {"step": "translate", "count": 3}
        save_json_atomic(state_path, payload)
        loaded = load_json_file(state_path)
        assert loaded == payload
        safe_unlink(state_path)
        assert load_json_file(state_path, default={}) == {}


def test_translation_summary_writer() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        summary_path = os.path.join(tmpdir, "translation_summary.txt")
        result_path = write_translation_summary(
            summary_path,
            file_name="demo.mp4",
            total_batches=3,
            success_batches=2,
            failed_batches=1,
            skipped_batches=0,
            retryable_retry_count=1,
            non_retryable_skip_count=0,
            elapsed_seconds=12,
            failures=[
                {
                    "batch": 2,
                    "range": [11, 20],
                    "type": "retryable_exhausted",
                    "error": "timeout",
                }
            ],
        )
        with open(result_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "文件: demo.mp4" in content
        assert "失败批次: 1" in content
        assert "批次#2 范围11-20 类型=retryable_exhausted 错误=timeout" in content


def test_runtime_log_writer() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        runtime_log_path = os.path.join(tmpdir, "runtime_log.txt")
        result_path = write_runtime_log(
            runtime_log_path,
            file_name="demo.mp4",
            log_text="[Transcriber] filtered segment\n[Translation] fallback\n",
        )
        with open(result_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "文件: demo.mp4" in content
        assert "[Transcriber] filtered segment" in content
        assert "[Translation] fallback" in content


def test_quality_report_writer() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = os.path.join(tmpdir, "quality_report.txt")
        runtime_log_path = os.path.join(tmpdir, "runtime_log.txt")
        result_path = write_quality_report(
            report_path,
            file_name="demo.mp4",
            segment_count=42,
            translated=True,
            has_diarization=False,
            threshold=18,
            overflow_count=2,
            fallback_count=1,
            translation_summary={
                "total_batches": 5,
                "failed_batches": 1,
                "skipped_batches": 0,
                "retryable_retries": 2,
                "summary_path": os.path.join(tmpdir, "translation_summary.txt"),
            },
            transcribe_elapsed_sec=12.3,
            optimize_elapsed_sec=6.5,
            translation_elapsed_sec=20.0,
            overflow_fix_elapsed_sec=3.1,
            total_elapsed_sec=41.9,
            runtime_log_path=runtime_log_path,
        )
        with open(result_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "文件: demo.mp4" in content
        assert "总字幕数: 42" in content
        assert "翻译摘要文件: translation_summary.txt" in content
        assert "运行日志文件: runtime_log.txt" in content
        assert "风险等级: 高" in content


def test_build_export_targets_translated() -> None:
    jobs, main_export = build_export_targets(
        "C:\\temp\\demo_导出",
        "demo",
        translated=True,
        has_diarization=False,
    )
    assert len(jobs) == 7
    assert main_export.endswith("demo_译文在上.srt")
    assert jobs[0]["kind"] == "srt" and jobs[0]["mode"] == "trans_first"
    assert jobs[-1]["kind"] == "ass" and jobs[-1]["mode"] == "trans_only"


def test_build_export_targets_diarization() -> None:
    jobs, main_export = build_export_targets(
        "C:\\temp\\demo_导出",
        "demo",
        translated=True,
        has_diarization=True,
        speakers=["spk2", "spk1"],
    )
    assert len(jobs) == 9
    assert main_export.endswith("demo_新闻稿.txt")
    assert jobs[0]["speaker"] == "spk1"
    assert jobs[0]["path"].endswith("demo_spk1_仅原文.srt")
    assert jobs[-1]["kind"] == "news_script"


def run_all() -> int:
    tests = [
        test_file_signature_compare,
        test_retryable_error_classifier,
        test_get_sub_content_modes,
        test_build_translation_contexts,
        test_build_overflow_contexts,
        test_detect_weak_overflow_boundaries,
        test_detect_cross_subtitle_translation_fragments,
        test_prepare_export_segments_applies_remove_punctuation,
        test_cleanup_temp_audio_files,
        test_repair_overflow_reflects_invalid_first_attempt,
        test_repair_overflow_honors_reflection_limit,
        test_repair_overflow_batch_filters_invalid_items,
        test_splitter_enforces_strict_english_max_words,
        test_splitter_skips_pause_split_for_tiny_fragments,
        test_splitter_heuristic_merge_does_not_exceed_max_words,
        test_redistribute_origin_text_avoids_mid_word_splits,
        test_apply_translation_result,
        test_build_translation_failure,
        test_update_translation_summary_counts,
        test_clean_punctuation_text,
        test_text_splitter_constraint,
        test_join_segment_texts_with_long_pause_boundaries,
        test_enforce_long_pause_boundaries,
        test_runtime_state_atomic_json,
        test_translation_summary_writer,
        test_runtime_log_writer,
        test_quality_report_writer,
        test_build_export_targets_translated,
        test_build_export_targets_diarization,
    ]
    passed = 0
    failed = []

    print("[Regression] Start")
    for t in tests:
        name = t.__name__
        try:
            t()
            passed += 1
            print(f"[PASS] {name}")
        except Exception as e:
            failed.append((name, str(e), traceback.format_exc()))
            print(f"[FAIL] {name}: {e}")

    print(f"[Regression] Done. passed={passed}, failed={len(failed)}")

    if failed:
        report_path = os.path.join(PROJECT_ROOT, "regression_report.txt")
        with open(report_path, "w", encoding="utf-8") as wf:
            wf.write(f"passed={passed}\nfailed={len(failed)}\n\n")
            for name, err, tb in failed:
                wf.write(f"## {name}\n")
                wf.write(f"error: {err}\n")
                wf.write(tb)
                wf.write("\n\n")
        print(f"[Regression] failure report: {report_path}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(run_all())
