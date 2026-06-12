# -*- coding: utf-8 -*-
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.common.config import cfg
from app.common.runtime_state import SPLIT_RESUME_FILE, load_json_file, save_json_atomic, safe_unlink
from app.common.utils import fix_timestamp_overlaps
from app.core.context_enhancer import build_semantic_map, enhance_context
from app.core.llm import llm_manager

# 每批处理的 segment 数量上限（控制单次 LLM 请求的文本长度，避免长文本后半段质量下滑）
# 现在作为默认值，实际值在 split() 中按 token 数动态计算
_DEFAULT_CHUNK_SIZE = 40
# 相邻批次之间的重叠 segment 数（为边界断句提供上下文，避免割裂）
# 适当减小重叠可减少重复上下文带来的 token 浪费，从而缩短断句耗时。
_CHUNK_OVERLAP = 2
# 原始 ASR 相邻 segment 若间隔达到该阈值，视为明显长气口/长停顿；
# 断句结果不应跨越此类边界继续合并。
_LONG_PAUSE_SPLIT_SEC = 0.5

_READABILITY_RESCUE_DURATION_SEC = 0.55
_READABILITY_RESCUE_SLACK_EN = 12
_READABILITY_RESCUE_SLACK_CJK = 12
_WEAK_PUNCT_END_RE = re.compile(r'[,;:]["\')\]]*$')
_STRONG_PUNCT_END_RE = re.compile(r'[.!?]["\')\]]*$')


