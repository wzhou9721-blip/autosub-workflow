import os
import shutil
import subprocess
import requests
import io
import logging
import sys
import zipfile
import time
from pathlib import Path
from typing import Callable
from PyQt6.QtCore import QThread, pyqtSignal
from app.common.batch_utils import (
    build_file_signature,
    clean_translated_segments,
    file_signature_equal,
    fmt_ass_time,
    fmt_srt_time,
    fmt_vtt_time,
    get_sub_content,
    is_retryable_translation_error,
    load_glossary_text,
    normalize_path,
    normalize_text_for_compare,
    translated_view_text,
)
from app.common.config import INTERNAL_BIN_PATH, BIN_PATH
from app.common.export_utils import (
    build_export_targets,
    write_ass,
    write_batch_summary,
    write_news_script,
    write_quality_report,
    write_runtime_log,
    write_srt,
    write_translation_summary,
    write_vtt,
)
from app.common.runtime_state import (
    BATCH_SESSION_STATE_FILE,
    BATCH_TRANSLATE_RESUME_FILE,
    OPTIMIZE_RESUME_FILE,
    SPLIT_RESUME_FILE,
    load_json_file,
    overflow_resume_path,
    save_json_atomic,
    safe_unlink,
    translate_resume_path,
)
from app.common.text_utils import TextSplitter, clean_punctuation_text
from app.common.utils import fix_timestamp_overlaps

try:
    from modelscope.hub.callback import ProgressCallback
    from modelscope.hub.snapshot_download import snapshot_download
except ImportError:
    snapshot_download = None
    ProgressCallback = object

class FileDownloadThread(QThread):
    progress = pyqtSignal(float, str)
    finished = pyqtSignal(str) # Returns the saved file path
    error = pyqtSignal(str)

    def __init__(self, url, save_path):
        super().__init__()
        self.url = url
        self.save_path = save_path
        self._is_running = True

    def run(self):
        try:
            os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
            
            response = requests.get(self.url, stream=True, timeout=30)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            block_size = 1024 * 1024 # 1MB
            downloaded_size = 0
            
            with open(self.save_path, 'wb') as f:
                for data in response.iter_content(block_size):
                    if not self._is_running:
                        break
                    f.write(data)
                    downloaded_size += len(data)
                    if total_size > 0:
                        percent = (downloaded_size / total_size) * 100
                        speed_mb = downloaded_size / 1024 / 1024
                        total_mb = total_size / 1024 / 1024
                        msg = f"已下载: {speed_mb:.1f}MB / {total_mb:.1f}MB"
                        self.progress.emit(percent, msg)
            
            if self._is_running:
                self.finished.emit(self.save_path)
            else:
                # Cleanup if stopped
                if os.path.exists(self.save_path):
                    os.remove(self.save_path)
                
        except Exception as e:
            self.error.emit(str(e))


class SplitThread(QThread):
    _TASK_SCOPE_PREFIX = "split"
    """
    异步执行断句优化的线程
    """
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, segments, context=""):
        super().__init__()
        self.segments = segments
        self.context = context
        self._is_running = True
        self._task_scope = f"{self._TASK_SCOPE_PREFIX}:{time.time_ns()}:{id(self)}"

    def run(self):
        from app.core.controller import task_controller
        from app.core.llm import llm_manager
        llm_manager.reset_cancel()
        llm_manager.reset_scope(self._task_scope)
        try:
            if not self._is_running:
                raise TaskCancelledError("任务已取消")
            self.progress.emit(20, "正在语义分析并重新断句...")
            new_segments = task_controller.start_split(self.segments, self.context, task_scope=self._task_scope)
            if not self._is_running:
                raise TaskCancelledError("任务已取消")
            self.progress.emit(100, "断句优化完成")
            self.finished.emit(new_segments)
        except Exception as e:
            self.error.emit(str(e))

    def stop(self):
        self._is_running = False
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)

class UnzipThread(QThread):
    """解压线程 (支持 .7z 和 .zip)"""
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, zip_file, extract_path):
        super().__init__()
        self.zip_file = zip_file
        self.extract_path = extract_path

    def run(self):
        try:
            # 确保目标目录存在
            os.makedirs(self.extract_path, exist_ok=True)
            
            # 使用 7z 命令行解压 (优先尝试)
            # 这种方式对 7z 格式支持最好 (包括 BCJ2 过滤器)
            self._extract_with_7z_cmd()
            
            # 删除压缩包
            if os.path.exists(self.zip_file):
                os.remove(self.zip_file)
            self.finished.emit()
        except Exception as e:
            self.error.emit(f"解压失败: {str(e)}")

    def _extract_with_7z_cmd(self):
        """使用 7z 命令行工具解压"""
        # 1. 尝试查找系统中的 7z
        seven_zip_cmd = "7z"
        
        # 2. 如果系统没有，检查内部资源目录下的 7za.exe
        # 注意：这里使用 INTERNAL_BIN_PATH (打包资源)，但也检查 BIN_PATH (用户可能自己放了)
        local_7za = INTERNAL_BIN_PATH / "7za.exe"
        if not local_7za.exists():
             local_7za = BIN_PATH / "7za.exe"
        
        # 检查系统是否有 7z
        has_system_7z = False
        try:
            subprocess.run([seven_zip_cmd, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            has_system_7z = True
        except FileNotFoundError:
            has_system_7z = False
            
        # 如果系统没有 7z，且本地也没有 7za.exe，则下载 7za.exe 到用户目录
        if not has_system_7z and not local_7za.exists():
            os.makedirs(BIN_PATH, exist_ok=True)
            # 下载 7za920.zip (Standalone console version)
            url = "https://www.7-zip.org/a/7za920.zip"
            zip_path = BIN_PATH / "7za920.zip"
            
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                with open(zip_path, 'wb') as f:
                    f.write(response.content)
                
                # 解压 7za.exe (使用 zipfile，这是 Python 自带的)
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extract('7za.exe', BIN_PATH)
                local_7za = BIN_PATH / "7za.exe"
            except Exception as e:
                raise Exception(f"无法下载 7za.exe 组件: {str(e)}")
            finally:
                if zip_path.exists():
                    os.remove(zip_path)

        # 确定使用的命令
        if has_system_7z:
            cmd_exe = seven_zip_cmd
        else:
            cmd_exe = str(local_7za.absolute())

        # 构造解压命令
        # 7z x archive.7z -o{output_dir} -y
        cmd = [
            cmd_exe, 
            "x", 
            os.path.abspath(self.zip_file), 
            f"-o{os.path.abspath(self.extract_path)}", 
            "-y"
        ]
        
        # 隐藏控制台窗口 (Windows)
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
        process = subprocess.run(cmd, startupinfo=startupinfo, capture_output=True, text=True)
        
        if process.returncode != 0:
            raise Exception(f"7z 解压失败: {process.stderr}")


class SuppressOutput:
    """上下文管理器：抑制 stdout/stderr 和 modelscope 日志"""

    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        self._loggers: dict[str, int] = {}
        for name in ["modelscope", "tqdm"]:
            logger = logging.getLogger(name)
            self._loggers[name] = logger.level
            logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *args):
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        for name, level in self._loggers.items():
            logging.getLogger(name).setLevel(level)


class PipInstallThread(QThread):
    """用于异步执行 pip install 的线程"""
    finished = pyqtSignal()
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, packages):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            # 使用当前虚拟环境的 python 执行 pip
            python_exe = sys.executable
            # 使用清华源加速下载
            cmd = [python_exe, "-m", "pip", "install", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + self.packages
            
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                text=True, 
                encoding='utf-8',
                errors='ignore',
                startupinfo=startupinfo
            )

            for line in process.stdout:
                self.progress.emit(line.strip())

            process.wait()
            if process.returncode == 0:
                self.finished.emit()
            else:
                self.error.emit(f"Pip 安装失败，退出代码: {process.returncode}")
        except Exception as e:
            self.error.emit(str(e))


