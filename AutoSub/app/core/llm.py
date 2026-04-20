# -*- coding: utf-8 -*-
import json
import logging
import re
import time
import threading
from typing import List, Dict, Any
import httpx
from openai import OpenAI
from app.common.config import cfg

# ── 全局取消标志：线程安全的 Event，set() 表示请求取消 ──
_cancel_event = threading.Event()
# 任务级取消标志：按 scope 隔离取消，避免互相干扰
_scope_cancel_events: Dict[str, threading.Event] = {}
_scope_cancel_lock = threading.Lock()


def interpret_openai_error(e, context: str = "") -> str:
    """
    将 OpenAI SDK / 兼容接口的异常转换为用户可读的中文提示。
    context: 调用场景描述，如"翻译"、"优化"、"云端转录"，用于定制提示文案。
    """
    prefix = f"[{context}] " if context else ""
    err_str = str(e).lower()
    err_raw = str(e)

    # ── OpenAI SDK 具名异常 ────────────────────────────────────────────
    try:
        import openai
        if isinstance(e, openai.AuthenticationError):
            return (f"{prefix}API Key 无效或已过期（401）。\n"
                    f"请检查全局设置中填写的 API Key 是否正确。\n"
                    f"建议：前往设置页面重新填写 API Key 后重试。")
        if isinstance(e, openai.PermissionDeniedError):
            return (f"{prefix}账户权限不足（403）。\n"
                    f"请确认 API Key 对应账户有权限使用当前模型或功能。\n"
                    f"建议：检查 API Key 权限或更换有权限的 Key。")
        if isinstance(e, openai.NotFoundError):
            return (f"{prefix}模型不存在或无法访问（404）。\n"
                    f"请检查设置中填写的模型名称是否正确。\n"
                    f"建议：确认模型名称拼写无误后重试。")
        if isinstance(e, openai.RateLimitError):
            return (f"{prefix}请求频率超限或账户余额不足（429）。\n"
                    f"请稍后重试，或检查账户余额是否充足。\n"
                    f"建议：降低并发数后点击重试。")
        if isinstance(e, openai.APITimeoutError):
            return (f"{prefix}API 请求超时（60秒无响应，已自动终止）。\n"
                    f"可能原因：网络不稳定、服务器繁忙或批次过大。\n"
                    f"建议：减小批次大小后重试。")
        if isinstance(e, openai.APIConnectionError):
            return (f"{prefix}无法连接到 API 服务器。\n"
                    f"请检查网络连接、Base URL 填写是否正确，以及是否需要代理。\n"
                    f"建议：检查代理设置或切换网络后重试。")
        if isinstance(e, openai.BadRequestError):
            return (f"{prefix}请求参数错误（400）。\n"
                    f"可能原因：上传文件格式不支持、参数填写有误。\n"
                    f"详情: {err_raw[:200]}\n"
                    f"建议：检查参数配置后重试。")
        if isinstance(e, openai.InternalServerError):
            return (f"{prefix}API 服务器内部错误（5xx）。\n"
                    f"这是服务商侧的问题。\n"
                    f"建议：等待几分钟后重试。")
    except ImportError:
        pass

    # ── 关键字兜底匹配 ─────────────────────────────────────────────────
    if "401" in err_str or "unauthorized" in err_str or "api key" in err_str or "authentication" in err_str:
        return (f"{prefix}API Key 无效或已过期（401）。\n"
                f"请检查全局设置中填写的 API Key 是否正确。\n"
                f"建议：前往设置页面重新填写 API Key 后重试。")
    if "402" in err_str or "payment" in err_str or "quota" in err_str or "billing" in err_str or "insufficient" in err_str:
        return (f"{prefix}账户余额不足或免费额度已用完（402）。\n"
                f"请为当前 API 账户充值后重试。\n"
                f"建议：充值后点击重试。")
    if "403" in err_str or "forbidden" in err_str or "permission" in err_str:
        return (f"{prefix}账户权限不足（403）。\n"
                f"请确认 API Key 对应账户有权限使用当前模型。\n"
                f"建议：检查 API Key 权限或更换有权限的 Key。")
    if "404" in err_str or "not found" in err_str or "model" in err_str and "not exist" in err_str:
        return (f"{prefix}模型不存在或无法访问（404）。\n"
                f"请检查设置中填写的模型名称是否正确。\n"
                f"建议：确认模型名称拼写无误后重试。")
    if "429" in err_str or "rate limit" in err_str or "too many" in err_str:
        return (f"{prefix}请求频率超限（429）。\n"
                f"建议：降低并发数后点击重试。")
    if "timeout" in err_str or "timed out" in err_str:
        return (f"{prefix}API 请求超时。\n"
                f"可能原因：网络不稳定或批次过大。\n"
                f"建议：减小批次大小后重试。")
    if "connection" in err_str or "connect" in err_str or "network" in err_str or "name or service" in err_str:
        return (f"{prefix}无法连接到 API 服务器。\n"
                f"建议：检查代理设置或切换网络后重试。")
    if "500" in err_str or "502" in err_str or "503" in err_str or "internal server" in err_str:
        return (f"{prefix}API 服务器内部错误（5xx）。\n"
                f"建议：等待几分钟后重试。")

    # ── 兜底：返回原始信息 ─────────────────────────────────────────────
    return f"{prefix}发生未知错误: {err_raw[:300]}\n建议：检查网络和配置后重试。"