class SubtitleSplitter:
    """
    使用 LLM 对原始转录文本进行语义断句和分段优化（分块处理版本）
    """

    def __init__(self):
        pass

    # ──────────────────────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────────────────────

    def split(self, segments, context="", progress_callback=None, task_scope=None):
        """
        对字幕进行断句优化。
        segments: list of dicts, 每个 dict 包含 'text' 和可选的 'words'。
        """
        if not segments:
            return []

        context = enhance_context(context, task_scope=task_scope)
        semantic_map = build_semantic_map(segments, context, task_scope=task_scope)

        max_cjk = cfg.max_word_count_cjk.value
        max_en  = cfg.max_word_count_english.value
        model   = cfg.optimize_model.value or "gpt-3.5-turbo"

        # 预合并：Whisper 常会输出大量 0.3s~0.8s 的单词级碎片
        # 先把这些极短片段合并成短语再交给 LLM，可显著提升断句质量
        original_count = len(segments)
        segments = self._premerge_short_segments(segments, max_cjk, max_en)
        if len(segments) < original_count:
            print(f"[Splitter] 预合并碎片: {original_count} → {len(segments)} 条")

        # 分块：动态计算 chunk_size，按 token 数控制每块不超过 ~2400 token
        sample = segments[:min(20, len(segments))]
        avg_chars = sum(len(s.get('text', '')) for s in sample) / max(1, len(sample))
        # CJK: ~1 token/char; Latin: ~0.5 token/char → use 0.5 as conservative estimate
        max_tokens_per_chunk = 2400
        chunk_size = max(15, min(60, int(max_tokens_per_chunk / max(avg_chars * 0.5, 1))))
        chunks = self._make_chunks(segments, chunk_size, _CHUNK_OVERLAP)
        total_chunks = len(chunks)
        print(f"[Splitter] 共 {len(segments)} 条字幕，分 {total_chunks} 块处理"
              f" (块大小={chunk_size}, 重叠={_CHUNK_OVERLAP})")

        all_new_texts = []

        # ── 断点续传：恢复已处理的 chunks ──
        _split_resume = SPLIT_RESUME_FILE
        resume_start = 0
        try:
            sr_data = load_json_file(_split_resume)
            if sr_data:
                if (sr_data.get("total_segments") == len(segments) and
                        sr_data.get("total_chunks") == total_chunks and
                        sr_data.get("first_text") == segments[0].get("text", "")):
                    saved_texts = sr_data.get("all_new_texts", [])
                    resume_start = sr_data.get("completed_chunks", 0)
                    all_new_texts = saved_texts
                    if resume_start > 0:
                        print(f"[Splitter] 断点续传：已恢复 {resume_start}/{total_chunks} 块")
        except Exception:
            pass

        chunks_to_process = [(i, segs, h) for i, (segs, h) in enumerate(chunks) if i >= resume_start]
        use_parallel = len(chunks_to_process) > 1
        max_workers = cfg.llm_max_workers.value

        if use_parallel:
            # 并行：提交各块 LLM 调用，收集结果后再按块序合并与 trim
            results = {}
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_idx = {
                    executor.submit(
                        self._process_one_chunk,
                        chunk_idx, chunk_segs, context, semantic_map, max_cjk, max_en, model, task_scope
                    ): chunk_idx
                    for chunk_idx, chunk_segs, _ in chunks_to_process
                }
                done_count = 0
                # 总步数 = 断句块数 + 1（时间轴整理）
                total_steps = total_chunks + 1
                for future in as_completed(future_to_idx):
                    if progress_callback:
                        done_count += 1
                        progress_callback(resume_start + done_count, total_steps)
                    cidx, chunk_texts = future.result()
                    results[cidx] = chunk_texts
            # 按块顺序合并：trim overlap 后 extend
            for chunk_idx in range(resume_start, total_chunks):
                chunk_segs, head_overlap = chunks[chunk_idx][0], chunks[chunk_idx][1]
                chunk_texts = results.get(chunk_idx, [])
                if chunk_idx > 0 and head_overlap > 0 and chunk_texts:
                    overlap_segs = chunks[chunk_idx - 1][0][-head_overlap:]
                    overlap_char_count = sum(
                        len(s.get('optimized_text', s.get('text', ''))) for s in overlap_segs
                    )
                    chunk_texts = self._trim_overlap_prefix(chunk_texts, overlap_char_count)
                all_new_texts.extend(chunk_texts)
                # 断点续传：每合并一块写一次
                try:
                    save_json_atomic(_split_resume, {
                        "total_segments": len(segments),
                        "total_chunks": total_chunks,
                        "first_text": segments[0].get("text", "") if segments else "",
                        "completed_chunks": chunk_idx + 1,
                        "all_new_texts": all_new_texts
                    })
                except Exception:
                    pass
        else:
            # 串行：块数很少时避免线程开销
            total_steps = total_chunks + 1
            for chunk_idx, (chunk_segs, head_overlap) in enumerate(chunks):
                if chunk_idx < resume_start:
                    continue
                if progress_callback:
                    progress_callback(chunk_idx + 1, total_steps)
                _, chunk_texts = self._process_one_chunk(
                    chunk_idx, chunk_segs, context, semantic_map, max_cjk, max_en, model, task_scope
                )
                if chunk_idx > 0 and head_overlap > 0:
                    overlap_segs = chunks[chunk_idx - 1][0][-head_overlap:]
                    overlap_char_count = sum(
                        len(s.get('optimized_text', s.get('text', ''))) for s in overlap_segs
                    )
                    chunk_texts = self._trim_overlap_prefix(chunk_texts, overlap_char_count)
                all_new_texts.extend(chunk_texts)
                try:
                    save_json_atomic(_split_resume, {
                        "total_segments": len(segments),
                        "total_chunks": total_chunks,
                        "first_text": segments[0].get("text", "") if segments else "",
                        "completed_chunks": chunk_idx + 1,
                        "all_new_texts": all_new_texts
                    })
                except Exception:
                    pass

        # 断句完成，清理续传文件
        try:
            safe_unlink(_split_resume)
        except Exception:
            pass

        if not all_new_texts:
            print("[Splitter] 断句结果为空，回退原始字幕")
            return segments

        # 兜底清理：去除残留的前置标点、Gladia 残留句点等格式问题
        all_new_texts = [self._cleanup_text(t) for t in all_new_texts]
        all_new_texts = [t for t in all_new_texts if t]

        # 后处理：二次按标点分割 + 强制字数限制
        all_new_texts = self._resplit_by_punctuation(all_new_texts, max_cjk, max_en)
        all_new_texts = self._enforce_length_limit(all_new_texts, max_cjk, max_en)
        pause_split_sec = float(getattr(cfg.long_pause_split_sec, "value", _LONG_PAUSE_SPLIT_SEC) or _LONG_PAUSE_SPLIT_SEC)
        all_new_texts = self._enforce_long_pause_boundaries(
            all_new_texts,
            segments,
            pause_split_sec=max(0.1, pause_split_sec),
        )

        # 时间戳对齐
        all_words = []
        for s in segments:
            if 'words' in s and s['words']:
                all_words.extend(s['words'])

        if not all_words:
            print("[Splitter] 警告: 未找到词级时间戳，将使用字符比例进行估算对齐")
            new_segments = self._align_timestamps_fallback(all_new_texts, segments)
        else:
            new_segments = self._align_timestamps(all_new_texts, all_words)

        total_steps = total_chunks + 1
        if progress_callback:
            progress_callback(total_steps, total_steps)

        print(f"[Splitter] 断句完成，生成了 {len(new_segments)} 条新字幕。")

        new_segments = fix_timestamp_overlaps(new_segments)
        new_segments = self._remove_zero_and_prefix_duplicates(new_segments)
        new_segments = self._attach_origin_index(new_segments, segments)
        new_segments = self._heuristic_merge(new_segments, max_cjk, max_en)

        return new_segments

    # ──────────────────────────────────────────────────────────────
    # 分块辅助（单块处理，供并行 worker 调用；不做 overlap trim，由主线程按序合并时做）
    # ──────────────────────────────────────────────────────────────

    def _process_one_chunk(self, chunk_idx, chunk_segs, context, semantic_map, max_cjk, max_en, model, task_scope=None):
        """处理单块：拼接文本、调用 LLM 断句、完整性校验。返回 (chunk_idx, chunk_texts)，不做 overlap trim。"""
        chunk_text = self._join_segment_texts(chunk_segs)
        if not chunk_text.strip():
            return (chunk_idx, [])
        rule_texts = self._build_rule_based_chunk_texts(chunk_segs, max_cjk, max_en)
        # Latin-script languages rely heavily on clause grammar; let the LLM
        # choose boundaries unless we need the rule fallback.
        prefer_llm = self._is_latin(chunk_text)
        if not prefer_llm and not self._chunk_needs_llm(rule_texts, chunk_segs, max_cjk, max_en):
            return (chunk_idx, rule_texts)
        punct_density = sum(1 for c in chunk_text if c in '.!?,;:') / max(len(chunk_text), 1)
        if punct_density < 0.005:
            chunk_text_for_llm = self._strip_punctuation(chunk_text)
        else:
            chunk_text_for_llm = chunk_text
        chunk_texts = self._split_chunk(chunk_text_for_llm, context, semantic_map, max_cjk, max_en, model, task_scope)
        if chunk_texts is None:
            chunk_texts = self._split_chunk(chunk_text_for_llm, context, semantic_map, max_cjk, max_en, model, task_scope)
            if chunk_texts is None:
                print(f"[Splitter] 第 {chunk_idx + 1} 块 LLM 断句失败（已重试一次），使用原始文本")
                chunk_texts = [s.get('optimized_text', s.get('text', '')) for s in chunk_segs]
        if chunk_texts is not None:
            # 先做轻量级长度校验，仅在长度偏差较大时才启用昂贵的内容保真对齐，
            # 避免每块都跑一遍全量 SequenceMatcher。
            combined_len = sum(len(t) for t in chunk_texts)
            src_len = len(chunk_text)
            ratio = combined_len / max(src_len, 1)
            if not (0.85 <= ratio <= 1.15):
                chunk_texts = self._enforce_original_content(chunk_texts, chunk_text)
                combined_len = sum(len(t) for t in chunk_texts)
                ratio = combined_len / max(src_len, 1)
            if not (0.85 <= ratio <= 1.30):
                print(f"[Splitter] 第 {chunk_idx + 1} 块完整性校验失败 (ratio={ratio:.2f})，回退原文")
                chunk_texts = rule_texts or [s.get('optimized_text', s.get('text', '')) for s in chunk_segs]
        return (chunk_idx, chunk_texts)

    @staticmethod
    def _needs_space_between(left, right):
        if not left or not right:
            return False
        if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff]$', left):
            return False
        if re.match(r'^[\u3400-\u4dbf\u4e00-\u9fff]', right):
            return False
        if left.endswith(("-", "—", "/")) or right.startswith(("'", ".", ",", "!", "?", ";", ":")):
            return False
        return not left.endswith(" ")

    def _join_text_pair(self, left, right):
        left = (left or "").strip()
        right = (right or "").strip()
        if not left:
            return right
        if not right:
            return left
        glue = " " if self._needs_space_between(left, right) else ""
        return f"{left}{glue}{right}"

    @staticmethod
    def _has_terminal_punctuation(text):
        return bool(re.search(r'[。！？.!?…]["\')\]]*$', (text or "").strip()))

    def _segment_exceeds_limit(self, text, max_cjk, max_en):
        stripped = (text or "").strip()
        if not stripped:
            return False
        if self._is_latin(stripped):
            return len(re.findall(r"\w+", stripped)) > max_en
        return len(stripped) > max_cjk

    def _build_rule_based_chunk_texts(self, chunk_segs, max_cjk, max_en):
        results = []
        current = ""
        current_suspicious = False
        prev_seg = None
        pause_split_sec = float(getattr(cfg.long_pause_split_sec, "value", _LONG_PAUSE_SPLIT_SEC) or _LONG_PAUSE_SPLIT_SEC)

        for seg in chunk_segs:
            text = (seg.get('optimized_text', seg.get('text', '')) or "").strip()
            if not text:
                continue

            seg_suspicious = bool(seg.get("is_suspicious"))
            if not current:
                current = text
                current_suspicious = seg_suspicious
                prev_seg = seg
                continue

            gap = max(0.0, float(seg.get("start", 0) or 0) - float(prev_seg.get("end", 0) or 0)) if prev_seg else 0.0
            force_boundary = (
                current_suspicious
                or seg_suspicious
                or gap >= max(0.1, pause_split_sec)
                or self._has_terminal_punctuation(current)
            )

            tentative = self._join_text_pair(current, text)
            if force_boundary or self._segment_exceeds_limit(tentative, max_cjk, max_en):
                results.append(current.strip())
                current = text
                current_suspicious = seg_suspicious
            else:
                current = tentative
                current_suspicious = current_suspicious or seg_suspicious
            prev_seg = seg

        if current.strip():
            results.append(current.strip())
        return results

    def _chunk_needs_llm(self, rule_texts, chunk_segs, max_cjk, max_en):
        if not rule_texts:
            return False
        if any(seg.get("is_suspicious") for seg in chunk_segs):
            return False
        return any(self._segment_exceeds_limit(text, max_cjk, max_en) for text in rule_texts)

    @staticmethod
    def _join_segment_texts(segs):
        result, _ = SubtitleSplitter._join_segment_texts_with_boundaries(segs)
        return result

    @staticmethod
    def _join_segment_texts_with_boundaries(segs, pause_split_sec=_LONG_PAUSE_SPLIT_SEC):
        """
        将多个 segment 的文本拼接为一整段字符串。
        对于拉丁语系（英/法/德/西等），相邻 segment 之间若末位和首位都是
        字母/数字，则自动补一个空格，防止跨 segment 的词被粘连。
        中日韩文本无需空格，逻辑一致。

        同时做以下清理：
        1. 前置标点：去除 Gladia/ASR 的段首句点（如 ".And" → "And"）
        2. 尾随句号去除（短片段）：Optimizer LLM 对 Whisper 短碎片（如 "We've won"）
           补加句号，拼接后变成段内乱句号（"We've won.The game.We managed."）。
           当文本 ≤ 6 个空格词（即为短碎片）时，去掉尾随句号，避免污染拼接结果。
        3. Gladia " . " 规范化：两侧有空格的句点 → 普通空格。
        """
        result = ""
        pause_boundaries = []
        prev_end = None
        for s in segs:
            text = s.get('optimized_text', s.get('text', ''))
            if not text:
                prev_end = s.get('end', prev_end)
                continue
            # 1. 去除前置标点
            text = re.sub(r'^[\s.!?,;]+', '', text)
            if not text:
                prev_end = s.get('end', prev_end)
                continue
            # 2. 去除短碎片的尾随句号（Optimizer 为每个 Whisper 短片段加的句号）
            #    判断标准：空格分词 ≤ 6 词（短到不太可能是完整长句）
            #    仅针对句号（.），保留问号（?）和感叹号（!）—— 后两者有语气意义
            word_count = len(text.split())
            if word_count <= 6 and text.rstrip().endswith('.'):
                text = text.rstrip()[:-1].rstrip()
            if not text:
                prev_end = s.get('end', prev_end)
                continue
            if prev_end is not None:
                gap = float(s.get('start', 0) or 0) - float(prev_end or 0)
                if gap >= pause_split_sec and result:
                    pause_boundaries.append(len(result))
            # 拼接：相邻两侧均为字母/数字时补空格（拉丁语系防粘词）
            if result and result[-1].isalnum() and text[0].isalnum():
                result += " "
            result += text
            prev_end = s.get('end', prev_end)
        # 3. 规范化 Gladia " . "（两侧带空格的句点）为普通空格
        result = re.sub(r'\s\.\s', ' ', result)
        # 压缩多余空格
        result = re.sub(r'\s{2,}', ' ', result)
        return result.strip(), pause_boundaries

    @staticmethod
    def _enforce_long_pause_boundaries(texts, source_segments, pause_split_sec=_LONG_PAUSE_SPLIT_SEC):
        if len(texts) < 1 or len(source_segments) < 2:
            return texts

        source_text, raw_boundaries = SubtitleSplitter._join_segment_texts_with_boundaries(
            source_segments,
            pause_split_sec=pause_split_sec,
        )
        if not source_text or not raw_boundaries:
            return texts

        source_nonspace_map = [i for i, ch in enumerate(source_text) if not ch.isspace()]
        if not source_nonspace_map:
            return texts

        total_source_chars = len(source_nonspace_map)
        total_target_chars = sum(sum(1 for ch in text if not ch.isspace()) for text in texts)
        if total_target_chars != total_source_chars:
            # 仅在内容计数完全对齐时才强制切开，避免误伤非保真场景。
            return texts

        boundary_counts = []
        raw_cursor = 0
        nonspace_cursor = 0
        raw_boundaries_sorted = sorted(set(raw_boundaries))
        boundary_iter = iter(raw_boundaries_sorted)
        next_boundary = next(boundary_iter, None)
        while next_boundary is not None:
            while raw_cursor < min(next_boundary, len(source_text)):
                if not source_text[raw_cursor].isspace():
                    nonspace_cursor += 1
                raw_cursor += 1
            boundary_counts.append(nonspace_cursor)
            next_boundary = next(boundary_iter, None)

        if not boundary_counts:
            return texts

        result = []
        cursor_nonspace = 0
        for text in texts:
            char_count = sum(1 for ch in text if not ch.isspace())
            if char_count <= 0:
                continue

            seg_start_nonspace = cursor_nonspace
            seg_end_nonspace = cursor_nonspace + char_count
            inner_boundaries = [
                bc for bc in boundary_counts
                if seg_start_nonspace < bc < seg_end_nonspace
            ]

            if not inner_boundaries:
                result.append(text)
                cursor_nonspace = seg_end_nonspace
                continue

            split_points_raw = [source_nonspace_map[seg_start_nonspace]]
            for bc in inner_boundaries:
                split_points_raw.append(source_nonspace_map[bc])
            split_points_raw.append(source_nonspace_map[seg_end_nonspace - 1] + 1)

            candidate_parts = []
            for raw_start, raw_end in zip(split_points_raw, split_points_raw[1:]):
                part = source_text[raw_start:raw_end].strip()
                if part:
                    candidate_parts.append(part)

            if len(candidate_parts) >= 2 and SubtitleSplitter._should_keep_pause_split(candidate_parts):
                result.extend(candidate_parts)
            else:
                result.append(text)

            cursor_nonspace = seg_end_nonspace

        return result if result else texts

    @staticmethod
    def _has_terminal_punctuation(text: str) -> bool:
        return bool(re.search(r'[.!?。！？]["\')\]]*$', (text or "").strip()))

    @staticmethod
    def _looks_like_latin(text: str) -> bool:
        latin_count = sum(1 for c in (text or "") if 'a' <= c.lower() <= 'z')
        return latin_count > len(text or "") * 0.4

    @staticmethod
    def _starts_lowercase_latin(text: str) -> bool:
        return bool(re.match(r"[a-zà-öø-ÿ]", (text or "").strip()))

    @staticmethod
    def _should_keep_pause_split(parts: list[str]) -> bool:
        if len(parts) < 2:
            return False

        for idx, part in enumerate(parts):
            stripped = (part or "").strip()
            if not stripped:
                return False

            if idx < len(parts) - 1:
                next_part = parts[idx + 1]
                if SubtitleSplitter._starts_lowercase_latin(next_part):
                    return False

            if SubtitleSplitter._has_terminal_punctuation(stripped):
                continue

            if SubtitleSplitter._looks_like_latin(stripped):
                word_count = len(re.findall(r'\S+', stripped))
                if word_count >= 6:
                    continue
                if word_count >= 3:
                    continue
                return False

            if sum(1 for ch in stripped if not ch.isspace()) < 6:
                return False

        return True

    @staticmethod
    def _pre_mark_terminators(text):
        """
        在句子终止符后预置 <br>，将标点边界确定性地转为分割点。
        中日韩标点（。！？…）无歧义，直接标记。
        英文 ! 和 ? 无歧义，直接标记。
        英文句点（.）仅在"后跟空格+大写字母"或"字符串末尾"时才标记，
        避免缩写词（Dr./Mr./etc.）被错误切断。
        """
        # CJK 句终标点
        result = re.sub(r'([。！？…])', r'\1<br>', text)
        # 英文 ! 和 ?
        result = re.sub(r'([!?])', r'\1<br>', result)
        # 英文句点：后接 空格+大写字母 时为句终
        result = re.sub(r'\.(\s+[A-Z])', r'.<br>\1', result)
        # 英文句点：字符串末尾
        result = re.sub(r'\.\s*$', '.<br>', result)
        # 清理重复 <br>
        result = re.sub(r'(<br>\s*)+', '<br>', result)
        return result

    def _premerge_short_segments(self, segments, max_cjk, max_en):
        """
        预合并极短碎片：Whisper 在说话停顿处常将一个短语拆成多个 0.3s~0.8s 的单词级 segment。
        这种碎片输入 LLM 后很难断句，预先按时间间隔和长度将它们合并成短语。

        合并条件（同时满足）：
        1. 当前 segment 是"碎片"：时长 < 1.2s 且文本极短（英文 ≤ 4 词 / 中文 ≤ 8 字）
        2. 与下一 segment 的时间间隔 < 0.5s
        3. 合并后总长度不超过目标字数上限
        """
        if len(segments) < 2:
            return segments

        def is_tiny(seg):
            dur  = seg.get('end', 0) - seg.get('start', 0)
            text = seg.get('optimized_text', seg.get('text', ''))
            if dur >= 1.2:
                return False
            if self._is_latin(text):
                # 用 split() 按空格分词，避免 "We've" 被 re.findall(r'\w+') 算成 2 词
                # 导致合并判断过早终止
                return len(text.split()) <= 4
            else:
                return len(text) <= 8

        def merge_two(a, b):
            text_a = a.get('optimized_text', a.get('text', ''))
            text_b = b.get('optimized_text', b.get('text', ''))
            # 去掉前段的尾随句号：被合并的前段（a）是短碎片，其句号是 Optimizer 补的，
            # 合并后变成段内句号（如 "We've won.The game."），需要提前清除
            if text_a.rstrip().endswith('.'):
                text_a = text_a.rstrip()[:-1].rstrip()
            # 前后两端均为字母/数字时补空格，其他情况也补空格（防止粘词）
            if text_a and text_b:
                sep = ' '
            else:
                sep = ''
            merged_text = (text_a + sep + text_b).strip()
            words_a = a.get('words', [])
            words_b = b.get('words', [])
            return {
                'start':          a.get('start', 0),
                'end':            b.get('end', 0),
                'text':           merged_text,
                'optimized_text': merged_text,
                'words':          words_a + words_b,
            }

        result = []
        i = 0
        while i < len(segments):
            curr = segments[i]
            while i + 1 < len(segments):
                nxt  = segments[i + 1]
                gap  = nxt.get('start', 0) - curr.get('end', 0)
                curr_text = curr.get('optimized_text', curr.get('text', ''))
                nxt_text  = nxt.get('optimized_text',  nxt.get('text', ''))

                # 判断是否应该合并
                if gap >= 0.5:
                    break  # 间隔太大，不合并
                if not is_tiny(curr):
                    break  # 当前段已不是碎片，停止向后合并

                # 检查合并后是否超出字数限制
                if self._is_latin(curr_text):
                    combined_words = len(re.findall(r'\w+', curr_text)) + len(re.findall(r'\w+', nxt_text))
                    if combined_words > max_en:
                        break
                else:
                    if len(curr_text) + len(nxt_text) > max_cjk:
                        break

                curr = merge_two(curr, nxt)
                i += 1

            result.append(curr)
            i += 1

        return result

    def _make_chunks(self, segments, chunk_size, overlap):
        """
        将 segments 切分为若干块。
        返回 list of (chunk_segs, head_overlap_count)。
        head_overlap_count: 本块头部有多少条属于上一块的重叠（后处理时需丢弃）。
        """
        if len(segments) <= chunk_size:
            return [(segments, 0)]

        chunks = []
        step = chunk_size - overlap
        i = 0
        while i < len(segments):
            end = min(i + chunk_size, len(segments))
            head_overlap = overlap if i > 0 else 0
            chunks.append((segments[i:end], head_overlap))
            if end == len(segments):
                break
            i += step
        return chunks

    def _trim_overlap_prefix(self, texts, overlap_char_count):
        """
        从 texts 列表头部移除大约 overlap_char_count 个字符对应的 segments，
        避免相邻块的重叠部分被重复收录到断句结果中。
        阈值设为 60%，留有余量防止切过头。
        """
        removed = 0
        threshold = overlap_char_count * 0.60
        for i, t in enumerate(texts):
            if removed >= threshold:
                return texts[i:]
            removed += len(t)
        # 极端情况：所有文本都不够 threshold，返回空列表
        return []

    def _split_chunk(self, chunk_text, context, semantic_map, max_cjk, max_en, model, task_scope=None):
        """对单块文本调用 LLM 进行断句，返回 list[str] 或 None（失败时）。"""

        # 根据视频语境动态生成场景专属规则，避免把足球解说规则用在其他类型的视频上
        scene_rules = ""
        if context:
            ctx_lower = context.lower()
            if any(kw in ctx_lower for kw in ["футбол", "soccer", "football", "fútbol", "futebol",
                                               "sport", "deporte", "esporte", "commentary", "comentario"]):
                scene_rules = """
**Scene-specific rules (sports/live commentary)**:
- Short exclamations (e.g. "Gol!", "Foul!", "Yes!", "Que golaço!") should be their own segment.
- Tactical explanations can occupy one full segment even if slightly longer than the limit.
- Repeated phrases (e.g. "Rubem, Rubem, Rubem") may stay together if they fit the limit.
- Keep a player name together with the action that immediately follows it."""
            elif any(kw in ctx_lower for kw in ["interview", "entrevista", "talk", "podcast",
                                                  "conversation", "conversación"]):
                scene_rules = """
**Scene-specific rules (interview/talk)**:
- Keep a question and its short confirmatory answer together if total length allows.
- Do NOT split rhetorical questions from their immediately following answer within the same turn."""
            elif any(kw in ctx_lower for kw in ["lecture", "教学", "课", "tutorial", "explainer",
                                                  "documentary", "纪录片"]):
                scene_rules = """
**Scene-specific rules (educational/documentary)**:
- Keep technical terms or named concepts together with their defining clause.
- Do NOT split a term from its immediately following explanation."""

        system_prompt = f"""You are a professional subtitle segmenter. Insert <br> at natural sentence/clause boundaries. Do NOT change, add, remove, or reorder any character.

<instructions>
1. **ZERO content change**: Copy every character exactly as-is. Output (with <br> removed) must be byte-for-byte identical to input. Do NOT add any sentence or phrase that does not appear in the input. Do NOT duplicate any phrase (each part of the input must appear exactly once).

2. **Segment size**:
   - CJK: target ~{max_cjk} chars, NEVER exceed {max_cjk}. Short segments are fine.
   - Latin: target ~{max_en} words, but grammar comes first. Slightly exceed the target rather than leaving an unfinished clause or phrase alone.

3. **Where to split**: At complete clause breaks, topic shifts, and sentence endings. ASR punctuation is a weak hint, not a hard boundary. Do NOT isolate very short fragments (< 5 words / < 8 CJK chars) when they clearly depend on adjacent text. Never break fixed phrases, proper nouns, or named entities.

4. **Semantic guidance**: Use the semantic map to recognize speaker turns, questions, answers, explanations, and references. Prefer boundaries at real turn/topic changes. Keep short answers, follow-up clauses, and pronoun references with the line they depend on when the length limit allows. Let meaning override suspicious ASR punctuation.

5. **Output**: ONLY the text with <br> inserted. No explanation, no markdown, no numbering.
{scene_rules}
</instructions>

<example>
Input: The committee has decided to postpone the vote until next Monday because several members were unable to attend today's session due to the severe weather conditions
Output: The committee has decided to postpone the vote until next Monday<br>because several members were unable to attend today's session<br>due to the severe weather conditions
</example>"""

        user_prompt = f"Segment this text:\n{chunk_text}"
        if context:
            user_prompt = f"Video context: {context}\n\n{user_prompt}"
        if semantic_map:
            user_prompt = (
                f"Semantic structure map (use as guidance; do not copy it):\n"
                f"{semantic_map}\n\n{user_prompt}"
            )

        # 动态估算 max_tokens：
        # 中文每个字约 1 token，加上 <br> 标记开销需要 * 2.5
        # 拉丁语每个字符约 0.4 token，加上 <br> 开销需要 * 1.8
        # 取较大值以保证不被截断
        cjk_count = sum(1 for c in chunk_text if '\u4e00' <= c <= '\u9fff')
        cjk_ratio = cjk_count / max(len(chunk_text), 1)
        token_multiplier = 2.5 if cjk_ratio > 0.3 else 1.8
        dynamic_max_tokens = min(4000, max(512, int(len(chunk_text) * token_multiplier)))

        try:
            result = llm_manager.call_llm(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                model=model,
                temperature=0,
                config_prefix="optimize",
                max_tokens=dynamic_max_tokens,
                task_scope=task_scope
            )
            texts = [t.strip() for t in result.split("<br>") if t.strip()]
            return texts if texts else None
        except Exception as e:
            print(f"[Splitter] LLM 调用失败: {e}")
            return None

    # ──────────────────────────────────────────────────────────────
    # 时间戳对齐（回退方案：字符比例估算）
    # ──────────────────────────────────────────────────────────────

    def _align_timestamps_fallback(self, new_texts, original_segments):
        """如果没有词级时间戳，则根据字符长度比例估算时间。"""
        new_segments = []

        total_text = "".join(s.get('optimized_text', s.get('text', '')) for s in original_segments)
        if not total_text:
            return original_segments

        total_duration = original_segments[-1]['end'] - original_segments[0]['start']
        start_offset   = original_segments[0]['start']

        current_char_count = 0
        total_chars = len(total_text)

        for text in new_texts:
            char_len = len(text)
            start_ratio = current_char_count / total_chars
            current_char_count += char_len
            end_ratio = current_char_count / total_chars

            new_segments.append({
                'start': start_offset + (start_ratio * total_duration),
                'end':   start_offset + (end_ratio   * total_duration),
                'text':           text,
                'optimized_text': text,
                'words': []
            })

        return new_segments

    # ──────────────────────────────────────────────────────────────
    # 后处理：二次按标点分割 + 强制字数上限
    # ──────────────────────────────────────────────────────────────

    def _resplit_by_punctuation(self, texts, max_cjk, max_en):
        results = []
        for text in texts:
            # 需要至少 2 个终止符才有拆分意义（1 个通常是本段末尾的句号，不需要拆）
            if self._count_sentence_terminators(text) < 2:
                results.append(text)
                continue

            # 标点即边界——移除旧有的"长度不足就不拆"条件：
            # 只要有 ≥2 个句子终止符，无论文本多短都应尊重标点边界进行拆分
            if self._is_latin(text):
                parts = re.findall(r'[^.!?¿¡]*[.!?]+(?:\s+|$)|[^.!?¿¡]+$', text)
            else:
                parts = re.findall(r'[^。！？]*[。！？]+|[^。！？]+$', text)

            valid_parts = [p.strip() for p in parts if p.strip()]

            # 如果所有分片都极短（< 4 词/字），说明是 Gladia 密集标点导致的
            # 过度切割，直接保留整段，交给 _heuristic_merge 处理。
            if valid_parts and self._is_latin(text):
                if all(len(re.findall(r'\w+', p)) < 4 for p in valid_parts):
                    results.append(text)
                    continue

            if valid_parts:
                results.extend(valid_parts)
            else:
                results.append(text)

        return results

    def _count_sentence_terminators(self, text):
        return len(re.findall(r'[。！？.!?]', text))

    def _enforce_length_limit(self, texts, max_cjk, max_en):
        results = []
        for text in texts:
            if self._is_latin(text):
                results.extend(self._split_by_max_words(text, max_en + _READABILITY_RESCUE_SLACK_EN))
            else:
                results.extend(self._split_by_max_chars(text, max_cjk))
        return [t for t in results if t]

    def _split_by_max_words(self, text, max_en):
        words = re.findall(r'\S+', text)
        # Latin-script languages use a soft target upstream; this method only
        # enforces the final hard cap passed by _enforce_length_limit().
        if len(words) <= max_en:
            return [text]

        parts = re.findall(r'[^.!?¿¡]*[.!?¿¡]+(?:\s+|$)|[^.!?¿¡]+$', text)
        results = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            part_words = re.findall(r'\S+', part)
            if len(part_words) <= max_en:
                results.append(part)
                continue
            # 语义感知截断：在 max_en 附近寻找自然断点，防止短语被劈开
            i = 0
            while i < len(part_words):
                remaining = len(part_words) - i
                if remaining <= max_en:
                    chunk = " ".join(part_words[i:]).strip()
                    if chunk:
                        results.append(chunk)
                    break
                split_at = self._find_natural_split(part_words, i, max_en, fwd_window=0)
                if split_at <= i:
                    split_at = min(i + max_en, len(part_words))
                chunk = " ".join(part_words[i:split_at]).strip()
                if chunk:
                    results.append(chunk)
                i = split_at
        return results

    @staticmethod
    def _find_natural_split(words: list, start: int, max_en: int,
                             back_window: int = 6, fwd_window: int = 0) -> int:
        """
        在 words[start:] 中找最佳截断点，返回截断后第一个词的索引。
        搜索范围：[default_end - back_window, default_end + fwd_window]
        默认不再向后放宽，避免突破用户设置的最大单词数。

        优先级：
          1. 逗号结尾（向前找）— 最自然，不超出
          2. 逗号结尾（向后找）— 仅在显式允许放宽时启用
          3. 连词/从句引导词（向前找）— 不超出
          4. 连词/从句引导词（向后找）— 仅在显式允许放宽时启用
          5. 回退到 default_end
        """
        _CONJUNCTIONS = {
            'and', 'but', 'or', 'so', 'yet', 'for', 'nor',
            'because', 'since', 'although', 'though', 'while',
            'when', 'if', 'unless', 'until', 'after', 'before',
            'as', 'that', 'which', 'who', 'where', 'however',
            'therefore', 'then', 'than',
        }
        default_end = min(start + max_en, len(words))
        back_floor  = max(start + 2, default_end - back_window)
        fwd_ceil    = min(len(words), default_end + fwd_window)

        # 优先级 1：逗号结尾，向前搜索（不超出 max_en）
        for j in range(default_end, back_floor, -1):
            if re.search(r',$', words[j - 1]):
                return j

        # 优先级 2：逗号结尾，向后搜索（在 tolerance 内略微超出）
        for j in range(default_end + 1, fwd_ceil + 1):
            if j <= len(words) and re.search(r',$', words[j - 1]):
                return j

        # 优先级 3：连词/从句引导词，向前搜索
        for j in range(default_end, back_floor, -1):
            if j < len(words):
                bare = re.sub(r'[^a-zA-Z]', '', words[j]).lower()
                if bare in _CONJUNCTIONS:
                    return j

        # 优先级 4：连词/从句引导词，向后搜索
        for j in range(default_end + 1, fwd_ceil):
            if j < len(words):
                bare = re.sub(r'[^a-zA-Z]', '', words[j]).lower()
                if bare in _CONJUNCTIONS:
                    return j

        return default_end

    def _split_by_max_chars(self, text, max_cjk):
        # 中文最大字数也应视为硬上限。
        if len(text) <= max_cjk:
            return [text]

        parts = re.findall(r'[^。！？]*[。！？]+|[^。！？]+$', text)
        results = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(part) <= max_cjk:
                results.append(part)
                continue
            for i in range(0, len(part), max_cjk):
                chunk = part[i:i + max_cjk].strip()
                if chunk:
                    results.append(chunk)
        return results

    # ──────────────────────────────────────────────────────────────
    # 时间戳对齐（词级精准对齐）
    # ──────────────────────────────────────────────────────────────

    def _align_timestamps(self, new_texts, all_words):
        """使用 difflib 进行全局序列比对，将词级时间戳映射到新的断句文本上。"""
        import difflib
        import unicodedata

        if not all_words:
            print("[Splitter] 警告: 缺少词级时间戳，使用回退算法")
            return self._align_timestamps_fallback(new_texts, [])

        # 构建 Master Timeline（原始字符流）
        master_chars  = []
        cleaned_words = []
        for w in all_words:
            word_text  = w.get('word', w.get('text', ''))
            clean_word = word_text.strip()
            if not clean_word:
                continue

            start = w.get('start', 0.0)
            end   = w.get('end',   0.0)
            cleaned_words.append({"word": clean_word, "start": start, "end": end})
            word_idx = len(cleaned_words) - 1
            duration = end - start
            char_len = len(clean_word)
            step = duration / char_len if char_len > 0 else 0

            for i, char in enumerate(clean_word):
                master_chars.append({
                    "char":     char,
                    "start":    start + i * step,
                    "end":      start + (i + 1) * step,
                    "word_idx": word_idx
                })

        if not master_chars:
            return self._align_timestamps_fallback(new_texts, [])

        # 构建 Target Timeline（目标字符流）
        target_chars     = []
        target_flat_text = ""
        for text in new_texts:
            for char in text:
                if char.isspace():
                    continue
                target_chars.append(char)
                target_flat_text += char

        master_flat_text = "".join(m["char"] for m in master_chars)

        # NFKC 归一化：统一全角/半角、等价字符，避免 AI 输出的字符与原文编码不同导致比对失败
        master_flat_norm = unicodedata.normalize("NFKC", master_flat_text)
        target_flat_norm = unicodedata.normalize("NFKC", target_flat_text)

        # 序列比对（autojunk=False 对中文很重要，使用归一化后的字符串比对）
        matcher = difflib.SequenceMatcher(None, master_flat_norm, target_flat_norm, autojunk=False)

        target_timestamps   = [None] * len(target_chars)
        target_word_indices = [None] * len(target_chars)

        for match in matcher.get_matching_blocks():
            a, b, size = match
            for i in range(size):
                if a + i < len(master_chars) and b + i < len(target_timestamps):
                    target_timestamps[b + i]   = {
                        "start": master_chars[a + i]["start"],
                        "end":   master_chars[a + i]["end"]
                    }
                    target_word_indices[b + i] = master_chars[a + i]["word_idx"]

        # 插值填补空缺
        first_valid_idx = next((i for i, ts in enumerate(target_timestamps) if ts is not None), -1)

        if first_valid_idx == -1:
            return self._align_timestamps_fallback(new_texts, [])

        last_end = 0.0
        if first_valid_idx > 0:
            start_time = target_timestamps[first_valid_idx]["start"]
            step = 0.01
            for i in range(first_valid_idx):
                target_timestamps[i] = {
                    "start": max(0, start_time - (first_valid_idx - i) * step),
                    "end":   max(0, start_time - (first_valid_idx - i - 1) * step)
                }
            last_end = target_timestamps[first_valid_idx - 1]["end"]
        else:
            last_end = target_timestamps[0]["start"]

        for i in range(first_valid_idx, len(target_timestamps)):
            if target_timestamps[i] is None:
                next_valid_idx = next((j for j in range(i + 1, len(target_timestamps))
                                       if target_timestamps[j] is not None), -1)
                if next_valid_idx != -1:
                    prev_end   = last_end
                    next_start = target_timestamps[next_valid_idx]["start"]
                    if next_start < prev_end:
                        next_start = prev_end + 0.1
                    count    = next_valid_idx - i
                    duration = next_start - prev_end
                    step     = duration / count
                    target_timestamps[i] = {"start": prev_end, "end": prev_end + step}
                else:
                    target_timestamps[i] = {"start": last_end, "end": last_end}
            last_end = target_timestamps[i]["end"]

        # 重组 Segment
        new_segments    = []
        current_char_idx = 0

        for text in new_texts:
            char_count = sum(1 for c in text if not c.isspace())

            if char_count > 0:
                start_idx   = current_char_idx
                end_idx     = current_char_idx + char_count - 1
                word_indices = [wi for wi in target_word_indices[start_idx:end_idx + 1] if wi is not None]
                if word_indices:
                    seg_start = cleaned_words[min(word_indices)]["start"]
                    seg_end   = cleaned_words[max(word_indices)]["end"]
                else:
                    seg_start = target_timestamps[start_idx]["start"]
                    seg_end   = target_timestamps[end_idx]["end"]
                current_char_idx += char_count
            else:
                seg_start = new_segments[-1]["end"] if new_segments else 0.0
                seg_end   = seg_start

            new_segments.append({
                'start':          seg_start,
                'end':            seg_end,
                'text':           text,
                'optimized_text': text,
                'words':          []
            })

        return new_segments

    # ──────────────────────────────────────────────────────────────
    # 内容保真
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _enforce_original_content(chunk_texts: list, chunk_text: str) -> list:
        """
        只保留 LLM 给出的断点位置，文本内容强制替换为原始转录文本。
        防止 LLM 在"零内容改动"规则下仍然偷偷改词（如 incomplete → complete）。

        原理：
        1. 用 SequenceMatcher 建立 LLM 输出字符 → 原始文本字符的映射表。
        2. 找到每个 LLM segment 末尾字符在原始文本中的对应位置。
        3. 用这些位置切割原始文本，保证输出内容 100% 来自原文。
        """
        import difflib
        import unicodedata

        def norm(s: str) -> str:
            s = unicodedata.normalize("NFKC", s)
            # CJK punctuation normalization: map fullwidth to ASCII equivalents
            # so SequenceMatcher can align despite LLM swapping punctuation forms
            s = s.replace('\u3001', ',')   # 、 → ,
            s = s.replace('\uff0c', ',')   # ， → ,
            s = s.replace('\u3002', '.')   # 。 → .
            s = s.replace('\uff01', '!')   # ！ → !
            s = s.replace('\uff1f', '?')   # ？ → ?
            s = s.replace('\uff1a', ':')   # ： → :
            s = s.replace('\uff1b', ';')   # ； → ;
            return "".join(c for c in s if not c.isspace())

        llm_combined = "".join(chunk_texts)
        llm_norm  = norm(llm_combined)
        orig_norm = norm(chunk_text)

        # 文本完全一致，直接返回
        if llm_norm == orig_norm:
            return chunk_texts

        # 差异极小（< 2 字符），也直接返回（避免无谓的映射开销）
        if abs(len(llm_norm) - len(orig_norm)) <= 1 and llm_norm == orig_norm:
            return chunk_texts

        # 用 SequenceMatcher 建立 llm_norm[i] → orig_norm[j] 的字符映射
        matcher = difflib.SequenceMatcher(None, llm_norm, orig_norm, autojunk=False)
        llm_to_orig: dict = {}
        for a, b, size in matcher.get_matching_blocks():
            for k in range(size):
                llm_to_orig[a + k] = b + k

        # 找出每个 LLM segment 末尾在 llm_norm 的字符偏移，再映射到 orig_norm
        orig_cut_positions = []
        llm_cursor = 0
        for seg_text in chunk_texts[:-1]:
            llm_cursor += len(norm(seg_text))
            # 向前后各扩展最多 20 字符，找最近的有效映射点
            orig_pos = -1
            for delta in range(20):
                for candidate in (llm_cursor - 1 - delta, llm_cursor - delta,
                                  llm_cursor - 1 + delta, llm_cursor + delta):
                    if 0 <= candidate < len(llm_norm) and candidate in llm_to_orig:
                        orig_pos = llm_to_orig[candidate] + 1  # 切在该字符之后
                        break
                if orig_pos >= 0:
                    break
            if orig_pos < 0:
                # 兜底：按比例估算
                ratio = llm_cursor / max(len(llm_norm), 1)
                orig_pos = int(ratio * len(orig_norm))
            # 跨越尾随标点：把句末标点（句号/问号/感叹号/逗号等）纳入当前段
            # 防止"congratulations." 被切成 "congratulations" + ".That is..."
            while orig_pos < len(orig_norm) and orig_norm[orig_pos] in '.!?,;:':
                orig_pos += 1
            orig_cut_positions.append(min(max(orig_pos, 0), len(orig_norm)))

        orig_cut_positions = sorted(set(orig_cut_positions))

        # 将"非空格字符偏移"转换为 chunk_text 原始字符串的索引
        orig_char_map = [i for i, c in enumerate(chunk_text) if not c.isspace()]

        result = []
        prev_raw = 0
        for cut_nonspace in orig_cut_positions:
            if cut_nonspace <= 0 or cut_nonspace > len(orig_char_map):
                continue
            raw_idx = orig_char_map[cut_nonspace - 1] + 1
            part = chunk_text[prev_raw:raw_idx].strip()
            if part:
                result.append(part)
            prev_raw = raw_idx
        last = chunk_text[prev_raw:].strip()
        if last:
            result.append(last)

        if not result:
            return chunk_texts

        changed = llm_norm != orig_norm
        if changed:
            print(f"[Splitter] 检测到 LLM 修改了原文，已强制还原为原始内容（{len(chunk_texts)} → {len(result)} 段）")
        return result

    # ──────────────────────────────────────────────────────────────
    # 去除零时长段 + 前缀重复段
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _remove_zero_and_prefix_duplicates(segments, min_duration_ms=100):
        """
        清理三类无效/重复段：
        1. 零时长（或极短）段：start == end 或 duration < min_duration_ms
        2. 前缀重复段：当前段文本是紧邻下一段文本的前缀 —— 只保留完整版
        3. 尾缀重复段：下一段文本与当前段末尾完全相同（LLM 断句重复句尾）
           —— 合并时间到当前段并删除下一段，避免导出时「两句不同译文、同一原文」
        """
        if not segments:
            return segments

        result = []
        n = len(segments)
        merged_into_prev = set()  # 已被合并到前一段的索引，不再单独加入 result

        def normalize(t):
            """去掉标点/空格后小写，用于比较"""
            return re.sub(r'[^\w]', '', (t or '').strip()).lower()

        for i, seg in enumerate(segments):
            if i in merged_into_prev:
                continue
            start = seg.get('start', 0)
            end = seg.get('end', 0)

            # 1. 去除零时长 / 极短段
            if (end - start) * 1000 < min_duration_ms:
                text = seg.get('text', '')
                print(f"[Splitter] 删除零时长段: [{start:.3f}→{end:.3f}] \"{text[:50]}...\"")
                continue

            # 2. 去除前缀重复段（当前文本是下一段文本的子串前缀）
            if i + 1 < n:
                cur_norm = normalize(seg.get('text', ''))
                next_norm = normalize(segments[i + 1].get('text', ''))
                if cur_norm and next_norm.startswith(cur_norm) and cur_norm != next_norm:
                    print(f"[Splitter] 删除前缀重复段: \"{(seg.get('text','') or '')[:50]}...\"")
                    continue

            # 3. 尾缀重复：下一段整段与当前段句尾相同 → 合并时间并丢弃下一段
            seg_out = dict(seg)
            if i + 1 < n and (i + 1) not in merged_into_prev:
                next_seg = segments[i + 1]
                cur_norm = normalize(seg_out.get('text', ''))
                next_norm = normalize(next_seg.get('text', ''))
                if next_norm and (cur_norm.endswith(next_norm) or cur_norm == next_norm):
                    seg_out['end'] = max(float(seg_out.get('end', 0)), float(next_seg.get('end', 0)))
                    merged_into_prev.add(i + 1)
                    print(f"[Splitter] 合并尾缀重复段: 下一段与句尾相同 \"{(next_seg.get('text','') or '')[:50]}...\"")

            result.append(seg_out)

        return result

    # ──────────────────────────────────────────────────────────────
    # 时间戳附加 + 启发式合并
    # ──────────────────────────────────────────────────────────────

    def _attach_origin_index(self, new_segments, original_segments):
        if not new_segments or not original_segments:
            return new_segments
        # 使用二分查找优化到 O(n log m)，避免 O(n*m) 双重循环
        import bisect
        orig_starts = [seg.get("start", 0.0) for seg in original_segments]
        for ns in new_segments:
            ns_start = ns.get("start", 0.0)
            ns_end   = ns.get("end",   0.0)
            # 找到 ns_start 在 orig_starts 中的插入位置
            pos = bisect.bisect_right(orig_starts, ns_start)
            # 候选范围：pos-1, pos（覆盖左右相邻的原始字幕）
            best_idx     = 0
            best_overlap = -1.0
            for i in range(max(0, pos - 1), min(len(original_segments), pos + 2)):
                orig_seg = original_segments[i]
                os_start = orig_seg.get("start", 0.0)
                os_end   = orig_seg.get("end",   0.0)
                overlap  = min(ns_end, os_end) - max(ns_start, os_start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_idx     = i
            ns["origin_idx"] = best_idx
            source_seg = original_segments[best_idx]
            ns["quality_score"] = source_seg.get("quality_score", 1.0)
            ns["quality_flags"] = list(source_seg.get("quality_flags", []))
            ns["is_suspicious"] = bool(source_seg.get("is_suspicious", False))
        return new_segments

    def _heuristic_merge(self, segments, max_cjk, max_en):
        """
        启发式合并：仅合并明显过于细碎的片段。
        收紧了英文的合并条件，避免把本应分开的句子强行拼在一起。
        """
        if len(segments) < 2:
            return segments

        merged  = []
        i       = 0
        # 固定极小值，只合并真正碎片化的片段，不随 max 变化。
        # min_en = 4：覆盖 Gladia 因标点预分割出的 3 词短句（"Yes." "Of course." "I think so."），
        # 使其能被连续合并进相邻字幕。Whisper 输出无标点，极少出现 ≤3 词段，影响可忽略。
        min_cjk = 6   # 中文：小于 6 字才认定为碎片（≤5 字的短句视为碎片）
        min_en  = 6   # 英文：小于 6 词才认定为碎片（≤5 词的短句视为碎片）

        while i < len(segments):
            curr     = segments[i]
            if i == len(segments) - 1:
                merged.append(curr)
                break

            next_seg     = segments[i + 1]
            curr_origin  = curr.get("origin_idx")
            next_origin  = next_seg.get("origin_idx")

            # 跨原始 segment 边界不合并（避免破坏时间轴结构）
            # 例外1：任一侧是碎片（< 1.5s 且 < 5词/8字）→ 继续走后续合并判断
            # 例外2：前一段碎片若已有句末标点，视为完整短句，不向后救援
            if curr_origin is not None and next_origin is not None and curr_origin != next_origin:
                curr_dur  = curr.get("end", 0) - curr.get("start", 0)
                next_dur  = next_seg.get("end", 0) - next_seg.get("start", 0)
                is_en_c   = self._is_latin(curr["text"])
                is_en_n   = self._is_latin(next_seg["text"])
                curr_tiny = curr_dur < 1.5 and (
                    len(re.findall(r'\w+', curr["text"])) < 5 if is_en_c
                    else len(curr["text"]) < 8
                )
                curr_tiny = curr_tiny and not re.search(
                    r'[.!?\u3002\uff01\uff1f]["\')\]]*$',
                    curr["text"].strip(),
                )
                next_tiny = next_dur < 1.5 and (
                    len(re.findall(r'\w+', next_seg["text"])) < 5 if is_en_n
                    else len(next_seg["text"]) < 8
                )
                latin_bridge = False
                latin_wait_for_next = False
                if is_en_c and is_en_n:
                    curr_word_count = len(re.findall(r'\w+', curr["text"]))
                    next_word_count = len(re.findall(r'\w+', next_seg["text"]))
                    seg_gap = next_seg.get('start', 0) - curr.get('end', 0)
                    latin_bridge = self._latin_fragment_wants_next(
                        curr["text"],
                        next_seg["text"],
                        curr_word_count,
                        next_word_count,
                        max_en,
                        seg_gap,
                    )
                    latin_wait_for_next = (
                        i + 2 < len(segments)
                        and self._latin_fragment_prefers_following(
                            curr["text"],
                            next_seg["text"],
                            curr_word_count,
                            next_word_count,
                            max_en,
                            seg_gap,
                        )
                    )
                if not (curr_tiny or (next_tiny and not latin_wait_for_next) or latin_bridge):
                    merged.append(curr)
                    i += 1
                    continue
                # next 是碎片，继续执行后续合并逻辑

            should_merge = False
            gap = next_seg['start'] - curr['end']

            if gap < 0.6:  # 间隔阈值：0.6s 以内的相邻段才考虑合并
                is_en = self._is_latin(curr['text'])

                if is_en:
                    curr_words = len(re.findall(r'\w+', curr['text']))
                    next_words = len(re.findall(r'\w+', next_seg['text']))
                    curr_dur = max(0.0, curr.get('end', 0) - curr.get('start', 0))
                    next_dur = max(0.0, next_seg.get('end', 0) - next_seg.get('start', 0))
                    tiny_curr = curr_words <= 3 and curr_dur <= _READABILITY_RESCUE_DURATION_SEC
                    tiny_next = next_words <= 3 and next_dur <= _READABILITY_RESCUE_DURATION_SEC
                    latin_bridge = self._latin_fragment_wants_next(
                        curr['text'], next_seg['text'], curr_words, next_words, max_en, gap
                    )
                    latin_wait_for_next = (
                        i + 2 < len(segments)
                        and self._latin_fragment_prefers_following(
                            curr['text'], next_seg['text'], curr_words, next_words, max_en, gap
                        )
                    )
                    if curr_words + next_words <= max_en:
                        # 只在以下更严格的条件下才合并：
                        # 1. 某一侧极短（< min_en 词）
                        if (curr_words < min_en or next_words < min_en) and not latin_wait_for_next:
                            should_merge = True
                        elif latin_bridge:
                            should_merge = True
                        # 2. 极短片段 + 极小间隔（几乎可以认定是同一句）
                        elif next_words <= 2 and gap < 0.15 and not latin_wait_for_next:
                            should_merge = True
                        # 注意：移除了"末尾无标点就合并"的规则，ASR 输出本来就少标点
                    else:
                        # 超出 max_en 时的特例：
                        # 当前段极短（≤ 3 词）且前一段太长无法接收 → 强制并入下一段
                        # 防止"Tonight between wanting"这类孤儿短段独立成行
                        if curr_words <= 3 and curr_words + next_words <= max_en:
                            should_merge = True
                        elif (
                            (tiny_curr or (tiny_next and not latin_wait_for_next))
                            and curr_words + next_words <= max_en + _READABILITY_RESCUE_SLACK_EN
                        ):
                            should_merge = True
                        elif latin_bridge:
                            should_merge = True

                else:
                    curr_len = len(curr['text'])
                    next_len = len(next_seg['text'])
                    curr_dur = max(0.0, curr.get('end', 0) - curr.get('start', 0))
                    next_dur = max(0.0, next_seg.get('end', 0) - next_seg.get('start', 0))
                    tiny_curr = curr_len <= 4 and curr_dur <= _READABILITY_RESCUE_DURATION_SEC
                    tiny_next = next_len <= 4 and next_dur <= _READABILITY_RESCUE_DURATION_SEC
                    if curr_len + next_len <= max_cjk:
                        if curr_len < min_cjk or next_len < min_cjk:
                            should_merge = True
                        elif next_len <= 2 and gap < 0.15:
                            should_merge = True
                    elif (
                        (tiny_curr or tiny_next)
                        and curr_len + next_len <= max_cjk + _READABILITY_RESCUE_SLACK_CJK
                    ):
                        should_merge = True

            if should_merge:
                new_text = f"{curr['text']} {next_seg['text']}".replace("  ", " ").strip()
                new_seg  = {
                    'start':          curr['start'],
                    'end':            next_seg['end'],
                    'text':           new_text,
                    'optimized_text': new_text,
                    'words':          curr.get('words', []) + next_seg.get('words', []),
                    'origin_idx':     curr_origin if curr_origin is not None else next_origin
                }
                segments[i + 1] = new_seg
                i += 1
            else:
                merged.append(curr)
                i += 1

        return merged

    # ──────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup_text(text: str) -> str:
        """
        清理断句结果中残留的格式问题：
        - 去除前置标点/空格（Gladia 句点跑到段首的情况）
        - 规范化段内残留的 Gladia " . " 格式为普通空格
        - 压缩多余空格
        """
        # 去除前置标点（含句点、感叹号、问号、逗号等）
        text = re.sub(r'^[\s.!?,;:]+', '', text)
        # 规范化段内残留的 " . "（Gladia 格式）
        text = re.sub(r'\s\.\s', ' ', text)
        # 压缩多余空格
        text = re.sub(r'\s{2,}', ' ', text)
        return text.strip()

    @staticmethod
    def _strip_punctuation(text: str) -> str:
        """移除标点符号，让断句 LLM 完全依赖语义而非句号来判断断点。保留空格和字母数字。"""
        return re.sub(r'[^\w\s]', '', text, flags=re.UNICODE).strip()

    @staticmethod
    def _latin_fragment_wants_next(curr_text: str, next_text: str,
                                   curr_words: int, next_words: int,
                                   max_en: int, gap: float) -> bool:
        """
        Rescue short Latin-script fragments using only generic punctuation cues.
        """
        if gap >= 0.6:
            return False
        if curr_words + next_words > max_en + _READABILITY_RESCUE_SLACK_EN:
            return False

        curr = (curr_text or "").strip()
        nxt = (next_text or "").strip()
        if not curr or not nxt:
            return False
        if _STRONG_PUNCT_END_RE.search(curr):
            return False

        return bool(_WEAK_PUNCT_END_RE.search(curr) and curr_words <= max(8, max_en // 2))

    @staticmethod
    def _latin_fragment_prefers_following(curr_text: str, next_text: str,
                                          curr_words: int, next_words: int,
                                          max_en: int, gap: float) -> bool:
        """
        Avoid pulling a weakly punctuated short fragment into the previous line
        when another following line is available.
        """
        if gap >= 0.6:
            return False
        if curr_words + next_words > max_en + _READABILITY_RESCUE_SLACK_EN:
            return False

        nxt = (next_text or "").strip()
        if not nxt:
            return False
        if next_words > max(8, max_en // 2):
            return False
        return bool(_WEAK_PUNCT_END_RE.search(nxt))

    def _is_latin(self, text):
        """判断文本是否属于拉丁语系（英/西/法/葡/意/德等），用于选择单词数还是字符数限制。"""
        latin_count = sum(1 for c in text if 'a' <= c.lower() <= 'z')
        return latin_count > len(text) * 0.4

    def _is_english(self, text):
        """判断文本是否为英语（纯 ASCII 拉丁字符，不含西语/法语/葡语特殊字符）。
        西语/法语/葡语含有大量 é ñ ç ã 等重音字符，可借此与英语区分。"""
        if not self._is_latin(text):
            return False
        # 若含有非 ASCII 拉丁字符（重音字母），视为其他拉丁语系而非英语
        non_ascii_latin = sum(1 for c in text if ord(c) > 127 and c.isalpha())
        return non_ascii_latin <= len(text) * 0.05

    def _contains_chinese(self, text):
        return any('\u4e00' <= c <= '\u9fff' for c in text)


subtitle_splitter = SubtitleSplitter()