def create_progress_callback_class(
    progress_callback: Callable[[int, str], None],
) -> type[ProgressCallback]:
    """创建一个自定义的 ProgressCallback 类，用于接收下载进度"""

    class CustomProgressCallback(ProgressCallback):
        def __init__(self, filename: str, file_size: int):
            super().__init__(filename, file_size)
            self.downloaded = 0

        def update(self, size: int):
            self.downloaded += size
            if self.file_size > 0:
                percentage = min(int(self.downloaded * 100 / self.file_size), 99)
                progress_callback(percentage, f"{self.filename}: {percentage}%")

        def end(self):
            pass

    return CustomProgressCallback


class ModelscopeDownloadThread(QThread):
    progress = pyqtSignal(int, str)
    error = pyqtSignal(str)

    def __init__(self, model_id: str, save_path: str):
        super().__init__()
        self.model_id = model_id
        self.save_path = save_path

    def run(self):
        if snapshot_download is None:
            self.error.emit("未安装 modelscope 库")
            return

        try:
            self.progress.emit(0, "开始下载...")

            callback_class = create_progress_callback_class(self.progress.emit)

            with SuppressOutput():
                snapshot_download(
                    self.model_id,
                    local_dir=self.save_path,
                    progress_callbacks=[callback_class],
                )

            self.progress.emit(100, "下载完成")

        except Exception as e:
            self.error.emit(str(e))


class TaskCancelledError(Exception):
    """Exception raised when a task is cancelled by user"""
    pass