class LLMManager:
    """
    统一管理 LLM 客户端和调用
    """
    _instance = None

    _lock = threading.Lock()

    # 默认请求超时（秒）：避免单次请求挂太久浪费 token 费用
    # 翻译批次通常 10-30s 完成，60s 足够覆盖大批次和慢网络
    DEFAULT_TIMEOUT = 60.0

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(LLMManager, cls).__new__(cls)
                    # 改为字典：每个 (api_key, base_url) 组合对应独立客户端
                    # 解决多 API Key 并发时互相覆盖的竞态问题
                    cls._instance.clients = {}
                    cls._instance._client_lock = threading.Lock()
        return cls._instance

    def _get_scope_event(self, scope: str) -> threading.Event:
        with _scope_cancel_lock:
            event = _scope_cancel_events.get(scope)
            if event is None:
                event = threading.Event()
                _scope_cancel_events[scope] = event
            return event

    def _is_cancelled(self, scope: str = None) -> bool:
        if _cancel_event.is_set():
            return True
        if not scope:
            return False
        return self._get_scope_event(scope).is_set()

    def cancel_scope(self, scope: str):
        if not scope:
            return
        self._get_scope_event(scope).set()
        print(f"[LLMManager] 已取消任务作用域: {scope}")

    def reset_scope(self, scope: str):
        if not scope:
            return
        self._get_scope_event(scope).clear()

    def cancel_all(self):
        """
        取消所有正在进行的 LLM 请求。
        设置全局取消标志，并关闭所有 httpx 客户端连接，
        使正在等待响应的请求立即抛出异常。
        """
        _cancel_event.set()
        with _scope_cancel_lock:
            for event in _scope_cancel_events.values():
                event.set()

        with self._client_lock:
            for key, client in list(self.clients.items()):
                try:
                    # 关闭底层 httpx 客户端，中断所有活跃连接
                    client.close()
                except Exception:
                    pass
            # 清空客户端缓存，下次调用时会重新创建
            self.clients.clear()
        print("[LLMManager] 已取消所有请求并关闭连接")

    def reset_cancel(self):
        """重置取消标志，允许新的请求。在启动新任务时调用。"""
        _cancel_event.clear()
        with _scope_cancel_lock:
            for event in _scope_cancel_events.values():
                event.clear()

    def get_client(self, config_prefix="llm"):
        """
        获取 OpenAI 客户端。每个不同的 (api_key, base_url) 组合拥有独立客户端实例，
        多线程并发调用不同 API 时不会互相干扰。
        config_prefix: 'optimize', 'llm', 'fix' 等，对应 config.py 中的前缀
        """
        try:
            api_key = getattr(cfg, f"{config_prefix}_api_key").value
            base_url = getattr(cfg, f"{config_prefix}_base_url").value

            # fix 前缀未单独配置时，自动回退到主 LLM 配置
            if not api_key and config_prefix == "fix":
                api_key = cfg.llm_api_key.value
                base_url = cfg.llm_base_url.value

            if not api_key:
                return None

        except AttributeError:
            if config_prefix == "llm":
                api_key = cfg.llm_api_key.value
                base_url = cfg.llm_base_url.value
            else:
                return None

        if not api_key:
            return None

        api_key = api_key.strip() if api_key else api_key
        base_url = (base_url.strip() if base_url else None) or None
        config_hash = f"{api_key}_{base_url}"

        # 快速路径：字典命中无需加锁
        if config_hash in self.clients:
            return self.clients[config_hash]

        # 慢速路径：首次创建加锁，防止重复创建
        with self._client_lock:
            if config_hash in self.clients:
                return self.clients[config_hash]
            # 显式设置读超时，确保超过 60s 未收到完整响应时自动断开，避免长时间挂起和费用
            timeout = httpx.Timeout(self.DEFAULT_TIMEOUT, read=self.DEFAULT_TIMEOUT)
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout
            )
            self.clients[config_hash] = client
            return client

    def _prepare_params(self, model, messages, temperature, max_tokens, expect_json):
        """
        Build API call params with model-specific adaptations.
        Centralizes Gemini / other model quirks in one place.
        """
        params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": self.DEFAULT_TIMEOUT
        }
        if max_tokens:
            params["max_tokens"] = max_tokens

        model_lower = str(model).lower()
        is_gemini = "gemini" in model_lower

        # Gemini via OpenAI-compat: max_tokens is a hard cutoff that truncates
        # mid-JSON. Its native output window is large enough (8k+), so we drop it.
        if is_gemini:
            params.pop("max_tokens", None)

        # Gemini tends to produce truncated output in json_object mode, skip it.
        if expect_json and not is_gemini:
            params["response_format"] = {"type": "json_object"}

        return params

    def call_llm(self, messages, model, temperature=0.3, config_prefix="llm", max_tokens=None, expect_json=False, task_scope: str = None):
        client = self.get_client(config_prefix)
        if not client:
            raise Exception(f"未配置 {config_prefix} API Key")
        max_retries = 3
        base_delay = 1.5
        last_error = None
        for attempt in range(max_retries):
            # ── 取消检查：每次重试前检查是否已取消 ──
            if self._is_cancelled(task_scope):
                raise Exception("任务已取消")

            try:
                params = self._prepare_params(model, messages, temperature, max_tokens, expect_json)

                try:
                    response = client.chat.completions.create(**params)
                    # 请求完成后再次检查取消（避免处理已取消任务的结果）
                    if self._is_cancelled(task_scope):
                        raise Exception("任务已取消")
                    return response.choices[0].message.content.strip()
                except Exception as e:
                    if self._is_cancelled(task_scope):
                        raise Exception("任务已取消")
                    if expect_json:
                        err = str(e).lower()
                        if "response_format" in err or "json" in err or "schema" in err or "unsupported" in err:
                            params.pop("response_format", None)
                            response = client.chat.completions.create(**params)
                            if self._is_cancelled(task_scope):
                                raise Exception("任务已取消")
                            return response.choices[0].message.content.strip()
                    raise e
            except Exception as e:
                last_error = e
                # ── 不可恢复的错误直接抛出，不重试 ──
                try:
                    import openai
                    _non_retryable = (
                        openai.AuthenticationError,
                        openai.PermissionDeniedError,
                        openai.NotFoundError,
                        openai.BadRequestError,
                    )
                    if isinstance(e, _non_retryable):
                        context_name = {"llm": "翻译", "optimize": "优化", "fix": "溢出修复"}.get(config_prefix, config_prefix)
                        readable_msg = interpret_openai_error(e, context=context_name)
                        logging.error(f"[{config_prefix.upper()}] {readable_msg}")
                        raise Exception(readable_msg)
                except ImportError:
                    pass
                if attempt < max_retries - 1:
                    # 429 限流错误使用更长退避
                    try:
                        import openai
                        if isinstance(e, openai.RateLimitError):
                            delay = base_delay * (3 ** attempt)
                        else:
                            delay = base_delay * (2 ** attempt)
                    except ImportError:
                        delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                    continue
                # 将原始异常转换为可读中文后重新抛出
                context_name = {"llm": "翻译", "optimize": "优化", "fix": "溢出修复"}.get(config_prefix, config_prefix)
                readable_msg = interpret_openai_error(last_error, context=context_name)
                logging.error(f"[{config_prefix.upper()}] {readable_msg}")
                raise Exception(readable_msg)

    def test_connection(self, config_prefix="llm"):
        """
        测试 API 连接并返回响应时间。
        使用最轻量的请求参数，兼容 OpenAI / Gemini / Claude 等各类模型。
        """
        try:
            # 获取配置
            api_key = getattr(cfg, f"{config_prefix}_api_key").value
            base_url = getattr(cfg, f"{config_prefix}_base_url").value
            model = getattr(cfg, f"{config_prefix}_model").value
            
            if not api_key:
                return False, "错误: 未填写 API Key", 0
            
            if api_key:
                api_key = api_key.strip()
            if base_url:
                base_url = base_url.strip()
            
            # 创建临时客户端，设置更精细的超时：
            # connect=10s（TCP+TLS握手），read=20s（等待响应），write=10s
            import httpx
            test_client = OpenAI(
                api_key=api_key, 
                base_url=base_url if base_url else None,
                timeout=httpx.Timeout(20.0, connect=10.0)
            )
            
            start_time = time.time()

            # 使用最兼容的参数组合：
            # - 不设 max_tokens（某些模型/中转站不支持或参数名不同）
            # - temperature 用 0.3 而非 0.0（某些模型不接受 0.0）
            # - 极短 prompt 减少延迟和费用
            try:
                response = test_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Hi"}],
                    max_tokens=3,
                    temperature=0.3
                )
            except Exception as first_err:
                err_str = str(first_err).lower()
                # 先判断是否是致命错误（模型不存在、无权限、限流），不重试
                if any(kw in err_str for kw in ["429", "rate limit", "too many", "resource_exhausted", "quota", "exceeded"]):
                    raise first_err  # 限流/配额耗尽，直接抛出让外层处理
                elif any(kw in err_str for kw in ["404", "not found", "model_not_found"]):
                    raise first_err  # 模型不存在
                elif any(kw in err_str for kw in ["401", "403", "unauthorized", "forbidden", "permission"]):
                    raise first_err  # 认证/权限问题
                # 如果是参数问题（400），尝试去掉 max_tokens 重试
                elif any(kw in err_str for kw in ["max_tokens", "max_output_tokens", "parameter", "invalid", "bad request", "400"]):
                    time.sleep(1.5)  # 避免连续请求
                    try:
                        response = test_client.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": "Hi"}],
                            temperature=0.3
                        )
                    except Exception as second_err:
                        # 第二次还失败，说明不是参数问题，抛出原始错误
                        raise first_err
                else:
                    raise first_err
            
            elapsed = time.time() - start_time
            
            # 只要有有效 response 就算成功
            if response and response.choices:
                return True, f"连接成功! (模型: {model})", elapsed
            else:
                return False, "错误: API 返回结构异常（无 choices）", elapsed
                
        except Exception as e:
            error_msg = str(e)
            err_lower = error_msg.lower()
            # 优先用 SDK 异常类型精确判断
            try:
                import openai
                if isinstance(e, openai.AuthenticationError):
                    return False, "错误: API Key 无效或未授权 (401)", 0
                elif isinstance(e, openai.PermissionDeniedError):
                    return False, "错误: 无权限访问该模型 (403)", 0
                elif isinstance(e, openai.NotFoundError):
                    return False, f"错误: 模型 '{model}' 不存在或 API 地址错误 (404)", 0
                elif isinstance(e, openai.RateLimitError):
                    return False, f"错误: 请求频率超限或配额不足 (429)。\n该模型在中转站可能无可用渠道或额度已耗尽，请检查中转站后台。", 0
                elif isinstance(e, openai.BadRequestError):
                    return False, f"错误: 请求参数有误 — {error_msg[:200]}", 0
            except ImportError:
                pass
            # 通用错误判断
            if "429" in error_msg or "rate limit" in err_lower or "quota" in err_lower or "exceeded" in err_lower:
                return False, f"错误: 请求频率超限或配额不足 (429)。\n该模型在中转站可能无可用渠道或额度已耗尽，请检查中转站后台。", 0
            elif "401" in error_msg:
                return False, "错误: API Key 无效或未授权", 0
            elif "404" in error_msg:
                return False, f"错误: 模型 '{model}' 不存在或 API 地址错误", 0
            elif "connection" in err_lower or "timeout" in err_lower or "connect" in err_lower:
                return False, "错误: 无法连接到服务器，请检查网络或 Base URL", 0
            else:
                return False, f"错误: {error_msg[:300]}", 0

