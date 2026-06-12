# -*- coding: utf-8 -*-
"""
Gladia Pre-recorded Speech-to-Text API 接入模块
API 文档: https://docs.gladia.io/chapters/pre-recorded-stt/quickstart
流程: 上传音频 → 提交转录任务 → 轮询等待结果 → 解析返回
"""
import os
import re
import time
import logging
import requests
from requests.exceptions import ConnectionError, Timeout, ReadTimeout
from app.common.config import cfg
from app.common.utils import apply_audio_preprocess, fix_timestamp_overlaps

GLADIA_BASE_URL = "https://api.gladia.io"
MAX_FILE_SIZE_MB = 500  # Gladia 单文件上传上限
GLADIA_SINGLE_LANGUAGE_MODEL = "solaria-3"
GLADIA_MULTILINGUAL_MODEL = "solaria-1"
GLADIA_SOLARIA_3_LANGUAGES = {"en", "fr", "de", "es", "it"}


class GladiaError(Exception):
    """Gladia 转录专用异常，携带用户可读的中文提示。"""
    pass


class GladiaASR:
    """
    使用 Gladia API 进行异步音频转录。
    支持自定义词汇表（从术语表自动提取源语言词条）。
    """

    def __init__(self, glossary_text: str = ""):
        self.api_key = cfg.gladia_api_key.value
        if not self.api_key:
            raise GladiaError("未配置 Gladia API Key，请在全局设置中填写后重试")
        self.api_key = self.api_key.strip()
        self.glossary_text = glossary_text
        self.headers = {
            "x-gladia-key": self.api_key,
            "Content-Type": "application/json"
        }

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def transcribe(self, audio_path: str, language: str = None,
                   word_timestamps: bool = True, context_prompt: str = "",
                   secondary_language: str = None, cancel_check=None) -> list:
        """
        完整转录流程：预检 → 上传 → 提交任务 → 轮询 → 解析。
        返回与 CloudASR 格式一致的 segment 列表。
        cancel_check: 可选，轮询过程中每轮会调用；若抛出异常则中止等待并向上抛出（用于用户点击终止转录）。
        """
        print(f"[GladiaASR] 开始转录: {audio_path}")

        upload_audio_path, cleanup_audio_path = self._prepare_audio_for_upload(audio_path)
        try:
            return self._transcribe_prepared(
                upload_audio_path,
                language=language,
                word_timestamps=word_timestamps,
                context_prompt=context_prompt,
                secondary_language=secondary_language,
                cancel_check=cancel_check,
            )
        finally:
            if cleanup_audio_path and os.path.exists(cleanup_audio_path):
                try:
                    os.remove(cleanup_audio_path)
                except Exception as e:
                    logging.warning(f"[GladiaASR] 删除预处理临时音频失败: {e}")

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _prepare_audio_for_upload(self, audio_path: str):
        """根据设置对上传前音频做可选本地预处理。"""
        if not cfg.gladia_local_denoise.value:
            return audio_path, None

        processed_path = apply_audio_preprocess(audio_path, gain_db=0.0, denoise=True)
        if os.path.abspath(processed_path) == os.path.abspath(audio_path):
            return audio_path, None

        print(f"[GladiaASR] 已启用本地降噪预处理: {processed_path}")
        return processed_path, processed_path

    def _transcribe_prepared(self, audio_path: str, language: str = None,
                             word_timestamps: bool = True, context_prompt: str = "",
                             secondary_language: str = None, cancel_check=None) -> list:
        """对已准备好的音频执行完整转录流程。"""
        if cancel_check:
            cancel_check()

        # 0. 上传前预检：文件大小
        self._check_file_size(audio_path)

        # 1. 上传音频，拿到 audio_url
        audio_url = self._upload_audio(audio_path)

        if cancel_check:
            cancel_check()

        # 2. 解析术语表 → 自定义词汇表
        vocabulary = self._parse_vocabulary()

        # 3. 提交转录任务
        job_id, result_url = self._submit_job(audio_url, language, word_timestamps, vocabulary,
                                              context_prompt=context_prompt,
                                              secondary_language=secondary_language)

        # 4. 轮询等待结果（最多等 30 分钟）；轮询中会按 cancel_check 响应终止
        result = self._poll_result(result_url, timeout=1800, cancel_check=cancel_check)

        # 5. 解析并返回
        segments = self._parse_result(result, word_timestamps)
        print(f"[GladiaASR] 转录完成，共 {len(segments)} 条片段。")
        return segments

    def _check_file_size(self, audio_path: str):
        """预检文件大小，超出限制时直接报错，避免浪费上传流量。"""
        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            raise GladiaError(
                f"音频文件过大（{size_mb:.1f} MB），Gladia 单次上传上限为 {MAX_FILE_SIZE_MB} MB。\n"
                f"请将视频切分为较短片段后重试。"
            )

    def _upload_audio(self, audio_path: str) -> str:
        """上传音频文件，返回 Gladia 托管的 audio_url。"""
        url = f"{GLADIA_BASE_URL}/v2/upload"
        upload_headers = {"x-gladia-key": self.api_key}
        try:
            with open(audio_path, "rb") as f:
                response = requests.post(
                    url,
                    headers=upload_headers,
                    files={"audio": (os.path.basename(audio_path), f, "audio/wav")},
                    timeout=300
                )
            self._raise_for_gladia_status(response, step="上传音频")
            audio_url = response.json().get("audio_url")
            print(f"[GladiaASR] 音频上传成功: {audio_url}")
            return audio_url
        except GladiaError:
            raise
        except (ConnectionError, Timeout, ReadTimeout) as e:
            raise GladiaError(
                f"上传音频时网络连接失败，请检查网络后重试。\n详情: {e}"
            )
        except Exception as e:
            raise GladiaError(f"上传音频时发生未知错误: {e}")

    def _submit_job(self, audio_url: str, language: str, word_timestamps: bool,
                    vocabulary: list, context_prompt: str = "",
                    secondary_language: str = None):
        """提交转录任务，返回 (job_id, result_url)。"""
        url = f"{GLADIA_BASE_URL}/v2/pre-recorded"

        payload = {
            "audio_url": audio_url,
            "punctuation_enhanced": True,
            "name_consistency": True,
        }

        # context_prompt 有意不传递：Gladia 的 context_prompt 与 Whisper 的 initial_prompt
        # 行为一致，模型视其为"之前说过的话"，在音频模糊或静音段容易续写成幻觉。
        # 视频语境信息由 LLM 优化阶段处理，不在 ASR 层注入。

        is_auto_language = not language or language.lower() == "auto"
        is_multilingual = bool(secondary_language and secondary_language != language)
        use_solaria_3 = (
            not is_auto_language
            and not is_multilingual
            and language.lower() in GLADIA_SOLARIA_3_LANGUAGES
        )
        if use_solaria_3:
            selected_model = GLADIA_SINGLE_LANGUAGE_MODEL
            payload["language_config"] = {
                "languages": [language],
                "code_switching": False
            }
        else:
            selected_model = GLADIA_MULTILINGUAL_MODEL
            languages = []
            if not is_auto_language:
                languages.append(language)
            if secondary_language and secondary_language.lower() != "auto":
                languages.append(secondary_language)
            payload["language_config"] = {
                "languages": languages,
                "code_switching": is_auto_language or is_multilingual
            }
        payload["model"] = selected_model
        print(f"[GladiaASR] 使用 Gladia 模型: {selected_model}, language_config={payload['language_config']}")

        # ── 转录调参 ────────────────────────────────────────────────────
        vocab_intensity = float(cfg.gladia_vocabulary_intensity.value)
        print(f"[GladiaASR] 转录参数：vocab_intensity={vocab_intensity}")

        # 说话人分离
        if cfg.gladia_diarization.value:
            payload["diarization"] = True
            max_spk = int(cfg.gladia_max_speakers.value)
            if max_spk > 0:
                payload["diarization_config"] = {"max_speakers": max_spk}
            print(f"[GladiaASR] 说话人分离已开启，max_speakers={max_spk}")

        # 自定义词汇表（仅在有有效词条时启用）
        if vocabulary:
            payload["custom_vocabulary"] = True
            payload["custom_vocabulary_config"] = {
                "vocabulary": vocabulary,
                "default_intensity": vocab_intensity
            }
            print(f"[GladiaASR] 注入自定义词汇: {len(vocabulary)} 条，权重: {vocab_intensity}")

        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=120)
            self._raise_for_gladia_status(response, step="提交转录任务")
            data = response.json()
            job_id = data.get("id")
            result_url = data.get("result_url")
            print(f"[GladiaASR] 任务已提交，Job ID: {job_id}")
            return job_id, result_url
        except GladiaError:
            raise
        except (ConnectionError, Timeout, ReadTimeout) as e:
            raise GladiaError(
                f"提交转录任务时网络连接超时，请检查网络后重试。\n详情: {e}"
            )
        except Exception as e:
            raise GladiaError(f"提交转录任务时发生未知错误: {e}")

    def _poll_result(self, result_url: str, timeout: int = 1800, cancel_check=None) -> dict:
        """轮询 result_url 直到 status == 'done'，返回结果 JSON。cancel_check 每轮调用，若抛出则中止。"""
        poll_headers = {"x-gladia-key": self.api_key}
        start = time.time()
        interval = 5
        consecutive_failures = 0

        while time.time() - start < timeout:
            if cancel_check:
                cancel_check()

            try:
                response = requests.get(result_url, headers=poll_headers, timeout=30)
                self._raise_for_gladia_status(response, step="获取转录结果")
                consecutive_failures = 0

                data = response.json()
                status = data.get("status", "")

                if status == "done":
                    print("[GladiaASR] 转录任务完成。")
                    return data
                elif status == "error":
                    # 从结果中提取详细错误信息
                    err_detail = ""
                    try:
                        err_detail = data.get("result", {}).get("metadata", {}).get("error", "")
                        if not err_detail:
                            err_detail = str(data.get("result", ""))
                    except Exception:
                        pass
                    raise GladiaError(
                        f"Gladia 转录任务失败（服务端处理错误）。\n"
                        f"可能原因：音频格式损坏、时长过长或内容无法识别。\n"
                        f"详情: {err_detail or '无详细信息'}"
                    )
                else:
                    elapsed = int(time.time() - start)
                    print(f"[GladiaASR] 等待转录中... ({elapsed}s, status={status})")

            except GladiaError:
                raise
            except (ConnectionError, Timeout, ReadTimeout):
                consecutive_failures += 1
                print(f"[GladiaASR] 轮询网络超时，第 {consecutive_failures} 次重试...")
                if consecutive_failures >= 5:
                    raise GladiaError(
                        "轮询转录结果时网络持续中断（已失败 5 次），请检查网络连接后重试。"
                    )
            except Exception as e:
                print(f"[GladiaASR] 轮询出错: {e}")

            time.sleep(interval)
            if time.time() - start > 60:
                interval = 10

        raise GladiaError(
            f"等待 Gladia 转录结果超时（超过 {timeout // 60} 分钟）。\n"
            f"可能原因：服务器繁忙或网络不稳定，建议稍后重试。"
        )

    def _parse_result(self, data: dict, need_words: bool) -> list:
        """将 Gladia 返回的 JSON 解析为统一的 segment 列表。"""
        try:
            result = data.get("result", {})
            transcription = result.get("transcription", {})
            utterances = transcription.get("utterances", [])

            if not utterances:
                print("[GladiaASR] 警告：转录结果为空，音频可能没有可识别的语音内容。")
                return []

            segments = []
            for utt in utterances:
                text = utt.get("text", "").strip()
                if not text:
                    continue

                seg = {
                    "start": utt.get("start", 0.0),
                    "end": utt.get("end", 0.0),
                    "text": text
                }

                # 保存说话人标签（开启 diarization 时存在）
                # 严格检查：None 和空字符串均不写入，避免污染非分离模式的数据
                speaker = utt.get("speaker")
                if speaker is not None and str(speaker).strip():
                    seg["speaker"] = str(speaker).strip()

                if need_words and utt.get("words"):
                    seg["words"] = [
                        {
                            "word": w.get("word", ""),
                            "start": w.get("start", 0.0),
                            "end": w.get("end", 0.0)
                        }
                        for w in utt["words"]
                    ]

                segments.append(seg)

            segments = self._filter_hallucinations(segments)
            segments = self._consolidate_utterances(segments)

            # 用词级时间戳重建 segment 的 start/end（词级时间戳更精准，
            # 避免 fix_timestamp_overlaps 截断重叠时产生异常短时长 segment）
            for seg in segments:
                seg_words = seg.get("words", [])
                if seg_words:
                    seg["start"] = seg_words[0]["start"]
                    seg["end"]   = seg_words[-1]["end"]

            segments = fix_timestamp_overlaps(segments)
            return segments

        except GladiaError:
            raise
        except Exception as e:
            raise GladiaError(f"解析转录结果时发生错误，数据格式可能异常: {e}")

    # ------------------------------------------------------------------
    # 错误解析
    # ------------------------------------------------------------------

    def _raise_for_gladia_status(self, response: requests.Response, step: str = ""):
        """根据 HTTP 状态码抛出对应的用户友好中文错误。"""
        code = response.status_code
        if code < 400:
            return

        # 尝试解析响应体中的 message
        try:
            body = response.json()
            server_msg = body.get("message") or body.get("error") or ""
        except Exception:
            server_msg = response.text[:200] if response.text else ""

        step_prefix = f"[{step}] " if step else ""

        if code == 400:
            raise GladiaError(
                f"{step_prefix}请求参数错误（400）。\n"
                f"可能原因：音频格式不受支持或参数配置有误。\n"
                f"服务器提示: {server_msg}"
            )
        elif code == 401:
            raise GladiaError(
                f"{step_prefix}Gladia API Key 无效或已过期（401）。\n"
                f"请在全局设置中重新填写正确的 Gladia API Key。"
            )
        elif code == 402:
            raise GladiaError(
                f"{step_prefix}Gladia 免费额度已用完，需要充值才能继续使用（402）。\n"
                f"请前往 https://app.gladia.io 查看账户余额并充值。"
            )
        elif code == 403:
            raise GladiaError(
                f"{step_prefix}当前账户权限不足（403）。\n"
                f"部分高级功能（如自定义词汇表）可能需要升级套餐。\n"
                f"服务器提示: {server_msg}"
            )
        elif code == 413:
            raise GladiaError(
                f"{step_prefix}上传文件过大，超出服务器接受限制（413）。\n"
                f"请将音频切分为较短片段后重试。"
            )
        elif code == 422:
            raise GladiaError(
                f"{step_prefix}音频文件无法被识别（422）。\n"
                f"可能原因：文件格式损坏、编码异常，或音频时长超出限制。\n"
                f"服务器提示: {server_msg}"
            )
        elif code == 429:
            raise GladiaError(
                f"{step_prefix}请求过于频繁，已触发 Gladia 速率限制（429）。\n"
                f"请稍等片刻后重试。"
            )
        elif code >= 500:
            raise GladiaError(
                f"{step_prefix}Gladia 服务器内部错误（{code}），请稍后重试。\n"
                f"如问题持续，可前往 https://status.gladia.io 查看服务状态。"
            )
        else:
            raise GladiaError(
                f"{step_prefix}收到未知错误响应（HTTP {code}）: {server_msg}"
            )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _consolidate_utterances(self, segments: list) -> list:
        """
        将 Gladia 短话语合并为接近 Whisper 段落长度的段落，
        避免过短的 utterance 传入断句 LLM 后被进一步拆碎。

        合并规则：
        - 当前段不足 MIN_WORDS 词，且与下一段间距 ≤ MAX_GAP 秒，且说话人相同
          → 继续合并，直到合并后词数超过 TARGET_WORDS 或间距过大为止
        - 说话人不同时强制切断（保留分离模式准确性）
        """
        import re as _re

        MIN_WORDS    = 10   # 少于此词数的段会尝试与后面合并
        TARGET_WORDS = 30   # 合并目标词数上限（超过后不再继续追加）
        MAX_GAP      = 1.2  # 两段之间最大允许间距（秒）

        if len(segments) < 2:
            return segments

        def _word_count(text: str) -> int:
            return len(_re.findall(r'\w+', text))

        merged = []
        cur = dict(segments[0])
        cur.setdefault("words", [])

        for nxt in segments[1:]:
            gap = nxt.get("start", 0.0) - cur.get("end", 0.0)
            cur_words_n = _word_count(cur.get("text", ""))
            nxt_words_n = _word_count(nxt.get("text", ""))
            combined_n  = cur_words_n + nxt_words_n

            cur_speaker = cur.get("speaker", "")
            nxt_speaker = nxt.get("speaker", "")
            same_speaker = (cur_speaker == nxt_speaker)

            # 只有当前段偏短、间距不大、说话人一致、合并后未超上限时才合并
            if (cur_words_n < MIN_WORDS
                    and gap <= MAX_GAP
                    and same_speaker
                    and combined_n <= TARGET_WORDS):
                cur["end"]  = nxt.get("end", cur["end"])
                cur["text"] = cur["text"].rstrip() + " " + nxt.get("text", "").lstrip()
                cur["words"] = cur["words"] + nxt.get("words", [])
            else:
                merged.append(cur)
                cur = dict(nxt)
                cur.setdefault("words", [])

        merged.append(cur)
        print(f"[GladiaASR] 话语合并：{len(segments)} 段 → {len(merged)} 段")
        return merged

    def _parse_vocabulary(self) -> list:
        """
        从术语表文本中提取源语言词条（即等号左侧部分），
        过滤掉纯中文/短词（< 4 字符），避免 Gladia 音素匹配误触。
        返回适合 custom_vocabulary_config.vocabulary 的列表。
        """
        if not self.glossary_text:
            return []

        pattern = re.compile(r"^\s*(.+?)\s*(?:=|:|=>|->)\s*(.+?)\s*$")
        seen = set()
        vocab = []

        for line in self.glossary_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for chunk in re.split(r"[;；]", line):
                chunk = chunk.strip()
                m = pattern.match(chunk)
                if not m:
                    continue
                src = m.group(1).strip()

                if len(src) < 4:
                    continue
                if all('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' for c in src if c.strip()):
                    continue
                if src.isdigit():
                    continue

                if src not in seen:
                    seen.add(src)
                    vocab.append({"value": src})

        return vocab

    def _filter_hallucinations(self, segments: list) -> list:
        """过滤常见幻觉片段。"""
        if not cfg.transcription_hallucination_filter.value:
            return segments

        hallucination_keywords = [
            "请不吝点赞", "订阅", "转发", "打赏", "感谢观看", "谢谢大家"
        ]
        filtered = []
        for seg in segments:
            text = seg.get("text", "")
            if any(k in text for k in hallucination_keywords):
                continue
            # 移除了"时长过短+字数过多"的启发式过滤：理由同 cloud_asr.py。
            filtered.append(seg)
        return filtered