class BatchTranscriptionThread(QThread):
    _TASK_SCOPE_PREFIX = "batch"
    _RETRYABLE_TRANSLATION_ERROR_KEYWORDS = (
        "timeout", "timed out", "connection", "connect", "network",
        "rate limit", "429", "temporarily unavailable", "503", "502", "504"
    )
    """
    批量处理线程：依次对多个文件执行 转录 → 优化 → 断句 → 翻译 → 溢出修复 → 自动导出 SRT
    每完成一个文件发射 file_done 信号，全部完成发射 all_done 信号。

    file_entries: list of dict，每项格式为
        { "path": str, "sourceLanguage": str, "targetLanguage": str }
    config: 全局基础配置（每个文件的 sourceLanguage/targetLanguage 会被 file_entries 里的值覆盖）
    """
    file_progress = pyqtSignal(int, int, str)       # (当前文件序号, 总文件数, 进度消息)
    file_done     = pyqtSignal(int, int, str, str)  # (当前, 总, 文件路径, 导出路径)
    all_done      = pyqtSignal(int, int)            # (成功数, 总数)
    error         = pyqtSignal(str)

    def __init__(self, file_entries: list, config: dict):
        super().__init__()
        self.file_entries = file_entries
        self.config       = config
        self._is_running  = True
        self._task_scope  = f"{self._TASK_SCOPE_PREFIX}:{id(self)}"
        self.batch_summary_path = ""
        self.batch_reports = []

    def stop(self):
        self._is_running = False
        # 仅取消当前批量任务作用域，避免误伤其他并行任务
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)
    def _make_cancel_cb(self):
        """返回一个进度回调：收到任何调用时检查取消标志。"""
        def cb(cur, tot):
            if not self._is_running:
                raise TaskCancelledError("任务已取消")
        return cb

    @staticmethod
    def _fmt_srt_time(sec: float) -> str:
        return fmt_srt_time(sec)

    @staticmethod
    def _fmt_vtt_time(sec: float) -> str:
        return fmt_vtt_time(sec)

    @staticmethod
    def _fmt_ass_time(sec: float) -> str:
        return fmt_ass_time(sec)

    @staticmethod
    def _normalize_text_for_compare(text: str) -> str:
        return normalize_text_for_compare(text)

    @staticmethod
    def _translated_view_text(seg: dict) -> str:
        return translated_view_text(seg)

    @staticmethod
    def _clean_translated_segments(segments: list, min_duration_ms: int = 100) -> list:
        return clean_translated_segments(segments, min_duration_ms=min_duration_ms)

    @staticmethod
    def _prepare_export_segments(
        segments: list,
        translated: bool,
        remove_punctuation: bool = False,
        hallucination_filter: bool = False,
    ) -> list:
        if not segments:
            return []

        prepared = [dict(seg) for seg in segments]
        fix_timestamp_overlaps(prepared)

        if translated and remove_punctuation:
            for seg in prepared:
                txt = (seg.get("translated_text") or "")
                if txt:
                    seg["translated_text"] = clean_punctuation_text(txt)

        if translated and hallucination_filter:
            cleaned = clean_translated_segments(prepared)
            if cleaned:
                prepared = cleaned

        fix_timestamp_overlaps(prepared)
        for i, seg in enumerate(prepared):
            seg["index"] = i + 1
        return prepared

    @staticmethod
    def _cleanup_temp_audio_files() -> None:
        from app.core.project import project_manager

        temp_audio_paths = list(project_manager.temp_audio_paths or [])
        for wav_path in temp_audio_paths:
            try:
                if wav_path and os.path.exists(wav_path):
                    safe_unlink(wav_path)
            except Exception:
                pass
        project_manager.temp_audio_paths = []

    @staticmethod
    def _get_sub_content(seg: dict, mode: str) -> str:
        return get_sub_content(seg, mode)

    @staticmethod
    def _load_glossary() -> str:
        return load_glossary_text()

    @staticmethod
    def _normalize_path(p: str) -> str:
        return normalize_path(p)

    @staticmethod
    def _build_file_signature(p: str) -> dict:
        return build_file_signature(p)

    @staticmethod
    def _file_signature_equal(a: dict, b: dict) -> bool:
        return file_signature_equal(a, b)

    @staticmethod
    def _is_retryable_translation_error(err: Exception) -> bool:
        return is_retryable_translation_error(
            err,
            keywords=BatchTranscriptionThread._RETRYABLE_TRANSLATION_ERROR_KEYWORDS,
        )

    @staticmethod
    def _build_translation_contexts(segments: list, batch_start: int, batch_size: int) -> tuple[str, str]:
        prev_ctx = " | ".join(
            (s.get("translated_text") or s.get("text", ""))
            for s in segments[max(0, batch_start - 4): batch_start]
        )
        next_ctx = " | ".join(
            s.get("text", "")
            for s in segments[batch_start + batch_size: batch_start + batch_size + 4]
        )
        return prev_ctx, next_ctx

    @staticmethod
    def _fill_missing_translations(batch: list[dict]) -> None:
        for sub in batch:
            if not (sub.get("translated_text") or "").strip():
                sub["translated_text"] = sub.get("text", "")

    @staticmethod
    def _apply_translation_result(batch: list[dict], result: list[dict]) -> None:
        result_map = {str(item.get("id")): item.get("cn", "") for item in result}
        used_result_indexes = set()

        for idx, item in enumerate(result):
            item_id = str(item.get("id"))
            if any(str(sub.get("index", 0)) == item_id for sub in batch):
                used_result_indexes.add(idx)

        for sub in batch:
            key = str(sub.get("index", 0))
            if key in result_map:
                sub["translated_text"] = result_map[key]

        unfilled = [j for j, sub in enumerate(batch) if not (sub.get("translated_text") or "").strip()]
        remaining_results = [item for idx, item in enumerate(result) if idx not in used_result_indexes]
        if unfilled and remaining_results:
            for k, j in enumerate(unfilled):
                if k < len(remaining_results):
                    cn = remaining_results[k].get("cn", "")
                    if cn.strip():
                        batch[j]["translated_text"] = cn

        BatchTranscriptionThread._fill_missing_translations(batch)

    def _translate_batch_with_retry(
        self,
        translator,
        batch: list[dict],
        *,
        prev_ctx: str,
        next_ctx: str,
        target_lang: str,
        glossary: str,
        batch_number: int,
        max_attempts: int = 2,
    ) -> tuple[bool, Exception | None, int]:
        retry_count = 0
        last_translate_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = translator.translate_batch(
                    batch,
                    prev_context=prev_ctx,
                    next_context=next_ctx,
                    target_lang_name=target_lang,
                    glossary=glossary,
                )
                self._apply_translation_result(batch, result)
                return True, None, retry_count
            except Exception as te:
                last_translate_error = te
                can_retry = self._is_retryable_translation_error(te)
                if can_retry and attempt < max_attempts:
                    retry_count += 1
                    print(f"[BatchTranslation] 批次 {batch_number} 可重试错误，第 {attempt} 次失败，准备重试: {te}")
                    continue
                break

        self._fill_missing_translations(batch)
        return False, last_translate_error, retry_count

    @staticmethod
    def _build_translation_failure(
        batch_start: int,
        batch_size: int,
        total_segments: int,
        err: Exception | None,
    ) -> tuple[dict, bool]:
        err_str = str(err) if err else "unknown"
        retryable = BatchTranscriptionThread._is_retryable_translation_error(err) if err else False
        fail_type = "retryable_exhausted" if retryable else "non_retryable"
        failure = {
            "batch": batch_start // batch_size + 1,
            "range": [batch_start + 1, min(batch_start + batch_size, total_segments)],
            "type": fail_type,
            "error": err_str,
        }
        return failure, fail_type == "non_retryable"

    def _write_translation_failure_summary(
        self,
        *,
        summary_path: str,
        file_name: str,
        total_batches: int,
        skipped_batches: int,
        retryable_retry_count: int,
        non_retryable_skip_count: int,
        file_failures: list[dict],
        start_ts: float,
        idx: int,
        total: int,
        translation_summary: dict,
    ) -> None:
        elapsed = int(time.time() - start_ts)
        failed_batches = len(file_failures)
        success_batches = max(0, total_batches - failed_batches - skipped_batches)
        translation_summary["summary_path"] = write_translation_summary(
            summary_path,
            file_name=file_name,
            total_batches=total_batches,
            success_batches=success_batches,
            failed_batches=failed_batches,
            skipped_batches=skipped_batches,
            retryable_retry_count=retryable_retry_count,
            non_retryable_skip_count=non_retryable_skip_count,
            elapsed_seconds=elapsed,
            failures=file_failures,
        )
        self.file_progress.emit(
            idx + 1,
            total,
            f"[{idx+1}/{total}] 翻译有部分失败，已生成摘要: {os.path.basename(summary_path)}"
        )

    def _save_translation_progress_snapshot(
        self,
        *,
        resume_path,
        segments: list[dict],
        total_segments: int,
        file_path: str,
        target_lang: str,
    ) -> None:
        self._save_translate_progress(
            resume_path,
            segments,
            total_segments,
            source_file_path=file_path,
            target_lang=target_lang,
        )

    def _handle_translation_cancel(
        self,
        *,
        resume_path,
        idx: int,
        segments: list[dict],
        total_segments: int,
        file_path: str,
        target_lang: str,
    ) -> None:
        self._save_translation_progress_snapshot(
            resume_path=resume_path,
            segments=segments,
            total_segments=total_segments,
            file_path=file_path,
            target_lang=target_lang,
        )
        self._save_batch_resume(idx, segments, total_segments, target_lang=target_lang)
        raise TaskCancelledError("任务已取消")

    @staticmethod
    def _update_translation_summary_counts(
        translation_summary: dict,
        *,
        file_failures: list[dict],
        skipped_batches: int,
        retryable_retry_count: int,
        non_retryable_skip_count: int,
    ) -> None:
        translation_summary["failed_batches"] = len(file_failures)
        translation_summary["skipped_batches"] = skipped_batches
        translation_summary["retryable_retries"] = retryable_retry_count
        translation_summary["non_retryable_skips"] = non_retryable_skip_count

    def _append_batch_report(self, file_path: str, status: str, detail: str = "", export_path: str = ""):
        self.batch_reports.append({
            "file": os.path.basename(file_path) if file_path else "",
            "path": file_path,
            "status": status,
            "detail": detail,
            "export": export_path,
        })

    def _write_batch_summary(self):
        if not self.file_entries:
            self.batch_summary_path = ""
            return ""
        first_path = self.file_entries[0].get("path", "")
        if not first_path:
            self.batch_summary_path = ""
            return ""
        summary_path = os.path.join(os.path.dirname(first_path), "批量任务摘要.txt")
        try:
            self.batch_summary_path = write_batch_summary(summary_path, self.batch_reports)
            return self.batch_summary_path
        except Exception as e:
            print(f"[BatchSummary] 写入摘要文件失败: {e}")
            return ""

    def _session_state_path(self):
        return BATCH_SESSION_STATE_FILE

    def _save_session_state(self, stage: str, current_file_index: int = 0):
        try:
            payload = {
                "stage": stage,
                "current_file_index": int(current_file_index),
                "file_entries": self.file_entries,
                "config": self.config,
                "saved_at": int(time.time()),
            }
            save_json_atomic(self._session_state_path(), payload)
        except Exception as e:
            print(f"[BatchSession] 保存会话状态失败: {e}")

    def _clear_session_state(self):
        try:
            safe_unlink(self._session_state_path())
        except Exception as e:
            print(f"[BatchSession] 清理会话状态失败: {e}")

    @staticmethod
    def _save_translate_progress(resume_path, segments, n_segs, source_file_path: str = "", target_lang: str = ""):
        """保存翻译进度到续传文件，包含完整的 segments 数据。"""
        try:
            # 保存完整 segments（含已翻译部分），恢复时可跳过转录/优化/断句
            serializable_segs = []
            for s in segments:
                seg_copy = {
                    "start": s.get("start", 0),
                    "end": s.get("end", 0),
                    "text": s.get("text", ""),
                    "optimized_text": s.get("optimized_text", ""),
                    "index": s.get("index", 0),
                }
                if (s.get("translated_text") or "").strip():
                    seg_copy["translated_text"] = s["translated_text"]
                if s.get("words"):
                    seg_copy["words"] = s["words"]
                if s.get("speaker") is not None:
                    seg_copy["speaker"] = s["speaker"]
                serializable_segs.append(seg_copy)

            translated_count = sum(
                1 for s in segments if (s.get("translated_text") or "").strip()
            )
            save_json_atomic(resume_path, {
                    "total": n_segs,
                    "first_text": segments[0].get("text", "") if segments else "",
                    "translated_count": translated_count,
                    "segments": serializable_segs,
                    "source_file_meta": BatchTranscriptionThread._build_file_signature(source_file_path) if source_file_path else None,
                    "target_lang": target_lang or "",
                })
            print(f"[Translation] 进度已保存: {translated_count}/{n_segs} 条")
        except Exception as e:
            print(f"[Translation] 保存进度失败: {e}")

    def _save_batch_resume(self, current_file_index: int, segments: list, n_segs: int, target_lang: str = ""):
        """保存批量任务断点：当前文件索引 + 当前文件 segments，便于再次开始时从该文件续传。"""
        try:
            file_signatures = [self._build_file_signature(e["path"]) for e in self.file_entries]
            serializable_segs = []
            for s in segments:
                seg_copy = {
                    "start": s.get("start", 0),
                    "end": s.get("end", 0),
                    "text": s.get("text", ""),
                    "optimized_text": s.get("optimized_text", ""),
                    "index": s.get("index", 0),
                }
                if (s.get("translated_text") or "").strip():
                    seg_copy["translated_text"] = s["translated_text"]
                if s.get("words"):
                    seg_copy["words"] = s["words"]
                if s.get("speaker") is not None:
                    seg_copy["speaker"] = s["speaker"]
                serializable_segs.append(seg_copy)
            translated_count = sum(
                1 for s in segments if (s.get("translated_text") or "").strip()
            )
            path = BATCH_TRANSLATE_RESUME_FILE
            save_json_atomic(path, {
                    "file_signatures": file_signatures,
                    "current_file_index": current_file_index,
                    "translated_count": translated_count,
                    "segments": serializable_segs,
                    "target_lang": target_lang or "",
                })
            print(f"[Translation] 批量断点已保存: 文件 {current_file_index + 1}/{len(self.file_entries)}，已译 {translated_count}/{n_segs} 条")
        except Exception as e:
            print(f"[Translation] 保存批量断点失败: {e}")

    def run(self):
        from app.core.controller import task_controller
        from app.core.llm import LLMTranslator, llm_manager
        from app.common.config import cfg

        self.batch_reports = []
        self.batch_summary_path = ""
        self._save_session_state(stage="batch_started", current_file_index=0)

        # 重置当前任务作用域的取消标志，允许新的 API 请求
        llm_manager.reset_scope(self._task_scope)

        total   = len(self.file_entries)
        success = 0
        glossary = self._load_glossary()

        # ── 批量断点续传：若上次在翻译中途被终止，从当时所在文件继续，不再重跑前面的文件 ──
        start_idx = 0
        saved_resume_data = None  # 若不为 None：{ "segments": [...], "translated_count": N }
        if self.config.get("workflowTranslate", False):
            _batch_resume_path = BATCH_TRANSLATE_RESUME_FILE
            if _batch_resume_path.exists():
                try:
                    br = load_json_file(_batch_resume_path)

                    saved_signatures = br.get("file_signatures")
                    if not saved_signatures:
                        # 兼容旧版本：仅存了路径
                        saved_paths = br.get("file_paths", [])
                        saved_signatures = [{"path": p, "size": None, "mtime_ns": None} for p in saved_paths]

                    current_signatures = [self._build_file_signature(e["path"]) for e in self.file_entries]
                    same_files = (
                        len(saved_signatures) == len(current_signatures) and
                        all(self._file_signature_equal(a, b) for a, b in zip(saved_signatures, current_signatures))
                    )

                    current_idx = br.get("current_file_index", -1)
                    if same_files and 0 <= current_idx < total:
                        start_idx = current_idx
                        expected_target = self.file_entries[start_idx].get("targetLanguage", self.config.get("targetLanguage", "Chinese"))
                        saved_target = br.get("target_lang", "")
                        if saved_target and saved_target != expected_target:
                            saved_resume_data = None
                        else:
                            saved_resume_data = {
                                "segments": br.get("segments", []),
                                "translated_count": br.get("translated_count", 0),
                            }
                            if saved_resume_data["segments"] and saved_resume_data["translated_count"] > 0:
                                print(f"[Translation] 从批量断点恢复: 从第 {start_idx + 1}/{total} 个文件继续，已译 {saved_resume_data['translated_count']} 条")
                            else:
                                saved_resume_data = None
                    else:
                        try:
                            safe_unlink(_batch_resume_path)
                        except Exception:
                            pass
                except Exception:
                    saved_resume_data = None

        for idx in range(start_idx, total):
            if not self._is_running:
                break

            self._save_session_state(stage="file_processing", current_file_index=idx)

            entry = self.file_entries[idx]
            file_path = entry["path"]
            file_name = os.path.basename(file_path)
            base_name = os.path.splitext(file_name)[0]

            # 用该文件自己的语言设置覆盖全局 config
            file_config = dict(self.config)
            file_config["sourceLanguage"]   = entry.get("sourceLanguage",   self.config.get("sourceLanguage",   "Auto"))
            file_config["targetLanguage"]   = entry.get("targetLanguage",   self.config.get("targetLanguage",   "Chinese"))
            file_config["multilingualMode"] = entry.get("multilingualMode", self.config.get("multilingualMode", False))
            file_config["secondaryLanguage"] = entry.get("secondaryLanguage", self.config.get("secondaryLanguage", None))
            target_lang = file_config["targetLanguage"]

            try:
                # ── 0. 检查翻译断点续传（当前文件）──────────────────────
                import hashlib, json as _json
                _file_hash = hashlib.md5(self._normalize_path(file_path).encode("utf-8")).hexdigest()[:12]
                _translate_resume = translate_resume_path(_file_hash)
                _resumed_segments = None

                # 若本次启动时已从批量断点恢复了当前文件，直接使用恢复的 segments
                if idx == start_idx and saved_resume_data and saved_resume_data.get("segments"):
                    _resumed_segments = saved_resume_data["segments"]
                    print(f"[Translation] 使用断点数据: {file_name}，已翻译 {saved_resume_data.get('translated_count', 0)} 条，跳过转录/优化/断句")
                elif file_config.get("workflowTranslate", False) and _translate_resume.exists():
                    try:
                        tr_data = load_json_file(_translate_resume)
                        saved_segs = tr_data.get("segments", [])
                        translated_count = tr_data.get("translated_count", 0)
                        saved_meta = tr_data.get("source_file_meta")
                        expected_meta = self._build_file_signature(file_path)
                        saved_target = tr_data.get("target_lang", "")

                        same_source = self._file_signature_equal(saved_meta, expected_meta) if saved_meta else True
                        same_target = (not saved_target) or (saved_target == target_lang)
                        if saved_segs and translated_count > 0 and same_source and same_target:
                            _resumed_segments = saved_segs
                            print(f"[Translation] 发现断点续传文件: {file_name}，"
                                  f"已翻译 {translated_count}/{len(saved_segs)} 条，跳过转录/优化/断句")
                    except Exception:
                        _resumed_segments = None

                file_start_ts = time.time()
                transcribe_elapsed_sec = None
                optimize_elapsed_sec = None
                translation_elapsed_sec = None
                overflow_fix_elapsed_sec = None

                if _resumed_segments is not None:
                    # 直接使用续传的 segments，跳过转录/优化/断句
                    segments = _resumed_segments
                    self.file_progress.emit(idx + 1, total,
                        f"[{idx+1}/{total}] 从断点恢复翻译: {file_name}")
                else:
                    # ── 1. 转录 ──────────────────────────────────────────
                    self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] 转录中: {file_name}")
                    _t0 = time.time()
                    segments = task_controller.start_process([file_path], file_config)
                    transcribe_elapsed_sec = round(time.time() - _t0, 1)
                    if not segments:
                        self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] 转录失败，跳过: {file_name}")
                        self._append_batch_report(file_path, "failed", "转录失败")
                        continue

                    context = file_config.get("videoContext", "")
                    cb = self._make_cancel_cb()

                    # ── 2. 优化 ──────────────────────────────────────────
                    _opt_start = time.time()
                    if file_config.get("workflowOptimize", True) and self._is_running:
                        self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] AI 优化中: {file_name}")
                        segments = task_controller.start_optimization(segments, context, cb, task_scope=self._task_scope)

                    # ── 3. 断句 ──────────────────────────────────────────
                    if file_config.get("workflowSplit", True) and self._is_running:
                        self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] 智能断句中: {file_name}")
                        segments = task_controller.start_split(segments, context, cb, task_scope=self._task_scope)
                    optimize_elapsed_sec = round(time.time() - _opt_start, 1)

                if not segments:
                    msg = f"[{idx+1}/{total}] 处理后结果为空，跳过: {file_name}"
                    self.file_progress.emit(idx + 1, total, msg)
                    self._append_batch_report(file_path, "failed", "处理后结果为空")
                    continue

                # 统一给每条打 index（翻译接口依赖此字段）
                for i, seg in enumerate(segments):
                    seg["index"] = i + 1
                    # 确保 text 字段存在（翻译使用 text 作为源文本）
                    if not seg.get("text"):
                        seg["text"] = seg.get("optimized_text", "")

                translated = file_config.get("workflowTranslate", False)
                translation_summary = {
                    "total_batches": 0,
                    "failed_batches": 0,
                    "skipped_batches": 0,
                    "retryable_retries": 0,
                    "non_retryable_skips": 0,
                    "summary_path": "",
                }

                # ── 4. 翻译 ──────────────────────────────────────────
                if translated and self._is_running:
                    if not cfg.llm_api_key.value:
                        self.file_progress.emit(idx + 1, total,
                            f"[{idx+1}/{total}] 跳过翻译（未配置 LLM API Key）: {file_name}")
                        translated = False
                    else:
                        self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] 翻译中: {file_name}")
                        self._save_session_state(stage="translation", current_file_index=idx)
                        translator = LLMTranslator(task_scope=self._task_scope)
                        batch_size = cfg.llm_batch_size.value
                        n_segs = len(segments)
                        translation_summary["total_batches"] = (n_segs + batch_size - 1) // batch_size

                        # _translate_resume 已在步骤 0 中计算好

                        file_failures = []
                        skipped_batches = 0
                        retryable_retry_count = 0
                        non_retryable_skip_count = 0
                        start_ts = time.time()

                        for bi in range(0, n_segs, batch_size):
                            if not self._is_running:
                                self._handle_translation_cancel(
                                    resume_path=_translate_resume,
                                    idx=idx,
                                    segments=segments,
                                    total_segments=n_segs,
                                    file_path=file_path,
                                    target_lang=target_lang,
                                )
                            batch = segments[bi: bi + batch_size]

                            # Skip already-translated batches (from resume)
                            if all((s.get("translated_text") or "").strip() for s in batch):
                                skipped_batches += 1
                                continue

                            prev_ctx, next_ctx = self._build_translation_contexts(segments, bi, batch_size)
                            translated_ok, last_translate_error, retries_used = self._translate_batch_with_retry(
                                translator,
                                batch,
                                prev_ctx=prev_ctx,
                                next_ctx=next_ctx,
                                target_lang=target_lang,
                                glossary=glossary,
                                batch_number=bi // batch_size + 1,
                            )
                            retryable_retry_count += retries_used

                            if not translated_ok:
                                failure, is_non_retryable = self._build_translation_failure(
                                    bi,
                                    batch_size,
                                    n_segs,
                                    last_translate_error,
                                )
                                if is_non_retryable:
                                    non_retryable_skip_count += 1
                                file_failures.append(failure)
                                print(f"[BatchTranslation] 批次错误({failure['type']}): {failure['error']}")

                            # ── 断点续传：每批翻译完后保存进度 ──
                            self._save_translation_progress_snapshot(
                                resume_path=_translate_resume,
                                segments=segments,
                                total_segments=n_segs,
                                file_path=file_path,
                                target_lang=target_lang,
                            )

                        self._update_translation_summary_counts(
                            translation_summary,
                            file_failures=file_failures,
                            skipped_batches=skipped_batches,
                            retryable_retry_count=retryable_retry_count,
                            non_retryable_skip_count=non_retryable_skip_count,
                        )

                        if file_failures:
                            summary_path = os.path.join(os.path.dirname(file_path), f"{base_name}_批量翻译摘要.txt")
                            try:
                                self._write_translation_failure_summary(
                                    summary_path=summary_path,
                                    file_name=file_name,
                                    total_batches=(n_segs + batch_size - 1) // batch_size,
                                    skipped_batches=skipped_batches,
                                    retryable_retry_count=retryable_retry_count,
                                    non_retryable_skip_count=non_retryable_skip_count,
                                    file_failures=file_failures,
                                    start_ts=start_ts,
                                    idx=idx,
                                    total=total,
                                    translation_summary=translation_summary,
                                )
                            except Exception as summary_err:
                                print(f"[BatchTranslation] 写入翻译摘要失败: {summary_err}")

                        translation_elapsed_sec = round(time.time() - start_ts, 1)
                        # 翻译完成，清理续传文件
                        try:
                            safe_unlink(_translate_resume)
                        except Exception:
                            pass

                # ── 5. 溢出修复（含断点续传）──────────────────────────────
                overflow_fix_elapsed_sec = 0.0
                if translated and file_config.get("workflowOverflowFix", False) and self._is_running:
                    self._save_session_state(stage="overflow_fix", current_file_index=idx)
                    threshold = cfg.max_line_count.value
                    overflow_indices = [
                        i for i, sub in enumerate(segments)
                        if len(sub.get("translated_text") or "") > threshold
                    ]
                    if overflow_indices:
                        fix_start_ts = time.time()
                        self.file_progress.emit(idx + 1, total,
                            f"[{idx+1}/{total}] 溢出修复中（{len(overflow_indices)} 条）: {file_name}")
                        fix_translator = LLMTranslator(task_scope=self._task_scope)
                        overflow_set = set(overflow_indices)
                        results_map = {i: [segments[i]] for i in range(len(segments))}

                        # 溢出修复断点：同文件用同一 hash，与翻译续传一致
                        _overflow_resume = overflow_resume_path(_file_hash)
                        _n_orig = len(segments)
                        _first_text = (segments[0].get("text", "") or "")[:80]

                        # 尝试加载未完成的修复进度
                        repaired_done = {}
                        if _overflow_resume.exists():
                            try:
                                od = load_json_file(_overflow_resume)
                                if (od.get("n_segments") == _n_orig and
                                        (od.get("first_text", "") or "")[:80] == _first_text and
                                        od.get("overflow_indices") == overflow_indices):
                                    repaired_done = {int(k): v for k, v in (od.get("repaired") or {}).items()}
                                    for oi, subs in repaired_done.items():
                                        results_map[oi] = subs
                                    if repaired_done:
                                        print(f"[OverflowFix] 断点恢复: 已修复 {len(repaired_done)}/{len(overflow_indices)} 条")
                            except Exception:
                                repaired_done = {}

                        remaining_overflow = [oi for oi in overflow_indices if oi not in repaired_done]

                        def _serialize_subs(subs):
                            return [{
                                "start": s.get("start", 0),
                                "end": s.get("end", 0),
                                "text": s.get("text", ""),
                                "optimized_text": s.get("optimized_text", ""),
                                "translated_text": s.get("translated_text", ""),
                                "speaker": s.get("speaker"),
                            } for s in subs]

                        def _save_overflow_resume():
                            try:
                                rep = {str(oi): _serialize_subs(results_map[oi]) for oi in overflow_indices if oi in results_map}
                                save_json_atomic(_overflow_resume, {
                                        "n_segments": _n_orig,
                                        "first_text": _first_text,
                                        "overflow_indices": overflow_indices,
                                        "repaired": rep,
                                    })
                            except Exception:
                                pass

                        def _build_repaired_subs(sub, text, repaired):
                            TextSplitter.redistribute_segment_texts_by_weights(text, repaired, weight_key="cn")
                            start    = sub.get("start", 0)
                            end      = sub.get("end", 0)
                            duration = end - start
                            total_len = sum(len(seg.get("cn", "")) for seg in repaired) or 1
                            new_subs  = []
                            cur_start = start
                            for seg in repaired:
                                seg_dur   = max(duration * len(seg.get("cn", "")) / total_len, 0.5)
                                seg_end   = min(cur_start + seg_dur, end)
                                entry = {
                                    "start":           cur_start,
                                    "end":             seg_end,
                                    "text":            seg.get("text", ""),
                                    "optimized_text":  seg.get("text", ""),
                                    "translated_text": seg.get("cn", ""),
                                }
                                if sub.get("speaker") is not None:
                                    entry["speaker"] = sub["speaker"]
                                new_subs.append(entry)
                                cur_start = seg_end
                            return new_subs

                        def _fix_chunk(chunk_indices):
                            if not self._is_running:
                                return None

                            batch_items = []
                            item_contexts = {}
                            for oi in chunk_indices:
                                sub = segments[oi]
                                prev_context, next_context = fix_translator.build_overflow_contexts(segments, oi)
                                item_contexts[oi] = (sub, prev_context, next_context)
                                batch_items.append({
                                    "id": oi,
                                    "text": sub.get("text", ""),
                                    "translated_text": sub.get("translated_text", ""),
                                    "prev_context": prev_context,
                                    "next_context": next_context,
                                })

                            batch_results = {}
                            if len(batch_items) > 1:
                                try:
                                    batch_results = fix_translator.repair_overflow_batch(batch_items, threshold)
                                except Exception as batch_err:
                                    print(
                                        f"[BatchOverflowFix] 批量修复失败 "
                                        f"{chunk_indices[0]}-{chunk_indices[-1]}: {batch_err}，回退逐条修复"
                                    )

                            chunk_result = {}
                            for oi in chunk_indices:
                                if not self._is_running:
                                    return None
                                sub, prev_context, next_context = item_contexts[oi]
                                text = sub.get("text", "")
                                cn = (sub.get("translated_text") or "")
                                repaired = batch_results.get(oi)
                                try:
                                    if not repaired:
                                        repaired = fix_translator.repair_overflow(
                                            text,
                                            cn,
                                            threshold,
                                            prev_context=prev_context,
                                            next_context=next_context,
                                        )
                                    chunk_result[oi] = _build_repaired_subs(sub, text, repaired)
                                except Exception as fe:
                                    print(f"[BatchOverflowFix] 修复失败 #{oi}: {fe}，保留原句")
                                    chunk_result[oi] = [sub]

                            return chunk_result

                        from concurrent.futures import ThreadPoolExecutor, as_completed
                        chunk_size = max(1, min(LLMTranslator._OVERFLOW_BATCH_SIZE, len(remaining_overflow)))
                        overflow_chunks = [
                            remaining_overflow[i:i + chunk_size]
                            for i in range(0, len(remaining_overflow), chunk_size)
                        ]
                        max_workers = min(cfg.llm_max_workers.value, len(overflow_chunks))
                        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
                            futures = {
                                executor.submit(_fix_chunk, chunk): tuple(chunk)
                                for chunk in overflow_chunks
                            }
                            for future in as_completed(futures):
                                if not self._is_running:
                                    _save_overflow_resume()
                                    raise TaskCancelledError("任务已取消")
                                chunk_result = future.result()
                                if not chunk_result:
                                    continue
                                for oi, result in chunk_result.items():
                                    results_map[oi] = result
                                _save_overflow_resume()

                        # 重建 segments（溢出条目被展开为多条）
                        new_segments = []
                        new_idx = 1
                        for i in range(len(segments)):
                            for rs in results_map.get(i, [segments[i]]):
                                rs["index"] = new_idx
                                new_segments.append(rs)
                                new_idx += 1
                        segments = new_segments

                        try:
                            safe_unlink(_overflow_resume)
                        except Exception:
                            pass
                        overflow_fix_elapsed_sec = round(time.time() - fix_start_ts, 1)

                segments = self._prepare_export_segments(
                    segments,
                    translated=translated,
                    remove_punctuation=bool(cfg.remove_punctuation.value),
                    hallucination_filter=bool(cfg.translation_hallucination_filter.value),
                )

                total_elapsed_sec = round(time.time() - file_start_ts, 1)

                # ── 6. 导出 ──────────────────────────────────────────────
                base_name = os.path.splitext(file_name)[0]
                export_root = os.path.join(os.path.dirname(file_path), f"{base_name}_导出")
                try:
                    os.makedirs(export_root, exist_ok=True)

                    # 判断是否有说话人分离数据
                    has_diarization = any("speaker" in seg for seg in segments)
                    speaker_segs = None
                    speaker_names = None
                    if has_diarization:
                        from collections import defaultdict

                        speaker_segs = defaultdict(list)
                        for seg in segments:
                            speaker_segs[seg.get("speaker", "unknown")].append(seg)
                        speaker_names = list(speaker_segs.keys())

                    threshold = cfg.max_line_count.value
                    overflow_count = sum(
                        1 for sub in segments
                        if len((sub.get("translated_text") or "")) > threshold
                    )
                    fallback_count = sum(
                        1 for sub in segments
                        if translated and sub.get("text", "").strip() and (sub.get("translated_text") or "").strip() == sub.get("text", "").strip()
                    )

                    from app.core.project import project_manager

                    def _write_quality_report(path, runtime_log_path=None):
                        write_quality_report(
                            path,
                            file_name=file_name,
                            segment_count=len(segments),
                            translated=translated,
                            has_diarization=has_diarization,
                            threshold=threshold,
                            overflow_count=overflow_count,
                            fallback_count=fallback_count,
                            translation_summary=translation_summary,
                            transcribe_elapsed_sec=transcribe_elapsed_sec,
                            optimize_elapsed_sec=optimize_elapsed_sec,
                            translation_elapsed_sec=translation_elapsed_sec,
                            overflow_fix_elapsed_sec=overflow_fix_elapsed_sec,
                            total_elapsed_sec=total_elapsed_sec,
                            runtime_log_path=runtime_log_path,
                        )

                    def _write_srt(path, mode, segs=None):
                        write_srt(path, segs if segs is not None else segments, mode)

                    def _write_vtt(path, mode):
                        write_vtt(path, segments, mode)

                    def _write_ass(path, mode):
                        write_ass(path, segments, mode)

                    def _write_news_script(path):
                        write_news_script(path, segments)

                    export_jobs, main_export = build_export_targets(
                        export_root,
                        base_name,
                        translated=translated,
                        has_diarization=has_diarization,
                        speakers=speaker_names,
                    )
                    for job in export_jobs:
                        kind = job["kind"]
                        if kind == "srt":
                            job_segments = segments
                            if job.get("speaker") is not None and speaker_segs is not None:
                                job_segments = speaker_segs[job["speaker"]]
                            _write_srt(job["path"], job["mode"], job_segments)
                        elif kind == "vtt":
                            _write_vtt(job["path"], job["mode"])
                        elif kind == "ass":
                            _write_ass(job["path"], job["mode"])
                        elif kind == "news_script":
                            _write_news_script(job["path"])
                        else:
                            raise ValueError(f"Unsupported export kind: {kind}")

                    runtime_log_path = os.path.join(export_root, f"{base_name}_运行日志.txt")
                    write_runtime_log(
                        runtime_log_path,
                        file_name=file_name,
                        log_text=project_manager.get_runtime_log_text(),
                    )

                    quality_report_path = os.path.join(export_root, f"{base_name}_质量报告.txt")
                    _write_quality_report(quality_report_path, runtime_log_path=runtime_log_path)

                    success += 1
                    detail_parts = []
                    if translated:
                        detail_parts.append(f"批次总数={translation_summary['total_batches']}")
                        detail_parts.append(f"失败批次={translation_summary['failed_batches']}")
                        detail_parts.append(f"续传跳过={translation_summary['skipped_batches']}")
                        detail_parts.append(f"重试次数={translation_summary['retryable_retries']}")
                        if translation_summary["summary_path"]:
                            detail_parts.append(f"翻译摘要={os.path.basename(translation_summary['summary_path'])}")
                    detail_parts.append(f"质量报告={os.path.basename(quality_report_path)}")
                    detail_parts.append(f"运行日志={os.path.basename(runtime_log_path)}")
                    detail = "；".join(detail_parts)
                    self._append_batch_report(file_path, "success", detail=detail, export_path=main_export)
                    self._save_session_state(stage="file_done", current_file_index=idx)
                    self.file_done.emit(idx + 1, total, file_path, main_export)
                except Exception as we:
                    self._append_batch_report(file_path, "failed", f"导出失败: {we}")
                    self.file_progress.emit(idx + 1, total, f"导出失败: {we}")

            except TaskCancelledError:
                self._append_batch_report(file_path, "cancelled", "任务已取消")
                self._save_session_state(stage="cancelled", current_file_index=idx)
                break
            except Exception as ex:
                self._append_batch_report(file_path, "failed", str(ex))
                self._save_session_state(stage="file_error", current_file_index=idx)
                self.file_progress.emit(idx + 1, total, f"[{idx+1}/{total}] 错误: {file_name} — {ex}")
            finally:
                self._cleanup_temp_audio_files()

        try:
            summary_path = self._write_batch_summary()
            if summary_path:
                self.file_progress.emit(total, total, f"批量任务摘要已生成: {os.path.basename(summary_path)}")
        except Exception as summary_err:
            print(f"[BatchSummary] 写入批量任务摘要失败: {summary_err}")

        # 全部文件处理完毕，清除批量断点文件（避免下次误从断点开始）
        try:
            safe_unlink(BATCH_TRANSLATE_RESUME_FILE)
        except Exception:
            pass

        if self._is_running:
            self._clear_session_state()
        self.all_done.emit(success, total)

