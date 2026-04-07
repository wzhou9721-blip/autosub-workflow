# -*- coding: utf-8 -*-
from typing import List, Dict, Any
import re

class TextSplitter:
    """ Utility class for splitting subtitles based on constraints """
    
    # Punctuation priority for splitting
    # High priority: Sentence terminators
    HIGH_PRIORITY_PUNCT = ['。', '！', '？', '!', '?', '\n']
    # Medium priority: Clause terminators
    MED_PRIORITY_PUNCT = ['，', '；', '：', '、', ',', ';', ':']
    _SAFE_LATIN_START_WORDS = {
        "a", "an", "the", "and", "but", "or", "so", "if", "when", "while",
        "because", "that", "to", "of", "in", "on", "at", "for", "from",
        "with", "by", "is", "are", "was", "were", "be", "been", "being",
        "as", "into", "onto", "over", "under",
    }
    
    @staticmethod
    def split_subtitle(
        text_cn: str, 
        text_origin: str, 
        start: float, 
        end: float, 
        max_chars: int
    ) -> List[Dict[str, Any]]:
        """
        Split a subtitle into multiple segments to satisfy the max_chars constraint.
        Attempts to preserve sentence structure and distributes original text and time proportionally.
        """
        if not text_cn:
            return []
            
        if len(text_cn) <= max_chars:
            return [{
                "start": start,
                "end": end,
                "text": text_origin,
                "translated_text": text_cn
            }]
            
        segments = []
        remaining_duration = end - start
        current_start = start
        
        # We need to split the Chinese text into chunks <= max_chars
        cn_chunks = TextSplitter._recursive_split(text_cn, max_chars)
        
        total_cn_len = len(text_cn)
        accumulated_cn_len = 0
        last_origin_idx = 0
        
        for i, chunk in enumerate(cn_chunks):
            chunk_len = len(chunk)
            accumulated_cn_len += chunk_len
            
            # 1. Calculate End Time
            if i == len(cn_chunks) - 1:
                chunk_end = end
            else:
                progress = accumulated_cn_len / total_cn_len
                chunk_end = start + (remaining_duration * progress)
            
            # 2. Calculate Original Text Segment
            if i == len(cn_chunks) - 1:
                chunk_origin = text_origin[last_origin_idx:]
            else:
                progress = accumulated_cn_len / total_cn_len
                target_idx = int(len(text_origin) * progress)
                split_idx = TextSplitter._adjust_split_point(text_origin, target_idx, last_origin_idx)
                chunk_origin = text_origin[last_origin_idx:split_idx]
                last_origin_idx = split_idx
                
            segments.append({
                "start": current_start,
                "end": chunk_end,
                "text": chunk_origin.strip(),
                "translated_text": chunk.strip()
            })
            
            current_start = chunk_end
            
        return segments

    @staticmethod
    def _recursive_split(text: str, max_chars: int) -> List[str]:
        if len(text) <= max_chars:
            return [text]
            
        # Try to find a split point
        # Look backwards from max_chars
        best_split = -1
        
        # Search range: from max_chars down to max_chars // 2 (don't make chunks too small if possible)
        search_end = max_chars
        search_start = max(1, max_chars // 2)
        
        # 1. Check High Priority Punctuation
        for i in range(search_end, search_start, -1):
            if i < len(text) and text[i-1] in TextSplitter.HIGH_PRIORITY_PUNCT:
                best_split = i
                break
        
        # 2. Check Medium Priority Punctuation
        if best_split == -1:
            for i in range(search_end, search_start, -1):
                if i < len(text) and text[i-1] in TextSplitter.MED_PRIORITY_PUNCT:
                    best_split = i
                    break
                    
        # 3. Fallback: Just cut at max_chars
        if best_split == -1:
            best_split = max_chars
            
        return [text[:best_split]] + TextSplitter._recursive_split(text[best_split:], max_chars)

    @staticmethod
    def _adjust_split_point(text: str, target_idx: int, min_idx: int) -> int:
        """ Adjust split point in source text to respect word boundaries (spaces) """
        if target_idx >= len(text):
            return len(text)
        if target_idx <= min_idx:
            return min_idx + 1 # Ensure at least 1 char progress

        boundary_candidates = []

        for i in range(target_idx, len(text)):
            if text[i].isspace():
                boundary_candidates.append(i + 1)
                break

        for i in range(target_idx - 1, min_idx - 1, -1):
            if text[i].isspace():
                split_idx = i + 1
                if split_idx > min_idx:
                    boundary_candidates.append(split_idx)
                break

        if boundary_candidates:
            return min(boundary_candidates, key=lambda idx: (abs(idx - target_idx), idx))

        punctuation_candidates = []
        punctuation_chars = set(",.;:!?，。！？；：、")
        for i in range(target_idx, len(text)):
            if text[i] in punctuation_chars:
                punctuation_candidates.append(i + 1)
                break

        for i in range(target_idx - 1, min_idx - 1, -1):
            if text[i] in punctuation_chars:
                split_idx = i + 1
                if split_idx > min_idx:
                    punctuation_candidates.append(split_idx)
                break

        if punctuation_candidates:
            return min(punctuation_candidates, key=lambda idx: (abs(idx - target_idx), idx))

        return target_idx

    @staticmethod
    def _distribute_space_delimited_origin(origin: str, weights: List[int]) -> List[str] | None:
        token_groups = re.findall(r"\S+\s*", origin)
        if len(token_groups) < 2 or len(token_groups) < len(weights):
            return None

        total_weight = sum(weights)
        if total_weight <= 0:
            return None

        cumulative_lengths = []
        running_length = 0
        for token in token_groups:
            running_length += len(token)
            cumulative_lengths.append(running_length)

        total_length = cumulative_lengths[-1]
        result = []
        last_token_idx = 0
        cumulative_weight = 0

        for i, weight in enumerate(weights):
            remaining_segments = len(weights) - i - 1
            if i == len(weights) - 1:
                result.append("".join(token_groups[last_token_idx:]).strip())
                break

            cumulative_weight += weight
            target_length = total_length * cumulative_weight / total_weight
            candidate_min = last_token_idx + 1
            candidate_max = len(token_groups) - remaining_segments
            if candidate_min > candidate_max:
                return None

            split_token_idx = min(
                range(candidate_min, candidate_max + 1),
                key=lambda idx: (
                    abs(cumulative_lengths[idx - 1] - target_length),
                    0 if token_groups[idx - 1].rstrip().endswith((".", "!", "?", ",", ";", ":", "，", "。", "！", "？", "；", "：", "、")) else 1,
                    idx,
                ),
            )
            result.append("".join(token_groups[last_token_idx:split_token_idx]).strip())
            last_token_idx = split_token_idx

        return result

    @staticmethod
    def _has_broken_latin_boundary(segment_texts: List[str]) -> bool:
        for left, right in zip(segment_texts, segment_texts[1:]):
            left_clean = re.sub(r"[^A-Za-z]+$", "", (left or "").strip())
            right_clean = re.sub(r"^[^A-Za-z]+", "", (right or "").strip())
            left_match = re.search(r"([A-Za-z]{6,})$", left_clean)
            right_match = re.match(r"([A-Za-z]{1,4})(?:[^A-Za-z]|$)", right_clean)
            if not left_match or not right_match:
                continue

            right_word = right_match.group(1).lower()
            if right_word in TextSplitter._SAFE_LATIN_START_WORDS:
                continue
            if right_word[:1].islower():
                return True

        return False

    @staticmethod
    def needs_origin_redistribution(origin: str, segment_texts: List[str]) -> bool:
        clean_origin = (origin or "").strip()
        clean_segments = [(text or "").strip() for text in segment_texts]
        if len(clean_segments) < 2 or not clean_origin:
            return False
        if any(not text for text in clean_segments):
            return True

        normalized_origin = re.sub(r"\s+", "", clean_origin)
        normalized_joined = "".join(re.sub(r"\s+", "", text) for text in clean_segments)
        if normalized_joined != normalized_origin:
            return True

        return TextSplitter._has_broken_latin_boundary(clean_segments)

    @staticmethod
    def redistribute_segment_texts_by_weights(
        origin: str,
        segments: List[Dict[str, Any]],
        weight_key: str = "cn",
    ) -> List[Dict[str, Any]]:
        if len(segments) < 2 or not (origin or "").strip():
            return segments

        segment_texts = [seg.get("text", "") for seg in segments]
        if not TextSplitter.needs_origin_redistribution(origin, segment_texts):
            return segments

        redistributed = TextSplitter.distribute_origin_by_weights(
            origin.strip(),
            [len((seg.get(weight_key) or "").strip()) for seg in segments],
        )
        for i, seg in enumerate(segments):
            if i < len(redistributed) and redistributed[i]:
                seg["text"] = redistributed[i]

        return segments

    @staticmethod
    def distribute_origin_by_weights(origin: str, weights: List[int]) -> List[str]:
        """
        按权重（如各段译文长度）将整段原文拆成多段，尽量在空格处断句。
        用于溢出修复时：LLM 返回的多段若原文相同，则按译文长度比例重新分配原文。
        """
        if not origin or not weights:
            return [origin] if weights else []
        total = sum(weights)
        if total <= 0:
            return [origin.strip()] + [""] * (len(weights) - 1)

        distributed_by_tokens = TextSplitter._distribute_space_delimited_origin(origin, weights)
        if distributed_by_tokens:
            return distributed_by_tokens

        n = len(weights)
        result = []
        last = 0
        cumulative_weight = 0
        for i in range(n):
            if i == n - 1:
                result.append(origin[last:].strip())
                break
            cumulative_weight += weights[i]
            progress = cumulative_weight / total
            target_idx = int(len(origin) * progress)
            split_idx = TextSplitter._adjust_split_point(origin, target_idx, last)
            result.append(origin[last:split_idx].strip())
            last = split_idx
        return result

def clean_punctuation_text(text: str) -> str:
    """
    去除译文中的纯分隔性标点，保留有语义/语气作用的标点。
    
    删除（替换为空格）：逗号、顿号、冒号、分号 —— 纯分隔，无语义
    删除（句末直接删）：句号 —— 句末不需要，句中替换为空格
    保留：问号、感叹号、省略号、破折号、引号、括号、书名号 —— 携带语气或语义
    """
    if not text:
        return ""
        
    # 1. 句末句号 → 直接删除（不留空格）
    text = re.sub(r'[。\.]\s*$', '', text)
    
    # 2. 纯分隔性标点 → 替换为空格（防止句子糊在一起）
    # 仅包括：中英文逗号、顿号、句号、冒号、分号
    text = re.sub(r'[，,、。：；\.:;]', ' ', text)
    
    # 3. 压缩多余空格
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text
