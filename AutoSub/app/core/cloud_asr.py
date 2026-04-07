# -*- coding: utf-8 -*-
import os
import logging
from openai import OpenAI
from app.common.config import cfg
from app.common.utils import fix_timestamp_overlaps
from app.core.llm import interpret_openai_error

class CloudASR:
    """
    Handles Cloud ASR transcription using OpenAI-compatible API
    """
    def __init__(self):
        self.api_key = cfg.asr_api_key.value
        self.base_url = cfg.asr_base_url.value
        self.model_name = cfg.asr_model.value
        
        # Fallback to defaults if not set
        if not self.api_key:
            logging.warning("[CloudASR] ASR API Key not set!")
            raise ValueError("未配置 Cloud ASR API Key，无法进行云端转录")
            
        if not self.base_url:
            self.base_url = "https://api.openai.com/v1"
        else:
            self.base_url = self.base_url.strip()
            
        # Ensure base_url does not end with /v1 if we append path, 
        # but OpenAI client usually handles base_url cleanly.
        # However, some providers like Groq need https://api.groq.com/openai/v1
        
        if not self.model_name:
            self.model_name = "whisper-1"
        else:
            self.model_name = self.model_name.strip() # Strip newline/spaces

        # Check for valid base_url (no empty string if api_key is set)
        client_base = self.base_url if self.base_url else None
        
        # Debugging Info
        masked_key = self.api_key[:4] + "***" + self.api_key[-4:] if self.api_key and len(self.api_key) > 8 else "***"
        print(f"[CloudASR] Init Client - BaseURL: {client_base}, APIKey: {masked_key}")
        
        # Enforce strict timeout and no retries to save costs
        self.client = OpenAI(
            api_key=self.api_key, 
            base_url=client_base,
            timeout=180.0
        )

    def transcribe(self, audio_path, language=None, word_timestamps=True, prompt=None, temperature=0):
        """
        Execute cloud transcription
        """
        if not os.path.exists(audio_path):
            logging.error(f"[CloudASR] Audio file not found: {audio_path}")
            return None

        print(f"[CloudASR] Starting cloud transcription for: {audio_path}")
        print(f"[CloudASR] Using model: {self.model_name}, Word Timestamps: {word_timestamps}")

        try:
            with open(audio_path, "rb") as audio_file:
                # Prepare parameters
                params = {
                    "file": audio_file,
                    "model": self.model_name,
                    "response_format": "verbose_json",
                    "temperature": temperature
                }
                
                # Add prompt if provided
                if prompt:
                    # Truncate prompt to avoid API errors (limit is 224 tokens)
                    # Reverted to 224 chars as 500 chars caused issues/encoding errors for some users/APIs
                    if len(prompt) > 224:
                        print(f"[CloudASR] Prompt too long, truncating to 224 chars: {prompt[:224]}...")
                        params["prompt"] = prompt[:224]
                    else:
                        params["prompt"] = prompt
                
                # Add word timestamp granularity if requested
                if word_timestamps:
                    params["timestamp_granularities"] = ["word", "segment"]
                else:
                    params["timestamp_granularities"] = ["segment"]
                
                # Add language if provided (and not Auto)
                if language and language.lower() != "auto":
                    # Whisper API uses ISO-639-1 codes (e.g. 'en', 'zh')
                    # We assume the caller passes the correct code or we map it?
                    # Transcriber.run passes 'zh', 'en' etc. from WHISPER_LANGUAGE_CODES
                    params["language"] = language

                # Call API
                # Note: openai>=1.0.0 uses client.audio.transcriptions.create
                try:
                    response = self.client.audio.transcriptions.create(**params, timeout=180)
                except Exception as api_err:
                    error_msg = str(api_err).lower()
                    # 部分提供商不支持 timestamp_granularities，自动降级重试
                    if "timestamp_granularities" in error_msg or "argument" in error_msg or "parameter" in error_msg:
                        print(f"[CloudASR] 提供商不支持词级时间戳，降级重试...")
                        if "timestamp_granularities" in params:
                            del params["timestamp_granularities"]
                            response = self.client.audio.transcriptions.create(**params, timeout=180)
                        else:
                            raise api_err
                    else:
                        raise api_err

                return self._process_response(response, word_timestamps)

        except Exception as e:
            readable_msg = interpret_openai_error(e, context="云端转录")
            print(f"[CloudASR] {readable_msg}")
            logging.error(f"[CloudASR] Error: {e}")
            raise Exception(readable_msg)

    def _process_response(self, response, need_words):
        """
        Process API response and align words to segments if necessary
        """
        # Extract segments and words
        # Note: OpenAI SDK v1.x returns objects, not dicts usually.
        # But let's handle it safely.
        
        segments = getattr(response, 'segments', [])
        words = getattr(response, 'words', [])
        text = getattr(response, 'text', "")
        
        # If no segments but we have text, create a single segment?
        if not segments and text:
            segments = [{"start": 0.0, "end": getattr(response, 'duration', 0.0), "text": text}]
            
        # Convert segments to list of dicts if they are objects
        processed_segments = []
        for seg in segments:
            seg_dict = {
                "start": getattr(seg, 'start', 0.0),
                "end": getattr(seg, 'end', 0.0),
                "text": getattr(seg, 'text', "").strip()
            }
            # If segment already has words, use them
            if hasattr(seg, 'words') and seg.words:
                seg_dict["words"] = [
                    {
                        "word": getattr(w, 'word', ""),
                        "start": getattr(w, 'start', 0.0),
                        "end": getattr(w, 'end', 0.0)
                    } for w in seg.words
                ]
            processed_segments.append(seg_dict)

        processed_segments = self._filter_hallucinations(processed_segments)

        # 先对齐词级时间戳（需要在 fix_timestamp_overlaps 之前，
        # 这样才能用词级时间戳修正 segment 级时间戳，避免截断压缩）
        if need_words and words and processed_segments and "words" not in processed_segments[0]:
            print("[CloudASR] Aligning root-level words to segments...")
            self._align_words_to_segments(processed_segments, words)

        # 用词级时间戳重建 segment 的 start/end（比 Whisper segment 级更精准）
        for seg in processed_segments:
            seg_words = seg.get("words", [])
            if seg_words:
                seg["start"] = seg_words[0]["start"]
                seg["end"]   = seg_words[-1]["end"]

        # 修复时间戳重叠（在词级修正之后执行，避免截断产生异常短时长）
        processed_segments = fix_timestamp_overlaps(processed_segments)
        return processed_segments

    def _filter_hallucinations(self, segments):
        if not segments:
            return segments
        if not cfg.transcription_hallucination_filter.value:
            return segments

        hallucination_keywords = [
            "请不吝点赞", "订阅", "转发", "打赏", "支持明镜", 
            "字幕由", "网易云音乐", "感谢观看", "谢谢大家", "关注", "点赞"
        ]
        filtered = []
        removed = 0
        for seg in segments:
            text = seg.get("text", "")
            if any(k in text for k in hallucination_keywords):
                removed += 1
                continue
            # 移除了"时长过短+字数过多"的启发式过滤：
            # Whisper 的 segment 级时间戳有时精度不足，导致真实语音的时长被
            # 错误地计算为极短值，该规则会把正常语音误判为幻觉造成漏字。
            # 幻觉过滤依赖上方的关键词黑名单，以及本地模式下的 no_speech_prob
            # 和 avg_logprob 置信度过滤（cloud 模式暂无这两个指标）。
            filtered.append(seg)

        if removed:
            print(f"[CloudASR] 过滤疑似幻觉片段: {removed} 条")
        return filtered

    def _align_words_to_segments(self, segments, words):
        """
        Align a flat list of words to segments based on timestamps.
        Logic referenced from 002/js/api.js (rootWordBuckets)
        """
        # Convert words to dicts if needed
        word_list = []
        for w in words:
            word_list.append({
                "word": getattr(w, 'word', ""),
                "start": getattr(w, 'start', 0.0),
                "end": getattr(w, 'end', 0.0)
            })
            
        # Sort words by start time just in case
        word_list.sort(key=lambda x: x["start"])
        
        # Bucket sort / distribution
        for seg in segments:
            seg_start = seg["start"]
            seg_end = seg["end"]
            
            # Find words that belong to this segment
            # We use a loose boundary to catch edge cases
            seg_words = []
            
            for w in word_list:
                # Logic: Word center point inside segment? Or overlap?
                # 002 logic: word.start >= seg.start - tolerance && word.end <= seg.end + tolerance
                # Let's use simple overlap or containment
                
                # Center point
                w_center = (w["start"] + w["end"]) / 2
                
                if seg_start <= w_center <= seg_end:
                    seg_words.append(w)
                elif w["start"] >= seg_start and w["end"] <= seg_end:
                    # Fully contained
                    if w not in seg_words:
                        seg_words.append(w)
            
            seg["words"] = seg_words
