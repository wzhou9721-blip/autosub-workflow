# -*- coding: utf-8 -*-
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from app.common.config import cfg
from app.common.runtime_state import OPTIMIZE_RESUME_FILE, load_json_file, save_json_atomic, safe_unlink
from app.common.utils import fix_timestamp_overlaps
from app.core.llm import llm_manager
from app.core.search import search_manager

# 断点续传临时文件路径
_RESUME_FILE = OPTIMIZE_RESUME_FILE

class SubtitleOptimizer:
    """
    使用 LLM 对转录后的字幕进行初步优化（纠错、标点、自然度）
    """
    def optimize(self, segments, context="", progress_callback=None, task_scope=None):
        """
        优化字幕片段
        segments: list of dicts with 'text'
        context: 视频语境描述
        progress_callback: 接收 (current_batch, total_batches) 的回调函数
        """
        if not segments:
            return []

        # 预先检查时间戳重叠
        segments = fix_timestamp_overlaps(segments)

        # 断点续传：尝试加载上次中断时保存的优化进度
        # 匹配条件：segments 数量相同且第一条原文一致，才认为是同一个任务
        try:
            resume_data = load_json_file(_RESUME_FILE)
            if resume_data:
                if (resume_data.get("total") == len(segments) and
                        resume_data.get("first_text") == segments[0].get("text", "")):
                    saved_map = {int(k): v for k, v in resume_data.get("optimized_map", {}).items()}
                    resumed = 0
                    for i, s in enumerate(segments):
                        if i in saved_map and not s.get("optimized_text"):
                            s["optimized_text"] = saved_map[i]
                            resumed += 1
                    if resumed > 0:
                        print(f"[Optimizer] 断点续传：已恢复 {resumed}/{len(segments)} 条优化结果")
        except Exception as e:
            print(f"[Optimizer] 断点续传加载失败（忽略）: {e}")
            
        # Check API Key
        if not cfg.optimize_api_key.value:
            print("[Optimizer] 未配置优化 API Key (Optimize)，跳过优化步骤。")
            return segments

        # 0. 联网搜索增强背景知识
        knowledge_context = ""
        if cfg.enable_web_search.value and context:
            search_query = f"{context} 相关的专业术语、人名、地名、核心概念"
            raw_knowledge = search_manager.search_knowledge(search_query)
            # 限制注入长度，防止搜索结果过长导致 token 超支
            knowledge_context = raw_knowledge[:500] if raw_knowledge else ""

        model = cfg.optimize_model.value or "gpt-3.5-turbo"
        # 按估算 token 数动态切批：每条文本约 30 token，每批不超过 1800 token
        # 这样遇到长句不会因上下文超限导致后半批质量下滑
        avg_tokens_per_seg = max(10, sum(len(s.get('text', '')) for s in segments[:20]) // max(1, min(20, len(segments))))
        batch_size = max(10, min(60, 1800 // avg_tokens_per_seg))
        max_workers = cfg.llm_max_workers.value
        
        # ── 规则引擎预筛选：纯数字/极短片段直接跳过 LLM ──
        prefilter_skipped = 0
        for s in segments:
            text = s.get('text', '').strip()
            # Pure numbers, timestamps, or very short fragments (≤3 chars)
            if (len(text) <= 3 or
                    text.replace('.', '').replace(',', '').replace(':', '').replace('-', '').replace(' ', '').isdigit()):
                s['optimized_text'] = s['text']
                prefilter_skipped += 1
        if prefilter_skipped > 0:
            print(f"[Optimizer] 预筛选跳过 {prefilter_skipped} 条（纯数字/极短片段）")

        total_segments = len(segments)
        batches = [segments[i:i + batch_size] for i in range(0, total_segments, batch_size)]
        total_batches = len(batches)
        
        def process_batch(batch_data):
            batch_idx, batch = batch_data
            subtitle_text = ""
            start_idx = batch_idx * batch_size
            low_conf_ids = []
            for i, s in enumerate(batch):
                seg_id = start_idx + i
                subtitle_text += f"[{seg_id}] {s['text']}\n"
                if s.get("is_suspicious"):
                    low_conf_ids.append(seg_id)

            # Build adjacent-batch context for cross-batch consistency
            adjacent_context = ""
            if batch_idx > 0:
                prev_batch = batches[batch_idx - 1]
                prev_lines = [s.get('text', '') for s in prev_batch[-2:]]
                adjacent_context += f"- Previous batch (last 2): {' | '.join(prev_lines)}\n"
            if batch_idx < len(batches) - 1:
                next_batch = batches[batch_idx + 1]
                next_lines = [s.get('text', '') for s in next_batch[:2]]
                adjacent_context += f"- Next batch (first 2): {' | '.join(next_lines)}\n"

            prompt = f"""
# Task
Correct ASR errors (typos, punctuation) in the subtitles below.

# Rules
1. **NO FACTUAL CHANGES**: Do NOT replace names, places, or orgs using external knowledge. Keep them as transcribed.
2. **NO ADDED CONTENT**: Do NOT add any new sentences, clauses, or words that are not in the original. Only fix typos and punctuation. The output must not contain words or phrases that do not appear in the input.
3. **NO GUESSING**: If unsure about a name/place, keep the original token.
4. **STAY IN ORIGINAL LANGUAGE**: Do NOT translate. If input is English, output MUST be English.
5. **FORMAT**: Keep [ID] prefix. One line per ID.
6. **NO EXPLANATION**: Output ONLY the corrected lines.
7. **NUMBERS/UNITS**: Keep numbers, dates, currency, units, symbols (°C, km/h, %, #, @, &) and mathematical expressions exactly as is.
8. **CONSISTENCY**: Keep the same proper noun forms throughout this batch.
9. **QUESTION/NEGATION**: Do NOT change question or negation intent.
10. **MIXED LANGUAGE**: Preserve mixed-language tokens, technical terms, abbreviations (e.g. FIFA, CEO, km) and brand names as-is.
11. **PUNCTUATION STRENGTH**: Keep the original tone (question/exclamation) if present.
12. **NO CASE CHANGE**: Do NOT change capitalization of proper nouns or acronyms unless it is clearly a typo.
13. **NO ADDITIONS**: Do NOT add words, explanations, or context that were not in the original.
14. **NO PUNCTUATION ADDITION**: Do NOT add sentence-ending punctuation (. ! ?) to segments that have none. Each segment may be a mid-sentence fragment from a longer utterance — adding a period would create false sentence boundaries. Only CORRECT existing punctuation (e.g. fix a misplaced comma), never INSERT new terminal punctuation.
15. **LOW-CONFIDENCE IDS**: For IDs listed in Low confidence IDs, be EXTRA conservative: keep the original wording, do not complete fragments, do not smooth syntax, do not infer missing words, and prefer leaving awkward text unchanged unless the typo is obvious.

# Context
- Video: {context if context else "None"}
- Knowledge: {knowledge_context if knowledge_context else "None"}
{adjacent_context}
- Low confidence IDs: {low_conf_ids if low_conf_ids else "None"}
# Subtitles
{subtitle_text}
"""
            try:
                result_text = llm_manager.call_llm(
                    messages=[
                        {"role": "user", "content": prompt}
                    ],
                    model=model,
                    temperature=0,
                    config_prefix="optimize",
                    max_tokens=2000,
                    task_scope=task_scope
                )
                return batch_idx, result_text
            except Exception as e:
                print(f"[Optimizer] 优化第 {batch_idx + 1} 批时出错: {e}")
                return batch_idx, None

        processed_batches = 0
        # 4. 识别需要处理的批次（跳过已有 optimized_text 的）
        batches_to_process = []
        for i, batch in enumerate(batches):
            needs_processing = any('optimized_text' not in s or not s['optimized_text'] for s in batch)
            if needs_processing:
                batches_to_process.append((i, batch))
            else:
                # 如果不需要处理，也要增加已处理计数
                processed_batches += 1
                if progress_callback:
                    progress_callback(processed_batches, total_batches)

        if not batches_to_process:
            print("[Optimizer] 所有字幕已有优化结果，跳过优化步骤。")
            return segments

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_batch = {executor.submit(process_batch, batch_data): batch_data[0] for batch_data in batches_to_process}
            
            for future in as_completed(future_to_batch):
                batch_idx, result_text = future.result()
                batch = batches[batch_idx]
                start_idx = batch_idx * batch_size
                
                if result_text:
                    # 解析结果
                    optimized_lines = result_text.split('\n')
                    optimized_map = {}
                    for line in optimized_lines:
                        if ']' in line:
                            try:
                                idx_part, text_part = line.split(']', 1)
                                idx = int(idx_part.replace('[', '').strip())
                                optimized_map[idx] = text_part.strip()
                            except (ValueError, IndexError):
                                continue
                    
                    # 更新当前 batch 的 segments
                    for i, s in enumerate(batch):
                        global_idx = start_idx + i
                        if global_idx in optimized_map:
                            new_text = optimized_map[global_idx]
                            original = s['text']
                            if self._is_english(original) and self._contains_chinese(new_text):
                                s['optimized_text'] = original
                            else:
                                if not self._is_safe_optimized_text(original, new_text, bool(s.get("is_suspicious"))):
                                    ratio = SequenceMatcher(None, original, new_text).ratio()
                                    print(f"[Optimizer] 改写过度 (ratio={ratio:.2f})，回退原文: [{global_idx}]")
                                    s['optimized_text'] = original
                                else:
                                    s['optimized_text'] = new_text
                        else:
                            s['optimized_text'] = s['text']
                else:
                    # Fallback
                    for s in batch:
                        s['optimized_text'] = s['text']
                
                processed_batches += 1
                if progress_callback:
                    progress_callback(processed_batches, total_batches)

                # 每批完成后保存断点续传文件
                try:
                    resume_map = {
                        i: s.get("optimized_text", "")
                        for i, s in enumerate(segments)
                        if s.get("optimized_text")
                    }
                    save_json_atomic(_RESUME_FILE, {
                        "total": len(segments),
                        "first_text": segments[0].get("text", "") if segments else "",
                        "optimized_map": resume_map
                    })
                except Exception:
                    pass

        print("[Optimizer] 字幕优化完成。")
        # 任务完成后清除断点续传临时文件
        try:
            safe_unlink(_RESUME_FILE)
        except Exception:
            pass
        # 再次修复时间戳，防止处理过程引入问题
        segments = fix_timestamp_overlaps(segments)
        return segments

    def _is_english(self, text):
        # 简单判断：如果文本中包含大量英文字符且几乎没有中文字符
        english_char_count = sum(1 for c in text if 'a' <= c.lower() <= 'z')
        return english_char_count > len(text) * 0.5

    def _contains_chinese(self, text):
        for char in text:
            if '\u4e00' <= char <= '\u9fff':
                return True
        return False

    @staticmethod
    def _tokenize_text(text):
        return re.findall(r"[A-Za-z0-9']+|[\u3400-\u4dbf\u4e00-\u9fff]", (text or "").lower())

    @staticmethod
    def _strip_punctuation_only(text):
        return re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]+", "", (text or "").lower())

    def _has_added_tokens(self, original, new_text):
        original_tokens = set(self._tokenize_text(original))
        new_tokens = self._tokenize_text(new_text)
        return any(token not in original_tokens for token in new_tokens)

    def _is_safe_optimized_text(self, original, new_text, suspicious: bool):
        if not new_text.strip():
            return False

        ratio = SequenceMatcher(None, original, new_text).ratio()
        if not suspicious:
            return ratio >= 0.7

        if self._strip_punctuation_only(original) == self._strip_punctuation_only(new_text):
            return True

        if self._has_added_tokens(original, new_text):
            return False

        if len(new_text.strip()) > len(original.strip()) + 6:
            return False

        return ratio >= 0.84

# 单例
subtitle_optimizer = SubtitleOptimizer()