class OptimizationThread(QThread):
    _TASK_SCOPE_PREFIX = "optimize"
    """
    异步执行字幕后处理的线程：初始优化 -> 智能断句（流水线模式）
    """
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, segments, config):
        super().__init__()
        self.segments = segments
        self.config = config
        self._is_running = True
        self._task_scope = f"{self._TASK_SCOPE_PREFIX}:{time.time_ns()}:{id(self)}"
        self._seg_queue = None  # 流水线队列引用，stop() 时用于解除 split_worker 阻塞

    def stop(self):
        self._is_running = False
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)
        # 若 split_worker 正阻塞在 queue.get()，发送哨兵让它退出
        if self._seg_queue is not None:
            try:
                self._seg_queue.put_nowait(None)
            except Exception:
                pass

    def run(self):
        import queue
        import threading
        from app.core.controller import task_controller
        from app.core.llm import llm_manager
        # 重置取消标志（含全局标志，防止上次终止后残留）
        llm_manager.reset_cancel()
        llm_manager.reset_scope(self._task_scope)
        # 清除断点续传文件，防止上次被终止的任务污染本次
        for _resume_file in [
            OPTIMIZE_RESUME_FILE,
            SPLIT_RESUME_FILE,
        ]:
            try:
                safe_unlink(_resume_file)
            except Exception:
                pass
        try:
            # Check cancel at start
            if not self._is_running:
                raise TaskCancelledError("任务已取消")

            t_optimize_split_start = time.time()
            # 1. 初始优化
            context = self.config.get("videoContext", "")
            workflow_optimize = self.config.get("workflowOptimize", True)
            workflow_split = self.config.get("workflowSplit", True)

            # 如果启用了联网搜索，先进行搜索
            from app.common.config import cfg
            if workflow_optimize:
                if cfg.enable_web_search.value and context:
                    self.progress.emit(10, "正在联网检索背景知识...")
                else:
                    self.progress.emit(10, "正在进行 AI 初始优化...")

            # ── 流水线模式：优化完成后立即送入断句 ──
            if workflow_optimize and workflow_split:
                seg_queue = queue.Queue()
                self._seg_queue = seg_queue  # 暴露给 stop() 使用
                split_error = [None]  # mutable container for thread error
                split_result = [None]

                total_segs = len(self.segments)
                opt_done_count = [0]
                split_done_count = [0]

                def opt_progress(current, total):
                    if not self._is_running:
                        raise TaskCancelledError("任务已取消")
                    opt_done_count[0] = current
                    # 优化占 10%-50%
                    p = 10 + int((current / total) * 40)
                    self.progress.emit(p, f"正在进行 AI 初始优化 ({current}/{total})...")

                def split_worker():
                    """Consumer thread: waits for optimized segments, then runs splitter."""
                    try:
                        optimized = seg_queue.get()  # blocks until producer puts data
                        if optimized is None:
                            return
                        def split_cb(current, total):
                            if not self._is_running:
                                raise TaskCancelledError("任务已取消")
                            split_done_count[0] = current
                            p = 50 + int((current / total) * 48)
                            if total > 1 and current == total - 1:
                                msg = "正在进行润色..."
                            elif current == total:
                                msg = "断句与润色完成"
                            else:
                                msg = f"正在进行智能语义断句 ({current}/{total})..."
                            self.progress.emit(p, msg)
                        split_result[0] = task_controller.start_split(optimized, context, split_cb, task_scope=self._task_scope)
                    except Exception as e:
                        split_error[0] = e

                # Start split consumer thread
                split_thread = threading.Thread(target=split_worker, daemon=True)
                split_thread.start()

                # Run optimizer (producer) — blocks until done
                optimized_segments = task_controller.start_optimization(self.segments, context, opt_progress, task_scope=self._task_scope)

                if not self._is_running:
                    seg_queue.put(None)
                    raise TaskCancelledError("任务已取消")

                # Feed optimized segments to split consumer
                seg_queue.put(optimized_segments)

                # Wait for split to finish
                # 说明：
                # - 之前这里使用 120s 超时，长视频（如 10-20 分钟）在正常情况下就可能超过这个时间，
                #   导致误判为“断句超时”并映射成“任务已取消”。
                # - 现在改为更宽松的超时时间，尽量避免正常任务被误终止。
                # - 如果仍然超时，则给出明确的“断句超时”提示，而不是“任务已取消”。
                split_thread.join(timeout=900)
                if split_thread.is_alive():
                    logging.warning("[OptimizationThread] split_worker 超时未退出，强制继续（可能是断句/润色阶段卡住）")
                    split_error[0] = TaskCancelledError("断句超时")

                if split_error[0]:
                    raise split_error[0]

                final_segments = split_result[0] if split_result[0] is not None else optimized_segments

            else:
                # ── 非流水线：保持原有串行逻辑 ──
                def opt_progress(current, total):
                    if not self._is_running:
                        raise TaskCancelledError("任务已取消")
                    p = 10 + int((current / total) * (60 - 10))
                    self.progress.emit(p, f"正在进行 AI 初始优化 ({current}/{total})...")

                if workflow_optimize:
                    optimized_segments = task_controller.start_optimization(self.segments, context, opt_progress, task_scope=self._task_scope)
                else:
                    self.progress.emit(60, "已跳过初始优化")
                    optimized_segments = self.segments
                
                if not self._is_running:
                    raise TaskCancelledError("任务已取消")

                if workflow_split:
                    self.progress.emit(60, "初始优化完成，正在进行智能断句优化...")
                    def split_progress(current, total):
                        if not self._is_running:
                            raise TaskCancelledError("任务已取消")
                        p = 60 + int((current / total) * (100 - 60))
                        if total > 1 and current == total - 1:
                            msg = "正在进行润色..."
                        else:
                            msg = "正在进行智能语义断句..." if current < total else "断句与润色完成"
                        self.progress.emit(p, msg)
                    final_segments = task_controller.start_split(optimized_segments, context, split_progress, task_scope=self._task_scope)
                else:
                    self.progress.emit(100, "已跳过智能断句优化")
                    final_segments = optimized_segments
            
            self.progress.emit(100, "优化完成")
            optimize_split_elapsed_sec = round(time.time() - t_optimize_split_start, 1)

            # 返回结果（含优化+断句用时，供界面打印及质量报告）
            result_data = {
                "raw": self.segments,
                "optimized": final_segments,
                "phase_timings": {"optimize_split_sec": optimize_split_elapsed_sec}
            }
            self.finished.emit(result_data)
        except TaskCancelledError as e:
            # 区分真正的“任务已取消”和内部超时等情况
            msg = str(e).strip() or "任务已取消"
            self.error.emit(msg)
        except Exception as e:
            self.error.emit(str(e))

