# -*- coding: utf-8 -*-
import os
import time
from app.common.cuda_setup import setup_cuda_paths

# 在导入 faster_whisper 之前设置 CUDA DLL 路径
setup_cuda_paths()

from faster_whisper import WhisperModel
from app.common.utils import extract_audio, fix_timestamp_overlaps
from app.common.config import cfg, MODEL_PATH
from app.core.project import project_manager

# 语言名称到代码的映射 (Whisper 要求使用 ISO 639-1 代码)
WHISPER_LANGUAGE_CODES = {
    "Chinese": "zh",
    "English": "en",
    "Japanese": "ja",
    "Korean": "ko",
    "French": "fr",
    "German": "de",
    "Spanish": "es",
    "Russian": "ru",
    "Portuguese": "pt",
    "Italian": "it",
    "Thai": "th",
    "Vietnamese": "vi",
    "Indonesian": "id",
    "Arabic": "ar",
    "Hindi": "hi",
    "Catalan": "ca"
}

class Transcriber:
    """ 
    负责语音识别的核心逻辑 
    """
    def __init__(self):
        self.model = None
        self.current_model_size = None

    def _get_model(self, model_size, device="cpu"):
        """ 加载或获取已加载的模型，支持热切换：模型尺寸或设备发生变化时自动卸载旧模型并加载新模型 """
        current_device = getattr(self, "current_device", None)
        if self.model is not None and (self.current_model_size != model_size or current_device != device):
            print(f"[Transcriber] 检测到模型切换 ({self.current_model_size}/{current_device} → {model_size}/{device})，正在卸载旧模型...")
            try:
                del self.model
            except Exception:
                pass
            self.model = None
            self.current_model_size = None
            self.current_device = None

        if self.model is None:
            print(f"[Transcriber] 正在加载模型: {model_size} ({device})...")
            model_path = MODEL_PATH / f"faster-whisper-{model_size}"
            
            if not model_path.exists():
                print(f"[Transcriber] 错误: 模型目录不存在 {model_path}")
                return None
            
            try:
                compute_type = "float16" if device == "cuda" else "int8"
                self.model = WhisperModel(str(model_path), device=device, compute_type=compute_type)
                self.current_model_size = model_size
                self.current_device = device
                print(f"[Transcriber] 模型加载完成，运行设备: {device.upper()}。")
            except Exception as e:
                print(f"[Transcriber] 错误: 使用 {device} 加载模型失败: {e}")
                if device == "cuda":
                    raise RuntimeError(
                        f"GPU (CUDA) 初始化失败，未切换至 CPU。\n\n"
                        f"可能原因：\n"
                        f"  1. 缺少 CUDA DLL（cublas64_12.dll / cudnn_ops_infer64_8.dll 等）\n"
                        f"  2. CUDA / cuDNN 版本不匹配\n"
                        f"  3. 显存不足\n\n"
                        f"请到「设置 → 转录设备」改为 CPU，或修复 CUDA 环境后重试。\n"
                        f"原始错误：{e}"
                    )
                return None
        
        return self.model

    def _get_initial_prompt(self, language):
        """
        [已停用] 不再向 Whisper 传递任何 initial_prompt。

        理由：Whisper 把 initial_prompt 当作"之前说过的话"（历史上下文），
        而非指令。即使是极短的自然口语片段，在音频模糊或静音时也可能被
        模型"延续"输出，成为幻觉的直接来源。

        当 language 已显式指定时，模型无需提示词辅助语言判断；
        当 language 为 Auto 时，我们无法预知语言，传入错语言的提示反而
        会干扰检测。
        标点风格由后续 Optimizer LLM 负责处理，无需此处引导。

        保留此方法以备将来特殊场景需要，调用方统一返回 None。
        """
        return None

    @staticmethod
    def _get_audio_duration(audio_path):
        """获取音频时长（秒），失败时返回 None。"""
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                n_frames = wf.getnframes()
                frame_rate = wf.getframerate()
                if frame_rate > 0:
                    return n_frames / float(frame_rate)
        except Exception as e:
            print(f"[Transcriber] 读取音频时长失败: {e}")
        return None

    @staticmethod
    def _merge_gaps_into_blocks(gaps, max_block_span_sec=300.0):
        """
        将全部空白区间尽量合并为少量块：按时间顺序合并，仅限制每块的跨度（首 gap 起点到末 gap 终点）
        不超过 max_block_span_sec，不要求空白之间相邻。每块一次音频截取 + 一次 ASR，再按原始 gap 过滤片段。
        返回: list of (block_start, block_end, list of (gap_start, gap_end))
        """
        if not gaps:
            return []
        gaps = sorted(gaps, key=lambda x: x[0])
        blocks = []
        current_gaps = [gaps[0]]
        for i in range(1, len(gaps)):
            g_start, g_end = gaps[i]
            block_start = current_gaps[0][0]
            # 当前块若加入此 gap，跨度 = 本 gap 终点 - 当前块起点
            new_span = g_end - block_start
            if new_span <= max_block_span_sec:
                current_gaps.append((g_start, g_end))
            else:
                blocks.append((current_gaps[0][0], current_gaps[-1][1], list(current_gaps)))
                current_gaps = [(g_start, g_end)]
        if current_gaps:
            blocks.append((current_gaps[0][0], current_gaps[-1][1], list(current_gaps)))
        return blocks

    def run(self, video_path, config, force_cpu=False, cancel_check=None):
        """ 
        运行转录流程 
        1. 提取音频
        2. 语音识别
        """
        print(f"[Transcriber] 开始处理: {video_path}")
        
        # 1. 提取音频
        temp_audio_paths = []
        audio_path = extract_audio(video_path)
        if not audio_path:
            print("[Transcriber] 音频提取失败，终止流程。")
            return None
        if os.path.abspath(audio_path) != os.path.abspath(video_path):
            temp_audio_paths.append(audio_path)

        project_manager.temp_audio_paths = temp_audio_paths
        
        # 2. 语音识别
        transcribe_mode = config.get("transcribeMode", cfg.transcribeMode.value)
        source_lang = config.get("sourceLanguage", "Auto")
        whisper_lang = WHISPER_LANGUAGE_CODES.get(source_lang) # 如果是 "Auto" 或不在字典里，则为 None
        word_timestamps = config.get("wordLevelTimestamps", cfg.wordLevelTimestamps.value)

        if transcribe_mode == "云端":
            provider = cfg.asr_provider.value  # "Whisper" 或 "Gladia"
            fallback_enabled = bool(cfg.cloud_asr_fallback_enabled.value)
            fallback_provider = cfg.cloud_asr_fallback_provider.value
            print(f"[Transcriber] 使用云端模式进行转录 (主提供商: {provider})...")

            # 读取术语表（供 Gladia 自定义词汇表联动使用）
            glossary_text = ""
            glossary_path = cfg.glossaryPath.value
            if glossary_path and os.path.exists(glossary_path):
                try:
                    with open(glossary_path, "r", encoding="utf-8") as gf:
                        glossary_text = gf.read()
                except Exception as ge:
                    print(f"[Transcriber] 读取术语表失败: {ge}")

            def _run_cloud_asr(selected_provider: str, _cancel_check=None):
                if selected_provider == "Gladia":
                    from app.core.gladia_asr import GladiaASR, GladiaError
                    try:
                        # 多语言模式：把次要语言也告诉 Gladia，让它正确处理双语内容
                        secondary_lang_name = config.get("secondaryLanguage") if config.get("multilingualMode") else None
                        secondary_lang_code = WHISPER_LANGUAGE_CODES.get(secondary_lang_name) if secondary_lang_name else None
                        gladia = GladiaASR(glossary_text=glossary_text)
                        results = gladia.transcribe(
                            audio_path,
                            language=whisper_lang,
                            word_timestamps=word_timestamps,
                            secondary_language=secondary_lang_code,
                            cancel_check=_cancel_check,
                        )
                        print(f"[Transcriber] Gladia 转录完成，共 {len(results)} 条片段。")
                        return results
                    except GladiaError as e:
                        print(f"[Transcriber] Gladia 转录错误: {e}")
                        raise Exception(str(e))

                # Whisper 兼容 API（Groq 等）：单次阻塞调用，在子线程中执行并用 cancel_check 轮询等待以便用户终止时可中断
                from concurrent.futures import ThreadPoolExecutor
                from app.core.cloud_asr import CloudASR
                cloud_asr = CloudASR()

                def _do_transcribe():
                    return cloud_asr.transcribe(
                        audio_path,
                        language=whisper_lang,
                        word_timestamps=word_timestamps,
                        prompt=None,
                        temperature=0,
                    )

                with ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(_do_transcribe)
                    while not future.done():
                        if _cancel_check:
                            _cancel_check()
                        time.sleep(0.5)
                    results = future.result()
                print(f"[Transcriber] 云端转录完成，共 {len(results)} 条片段。")
                return results

            try:
                if cancel_check:
                    cancel_check()
                results = _run_cloud_asr(provider, _cancel_check=cancel_check)
                # ── 云端模式下也复用本地的空白区间补录逻辑 ──
                gap_fill_enabled = bool(config.get("gapFillEnabled", cfg.gap_fill_enabled.value))
                multilingual_enabled = bool(config.get("multilingualMode"))
                if gap_fill_enabled and results and (whisper_lang or multilingual_enabled):
                    secondary_lang_name = config.get("secondaryLanguage") if multilingual_enabled else None
                    secondary_lang_code = WHISPER_LANGUAGE_CODES.get(secondary_lang_name) if secondary_lang_name else None
                    # 多语言模式优先使用次要语言；单语言模式沿用主语言
                    fill_language = secondary_lang_code if multilingual_enabled else whisper_lang

                    raw_gap_threshold = config.get(
                        "CloudMultilingualGapThresholdSec",
                        cfg.cloud_multilingual_gap_threshold_sec.value
                    )
                    try:
                        gap_threshold = float(raw_gap_threshold)
                    except Exception:
                        gap_threshold = float(cfg.cloud_multilingual_gap_threshold_sec.value)
                    gap_threshold = max(3.0, gap_threshold)

                    results = self._fill_gaps_multilingual_cloud(
                        results,
                        audio_path,
                        provider=provider,
                        word_timestamps=word_timestamps,
                        gap_threshold=gap_threshold,
                        fill_language=fill_language,
                        cancel_check=cancel_check,
                        glossary_text=glossary_text,
                    )
                    results = fix_timestamp_overlaps(results)

                return results
            except Exception as primary_error:
                if not fallback_enabled:
                    raise

                candidate = fallback_provider
                if candidate == provider:
                    candidate = "Gladia" if provider == "Whisper" else "Whisper"

                print(
                    f"[Transcriber] 主提供商 {provider} 失败，正在尝试备用提供商 {candidate}... "
                    f"原因: {primary_error}"
                )
                try:
                    if cancel_check:
                        cancel_check()
                    results = _run_cloud_asr(candidate, _cancel_check=cancel_check)
                    gap_fill_enabled = bool(config.get("gapFillEnabled", cfg.gap_fill_enabled.value))
                    multilingual_enabled = bool(config.get("multilingualMode"))
                    if gap_fill_enabled and results and (whisper_lang or multilingual_enabled):
                        secondary_lang_name = config.get("secondaryLanguage") if multilingual_enabled else None
                        secondary_lang_code = WHISPER_LANGUAGE_CODES.get(secondary_lang_name) if secondary_lang_name else None
                        fill_language = secondary_lang_code if multilingual_enabled else whisper_lang

                        raw_gap_threshold = config.get(
                            "CloudMultilingualGapThresholdSec",
                            cfg.cloud_multilingual_gap_threshold_sec.value
                        )
                        try:
                            gap_threshold = float(raw_gap_threshold)
                        except Exception:
                            gap_threshold = float(cfg.cloud_multilingual_gap_threshold_sec.value)
                        gap_threshold = max(3.0, gap_threshold)

                        results = self._fill_gaps_multilingual_cloud(
                            results,
                            audio_path,
                            provider=candidate,
                            word_timestamps=word_timestamps,
                            gap_threshold=gap_threshold,
                            fill_language=fill_language,
                            cancel_check=cancel_check,
                            glossary_text=glossary_text,
                        )
                        results = fix_timestamp_overlaps(results)

                    return results
                except Exception as fallback_error:
                    raise Exception(
                        f"云端转录失败：主提供商 {provider} 与备用提供商 {candidate} 均不可用。\n"
                        f"主错误：{primary_error}\n"
                        f"备用错误：{fallback_error}"
                    )

        # 本地模式
        model_size = config.get("asrModel", "base")
        
        # 决定使用什么设备
        if force_cpu:
            device = "cpu"
        else:
            device = "cuda" if cfg.fasterWhisperDevice.value == "GPU" else "cpu"
        
        model = self._get_model(model_size, device=device)
        if not model:
            return None

        print(f"[Transcriber] 正在识别音频: {audio_path}...")
        
        # 处理语言参数
        source_lang = config.get("sourceLanguage", "Auto")
        whisper_lang = WHISPER_LANGUAGE_CODES.get(source_lang) # 如果是 "Auto" 或不在字典里，则为 None
        
        # 获取初始提示词
        initial_prompt = self._get_initial_prompt(whisper_lang)
        
        # 幻觉文本关键词列表（涵盖中/英/西/葡/法等常见幻觉词）
        hallucination_keywords = [
            # 中文
            "请不吝点赞", "订阅", "转发", "打赏", "支持明镜",
            "字幕由", "网易云音乐", "感谢观看", "谢谢大家",
            "点击订阅", "关注我们", "下期再见",
            # 英文
            "Subscribe", "Like and subscribe", "Thanks for watching",
            "Subtitles by", "Translated by", "www.",
            # 西班牙语
            "Suscríbete", "Suscribete", "No olvides suscribirte",
            "Gracias por ver", "Dale like",
            # 葡萄牙语
            "Inscreva-se", "Curta o vídeo", "Obrigado por assistir",
            # 法语
            "Abonnez-vous", "Merci d'avoir regardé",
        ]
        
        try:
            # 从用户配置直接读取所有转录参数
            word_timestamps = config.get("wordLevelTimestamps", cfg.wordLevelTimestamps.value)
            vad_filter = bool(cfg.vad_filter.value)
            hallucination_filter_enabled = bool(cfg.transcription_hallucination_filter.value)
            vad_parameters = None
            if vad_filter:
                vad_parameters = dict(
                    threshold=cfg.vad_threshold.value,
                    min_silence_duration_ms=cfg.vad_min_silence_duration_ms.value,
                    # 在每段检测到的语音两端各填充若干 ms：
                    # 远场/弱信号说话人在句子边界处语音概率短暂低于阈值，
                    # 填充可避免开头/结尾被截断甚至整段被跳过。
                    speech_pad_ms=cfg.vad_speech_pad_ms.value,
                    # 短于此时长的疑似语音片段直接丢弃（Silero 内部默认 250ms）；
                    # 调低到 100ms 有助于捕获短促词语，减少漏听。
                    min_speech_duration_ms=cfg.vad_min_speech_duration_ms.value,
                )

            transcribe_kwargs = dict(
                beam_size=cfg.beam_size.value,
                # temperature=0 时为完全确定性采样，best_of>1 只是重复生成相同结果，
                # 纯属浪费计算资源（速度降低 best_of 倍）。强制设为 1。
                best_of=1,
                patience=cfg.patience.value,
                repetition_penalty=cfg.repetition_penalty.value,
                no_speech_threshold=cfg.no_speech_threshold.value if hallucination_filter_enabled else None,
                log_prob_threshold=cfg.log_prob_threshold.value if hallucination_filter_enabled else None,
                compression_ratio_threshold=(
                    cfg.compression_ratio_threshold.value if hallucination_filter_enabled else None
                ),
                condition_on_previous_text=cfg.condition_on_previous_text.value,
                language=whisper_lang,
                initial_prompt=initial_prompt,
                word_timestamps=word_timestamps,
                vad_filter=vad_filter,
                vad_parameters=vad_parameters,
                suppress_blank=True,
                suppress_tokens=[-1, 50257, 50362],
                # 固定 temperature=0，禁用 faster-whisper 的升温回退机制。
                # 默认值是列表 [0, 0.2, 0.4, 0.6, 0.8, 1.0]，噪声片段会依次
                # 升温重试直至 1.0（接近随机采样），这是幻觉严重的主要根源。
                temperature=0,
            )
            # hallucination_silence_threshold 需要词级时间戳支持（faster-whisper >= 0.9）
            # 静音超过 2 秒时自动增强幻觉检测，可大幅减少静音段的捏造文本
            if word_timestamps and hallucination_filter_enabled:
                transcribe_kwargs["hallucination_silence_threshold"] = 2.0

            segments, info = model.transcribe(audio_path, **transcribe_kwargs)

            print(f"[Transcriber] 识别中 (检测到语言: {info.language}, 置信度: {info.language_probability:.2f})...")

            results = []
            for segment in segments:
                if cancel_check:
                    cancel_check()
                text = segment.text.strip()
                
                # 过滤幻觉文本和无意义短句
                if not text or len(text) < 1:
                    continue
                
                # 跳过包含幻觉关键词的文本（可开关）
                if hallucination_filter_enabled and any(keyword in text for keyword in hallucination_keywords):
                    print(f"[Transcriber] 过滤幻觉关键词: {text[:40]}")
                    continue
                
                # 跳过纯音乐标记
                if text.startswith(("【", "[", "(", "（")) and text.endswith(("】", "]", ")", "）")):
                    continue

                # ── 置信度过滤（减少幻觉，保留真实语音）──────────────────────
                # 阈值从设置界面读取，用户可在"高级转录参数"中调整
                # no_speech_threshold（默认 0.7）：超过此值认为是静音/幻觉
                no_speech_prob = getattr(segment, 'no_speech_prob', 0.0)
                nsp_threshold = cfg.no_speech_threshold.value
                if hallucination_filter_enabled and no_speech_prob > nsp_threshold:
                    print(f"[Transcriber] 过滤高无语音概率片段 "
                          f"(no_speech_prob={no_speech_prob:.2f}>{nsp_threshold}): {text[:40]}")
                    continue

                # log_prob_threshold（默认 -1.0）：低于此值认为解码置信度极低
                avg_logprob = getattr(segment, 'avg_logprob', 0.0)
                lp_threshold = cfg.log_prob_threshold.value
                if hallucination_filter_enabled and avg_logprob < lp_threshold:
                    print(f"[Transcriber] 过滤低置信度片段 "
                          f"(avg_logprob={avg_logprob:.2f}<{lp_threshold}): {text[:40]}")
                    continue

                # ───────────────────────────────────────────────────────────

                print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {text}")
                
                res = {
                    "start": segment.start,
                    "end": segment.end,
                    "text": text
                }
                
                # 如果开启了词级时间戳，保存词级信息
                if word_timestamps and hasattr(segment, 'words') and segment.words:
                    res["words"] = []
                    for w in segment.words:
                        res["words"].append({
                            "start": w.start,
                            "end": w.end,
                            "word": w.word
                        })
                
                results.append(res)
        except Exception as e:
            print(f"[Transcriber] 识别过程中出错: {e}")
            if not force_cpu and ("cublas" in str(e).lower() or "cuda" in str(e).lower()):
                raise RuntimeError(
                    f"转录过程中 GPU (CUDA) 出现错误，未自动切换至 CPU。\n\n"
                    f"请到「设置 → 转录设备」改为 CPU，或修复 CUDA 环境后重试。\n"
                    f"原始错误：{e}"
                )
            return None
        
        # 用词级时间戳修正 segment 级时间戳。
        # 原因：fix_timestamp_overlaps 会把相邻重叠 segment 的 end 截断为下一段的 start，
        # 当三段连续重叠时（A→B→C），B 的 end 被截为 C.start，而 C.start 与 B.start
        # 可能仅相差 0.1 秒，导致 B 时长极短（如 0.12s），内容实为真实语音却被误判。
        # 词级时间戳由 DTW 独立计算，比 segment 级更准确，用它重建 start/end 可避免此问题。
        if word_timestamps:
            for seg in results:
                words = seg.get("words", [])
                if words:
                    seg["start"] = words[0]["start"]
                    seg["end"]   = words[-1]["end"]

        results = fix_timestamp_overlaps(results)

        # 空白区间补录：扫描整段音频中的长空白区间（默认 >=10s），
        # 多语言模式下优先使用次要语言，单语言模式下沿用主语言。
        gap_fill_enabled = bool(config.get("gapFillEnabled", cfg.gap_fill_enabled.value))
        multilingual_enabled = bool(config.get("multilingualMode"))
        if gap_fill_enabled and results and (whisper_lang or multilingual_enabled):
            secondary_lang_name = config.get("secondaryLanguage") if multilingual_enabled else None
            secondary_lang_code = WHISPER_LANGUAGE_CODES.get(secondary_lang_name) if secondary_lang_name else None
            # 多语言模式优先使用次要语言；单语言模式沿用主语言
            fill_language = secondary_lang_code if multilingual_enabled else whisper_lang

            raw_gap_threshold = config.get(
                "multilingualGapThresholdSec",
                cfg.multilingual_gap_threshold_sec.value
            )
            try:
                gap_threshold = float(raw_gap_threshold)
            except Exception:
                gap_threshold = float(cfg.multilingual_gap_threshold_sec.value)
            # 下限保护，避免阈值过小导致大量无效补录
            gap_threshold = max(3.0, gap_threshold)

            results = self._fill_gaps_multilingual(
                results, audio_path, transcribe_kwargs, word_timestamps,
                gap_threshold=gap_threshold,
                fill_language=fill_language,
                cancel_check=cancel_check
            )
            results = fix_timestamp_overlaps(results)

        print(f"[Transcriber] 转录完成，共 {len(results)} 条片段。")
        return results

    def _fill_gaps_multilingual(self, results, audio_path, base_kwargs,
                                word_timestamps, gap_threshold=10.0,
                                fill_language=None, cancel_check=None):
        """
        空白区间补录：主转录后，对超过 gap_threshold 秒的空白区间，
        提取对应音频片段并重新转录。

        fill_language:
          - 非 None（如 'es'）：用指定语言重转，精度最高（多语言模式下由用户指定次要语言）
          - None：language=None，Whisper 自动检测语言（兜底方案）

        典型场景：用户设置语言=English，但发布会中有记者用西语提问；
        多语言模式下用户指定次要语言=Spanish，对应区间会优先用西语重转。
        """
        import os
        import re as _re
        import tempfile
        import subprocess
        from app.common.utils import get_ffmpeg_path

        # ── 1. 先统计整段音频时长，并按已识别区间求补集（覆盖首尾空白） ─────────────
        audio_duration = self._get_audio_duration(audio_path)
        intervals = []
        for seg in results:
            s = float(seg.get("start", 0) or 0)
            e = float(seg.get("end", 0) or 0)
            if e > s:
                intervals.append((s, e))
        intervals.sort(key=lambda x: x[0])

        merged_intervals = []
        for s, e in intervals:
            if not merged_intervals or s > merged_intervals[-1][1]:
                merged_intervals.append([s, e])
            else:
                merged_intervals[-1][1] = max(merged_intervals[-1][1], e)

        gaps = []
        coverage_end = audio_duration if (audio_duration and audio_duration > 0) else (merged_intervals[-1][1] if merged_intervals else 0)
        if merged_intervals:
            # 头部空白
            if merged_intervals[0][0] >= gap_threshold:
                gaps.append((0.0, merged_intervals[0][0]))
            # 中间空白
            for i in range(1, len(merged_intervals)):
                gap_start = merged_intervals[i - 1][1]
                gap_end = merged_intervals[i][0]
                if gap_end - gap_start >= gap_threshold:
                    gaps.append((gap_start, gap_end))
            # 尾部空白
            tail_gap = coverage_end - merged_intervals[-1][1]
            if tail_gap >= gap_threshold:
                gaps.append((merged_intervals[-1][1], coverage_end))
        elif coverage_end >= gap_threshold:
            # 极端情况：无任何识别结果，整段作为空白候选
            gaps.append((0.0, coverage_end))

        # ── 2. 异常长段检测 ──────────────────────────────────────────────
        anomalous_segs = []   # 记录异常段，填补后修正其起点
        # Whisper 在双语窗口中有时把英语回答的时间戳错误地提前到西语问题的位置，
        # 造成例如 "No, it's for you."（5词）却跨越 11 秒的情况。
        # 检测方法：实际时长 > 预期时长 × 4 且多出至少 6 秒，
        # 则认为该段开头藏有未转录的内容，将其前端作为"幽灵间隙"补充填补。
        for i, seg in enumerate(results):
            text        = seg.get("text", "").strip()
            seg_start   = seg.get("start", 0)
            seg_end     = seg.get("end",   0)
            actual_dur  = seg_end - seg_start
            if actual_dur <= 0 or not text:
                continue

            # 估算预期时长（英文按词数，中文按字数，0.45 秒/词）
            is_en        = sum(1 for c in text if 'a' <= c.lower() <= 'z') > len(text) * 0.4
            unit_count   = len(_re.findall(r'\w+', text)) if is_en else len(text)
            expected_dur = max(0.5, unit_count * 0.45)

            if actual_dur > expected_dur * 4 and actual_dur - expected_dur > 6.0:
                prev_end       = results[i - 1].get("end", 0) if i > 0 else 0
                true_start_est = seg_end - expected_dur   # 估算真实语音起点

                # 拆成两个独立的小填补窗口：
                #   窗口A: prev_end → seg_start  （段之前的空白）
                #   窗口B: seg_start → true_start_est（段内部的隐藏内容）
                added = False
                if seg_start - prev_end >= 3.0:
                    gaps.append((prev_end, seg_start))
                    added = True
                if true_start_est - seg_start >= 3.0:
                    gaps.append((seg_start, true_start_est))
                    added = True

                if added:
                    # 记录该异常段的引用，填补完成后修正其起点，
                    # 防止 fix_timestamp_overlaps 把它压缩成碎片
                    anomalous_segs.append({
                        "seg":          seg,
                        "window_start": prev_end,
                        "window_end":   true_start_est,
                    })
                    print(f"[Transcriber] 检测到异常长段 [{seg_start:.1f}s-{seg_end:.1f}s] "
                          f"'{text[:30]}' (预期{expected_dur:.1f}s/实际{actual_dur:.1f}s)，"
                          f"疑似隐藏内容: {prev_end:.1f}s-{true_start_est:.1f}s（拆为两窗口）")

        # 去重、排序，过滤相互重叠的区间
        gaps = sorted(set(gaps), key=lambda x: x[0])
        merged_gaps: list = []
        for g in gaps:
            if merged_gaps and g[0] < merged_gaps[-1][1]:
                merged_gaps[-1] = (merged_gaps[-1][0], max(merged_gaps[-1][1], g[1]))
            else:
                merged_gaps.append(list(g))
        gaps = [tuple(g) for g in merged_gaps]

        if not gaps:
            return results

        # 合并全部空白为尽量少的块（仅限制单块跨度上限），每块一次截取 + 一次 ASR
        blocks = self._merge_gaps_into_blocks(gaps, max_block_span_sec=300.0)
        print(f"[Transcriber] 发现 {len(gaps)} 个需补录区间（含异常长段），合并为 {len(blocks)} 块，启动空白区间补录...")

        ffmpeg_exe  = get_ffmpeg_path()
        new_segments = []
        pad = 0.3

        fill_kwargs = dict(base_kwargs)
        fill_kwargs["language"]   = fill_language
        fill_kwargs["vad_filter"] = False
        fill_kwargs.pop("vad_parameters", None)
        lang_desc = fill_language if fill_language else "自动检测"
        print(f"[Transcriber] 填补语言: {lang_desc}")

        hallucination_keywords = [
            "请不吝点赞", "订阅", "转发", "感谢观看",
            "Subscribe", "Thanks for watching", "Subtitles by",
            "Suscríbete", "Inscreva-se",
        ]

        for block_start, block_end, gaps_in_block in blocks:
            if cancel_check:
                cancel_check()

            t_start = max(0.0, block_start - pad)
            t_dur   = (block_end - block_start) + pad * 2

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)

            try:
                cmd = [
                    ffmpeg_exe,
                    "-ss", f"{t_start:.3f}",
                    "-t",  f"{t_dur:.3f}",
                    "-i",  audio_path,
                    "-ar", "16000", "-ac", "1", "-vn", "-y",
                    tmp_path
                ]
                ret = subprocess.run(cmd, capture_output=True)
                if ret.returncode != 0 or not os.path.exists(tmp_path):
                    print(f"[Transcriber] 填补块提取失败（{block_start:.1f}s-{block_end:.1f}s），跳过")
                    continue

                fill_segs, fill_info = self.model.transcribe(tmp_path, **fill_kwargs)
                detected_lang = fill_info.language
                seg_list = list(fill_segs)
                if not seg_list:
                    continue

                print(f"[Transcriber] 填补块 {block_start:.1f}s-{block_end:.1f}s（含 {len(gaps_in_block)} 个空白），"
                      f"检测语言: {detected_lang}，共 {len(seg_list)} 段")

                offset = t_start
                for seg in seg_list:
                    text = seg.text.strip()
                    if not text or len(text) < 1:
                        continue

                    if cfg.transcription_hallucination_filter.value and any(k in text for k in hallucination_keywords):
                        continue

                    nsp = getattr(seg, "no_speech_prob", 0.0)
                    alp = getattr(seg, "avg_logprob", 0.0)
                    fill_nsp_threshold = min(0.95, cfg.no_speech_threshold.value + 0.15)
                    fill_lp_threshold  = cfg.log_prob_threshold.value - 0.6
                    if cfg.transcription_hallucination_filter.value and nsp > fill_nsp_threshold:
                        print(f"[Transcriber] 填补过滤(no_speech={nsp:.2f}>{fill_nsp_threshold:.2f}): {text[:40]}")
                        continue
                    if cfg.transcription_hallucination_filter.value and alp < fill_lp_threshold:
                        print(f"[Transcriber] 填补过滤(logprob={alp:.2f}<{fill_lp_threshold:.2f}): {text[:40]}")
                        continue

                    seg_start = offset + seg.start
                    seg_end   = offset + seg.end

                    # 只保留与当前块内任一原始空白有交集的片段
                    if not any(min(seg_end, g_end) > max(seg_start, g_start) for g_start, g_end in gaps_in_block):
                        continue

                    new_seg = {
                        "start": round(seg_start, 3),
                        "end":   round(seg_end,   3),
                        "text":  text,
                    }
                    if word_timestamps and hasattr(seg, "words") and seg.words:
                        new_seg["words"] = [
                            {
                                "start": round(offset + w.start, 3),
                                "end":   round(offset + w.end,   3),
                                "word":  w.word,
                            }
                            for w in seg.words
                        ]
                        if new_seg["words"]:
                            new_seg["start"] = new_seg["words"][0]["start"]
                            new_seg["end"]   = new_seg["words"][-1]["end"]

                    new_segments.append(new_seg)
                    print(f"[Transcriber] 填补: [{new_seg['start']:.2f}s -> {new_seg['end']:.2f}s] {text}")

            except Exception as e:
                print(f"[Transcriber] 填补块 {block_start:.1f}s-{block_end:.1f}s 出错: {e}")
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        if new_segments:
            results = results + new_segments
            results.sort(key=lambda x: x.get("start", 0))
            print(f"[Transcriber] 空白区间补录完成，新增 {len(new_segments)} 条片段")

            # 修正异常段起点：把异常段的 start 推到填补内容结束之后，
            # 防止 fix_timestamp_overlaps 把它压缩成毫秒级碎片。
            for anom in anomalous_segs:
                seg_dict     = anom["seg"]
                window_start = anom["window_start"]
                window_end   = anom["window_end"]
                last_fill_end = window_start
                for ns in new_segments:
                    ns_start = ns.get("start", 0)
                    ns_end   = ns.get("end",   0)
                    if ns_start >= window_start and ns_end <= window_end + 2.0:
                        last_fill_end = max(last_fill_end, ns_end)
                if last_fill_end > seg_dict.get("start", 0):
                    old_s = seg_dict["start"]
                    shift = last_fill_end - old_s
                    seg_dict["start"] = last_fill_end
                    # 同步平移词级时间戳，防止 Splitter 用旧的 words[0].start 覆盖修正结果
                    for w in seg_dict.get("words", []):
                        w["start"] = round(w.get("start", 0) + shift, 3)
                        w["end"]   = round(w.get("end",   0) + shift, 3)
                    print(f"[Transcriber] 修正异常段起点: "
                          f"'{seg_dict.get('text','')[:30]}' "
                          f"{old_s:.2f}s → {last_fill_end:.2f}s（词级时间戳同步平移 {shift:.2f}s）")

        if cfg.transcription_hallucination_filter.value:
            # 去除连续重复段：相邻两段文本相同（不限时长），视为 Whisper 重复输出
            deduped = []
            for seg in results:
                if (deduped
                        and seg.get("text", "").strip() == deduped[-1].get("text", "").strip()):
                    # 文本完全相同：合并时间范围到前一段，丢弃当前段
                    deduped[-1]["end"] = max(deduped[-1].get("end", 0), seg.get("end", 0))
                    print(f"[Transcriber] 去重连续重复段: [{seg.get('start'):.2f}s] {seg.get('text','')[:40]}")
                    continue
                deduped.append(seg)
            results = deduped

            # 检测"短时间段塞入过长文本"的幻觉：
            # 如果一个段的时长很短但文本很长（语速不合理），标记为可疑
            for seg in results:
                duration = seg.get("end", 0) - seg.get("start", 0)
                text = seg.get("text", "").strip()
                if duration > 0 and text:
                    # 估算合理字符数：英文约 15 字符/秒，中文约 6 字/秒
                    # 超过 3 倍合理速度视为幻觉
                    max_reasonable_chars = duration * 45  # 非常宽松的上限
                    if len(text) > max_reasonable_chars and duration < 2.0:
                        print(f"[Transcriber] 疑似幻觉（{duration:.1f}s 内 {len(text)} 字符）: {text[:60]}")
                        seg["text"] = ""  # 清空幻觉文本

            # 清除被标记为空的幻觉段
            results = [seg for seg in results if seg.get("text", "").strip()]

        return results

    def _fill_gaps_multilingual_cloud(self, results, audio_path,
                                      provider: str,
                                      word_timestamps: bool,
                                      gap_threshold: float = 10.0,
                                      fill_language: str | None = None,
                                      cancel_check=None,
                                      glossary_text: str = ""):
        """
        云端模式下的空白区间补录：
        - 复用本地模式的空白检测逻辑（按 gap_threshold 统计首尾/中间长空白）
        - 对每个空白区间裁剪音频片段，调用云端 ASR（Gladia / Whisper 兼容）进行补录
        - 将补录结果按时间轴平移后插回原结果列表
        """
        import os
        import tempfile
        import subprocess
        from app.common.utils import get_ffmpeg_path

        if not results:
            return results

        # ── 1. 检测需填补的空白区间（与本地版本保持一致的策略） ─────────────
        audio_duration = self._get_audio_duration(audio_path)
        intervals = []
        for seg in results:
            s = float(seg.get("start", 0) or 0)
            e = float(seg.get("end", 0) or 0)
            if e > s:
                intervals.append((s, e))
        intervals.sort(key=lambda x: x[0])

        merged_intervals = []
        for s, e in intervals:
            if not merged_intervals or s > merged_intervals[-1][1]:
                merged_intervals.append([s, e])
            else:
                merged_intervals[-1][1] = max(merged_intervals[-1][1], e)

        gaps = []
        coverage_end = audio_duration if (audio_duration and audio_duration > 0) else (merged_intervals[-1][1] if merged_intervals else 0)
        if merged_intervals:
            # 头部空白
            if merged_intervals[0][0] >= gap_threshold:
                gaps.append((0.0, merged_intervals[0][0]))
            # 中间空白
            for i in range(1, len(merged_intervals)):
                gap_start = merged_intervals[i - 1][1]
                gap_end = merged_intervals[i][0]
                if gap_end - gap_start >= gap_threshold:
                    gaps.append((gap_start, gap_end))
            # 尾部空白
            tail_gap = coverage_end - merged_intervals[-1][1]
            if tail_gap >= gap_threshold:
                gaps.append((merged_intervals[-1][1], coverage_end))
        elif coverage_end >= gap_threshold:
            gaps.append((0.0, coverage_end))

        if not gaps:
            return results

        # 合并全部空白为尽量少的块（仅限制单块跨度上限），每块一次截取 + 一次 ASR
        blocks = self._merge_gaps_into_blocks(gaps, max_block_span_sec=300.0)
        # 云端单次补录最多处理块数，避免极端情况
        max_blocks = 10
        if len(blocks) > max_blocks:
            print(f"[Transcriber] (云端) 合并后 {len(blocks)} 块，仅处理前 {max_blocks} 块")
            blocks = blocks[:max_blocks]

        print(f"[Transcriber] (云端) 发现 {len(gaps)} 个空白区间，合并为 {len(blocks)} 块，启动空白区间补录...")

        ffmpeg_exe = get_ffmpeg_path()
        new_segments: list[dict] = []
        pad = 0.3
        lang_desc = fill_language if fill_language else "自动检测"
        print(f"[Transcriber] (云端) 填补语言: {lang_desc}，提供商: {provider}")

        from concurrent.futures import ThreadPoolExecutor
        from app.core.cloud_asr import CloudASR
        from app.core.gladia_asr import GladiaASR

        hallucination_keywords = [
            "请不吝点赞", "订阅", "转发", "感谢观看",
            "Subscribe", "Thanks for watching", "Subtitles by",
            "Suscríbete", "Inscreva-se",
        ]

        for block_start, block_end, gaps_in_block in blocks:
            if cancel_check:
                try:
                    cancel_check()
                except Exception:
                    raise

            t_start = max(0.0, block_start - pad)
            t_dur = (block_end - block_start) + pad * 2

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)

            try:
                cmd = [
                    ffmpeg_exe,
                    "-ss", f"{t_start:.3f}",
                    "-t", f"{t_dur:.3f}",
                    "-i", audio_path,
                    "-ar", "16000", "-ac", "1", "-vn", "-y",
                    tmp_path,
                ]
                ret = subprocess.run(cmd, capture_output=True)
                if ret.returncode != 0 or not os.path.exists(tmp_path):
                    print(f"[Transcriber] (云端) 填补块提取失败（{block_start:.1f}s-{block_end:.1f}s），跳过")
                    continue

                if provider == "Gladia":
                    gladia = GladiaASR(glossary_text=glossary_text)
                    block_results = gladia.transcribe(
                        tmp_path,
                        language=fill_language,
                        word_timestamps=word_timestamps,
                        secondary_language=None,
                        cancel_check=cancel_check,
                    )
                else:
                    cloud_asr = CloudASR()

                    def _do_fill_transcribe():
                        return cloud_asr.transcribe(
                            tmp_path,
                            language=fill_language,
                            word_timestamps=word_timestamps,
                            prompt=None,
                            temperature=0,
                        )

                    with ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(_do_fill_transcribe)
                        while not future.done():
                            if cancel_check:
                                cancel_check()
                            time.sleep(0.5)
                        block_results = future.result()

                if not block_results:
                    continue

                print(f"[Transcriber] (云端) 填补块 {block_start:.1f}s-{block_end:.1f}s（含 {len(gaps_in_block)} 个空白），返回 {len(block_results)} 段")

                for seg in block_results:
                    text = (seg.get("text") or "").strip()
                    if not text:
                        continue

                    if cfg.transcription_hallucination_filter.value and any(k in text for k in hallucination_keywords):
                        continue

                    offset = t_start
                    seg_start = offset + float(seg.get("start", 0) or 0)
                    seg_end = offset + float(seg.get("end", 0) or 0)

                    # 只保留与当前块内任一原始空白有交集的片段，避免重复已覆盖区间
                    if not any(min(seg_end, g_end) > max(seg_start, g_start) for g_start, g_end in gaps_in_block):
                        continue

                    new_seg = {
                        "start": round(seg_start, 3),
                        "end": round(seg_end, 3),
                        "text": text,
                    }

                    if word_timestamps and seg.get("words"):
                        words = []
                        for w in seg["words"]:
                            ws = offset + float(w.get("start", 0) or 0)
                            we = offset + float(w.get("end", 0) or 0)
                            words.append({
                                "start": round(ws, 3),
                                "end": round(we, 3),
                                "word": w.get("word") or w.get("text") or "",
                            })
                        if words:
                            words.sort(key=lambda x: x["start"])
                            new_seg["words"] = words
                            new_seg["start"] = words[0]["start"]
                            new_seg["end"] = words[-1]["end"]

                    new_segments.append(new_seg)
                    print(f"[Transcriber] (云端) 填补: [{new_seg['start']:.2f}s -> {new_seg['end']:.2f}s] {text}")

            except Exception as e:
                print(f"[Transcriber] (云端) 填补块 {block_start:.1f}s-{block_end:.1f}s 出错: {e}")
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        if new_segments:
            results = results + new_segments
            results.sort(key=lambda x: x.get("start", 0))
            print(f"[Transcriber] (云端) 空白区间补录完成，新增 {len(new_segments)} 条片段")

        return results