llm_manager = LLMManager()

class LLMTranslator:
    """
    参考 002 项目的翻译逻辑实现
    """
    _OVERFLOW_CONTEXT_ITEMS = 2
    _OVERFLOW_CONTEXT_MAX_CHARS = 80
    _OVERFLOW_BATCH_SIZE = 4
    _WEAK_BOUNDARY_END_TOKENS_EN = {
        "and", "but", "or", "so", "that", "if", "when", "while", "because",
        "to", "of",
    }
    _WEAK_BOUNDARY_START_TOKENS_EN = {
        "and", "but", "or", "so", "then", "because", "if", "when",
        "while", "that", "to", "of",
    }
    _CONTINUATION_START_TOKENS_EN = {
        "and", "but", "or", "so", "then", "because", "if", "when", "while",
        "that", "which", "who", "whose", "where", "after", "before", "until",
        "since", "as", "than", "to", "of", "in", "on", "at", "for", "from",
        "with", "by", "into", "onto", "over", "under",
    }
    _WEAK_BOUNDARY_END_TOKENS_CJK = (
        "的", "了", "吗", "呢", "啊", "吧", "和", "但", "而", "把", "被",
        "也", "都", "又", "还", "再", "就", "才", "并",
    )
    _WEAK_BOUNDARY_START_TOKENS_CJK = (
        "的", "了", "吗", "呢", "啊", "吧", "而", "但", "和", "把", "被",
        "也", "都", "又", "还", "再", "就", "才", "并", "在",
    )
    _DANGLING_TRANSLATION_END_TOKENS_CJK = (
        "的", "地", "得", "上的", "中的", "里的", "下的", "前的", "后的",
    )
    _TINY_TRANSLATION_FRAGMENTS_CJK = (
        "在", "于", "从", "向", "对", "给", "把", "被", "和", "与", "并",
        "也", "都", "又", "还", "就", "才", "而", "但", "的",
    )

    def __init__(self, task_scope: str = None):
        # 不再缓存配置值，改为动态读取，确保运行时修改能即时生效
        self._task_scope = task_scope

    @property
    def target_lang(self):
        return cfg.targetLanguage.value

    @property
    def video_context(self):
        return cfg.videoContext.value

    @property
    def model(self):
        return cfg.llm_model.value

    @classmethod
    def build_overflow_contexts(cls, subtitles: List[Dict[str, Any]], index: int) -> tuple[str, str]:
        def pick_text(sub: Dict[str, Any]) -> str:
            return re.sub(
                r"\s+",
                " ",
                (
                    sub.get("translated_text")
                    or sub.get("optimized_text")
                    or sub.get("text")
                    or ""
                ).replace("\n", " ")
            ).strip()

        prev_items = [
            pick_text(sub)
            for sub in subtitles[max(0, index - cls._OVERFLOW_CONTEXT_ITEMS): index]
            if pick_text(sub)
        ]
        next_items = [
            pick_text(sub)
            for sub in subtitles[index + 1: index + 1 + cls._OVERFLOW_CONTEXT_ITEMS]
            if pick_text(sub)
        ]
        prev_context = cls._trim_overflow_context(" | ".join(prev_items), keep_tail=True)
        next_context = cls._trim_overflow_context(" | ".join(next_items), keep_tail=False)
        return prev_context, next_context

    @classmethod
    def _trim_overflow_context(cls, text: str, keep_tail: bool) -> str:
        cleaned = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
        if len(cleaned) <= cls._OVERFLOW_CONTEXT_MAX_CHARS:
            return cleaned
        return cleaned[-cls._OVERFLOW_CONTEXT_MAX_CHARS:] if keep_tail else cleaned[:cls._OVERFLOW_CONTEXT_MAX_CHARS]

    @classmethod
    def _strip_boundary_punctuation(cls, text: str, leading: bool) -> str:
        if not text:
            return ""
        pattern = r"^[\s\"'“”‘’\(\)\[\]{}<>.,!?;:，。！？；：、…-]+"
        if leading:
            return re.sub(pattern, "", text).strip()
        return re.sub(r"[\s\"'“”‘’\(\)\[\]{}<>.,!?;:，。！？；：、…-]+$", "", text).strip()

    @classmethod
    def _segment_has_weak_end(cls, text: str) -> bool:
        stripped = cls._strip_boundary_punctuation(text, leading=False)
        if not stripped:
            return False
        if any(stripped.endswith(token) for token in cls._WEAK_BOUNDARY_END_TOKENS_CJK):
            return True
        match = re.search(r"([A-Za-z']+)$", stripped.lower())
        return bool(match and match.group(1) in cls._WEAK_BOUNDARY_END_TOKENS_EN)

    @classmethod
    def _segment_has_weak_start(cls, text: str) -> bool:
        stripped = cls._strip_boundary_punctuation(text, leading=True)
        if not stripped:
            return False
        if any(stripped.startswith(token) for token in cls._WEAK_BOUNDARY_START_TOKENS_CJK):
            return True
        match = re.match(r"([A-Za-z']+)", stripped.lower())
        return bool(match and match.group(1) in cls._WEAK_BOUNDARY_START_TOKENS_EN)

    @classmethod
    def _segment_is_tiny_cjk_fragment(cls, text: str) -> bool:
        stripped = cls._strip_boundary_punctuation(text, leading=True)
        stripped = cls._strip_boundary_punctuation(stripped, leading=False)
        if not stripped:
            return False
        cjk_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", stripped)
        return 0 < len(cjk_chars) <= 1

    @classmethod
    def _has_weak_overflow_boundaries(cls, segments: List[Dict[str, str]]) -> bool:
        if len(segments) < 2:
            return False
        for left, right in zip(segments, segments[1:]):
            left_text = (left.get("text") or "").strip()
            right_text = (right.get("text") or "").strip()
            left_cn = (left.get("cn") or "").strip()
            right_cn = (right.get("cn") or "").strip()
            if (
                cls._segment_has_weak_end(left_text)
                or cls._segment_has_weak_start(right_text)
                or cls._segment_has_weak_end(left_cn)
                or cls._segment_has_weak_start(right_cn)
                or cls._segment_is_tiny_cjk_fragment(left_cn)
                or cls._segment_is_tiny_cjk_fragment(right_cn)
            ):
                return True
        return False

    @classmethod
    def _source_pair_is_continuation(cls, left_text: str, right_text: str) -> bool:
        left = (left_text or "").strip()
        right = (right_text or "").strip()
        if not left or not right:
            return False
        if re.search(r'[。！？.!?…]["\')\]]*$', left):
            return False

        match = re.match(r"([A-Za-z']+)", right)
        if not match:
            return False
        token = match.group(1).lower()
        return token in cls._CONTINUATION_START_TOKENS_EN or token[:1].islower()

    @classmethod
    def _translation_has_dangling_modifier(cls, text: str) -> bool:
        raw = (text or "").strip()
        if not raw or re.search(r'[。！？.!?…]$', raw):
            return False
        stripped = cls._strip_boundary_punctuation(raw, leading=False)
        if not stripped:
            return False
        return any(stripped.endswith(token) for token in cls._DANGLING_TRANSLATION_END_TOKENS_CJK)

    @classmethod
    def _translation_is_tiny_fragment(cls, text: str) -> bool:
        stripped = cls._strip_boundary_punctuation(text, leading=True)
        stripped = cls._strip_boundary_punctuation(stripped, leading=False)
        if not stripped:
            return False
        cjk_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", stripped)
        return 0 < len(cjk_chars) <= 2 and stripped in cls._TINY_TRANSLATION_FRAGMENTS_CJK

    @classmethod
    def _needs_continuation_reflection(
        cls,
        source_batch: List[Dict[str, Any]],
        translated_batch: List[Dict[str, Any]],
    ) -> bool:
        if not source_batch or not translated_batch:
            return False

        for item in translated_batch:
            cn = (item.get("cn") or "").strip()
            if cls._translation_has_dangling_modifier(cn) or cls._translation_is_tiny_fragment(cn):
                return True

        paired_count = min(len(source_batch), len(translated_batch))
        for i in range(paired_count - 1):
            left_src = source_batch[i].get("text", "")
            right_src = source_batch[i + 1].get("text", "")
            if not cls._source_pair_is_continuation(left_src, right_src):
                continue

            left_cn = (translated_batch[i].get("cn") or "").strip()
            right_cn = (translated_batch[i + 1].get("cn") or "").strip()
            if (
                cls._translation_has_dangling_modifier(left_cn)
                or cls._translation_has_dangling_modifier(right_cn)
                or cls._translation_is_tiny_fragment(left_cn)
                or cls._translation_is_tiny_fragment(right_cn)
            ):
                return True

        return False

    @staticmethod
    def _source_batch_ids(source_batch: List[Dict[str, Any]]) -> List[str]:
        return [str(sub.get("index", i + 1)) for i, sub in enumerate(source_batch or [])]

    @classmethod
    def _normalize_translated_batch_to_source(
        cls,
        source_batch: List[Dict[str, Any]],
        translated_batch: List[Dict[str, Any]] | None,
    ) -> List[Dict[str, Any]]:
        if not source_batch:
            return translated_batch or []

        source_ids = cls._source_batch_ids(source_batch)
        source_id_set = set(source_ids)
        translated_batch = translated_batch or []

        matched: Dict[str, Dict[str, Any]] = {}
        matched_count = 0
        for item in translated_batch:
            item_id = item.get("id")
            key = str(item_id) if item_id is not None else ""
            cn = str(item.get("cn", "") or "").strip()
            if key in source_id_set and key not in matched:
                matched[key] = {
                    "id": int(key) if key.isdigit() else key,
                    "cn": cn,
                }
                if cn:
                    matched_count += 1

        if matched_count == 0 and len(translated_batch) == len(source_batch):
            print("[Translator] Returned ids do not match the source batch; remapping by batch order.")
            remapped = []
            for src_id, item in zip(source_ids, translated_batch):
                remapped.append({
                    "id": int(src_id) if src_id.isdigit() else src_id,
                    "cn": str(item.get("cn", "") or "").strip(),
                })
            return remapped

        if 0 < matched_count < len(source_batch):
            print(
                f"[Translator] Partial id match detected ({matched_count}/{len(source_batch)}); "
                "keeping unmatched subtitles empty to avoid downstream misalignment."
            )

        normalized = []
        for src_id in source_ids:
            normalized.append(
                matched.get(
                    src_id,
                    {
                        "id": int(src_id) if src_id.isdigit() else src_id,
                        "cn": "",
                    },
                )
            )
        return normalized

    @classmethod
    def _merge_context_strings(cls, left: str, right: str, keep_tail: bool) -> str:
        merged = " | ".join(part for part in [left, right] if part)
        return cls._trim_overflow_context(merged, keep_tail=keep_tail) if merged else ""

    @staticmethod
    def _is_suspicious_subtitle(sub: Dict[str, Any]) -> bool:
        return bool(sub.get("is_suspicious"))

    def _build_translation_work_items(self, batch_subtitles: List[Dict[str, Any]]) -> List[tuple[int, int, bool]]:
        if not batch_subtitles:
            return []
        if not any(self._is_suspicious_subtitle(sub) for sub in batch_subtitles):
            return [(0, len(batch_subtitles), False)]

        work_items = []
        i = 0
        total = len(batch_subtitles)
        while i < total:
            if self._is_suspicious_subtitle(batch_subtitles[i]):
                end = min(total, i + 3)
                while end > i + 1 and not any(self._is_suspicious_subtitle(sub) for sub in batch_subtitles[i:end]):
                    end -= 1
                work_items.append((i, end, True))
                i = end
            else:
                start = i
                while i < total and not self._is_suspicious_subtitle(batch_subtitles[i]):
                    i += 1
                work_items.append((start, i, False))
        return work_items

    def _build_sub_batch_contexts(
        self,
        batch_subtitles: List[Dict[str, Any]],
        start: int,
        end: int,
        prev_context: str,
        next_context: str,
    ) -> tuple[str, str]:
        local_prev = " | ".join(
            (sub.get("text", "") or "").replace("\n", " ").strip()
            for sub in batch_subtitles[max(0, start - 2):start]
            if (sub.get("text", "") or "").strip()
        )
        local_next = " | ".join(
            (sub.get("text", "") or "").replace("\n", " ").strip()
            for sub in batch_subtitles[end:min(len(batch_subtitles), end + 2)]
            if (sub.get("text", "") or "").strip()
        )
        merged_prev = self._merge_context_strings(prev_context, local_prev, keep_tail=True)
        merged_next = self._merge_context_strings(local_next, next_context, keep_tail=False)
        return merged_prev, merged_next

    def _stabilize_suspicious_translations(
        self,
        batch_subtitles: List[Dict[str, Any]],
        translated_batch: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        stabilized = self._normalize_translated_batch_to_source(batch_subtitles, translated_batch)
        for i, item in enumerate(stabilized):
            if i >= len(batch_subtitles) or not batch_subtitles[i].get("is_suspicious"):
                continue
            src_text = (batch_subtitles[i].get("text") or "").strip()
            cn = (item.get("cn") or "").strip()
            if not cn:
                item["cn"] = src_text
                continue
            if len(cn) > max(40, len(src_text) * 3) and len(src_text) <= 20:
                item["cn"] = src_text
                continue
            prev_cn = (stabilized[i - 1].get("cn") or "").strip() if i > 0 else ""
            next_cn = (stabilized[i + 1].get("cn") or "").strip() if i + 1 < len(stabilized) else ""
            if cn and (cn == prev_cn or cn == next_cn) and cn != src_text:
                item["cn"] = src_text
        return stabilized

    def translate_batch(self,
                       batch_subtitles: List[Dict[str, Any]],
                       prev_context: str,
                       next_context: str,
                       target_lang_name: str = "Chinese",
                       glossary: str = "") -> List[Dict[str, Any]]:
        """翻译一个批次的字幕；可疑条目会自动拆小批并采用更保守策略。"""
        work_items = self._build_translation_work_items(batch_subtitles)
        if len(work_items) <= 1:
            conservative = bool(work_items and work_items[0][2])
            translated = self._translate_batch_core(
                batch_subtitles,
                prev_context,
                next_context,
                target_lang_name=target_lang_name,
                glossary=glossary,
                conservative=conservative,
            )
            return self._stabilize_suspicious_translations(batch_subtitles, translated)

        merged_results: List[Dict[str, Any]] = []
        for start, end, conservative in work_items:
            sub_batch = batch_subtitles[start:end]
            local_prev, local_next = self._build_sub_batch_contexts(
                batch_subtitles, start, end, prev_context, next_context
            )
            merged_results.extend(
                self._translate_batch_core(
                    sub_batch,
                    local_prev,
                    local_next,
                    target_lang_name=target_lang_name,
                    glossary=glossary,
                    conservative=conservative,
                )
            )
        return self._stabilize_suspicious_translations(batch_subtitles, merged_results)

    def _translate_batch_core(
        self,
        batch_subtitles: List[Dict[str, Any]],
        prev_context: str,
        next_context: str,
        target_lang_name: str = "Chinese",
        glossary: str = "",
        conservative: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        翻译一个批次的字幕
        """
        max_chars = cfg.max_line_count.value

        # 翻译时的字符软上限：取原文最长条目长度与溢出阈值中的较大值，
        # 再留 30% 余量。避免用过紧的溢出阈值（如 23）强压译文导致丢标点。
        # 真正超长的译文由后续"溢出修复"环节处理。
        longest_src = max((len(sub.get("text", "").replace('\n', ' ').strip()) for sub in batch_subtitles), default=0)
        translate_char_limit = max(max_chars, int(longest_src * 1.3), 35)

        # 构建当前批次的精简 JSON
        current_batch_json = []
        batch_source_texts = []
        for sub in batch_subtitles:
            clean_text = sub.get("text", "").replace('\n', ' ').strip()
            current_batch_json.append({
                "id": sub.get("index", 0),
                "text": clean_text
            })
            batch_source_texts.append(clean_text)

        # 只注入本批次源文本中实际出现的术语，避免无关条目干扰
        filtered_glossary = self._filter_glossary_for_batch(batch_source_texts, glossary)
        glossary_block = f"Glossary (relevant terms only):\n{filtered_glossary}" if filtered_glossary else ""

        suspicious_ids = [sub.get("index", 0) for sub in batch_subtitles if sub.get("is_suspicious")]

        # Generate target-language-specific style guidance to avoid translationese
        target_lang_style = ""
        if "chinese" in target_lang_name.lower() or target_lang_name == "Chinese":
            target_lang_style = (
                '\n4. Use natural Chinese word order inside each subtitle, but do not borrow or move meaning across subtitle boundaries unless the source is explicitly unfinished.'
                '\n5. Keep subtitle boundaries stable. Do not redistribute neighboring source text just to make the Chinese smoother.'
                '\n6. Never leave dangling modifiers or stranded fragments such as "...的", "...上的", or a lone function word/preposition in a subtitle.'
            )
        elif "japanese" in target_lang_name.lower() or target_lang_name == "Japanese":
            target_lang_style = "\n4. Use natural Japanese SOV word order."
        elif "korean" in target_lang_name.lower() or target_lang_name == "Korean":
            target_lang_style = "\n4. Use natural Korean SOV word order."

        def build_prompt(strict: bool):
            conservative_block = ""
            if conservative:
                conservative_block = (
                    "\n4. Low-confidence mode: these subtitles may come from uncertain ASR. Prefer literal translation, keep ambiguity, "
                    "and NEVER complete partial thoughts or guess missing words."
                    "\n5. If the source is fragmentary or awkward, the translation may also stay fragmentary or awkward. Do not smooth it into a fuller sentence."
                )
            if strict:
                return f"""You are a subtitle translation program. Output compressed JSON only.

Translate [current_batch] into {target_lang_name}.
Video Context: {self.video_context}
{glossary_block}

# Format
- Output single-line compressed JSON: {{"translations":[{{"id":1,"cn":"translation"}}]}}
- No Markdown, no explanation, no line breaks.

# Translation Rules
1. Translate faithfully first, naturally second. Do not infer unstated meaning.
2. Keep names, places, and terms consistent with context_before_translated.
3. Keep each subtitle aligned to its own source line. Do NOT redistribute content across neighboring subtitles unless the source is explicitly unfinished and the meaning is obvious.{conservative_block}{target_lang_style}
7. Preserve the original tone and punctuation rhythm. Sentence-ending punctuation (periods, question marks, exclamation marks, ellipses) in the source MUST appear in the translation. Never merge multiple sentences into one unpunctuated run-on.
8. Low-confidence IDs: {suspicious_ids if suspicious_ids else "None"}.

# Example
In: [{{"id":1,"text":"Hi"}}]
Out: {{"translations":[{{"id":1,"cn":"嗨"}}]}}
"""
            return f"""You are a subtitle translation program.

Translate [current_batch] into {target_lang_name}.
Video Context: {self.video_context}
{glossary_block}

# Format
- Output JSON array: [{{"id":1,"cn":"translation"}}]
- No extra text.

# Translation Rules
1. Translate faithfully first, naturally second. Do not infer unstated meaning.
2. Keep names, places, and terms consistent with previous translations.
3. Keep each subtitle aligned to its own source line. Do NOT redistribute content across neighboring subtitles unless the source is explicitly unfinished and the meaning is obvious.{conservative_block}{target_lang_style}
7. Preserve the original tone and punctuation rhythm. Sentence-ending punctuation in the source MUST appear in the translation. Never merge multiple sentences into one unpunctuated run-on.
8. Low-confidence IDs: {suspicious_ids if suspicious_ids else "None"}.
"""
        user_payload = json.dumps({
            "context_before_translated": prev_context,
            "current_batch": current_batch_json,
            "context_after_source": next_context
        }, ensure_ascii=False)
        
        # 按批次大小和译文字符上限动态估算所需 max_tokens
        # 每条译文最多 translate_char_limit 字符，JSON 结构额外开销约 15 token/条
        # 中文每字约 1-2 token；6.0 过于保守，降至 3.5 节省成本
        # Gemini 在 call_llm 层会自动忽略此参数（不传 max_tokens）
        dynamic_max_tokens = min(8192, max(2000, int(len(batch_subtitles) * translate_char_limit * 3.5)))

        try:
            response_content = llm_manager.call_llm(
                messages=[
                    {"role": "system", "content": build_prompt(True)},
                    {"role": "user", "content": user_payload}
                ],
                model=self.model,
                temperature=0.1 if conservative else 0.2,
                config_prefix="llm",
                max_tokens=dynamic_max_tokens,
                expect_json=True,
                task_scope=self._task_scope
            )
            
            translated_batch = self._parse_translated_batch(response_content, len(batch_subtitles))
            translated_batch = self._normalize_translated_batch_to_source(batch_subtitles, translated_batch)
            # 若条数不足或存在空译文，触发宽松格式重试
            has_empty = any(not item.get("cn", "").strip() for item in (translated_batch or []))
            if not translated_batch or len(translated_batch) < len(batch_subtitles) or has_empty:
                response_content = llm_manager.call_llm(
                    messages=[
                        {"role": "system", "content": build_prompt(False)},
                        {"role": "user", "content": user_payload}
                    ],
                    model=self.model,
                    temperature=0.1 if conservative else 0.2,
                    config_prefix="llm",
                    max_tokens=dynamic_max_tokens,
                    expect_json=False,
                    task_scope=self._task_scope
                )
                translated_batch = self._parse_translated_batch(response_content, len(batch_subtitles))
                translated_batch = self._normalize_translated_batch_to_source(batch_subtitles, translated_batch)

            # 最终兜底：若某条 cn 仍为空，用原文占位，避免字幕静默丢失
            for i, item in enumerate(translated_batch or []):
                if not item.get("cn", "").strip():
                    src_text = batch_subtitles[i].get("text", "") if i < len(batch_subtitles) else ""
                    item["cn"] = src_text
                    print(f"[Translator] 第 {item.get('id', i)} 条译文为空，已回退为原文: {src_text[:50]}")

            # 补全缺失条目：LLM 返回条数少于输入时，为缺失的条目创建原文占位
            if translated_batch is not None and len(translated_batch) < len(batch_subtitles):
                existing_ids = {str(item.get("id")) for item in translated_batch}
                for i, sub in enumerate(batch_subtitles):
                    sub_id = str(sub.get("index", i + 1))
                    if sub_id not in existing_ids:
                        fallback_item = {"id": int(sub_id) if sub_id.isdigit() else i + 1, "cn": sub.get("text", "")}
                        translated_batch.append(fallback_item)
                        print(f"[Translator] 第 {sub_id} 条缺失，已补全为原文: {sub.get('text', '')[:50]}")

            # ── 反思翻译（可选）────────────────────────────────────────────
            if (not conservative) and cfg.llm_reflect.value and translated_batch:
                translated_batch = self._reflect_batch(
                    translated_batch, current_batch_json,
                    prev_context, next_context,
                    target_lang_name, glossary_block,
                    dynamic_max_tokens
                )
            elif (not conservative) and translated_batch and self._needs_continuation_reflection(current_batch_json, translated_batch):
                print("[Translator] 检测到跨条残句/悬空定语，触发定向反思修复")
                translated_batch = self._reflect_batch(
                    translated_batch, current_batch_json,
                    prev_context, next_context,
                    target_lang_name, glossary_block,
                    dynamic_max_tokens
                )

            translated_batch = self._apply_glossary_to_batch(translated_batch, glossary)
            return translated_batch
            
        except Exception as e:
            logging.error(f"Translation failed: {e}")
            print(f"Translation failed: {e}") # Ensure user sees this in console
            raise e # Re-raise exception to let caller handle it

    def _try_parse_json(self, text: str):
        """尝试解析 JSON，兼容有效 JSON 后跟多余内容的情况（如 Gemini 的输出风格）。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            try:
                result, _ = json.JSONDecoder().raw_decode(text.lstrip())
                return result
            except Exception:
                return None

    def _try_repair_json(self, text: str):
        """
        Attempt to repair common JSON issues before resorting to a full LLM retry.
        Handles: trailing commas, truncated tails, missing brackets, BOM, control chars.
        """
        if not text:
            return None
        s = text.strip()
        # Remove BOM
        if s.startswith('\ufeff'):
            s = s[1:]
        # Remove control characters except newline/tab
        s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
        # Strip markdown fences
        if s.startswith("```json"):
            s = s[7:]
        if s.startswith("```"):
            s = s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()

        # Fix trailing comma before ] or }
        s = re.sub(r',\s*([}\]])', r'\1', s)

        # Try to close truncated JSON: find the outermost opening bracket
        result = self._try_parse_json(s)
        if result is not None:
            return result

        # Truncated array: ends mid-object or after a comma
        if s.startswith('['):
            # Remove incomplete trailing object (no closing brace)
            repaired = re.sub(r',\s*\{[^}]*$', '', s)
            if not repaired.rstrip().endswith(']'):
                repaired = repaired.rstrip().rstrip(',') + ']'
            result = self._try_parse_json(repaired)
            if result is not None:
                print(f"[Translator] JSON repair succeeded (truncated array)")
                return result

        # Truncated object wrapping an array
        if s.startswith('{'):
            # Try closing with }
            for suffix in ['}', ']}', '"]}', '"}]}']:
                result = self._try_parse_json(s + suffix)
                if result is not None:
                    print(f"[Translator] JSON repair succeeded (truncated object)")
                    return result
            # Extract inner array
            arr = self._extract_json_array(s)
            if arr:
                result = self._try_parse_json(arr)
                if result is not None:
                    return result

        return None

    def _extract_partial_translations(self, text: str):
        """
        截断响应兜底：用正则逐条提取已完成的翻译条目。
        当 Gemini 等模型在 max_tokens 限制下输出不完整 JSON 时，
        可以从残缺的响应中抢救出已生成的部分译文。
        匹配格式：{"id": N, "cn": "..."} 或 {"id":N,"cn":"..."}
        """
        pattern = re.compile(
            r'\{[^{}]*"id"\s*:\s*(\d+)[^{}]*"cn"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
            re.DOTALL
        )
        results = []
        for m in pattern.finditer(text):
            results.append({"id": int(m.group(1)), "cn": m.group(2)})
        if results:
            print(f"[Translator] 截断响应抢救: 提取到 {len(results)} 条完整译文")
        return results

    def _reflect_batch(self,
                       translated_batch: List[Dict[str, Any]],
                       source_batch: List[Dict[str, Any]],
                       prev_context: str,
                       next_context: str,
                       target_lang_name: str,
                       glossary_block: str,
                       max_tokens: int) -> List[Dict[str, Any]]:
        """
        反思翻译：让 LLM 审查初稿并输出改进版本。
        参考吴恩达"翻译-反思-翻译"方法论。
        只在 cfg.llm_reflect 开启时调用。
        """
        # 构建初稿对照表，供 LLM 审查
        draft_pairs = []
        for src, trs in zip(source_batch, translated_batch):
            draft_pairs.append({
                "id": src.get("id", 0),
                "src": src.get("text", ""),
                "draft": trs.get("cn", "")
            })

        reflect_prompt = f"""You are a professional subtitle translation reviewer.

You will be given subtitle translation drafts. Your task:
1. Review each draft translation for accuracy, naturalness, and consistency.
2. Output an improved final translation for each subtitle.

Target language: {target_lang_name}
Video Context: {self.video_context}
{glossary_block}

Context before (translated): {prev_context or "N/A"}
Context after (source): {next_context or "N/A"}

Review criteria:
- Accuracy: Does the translation faithfully convey the source meaning?
- Naturalness: Does it read like native {target_lang_name}, not translated text?
- Consistency: Are names, terms, and tone consistent with context?
- Punctuation: Sentence-ending punctuation must be preserved.
- Cross-subtitle flow: if adjacent subtitles form one sentence, the translations must read naturally when concatenated. Redistribute phrase boundaries across neighboring subtitles if needed, and avoid dangling modifiers or orphaned fragments such as "...的", "...上的", or lone function words.

Output ONLY compressed JSON: {{"translations":[{{"id":1,"cn":"improved translation"}}]}}
No explanation, no markdown."""

        user_payload = json.dumps({
            "drafts": draft_pairs
        }, ensure_ascii=False)

        try:
            response_content = llm_manager.call_llm(
                messages=[
                    {"role": "system", "content": reflect_prompt},
                    {"role": "user", "content": user_payload}
                ],
                model=self.model,
                temperature=0.2,
                config_prefix="llm",
                max_tokens=max_tokens,
                expect_json=True,
                task_scope=self._task_scope
            )
            reflected = self._parse_translated_batch(response_content, len(translated_batch))
            reflected = self._normalize_translated_batch_to_source(source_batch, reflected)

            # 校验：条数必须匹配，且无空译文，否则保留原初稿
            if (reflected and
                    len(reflected) == len(translated_batch) and
                    all(item.get("cn", "").strip() for item in reflected)):
                print(f"[Reflect] 反思翻译完成，共 {len(reflected)} 条")
                return reflected
            else:
                print(f"[Reflect] 反思结果不完整（{len(reflected) if reflected else 0}/{len(translated_batch)}），保留初稿")
                return translated_batch

        except Exception as e:
            logging.warning(f"[Reflect] 反思翻译失败，保留初稿: {e}")
            return translated_batch

    def _parse_translated_batch(self, response_content: str, expected_count: int):
        print(f"[Translator] 原始响应(前300字): {response_content[:300]}")
        content = response_content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        parsed = self._try_parse_json(content)

        if parsed is None:
            array_text = self._extract_json_array(content)
            if array_text:
                parsed = self._try_parse_json(array_text)
        if parsed is None:
            obj_text = self._extract_json_object(content)
            if obj_text:
                parsed = self._try_parse_json(obj_text)

        # ── Repair chain: try fixing broken JSON before falling back to regex ──
        if parsed is None:
            parsed = self._try_repair_json(content)

        # ── Partial extraction: regex-based rescue for truncated responses ──
        if parsed is None:
            rescued = self._extract_partial_translations(content)
            if rescued:
                return rescued

        if isinstance(parsed, dict):
            # 优先查常见包装键
            found = False
            for key in ["data", "translations", "result", "output", "items", "subtitles", "results"]:
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    found = True
                    break
            if not found:
                # 尝试找任意值为 list 的键
                for key, val in parsed.items():
                    if isinstance(val, list) and len(val) > 0:
                        parsed = val
                        found = True
                        break
            if not found:
                # Gemini 可能返回 {"1": "译文1", "2": "译文2"} 格式
                if all(str(k).isdigit() for k in parsed.keys()):
                    normalized = [{"id": int(k), "cn": v} for k, v in sorted(parsed.items(), key=lambda x: int(x[0]))]
                    print(f"[Translator] 检测到 Gemini id:text 字典格式，共 {len(normalized)} 条")
                    return normalized
                # 截断响应导致 _extract_json_object 仅抓到最后一个完整条目 {"id":N,"cn":"..."}
                # 优先用正则从原始响应中抢救所有完整译文，再降级为单条兜底
                if "id" in parsed and ("cn" in parsed or "translation" in parsed):
                    rescued = self._extract_partial_translations(response_content)
                    if rescued:
                        print(f"[Translator] 截断响应救援：正则抢救 {len(rescued)} 条")
                        return rescued
                    cn_val = parsed.get("cn", parsed.get("translation", ""))
                    if cn_val:
                        print(f"[Translator] 截断响应：仅回收 1 条译文 id={parsed.get('id')}")
                        return [{"id": parsed.get("id"), "cn": cn_val}]
                print(f"[Translator] 无法识别的 dict 格式，键名: {list(parsed.keys())[:5]}")
                return []

        if not isinstance(parsed, list):
            print(f"[Translator] 解析结果既不是 list 也不是 dict，类型: {type(parsed).__name__}，内容前100字: {str(parsed)[:100]}")
            # 最后兜底：截断响应中用正则逐条抢救已完成的翻译条目
            rescued = self._extract_partial_translations(response_content)
            if rescued:
                return rescued
            return []

        normalized = []
        for i, item in enumerate(parsed):
            if isinstance(item, dict):
                item_id = item.get("id", item.get("index", item.get("idx", None)))
                cn = item.get("cn", item.get("translation", item.get("translated_text", item.get("output", item.get("text", "")))))
            else:
                item_id = None
                cn = str(item)
            if item_id is None and expected_count == len(parsed):
                item_id = i + 1
            normalized.append({"id": item_id, "cn": cn})

        if not normalized:
            print(f"[Translator] 解析后结果为空，原始内容前200字: {response_content[:200]}")

        return normalized

    def _extract_json_array(self, text: str):
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return ""

    def _extract_json_object(self, text: str):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return ""

    def _parse_glossary_pairs(self, glossary: str):
        if not glossary:
            return []
        text = glossary.replace("；", ";").replace("，", ",")
        parts = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for chunk in line.split(";"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                parts.extend([c.strip() for c in chunk.split(",") if c.strip()])
        pairs = []
        pattern = re.compile(r"^\s*(.+?)\s*(?:=|:|=>|->)\s*(.+?)\s*$")
        for item in parts:
            m = pattern.match(item)
            if not m:
                continue
            src = m.group(1).strip()
            tgt = m.group(2).strip()
            if src and tgt:
                pairs.append((src, tgt))
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        return pairs

    def _apply_glossary_to_text(self, text: str, glossary: str):
        if not text or not glossary:
            return text
        pairs = self._parse_glossary_pairs(glossary)
        if not pairs:
            return text
        updated = text
        for src, tgt in pairs:
            if re.search(r"[A-Za-z0-9_]", src):
                pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(src)}(?![A-Za-z0-9_])")
                updated = pattern.sub(tgt, updated)
            else:
                updated = updated.replace(src, tgt)
        return updated

    def _filter_glossary_for_batch(self, batch_source_texts: List[str], glossary: str) -> str:
        """从术语表中筛选出本批次源文本实际出现的条目，返回格式化字符串。"""
        if not glossary:
            return ""
        pairs = self._parse_glossary_pairs(glossary)
        if not pairs:
            return glossary
        combined = " ".join(batch_source_texts).lower()
        relevant = [f"{src} → {tgt}" for src, tgt in pairs if src.lower() in combined]
        return "\n".join(relevant) if relevant else ""

    def _apply_glossary_to_batch(self, batch: List[Dict[str, Any]], glossary: str):
        """仅补充漏译：只在译文中找不到术语目标译法时才替换，避免覆盖 AI 的正确翻译。"""
        if not batch or not glossary:
            return batch
        pairs = self._parse_glossary_pairs(glossary)
        if not pairs:
            return batch
        updated = []
        for item in batch:
            cn = item.get("cn", "")
            for src, tgt in pairs:
                # 只有当目标译法不在译文中时，才做替换补充（防止覆盖 AI 的正确结果）
                if tgt not in cn:
                    if re.search(r"[A-Za-z0-9_]", src):
                        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(src)}(?![A-Za-z0-9_])")
                        cn = pattern.sub(tgt, cn)
                    else:
                        cn = cn.replace(src, tgt)
            updated.append({"id": item.get("id"), "cn": cn})
        return updated

    def split_overflow(self, text: str, translated_text: str, max_chars: int,
                       prev_context: str = "", next_context: str = "") -> List[Dict[str, str]]:
        """
        溢出修复：把溢出的原文、译文和最大不换行字数发给 API，由模型一次性断句，
        返回多段，每段含原文片段与译文片段，译文每段 ≤ max_chars，且符合阅读习惯。
        允许参考前后条字幕判断承接关系，但只能拆当前条。
        """
        fix_model = cfg.fix_model.value or self.model
        prev_context = self._trim_overflow_context(prev_context, keep_tail=True)
        next_context = self._trim_overflow_context(next_context, keep_tail=False)
        context_block = ""
        if prev_context or next_context:
            context_block = f"""
# 邻接字幕上下文（只用于判断当前条是否承接前后句，不得写入输出）
上文：{prev_context or "无"}
下文：{next_context or "无"}
"""

        prompt = f"""你负责字幕溢出修复断句。给定一条过长的原文与对应译文，请拆成多段，使每段译文不超过 {max_chars} 字，且每段的原文、译文都自然可读。

# 要求
1. 输出多段，每段包含原文片段 "text" 与译文片段 "cn"。
2. 每段 "cn" 的字符数必须 ≤ {max_chars}。
3. 原文、译文内容不得增删改，只做断句；断句处必须符合阅读习惯。
4. 不要因为说话停顿、换气、犹豫、语气停顿就断句；如果只是停顿但语义未结束，必须保持在同一段。
5. 优先按完整句子、完整分句、完整从句或完整并列结构断开；不要把连接词、介词、助词、冠词、短语残片单独留在段首或段尾。
6. 如果当前条与上文或下文有承接关系，只把上下文当作判断依据，仍然只能拆当前条，不能把上下文内容写进结果。
7. 只输出一个 JSON 数组，格式: [{{"text":"原文片段1","cn":"译文1"}}, {{"text":"原文片段2","cn":"译文2"}}, ...]，不要 markdown 包裹或其它说明。{context_block}

# 原文
{text}

# 译文（当前过长，需按上述规则拆成多段）
{translated_text}
"""

        try:
            raw = llm_manager.call_llm(
                messages=[{"role": "user", "content": prompt}],
                model=fix_model,
                temperature=0.2,
                config_prefix="fix",
                max_tokens=min(4000, max(500, int((len(text) + len(translated_text)) * 2.5))),
                task_scope=self._task_scope
            )
            parsed = self._parse_json_clean(raw)
            if isinstance(parsed, dict):
                for k in ["segments", "data", "result", "items"]:
                    if k in parsed and isinstance(parsed[k], list):
                        parsed = parsed[k]
                        break
            if not isinstance(parsed, list) or len(parsed) < 2:
                raise ValueError("API 未返回有效多段结果")
            segments = []
            for item in parsed:
                t = (item.get("text") or item.get("orig") or "").strip()
                c = (item.get("cn") or item.get("translation") or "").strip()
                if not c:
                    continue
                if len(c) > max_chars:
                    raise ValueError(f"某段译文超过 {max_chars} 字")
                segments.append({"text": t or "…", "cn": c})
            if not segments:
                raise ValueError("解析后无有效段")
            # 简单完整性：总译文长度不应明显缩水或膨胀
            combined_cn = "".join(s["cn"] for s in segments)
            if len(combined_cn) < len(translated_text) * 0.5 or len(combined_cn) > len(translated_text) * 1.5:
                raise ValueError("断句后总译文长度异常")
            if self._has_weak_overflow_boundaries(segments):
                raise ValueError("断句边界疑似落在残缺短语或停顿位置")
            print(f"[SplitOverflow] API 断句成功: {len(segments)} 段")
            return segments
        except Exception as e:
            logging.warning(f"[SplitOverflow] 单次 API 断句失败: {e}")
            raise

    def repair_overflow(self, text: str, translated_text: str, max_chars: int,
                        prev_context: str = "", next_context: str = "",
                        reflect_rounds: int | None = None) -> List[Dict[str, str]]:
        """
        Run overflow repair with one initial model attempt plus optional
        reflection retries that feed validation issues back to the model.
        """
        fix_model = cfg.fix_model.value or self.model
        prev_context = self._trim_overflow_context(prev_context, keep_tail=True)
        next_context = self._trim_overflow_context(next_context, keep_tail=False)
        extra_reflections = cfg.fix_reflect_rounds.value if reflect_rounds is None else reflect_rounds
        total_attempts = 1 + max(0, int(extra_reflections or 0))
        max_tokens = min(4000, max(500, int((len(text) + len(translated_text)) * 2.5)))
        feedback = ""
        last_error: Exception = ValueError("Overflow repair did not return a valid result.")

        for attempt in range(total_attempts):
            raw = ""
            try:
                prompt = self._build_overflow_repair_prompt(
                    text=text,
                    translated_text=translated_text,
                    max_chars=max_chars,
                    prev_context=prev_context,
                    next_context=next_context,
                    feedback=feedback,
                )
                raw = llm_manager.call_llm(
                    messages=[{"role": "user", "content": prompt}],
                    model=fix_model,
                    temperature=0.2,
                    config_prefix="fix",
                    max_tokens=max_tokens,
                    expect_json=True,
                    task_scope=self._task_scope
                )
                segments = self._parse_overflow_segments(raw)
                issues = self._collect_overflow_validation_issues(
                    segments=segments,
                    translated_text=translated_text,
                    max_chars=max_chars,
                )
                if not issues:
                    print(f"[SplitOverflow] API split succeeded on attempt {attempt + 1}/{total_attempts}: {len(segments)} segments")
                    return segments

                last_error = ValueError("; ".join(issues))
                logging.warning(
                    f"[SplitOverflow] attempt {attempt + 1}/{total_attempts} invalid: {'; '.join(issues)}"
                )
                if attempt < total_attempts - 1:
                    feedback = self._build_overflow_repair_feedback(
                        issues=issues,
                        segments=segments,
                        raw_response=raw,
                        max_chars=max_chars,
                    )
                    print(f"[SplitOverflow] invalid result on attempt {attempt + 1}/{total_attempts}, regenerating with feedback")
            except Exception as e:
                last_error = e
                logging.warning(f"[SplitOverflow] attempt {attempt + 1}/{total_attempts} failed: {e}")
                if attempt < total_attempts - 1:
                    feedback = self._build_overflow_repair_feedback(
                        issues=[str(e)],
                        segments=None,
                        raw_response=raw,
                        max_chars=max_chars,
                    )
                    print(f"[SplitOverflow] retrying overflow repair after failed attempt {attempt + 1}/{total_attempts}")

        raise last_error

    def _build_overflow_repair_prompt(
        self,
        *,
        text: str,
        translated_text: str,
        max_chars: int,
        prev_context: str,
        next_context: str,
        feedback: str = "",
    ) -> str:
        context_block = ""
        if prev_context or next_context:
            context_block = f"""
# Adjacent context
- Previous subtitles (reference only, never copy into the output): {prev_context or "N/A"}
- Next subtitles (reference only, never copy into the output): {next_context or "N/A"}
"""

        feedback_block = ""
        if feedback:
            feedback_block = f"""
# Previous attempt feedback
{feedback}
"""

        return f"""You are fixing an overflowed subtitle by splitting one subtitle into multiple aligned subtitle segments.

# Output
Return JSON array only:
[{{"text":"source fragment 1","cn":"translation fragment 1"}}, {{"text":"source fragment 2","cn":"translation fragment 2"}}]

# Hard rules
1. Split only the current subtitle. Use adjacent context only to judge continuity.
2. Preserve all content and meaning. Do not add, remove, paraphrase, summarize, or reorder anything.
3. Each "cn" fragment must be <= {max_chars} characters.
4. Keep the "text" fragments and "cn" fragments aligned in order.
5. Prefer complete sentences, clauses, or phrase groups.
6. Never leave dangling fragments at a boundary, especially conjunctions, prepositions, articles, particles, lone pronouns, or tiny trailing words.
7. If the feedback says the previous result was invalid, regenerate the full JSON array from scratch and fix every listed problem.
8. No markdown. No explanation. JSON only.
{context_block}{feedback_block}

# Source text
{text}

# Current translation
{translated_text}
"""

    def repair_overflow_batch(self, items: List[Dict[str, Any]], max_chars: int) -> Dict[int, List[Dict[str, str]]]:
        """
        Best-effort batch overflow repair. Items that fail validation are omitted
        from the result so the caller can retry them individually.
        """
        prepared_items = []
        for item in items:
            item_id = item.get("id")
            text = str(item.get("text") or "").strip()
            translated = str(item.get("translated_text") or item.get("cn") or "").strip()
            if item_id is None or not text or not translated:
                continue
            prepared_items.append({
                "id": int(item_id),
                "text": text,
                "cn": translated,
                "prev_context": self._trim_overflow_context(str(item.get("prev_context") or ""), keep_tail=True),
                "next_context": self._trim_overflow_context(str(item.get("next_context") or ""), keep_tail=False),
            })

        if len(prepared_items) < 2:
            return {}

        prompt = self._build_overflow_batch_prompt(prepared_items, max_chars)
        max_tokens = min(
            6000,
            max(
                1500,
                int(sum(len(item["text"]) + len(item["cn"]) for item in prepared_items) * 2.0),
            ),
        )
        raw = llm_manager.call_llm(
            messages=[{"role": "user", "content": prompt}],
            model=(cfg.fix_model.value or self.model),
            temperature=0.2,
            config_prefix="fix",
            max_tokens=max_tokens,
            expect_json=True,
            task_scope=self._task_scope
        )
        parsed = self._parse_overflow_batch_segments(raw)
        validated: Dict[int, List[Dict[str, str]]] = {}

        for item in prepared_items:
            item_id = item["id"]
            segments = parsed.get(item_id)
            if not segments:
                continue
            issues = self._collect_overflow_validation_issues(
                segments=segments,
                translated_text=item["cn"],
                max_chars=max_chars,
            )
            if issues:
                logging.warning(
                    f"[SplitOverflowBatch] item {item_id} invalid in batch result: {'; '.join(issues)}"
                )
                continue
            validated[item_id] = segments

        print(f"[SplitOverflowBatch] batch repaired {len(validated)}/{len(prepared_items)} items")
        return validated

    def _build_overflow_batch_prompt(self, items: List[Dict[str, Any]], max_chars: int) -> str:
        payload = json.dumps(items, ensure_ascii=False)
        return f"""You are fixing multiple overflowed subtitles in one batch.

# Output
Return JSON array only:
[{{"id":1,"segments":[{{"text":"source fragment","cn":"translation fragment"}}]}}]

# Hard rules
1. You must return one result object for every input item.
2. Keep each item's id unchanged.
3. For every item, split only the current subtitle. Use prev_context and next_context only to judge continuity.
4. Preserve all content and meaning. Do not add, remove, paraphrase, summarize, or reorder anything.
5. Each "cn" fragment must be <= {max_chars} characters.
6. Keep the "text" fragments and "cn" fragments aligned in order.
7. Prefer complete sentences, clauses, or phrase groups.
8. Never leave dangling fragments at a boundary, especially conjunctions, prepositions, articles, particles, lone pronouns, or tiny trailing words.
9. No markdown. No explanation. JSON only.

# Input items
{payload}
"""

    def _parse_overflow_batch_segments(self, response: str) -> Dict[int, List[Dict[str, str]]]:
        parsed = self._parse_json_clean(response)
        if isinstance(parsed, dict):
            for key in ["items", "results", "data", "output"]:
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
        if not isinstance(parsed, list):
            raise ValueError("Overflow batch repair must return a JSON array.")

        result: Dict[int, List[Dict[str, str]]] = {}
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id", item.get("index", item.get("idx")))
            segments = item.get("segments", item.get("items", item.get("data")))
            if item_id is None or not isinstance(segments, list):
                continue
            try:
                result[int(item_id)] = self._normalize_overflow_segments(segments)
            except Exception as e:
                logging.warning(f"[SplitOverflowBatch] failed to parse item {item_id}: {e}")
        return result

    def _parse_overflow_segments(self, response: str) -> List[Dict[str, str]]:
        parsed = self._parse_json_clean(response)
        if isinstance(parsed, dict):
            for key in ["segments", "data", "result", "items"]:
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
        if not isinstance(parsed, list):
            raise ValueError("Overflow repair must return a JSON array.")
        return self._normalize_overflow_segments(parsed)

    def _normalize_overflow_segments(self, items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        segments = []
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Overflow segment {idx} is not an object.")
            source_text = str(item.get("text") or item.get("orig") or item.get("source") or "").strip()
            translated = str(item.get("cn") or item.get("translation") or item.get("translated_text") or "").strip()
            if not translated:
                raise ValueError(f"Overflow segment {idx} is missing cn.")
            segments.append({"text": source_text, "cn": translated})
        return segments

    def _collect_overflow_validation_issues(
        self,
        *,
        segments: List[Dict[str, str]],
        translated_text: str,
        max_chars: int,
    ) -> list[str]:
        issues: list[str] = []
        if len(segments) < 2:
            issues.append("Return at least 2 subtitle segments.")

        for idx, item in enumerate(segments, start=1):
            translated = (item.get("cn") or "").strip()
            if not translated:
                issues.append(f'Segment {idx} has an empty "cn" field.')
                continue
            if len(translated) > max_chars:
                issues.append(f'Segment {idx} exceeds the {max_chars}-character limit.')

        if segments:
            combined_cn = "".join((item.get("cn") or "") for item in segments)
            ratio = len(combined_cn) / max(len(translated_text), 1)
            if not (0.5 <= ratio <= 1.5):
                issues.append(
                    f"Combined translation length is abnormal ({ratio:.2f}x of the input translation)."
                )
            if self._has_weak_overflow_boundaries(segments):
                issues.append("Do not split at weak boundaries or leave dangling fragments.")

        return issues

    def _build_overflow_repair_feedback(
        self,
        *,
        issues: list[str],
        segments: List[Dict[str, str]] | None,
        raw_response: str,
        max_chars: int,
    ) -> str:
        lines = [
            "The previous overflow repair result is invalid.",
            "Fix every problem below and regenerate the full JSON array from scratch.",
            "Problems:",
        ]
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append(f'- Every "cn" fragment must be <= {max_chars} characters.')

        if segments:
            preview = json.dumps(segments, ensure_ascii=False)
            lines.append(f"Previous parsed result: {preview[:600]}")
        elif raw_response:
            compact = re.sub(r"\s+", " ", raw_response).strip()
            lines.append(f"Previous raw response: {compact[:600]}")

        return "\n".join(lines)

    def _parse_json_clean(self, response: str):
        """清理 LLM 响应并解析 JSON。"""
        content = response.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        try:
            return json.loads(content)
        except Exception:
            arr = self._extract_json_array(content)
            if arr:
                try:
                    return json.loads(arr)
                except Exception:
                    pass
        return None