class TranscriptionThread(QThread):
    _TASK_SCOPE_PREFIX = "transcribe"
    """
    完整的转录流程线程：语音识别 -> 初始优化 -> 智能断句
    """
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    # 语音识别一完成就发出，便于界面即时保存 raw，优化/断句失败时可直接重新优化
    raw_ready = pyqtSignal(list)

    def __init__(self, files, config):
        super().__init__()
        self.files = files
        self.config = config
        self._is_running = True
        self._task_scope = f"{self._TASK_SCOPE_PREFIX}:{time.time_ns()}:{id(self)}"
        self._seg_queue = None  # 流水线队列引用，stop() 时用于解除 split_worker 阻塞

    def stop(self):
        self._is_running = False
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)
        if self._seg_queue is not None:
            try:
                self._seg_queue.put_nowait(None)
            except Exception:
                pass

    def run(self):
        from app.core.controller import task_controller
        from app.core.llm import llm_manager
        # 重置取消标志（含全局标志，防止上次终止后残留）
        llm_manager.reset_cancel()
        llm_manager.reset_scope(self._task_scope)
        # 清除断点续传文件，防止上次被终止的任务污染本次
        for _resume_file in [
            OPTIMIZE_RESUME_FILE,
            SPLIT_RESUME_FILE,
        ]:
            try:
                safe_unlink(_resume_file)
            except Exception:
                pass
        try:
            if not self._is_running:
                raise TaskCancelledError("任务已取消")

            # 1. 语音识别
            self.progress.emit(10, "正在进行语音识别...")
            t_transcribe_start = time.time()

            def _cancel_check():
                if not self._is_running:
                    raise TaskCancelledError("任务已取消")

            segments = task_controller.start_process(self.files, self.config, cancel_check=_cancel_check)
            transcribe_elapsed_sec = round(time.time() - t_transcribe_start, 1)

            if not segments:
                self.finished.emit({})
                return

            if not self._is_running:
                raise TaskCancelledError("任务已取消")

            # 保存原始转录结果并立即通知界面，以便优化/断句失败时可直接重新优化
            raw_segments = [s.copy() for s in segments]
            self.raw_ready.emit(raw_segments)

            # 2. 初始优化 + 断句
            t_optimize_split_start = time.time()
            context = self.config.get("videoContext", "")
            workflow_optimize = self.config.get("workflowOptimize", True)
            workflow_split = self.config.get("workflowSplit", True)
            
            from app.common.config import cfg

            # ── 流水线模式：优化完成后立即送入断句 ──
            if workflow_optimize and workflow_split:
                import queue as _q
                import threading as _th

                seg_queue = _q.Queue()
                self._seg_queue = seg_queue  # 暴露给 stop() 使用
                split_error = [None]
                split_result = [None]

                if cfg.enable_web_search.value and context:
                    self.progress.emit(60, "正在联网检索背景知识...")
                else:
                    self.progress.emit(60, "语音识别完成，正在 AI 优化...")

                def opt_progress(current, total):
                    if not self._is_running:
                        raise TaskCancelledError("任务已取消")
                    p = 60 + int((current / total) * 25)
                    self.progress.emit(p, f"正在进行 AI 初始优化 ({current}/{total})...")

                def split_worker():
                    try:
                        optimized = seg_queue.get()
                        if optimized is None:
                            return
                        def split_cb(current, total):
                            if not self._is_running:
                                raise TaskCancelledError("任务已取消")
                            p = 85 + int((current / total) * 13)
                            if total > 1 and current == total - 1:
                                msg = "正在进行润色..."
                            elif current == total:
                                msg = "断句与润色完成"
                            else:
                                msg = f"正在进行智能语义断句 ({current}/{total})..."
                            self.progress.emit(p, msg)
                        split_result[0] = task_controller.start_split(optimized, context, split_cb, task_scope=self._task_scope)
                    except Exception as e:
                        split_error[0] = e

                split_thread = _th.Thread(target=split_worker, daemon=True)
                split_thread.start()

                optimized_segments = task_controller.start_optimization(segments, context, opt_progress, task_scope=self._task_scope)

                if not self._is_running:
                    seg_queue.put(None)
                    raise TaskCancelledError("任务已取消")

                seg_queue.put(optimized_segments)
                # 与 OptimizationThread 一致：长视频断句+润色可能超过 2 分钟，用 900s 避免误判为“任务已取消”
                split_thread.join(timeout=900)
                if split_thread.is_alive():
                    logging.warning("[TranscriptionThread] split_worker 超时未退出，强制继续（可能是断句/润色阶段卡住）")
                    split_error[0] = TaskCancelledError("断句超时")

                if split_error[0]:
                    raise split_error[0]

                final_segments = split_result[0] if split_result[0] is not None else optimized_segments

            else:
                # ── 非流水线：保持原有串行逻辑 ──
                if workflow_optimize:
                    if cfg.enable_web_search.value and context:
                        self.progress.emit(60, "正在联网检索背景知识...")
                    else:
                        self.progress.emit(60, "语音识别完成，正在 AI 优化...")

                def opt_progress(current, total):
                    if not self._is_running:
                        raise TaskCancelledError("任务已取消")
                    p = 60 + int((current / total) * (85 - 60))
                    self.progress.emit(p, f"正在进行 AI 初始优化 ({current}/{total})...")
                    
                if workflow_optimize:
                    optimized_segments = task_controller.start_optimization(segments, context, opt_progress, task_scope=self._task_scope)
                else:
                    self.progress.emit(85, "已跳过初始优化")
                    optimized_segments = segments
                
                if not self._is_running:
                    raise TaskCancelledError("任务已取消")

                if workflow_split:
                    self.progress.emit(85, "初始优化完成，正在进行智能断句优化...")
                    def split_progress(current, total):
                        if not self._is_running:
                            raise TaskCancelledError("任务已取消")
                        p = 85 + int((current / total) * (98 - 85))
                        if total > 1 and current == total - 1:
                            msg = "正在进行润色..."
                        else:
                            msg = "正在进行智能语义断句..." if current < total else "断句与润色完成"
                        self.progress.emit(p, msg)
                    final_segments = task_controller.start_split(optimized_segments, context, split_progress, task_scope=self._task_scope)
                else:
                    self.progress.emit(98, "已跳过智能断句优化")
                    final_segments = optimized_segments
            
            self.progress.emit(100, "全部处理完成")
            optimize_split_elapsed_sec = round(time.time() - t_optimize_split_start, 1)

            # 返回包含原始、最终结果及各环节用时
            result_data = {
                "raw": raw_segments,
                "optimized": final_segments,
                "phase_timings": {
                    "transcribe_sec": transcribe_elapsed_sec,
                    "optimize_split_sec": optimize_split_elapsed_sec,
                }
            }
            self.finished.emit(result_data)
        except TaskCancelledError:
            self.error.emit("任务已取消")
        except Exception as e:
            self.error.emit(str(e))
        finally:
            BatchTranscriptionThread._cleanup_temp_audio_files()
