# -*- coding: utf-8 -*-
import logging
import os
import re
import time
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QFrame, QFileDialog
)
from qfluentwidgets import (
    CardWidget, SpinBox,
    StrongBodyLabel, CaptionLabel, SubtitleLabel,
    TableView, PrimaryPushButton, SwitchButton,
    SmoothScrollArea, BodyLabel, LineEdit, TextEdit,
    PushButton, DropDownPushButton, RoundMenu, Action,
    InfoBar, InfoBarPosition, ProgressBar,
    FluentIcon as FIF, TransparentToolButton
)

from app.common.config import cfg
from app.common.export_utils import write_runtime_log
from app.common.text_utils import TextSplitter, clean_punctuation_text
from app.common.utils import fix_timestamp_overlaps
from app.common.thread import TaskCancelledError
from app.core.llm import LLMTranslator
from app.core.project import project_manager
from app.components.setting_cards import CalibrationPanel

class OverflowFixThread(QThread):
    _TASK_SCOPE_PREFIX = "overflow_fix"
    """ Thread for running overflow repair """
    progress_update = pyqtSignal(int, int) # current, total
    finished_signal = pyqtSignal(bool, str) # success, message
    status_update = pyqtSignal(str) # Status message update
    
    def __init__(self, subtitles: List[Dict[str, Any]], threshold: int):
        super().__init__()
        self.subtitles = subtitles
        self.threshold = threshold
        self.is_running = True
        self.new_subtitles = []
        self._task_scope = f"{self._TASK_SCOPE_PREFIX}:{time.time_ns()}:{id(self)}"

    def run(self):
        from app.core.llm import llm_manager
        llm_manager.reset_cancel()
        llm_manager.reset_scope(self._task_scope)
        try:
            if not self.is_running:
                raise TaskCancelledError("任务已取消")

            # 优先用修复专用 API Key，未配置则回退到主 LLM Key
            effective_key = cfg.fix_api_key.value or cfg.llm_api_key.value
            if not effective_key:
                print("[OverflowFix] 未配置修复 API Key，跳过溢出修复步骤。")
                self.finished_signal.emit(True, "修复跳过 (未配置 API Key)")
                return

            translator = LLMTranslator(task_scope=self._task_scope)
            max_workers = cfg.llm_max_workers.value
            
            # Iterative repair loop (max 3 passes)
            max_iterations = 3
            current_subs = self.subtitles
            
            for attempt in range(max_iterations):
                if not self.is_running:
                     raise TaskCancelledError("任务已取消")
                
                # Identify segments that need repair
                overflow_indices = [i for i, sub in enumerate(current_subs) if len((sub.get("translated_text") or "")) > self.threshold]
                
                if not overflow_indices and attempt > 0:
                    break # Done!

                if not overflow_indices:
                    # No overflow even in first pass? Just update current_subs and break
                    self.new_subtitles = current_subs
                    break

                total = len(current_subs)
                n_overflow = len(overflow_indices)
                self.status_update.emit(f"第 {attempt + 1} 轮修复中（{n_overflow} 条溢出）...")

                # Define worker function — only called for overflow items
                def repair_segment(data):
                    idx, sub = data
                    if not self.is_running:
                        return idx, [sub]
                    text = sub.get("text", "")
                    cn = (sub.get("translated_text") or "")
                    prev_context, next_context = translator.build_overflow_contexts(current_subs, idx)
                    try:
                        segments = translator.repair_overflow(
                            text,
                            cn,
                            self.threshold,
                            prev_context=prev_context,
                            next_context=next_context,
                        )
                        TextSplitter.redistribute_segment_texts_by_weights(text, segments, weight_key="cn")

                        # Timestamp interpolation with minimum duration floor
                        start = sub.get("start", 0)
                        end = sub.get("end", 0)
                        duration = end - start
                        MIN_SEG_DURATION = 1.2  # seconds — shortest readable subtitle

                        total_seg_len = sum(len(seg.get("cn", "")) for seg in segments)
                        if total_seg_len == 0: total_seg_len = 1

                        durations = [duration * (len(seg.get("cn", "")) / total_seg_len) for seg in segments]
                        durations = [max(d, MIN_SEG_DURATION) for d in durations]
                        total_floored = sum(durations)
                        if total_floored > duration > 0:
                            scale = duration / total_floored
                            durations = [d * scale for d in durations]

                        new_subs = []
                        current_start = start
                        parent_speaker = sub.get("speaker")
                        for seg, seg_duration in zip(segments, durations):
                            seg_end = current_start + seg_duration
                            entry = {
                                "start": current_start,
                                "end": seg_end,
                                "text": seg.get("text", ""),
                                "translated_text": seg.get("cn", "")
                            }
                            if parent_speaker is not None:
                                entry["speaker"] = parent_speaker
                            new_subs.append(entry)
                            current_start = seg_end
                        return idx, new_subs
                    except Exception as e:
                        print(f"Repair failed for index {sub.get('index')}: {e}")
                        return idx, [sub]

                # 非溢出条目直接填入结果字典
                overflow_set = set(overflow_indices)
                results_map = {i: [sub] for i, sub in enumerate(current_subs) if i not in overflow_set}

                # 溢出条目并行修复（只读邻接上下文，可安全并行）
                max_workers = cfg.llm_max_workers.value
                processed_count = 0
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_idx = {
                        executor.submit(repair_segment, (i, current_subs[i])): i
                        for i in sorted(overflow_indices)
                    }
                    for future in as_completed(future_to_idx):
                        if not self.is_running:
                            # 取消前写出已修复部分
                            self.new_subtitles = []
                            current_id = 1
                            for j in range(total):
                                repaired = results_map.get(j, [current_subs[j]])
                                for rs in repaired:
                                    rs["index"] = current_id
                                    self.new_subtitles.append(rs)
                                    current_id += 1
                            raise TaskCancelledError("任务已取消")
                        idx, repaired_subs = future.result()
                        results_map[idx] = repaired_subs
                        processed_count += 1
                        self.progress_update.emit(processed_count, n_overflow)

                # Reconstruct the list in order
                self.new_subtitles = []
                current_id = 1
                for i in range(total):
                    repaired = results_map.get(i, [current_subs[i]])
                    for rs in repaired:
                        rs["index"] = current_id
                        self.new_subtitles.append(rs)
                        current_id += 1
                
                # Update input for next iteration
                current_subs = self.new_subtitles
            
            self.finished_signal.emit(True, "修复完成")
            
        except TaskCancelledError:
            self.finished_signal.emit(False, "任务已终止")
        except Exception as e:
            self.finished_signal.emit(False, str(e))

    def stop(self):
        self.is_running = False
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)


class TranslationThread(QThread):
    _TASK_SCOPE_PREFIX = "translation"
    """ Thread for running batch translation """
    progress_update = pyqtSignal(int, int) # current, total
    batch_completed = pyqtSignal(list) # list of updated subtitles (optional, or just notify)
    finished_signal = pyqtSignal(bool, str) # success, message
    status_update = pyqtSignal(str) # Status message update

    def __init__(self, subtitles: List[Dict[str, Any]], glossary: str = ""):
        super().__init__()
        self.subtitles = subtitles
        self.glossary = glossary
        self.is_running = True
        self._task_scope = f"{self._TASK_SCOPE_PREFIX}:{time.time_ns()}:{id(self)}"

    def run(self):
        from app.core.llm import llm_manager
        llm_manager.reset_cancel()
        llm_manager.reset_scope(self._task_scope)
        try:
            if not self.is_running:
                raise TaskCancelledError("任务已取消")

            if not cfg.llm_api_key.value:
                print("[Translation] 未配置翻译 API Key (LLM)，跳过翻译步骤。")
                self.finished_signal.emit(True, "翻译跳过 (未配置 LLM API)")
                return

            translator = LLMTranslator(task_scope=self._task_scope)
            total = len(self.subtitles)
            batch_size = cfg.llm_batch_size.value
            max_workers = cfg.llm_max_workers.value
            mode = cfg.llm_translation_mode.value

            # ── 断点续传：跳过已翻译的批次 ──────────────────────────────────
            start_index = 0
            for i in range(0, total, batch_size):
                batch_end = min(i + batch_size, total)
                if all((sub.get("translated_text") or "") for sub in self.subtitles[i:batch_end]):
                    start_index = batch_end
                    self.progress_update.emit(start_index, total)
                else:
                    break

            processed_count = start_index
            self.errors = []
            fallback_batches = 0

            # ── 公共工具函数 ─────────────────────────────────────────────────

            def apply_results(batch, translated_data):
                """将翻译结果写回字幕列表，优先按 id 匹配，降级按位置顺序回填。"""
                if not translated_data:
                    return
                result_map = {str(item.get("id")): item.get("cn", "") for item in translated_data}
                hit_count = 0
                for sub in batch:
                    idx = str(sub.get("index", 0))
                    if idx in result_map:
                        sub["translated_text"] = result_map[idx]
                        if (result_map[idx] or "").strip():
                            hit_count += 1
                if hit_count == 0 and len(translated_data) == len(batch):
                    print(f"[Translator] ID 匹配全部失败！batch index 范围: "
                          f"{batch[0].get('index')}~{batch[-1].get('index')}，"
                          f"返回 id 范围: {translated_data[0].get('id')}~{translated_data[-1].get('id')}")
                # 位置顺序兜底：对仍无译文的条目，按返回顺序赋值（不再要求条数完全相等）
                unfilled = [j for j, sub in enumerate(batch) if not (sub.get("translated_text") or "").strip()]
                if hit_count == 0 and unfilled and len(translated_data) == len(batch):
                    for k, j in enumerate(unfilled):
                        if k < len(translated_data):
                            cn = translated_data[k].get("cn", "")
                            if cn.strip():
                                batch[j]["translated_text"] = cn
                # 最终兜底：仍无译文的条目用原文填充，绝不留空
                for sub in batch:
                    if not (sub.get("translated_text") or "").strip():
                        sub["translated_text"] = sub.get("text", "")
                        print(f"[Translator] 第 {sub.get('index', '?')} 条最终兜底为原文")

            def do_translate(batch, prev_ctx, next_ctx):
                """执行单批翻译，返回 (translated_data, error_str|None)。"""
                try:
                    data = translator.translate_batch(
                        batch,
                        prev_context=prev_ctx,
                        next_context=next_ctx,
                        glossary=self.glossary
                    )
                    return data, None
                except Exception as e:
                    err_text = str(e)
                    if "任务已取消" not in err_text:
                        print(f"[Translation] Batch error: {err_text}")
                    for sub in batch:
                        if not (sub.get("translated_text") or "").strip():
                            sub["translated_text"] = sub.get("text", "")
                    return [], err_text

            def build_prev_ctx(i, window):
                """读取已翻译的上文（每次实时读取，确保稳健模式下拿到最新译文）。"""
                prev_start = max(0, i - window)
                texts = [s.get("translated_text", "") or s.get("text", "")
                         for s in self.subtitles[prev_start:i]]
                return " | ".join(texts) if texts else "（无上文）"

            def build_next_ctx(i, window):
                """读取原文下文供 LLM 预判话题/人名。"""
                next_end = min(total, i + batch_size + window)
                texts = [s.get("text", "") for s in self.subtitles[i + batch_size:next_end]]
                return " | ".join(texts)

            # ── 稳健模式：严格顺序，每批完成后立即刷新上文 ──────────────────
            if mode == "稳健":
                self.status_update.emit("稳健模式：顺序翻译（强上下文）...")
                context_window = 6
                for i in range(start_index, total, batch_size):
                    if not self.is_running:
                        raise TaskCancelledError("任务已取消")
                    batch = self.subtitles[i:i + batch_size]
                    # 上文在此时读取，前序批次已写入 translated_text，拿到真实译文
                    prev_ctx = build_prev_ctx(i, context_window)
                    next_ctx = build_next_ctx(i, context_window)
                    translated_data, error = do_translate(batch, prev_ctx, next_ctx)
                    if error:
                        self.errors.append(error)
                        fallback_batches += 1
                    apply_results(batch, translated_data)
                    processed_count += len(batch)
                    self.progress_update.emit(processed_count, total)

            # ── 折中模式：小并行分组，组内并行、组间顺序 ────────────────────
            elif mode == "折中":
                self.status_update.emit("折中模式：小并行翻译（局部上下文）...")
                context_window = 4
                group_size = min(3, max(2, max_workers))
                batch_starts = list(range(start_index, total, batch_size))
                groups = [batch_starts[k:k + group_size]
                          for k in range(0, len(batch_starts), group_size)]

                for group in groups:
                    if not self.is_running:
                        raise TaskCancelledError("任务已取消")
                    # 组开始时统一快照上下文（组内各批共享同一时间点的 prev_ctx）
                    group_tasks = [
                        (i,
                         self.subtitles[i:i + batch_size],
                         build_prev_ctx(i, context_window),
                         build_next_ctx(i, context_window))
                        for i in group
                    ]
                    with ThreadPoolExecutor(max_workers=group_size) as executor:
                        futures = {
                            executor.submit(do_translate, batch, prev_ctx, next_ctx): (i, batch)
                            for i, batch, prev_ctx, next_ctx in group_tasks
                        }
                        for future in as_completed(futures):
                            if not self.is_running:
                                executor.shutdown(wait=False)
                                raise TaskCancelledError("任务已取消")
                            i, batch = futures[future]
                            translated_data, error = future.result()
                            if error:
                                self.errors.append(error)
                                fallback_batches += 1
                            apply_results(batch, translated_data)
                            processed_count += len(batch)
                            self.progress_update.emit(processed_count, total)

            # ── 极速模式：全并行，不传上下文 ─────────────────────────────────
            elif mode == "极速":
                self.status_update.emit("极速模式：全并行翻译（无上下文）...")
                batch_starts = list(range(start_index, total, batch_size))
                tasks = [(i, self.subtitles[i:i + batch_size]) for i in batch_starts]
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(do_translate, batch, "（无上文）", ""): (i, batch)
                        for i, batch in tasks
                    }
                    for future in as_completed(futures):
                        if not self.is_running:
                            executor.shutdown(wait=False)
                            raise TaskCancelledError("任务已取消")
                        i, batch = futures[future]
                        translated_data, error = future.result()
                        if error:
                            self.errors.append(error)
                            fallback_batches += 1
                        apply_results(batch, translated_data)
                        processed_count += len(batch)
                        self.progress_update.emit(processed_count, total)

            # ── 自定义模式：完全按用户配置的批数和并发数全并行，传局部上下文 ──
            else:
                self.status_update.emit(f"自定义模式：并发={max_workers}，批数={batch_size}...")
                context_window = 4
                batch_starts = list(range(start_index, total, batch_size))
                tasks = [
                    (i,
                     self.subtitles[i:i + batch_size],
                     build_prev_ctx(i, context_window),
                     build_next_ctx(i, context_window))
                    for i in batch_starts
                ]
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(do_translate, batch, prev_ctx, next_ctx): (i, batch)
                        for i, batch, prev_ctx, next_ctx in tasks
                    }
                    for future in as_completed(futures):
                        if not self.is_running:
                            executor.shutdown(wait=False)
                            raise TaskCancelledError("任务已取消")
                        i, batch = futures[future]
                        translated_data, error = future.result()
                        if error:
                            self.errors.append(error)
                            fallback_batches += 1
                        apply_results(batch, translated_data)
                        processed_count += len(batch)
                        self.progress_update.emit(processed_count, total)

            if self.errors and fallback_batches > 0:
                self.status_update.emit(f"部分批次失败，已回退原文 ({fallback_batches} 批)")

            # ── 全批失败检测 ─────────────────────────────────────────────────
            total_tasks = len(list(range(start_index, total, batch_size)))
            if self.errors and len(self.errors) == total_tasks:
                any_translated = any((sub.get("translated_text") or "").strip() for sub in self.subtitles)
                if any_translated:
                    self.finished_signal.emit(True, "翻译完成")
                    return
                error_summary = "; ".join(list(set(self.errors))[:1])
                raise Exception(f"所有批次翻译失败。错误示例: {error_summary}")

            # ── 漏译补全（顺序执行，保证上下文质量）────────────────────────
            for attempt in range(3):
                if not self.is_running:
                    raise TaskCancelledError("任务已取消")
                missing_indices = [
                    idx for idx, sub in enumerate(self.subtitles)
                    if not (sub.get("translated_text") or "").strip()
                ]
                if not missing_indices:
                    break
                msg = f"正在补全漏译 ({len(missing_indices)} 条) - 第 {attempt + 1} 次尝试..."
                self.status_update.emit(msg)
                print(f"[Translation] {msg}")
                missing_batches = [missing_indices[k:k + batch_size]
                                   for k in range(0, len(missing_indices), batch_size)]
                for batch_indices in missing_batches:
                    if not self.is_running:
                        raise TaskCancelledError("任务已取消")
                    batch_subs = [self.subtitles[idx] for idx in batch_indices]
                    first_idx = batch_indices[0]
                    prev_sub = self.subtitles[max(0, first_idx - 1)]
                    next_sub = self.subtitles[min(total - 1, batch_indices[-1] + 1)]
                    prev_ctx = (prev_sub.get("translated_text") or prev_sub.get("text", ""))
                    next_ctx = next_sub.get("text", "")
                    translated_data, _ = do_translate(batch_subs, prev_ctx, next_ctx)
                    apply_results(batch_subs, translated_data)

            # ── 最终兜底：仍有漏译则回填原文 ─────────────────────────────────
            still_missing = [sub for sub in self.subtitles if not (sub.get("translated_text") or "").strip()]
            if still_missing:
                for sub in still_missing:
                    sub["translated_text"] = sub.get("text", "")
                self.status_update.emit(f"漏译回退原文 ({len(still_missing)} 条)")
                print(f"[Translation] 警告: 仍有 {len(still_missing)} 条字幕未翻译")

            self.finished_signal.emit(True, "翻译完成")

        except TaskCancelledError:
            self.finished_signal.emit(False, "任务已终止")
        except Exception as e:
            self.finished_signal.emit(False, str(e))

    def stop(self):
        self.is_running = False
        from app.core.llm import llm_manager
        llm_manager.cancel_scope(self._task_scope)


class RepairPreviewCard(CardWidget):
    """ Mini card for displaying split subtitle segments in repair preview """
    def __init__(self, index: int, data: Dict[str, Any], parent=None):
        super().__init__(parent=parent)
        self.data = data
        self._init_ui(index)

    def _init_ui(self, index):
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(16, 12, 16, 12)
        self.v_layout.setSpacing(6)
        
        # Header: Index and Time
        header_layout = QHBoxLayout()
        index_label = CaptionLabel(f"拆分 #{index}", self)
        index_label.setStyleSheet("color: #8A9099; font-weight: bold;")
        
        start_time = self._format_time(self.data.get("start", 0))
        end_time = self._format_time(self.data.get("end", 0))
        time_label = CaptionLabel(f"{start_time} --> {end_time}", self)
        time_label.setStyleSheet("color: #6B7280;")
        
        header_layout.addWidget(index_label)
        header_layout.addSpacing(10)
        header_layout.addWidget(time_label)
        header_layout.addStretch(1)
        
        # Original (Editable)
        self.original_edit = LineEdit(self)
        self.original_edit.setText(self.data.get("text", ""))
        self.original_edit.setPlaceholderText("原文")
        self.original_edit.setStyleSheet("""
            QLineEdit {
                color: #6B7280;
                font-size: 13px;
                font-family: 'Segoe UI', sans-serif;
                background: transparent;
                border: none;
                padding: 4px 0;
            }
            QLineEdit:hover {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 0px;
            }
            QLineEdit:focus {
                background: rgba(255, 255, 255, 0.08);
                color: #8A9099;
                border-radius: 0px;
            }
        """)
        
        # Translation (Editable)
        self.trans_edit = TextEdit(self)
        self.trans_edit.setText(self.data.get("translated_text", ""))
        self.trans_edit.setPlaceholderText("译文")
        self.trans_edit.setStyleSheet("""
            QTextEdit {
                color: #8A9099;
                font-weight: 600;
                font-size: 15px;
                background: transparent;
                border: none;
                selection-background-color: #3FA266;
                selection-color: #FFFFFF;
            }
            QTextEdit:hover {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 0px;
            }
            QTextEdit:focus {
                background: rgba(255, 255, 255, 0.08);
                border-radius: 0px;
            }
        """)
        # Adjust height based on content
        self.trans_edit.document().documentLayout().documentSizeChanged.connect(
            lambda: self._adjust_height(self.trans_edit)
        )
        self._adjust_height(self.trans_edit)
        
        self.v_layout.addLayout(header_layout)
        self.v_layout.addWidget(self.original_edit)
        self.v_layout.addWidget(self.trans_edit)
        
        # Styled to look distinct (lighter background maybe?)
        self.setStyleSheet("""
            RepairPreviewCard { 
                background-color: transparent; 
                border: none; 
                border-radius: 0px; 
            }
        """)

    def _adjust_height(self, edit_widget):
        doc_height = edit_widget.document().size().height()
        margins = edit_widget.contentsMargins()
        height = int(doc_height + margins.top() + margins.bottom() + 10)
        edit_widget.setFixedHeight(height)

    def _format_time(self, seconds: float) -> str:
        msecs = int(seconds * 1000)
        hours = msecs // 3600000
        mins = (msecs % 3600000) // 60000
        secs = (msecs % 60000) // 1000
        ms = msecs % 1000
        return f"{hours:02}:{mins:02}:{secs:02},{ms:03}"

class SubtitleCard(CardWidget):
    """ Individual Subtitle Card showing Original, Translation and Repair status """
    
    def __init__(self, index: int, data: Dict[str, Any], threshold: int, parent=None):
        super().__init__(parent=parent)
        self.index = index
        self.data = data
        self.threshold = threshold
        self.is_overflow = False
        self.show_repair_details = False
        
        self._init_ui()
        self.update_status()

    def _init_ui(self):
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(20, 16, 20, 16)
        self.v_layout.setSpacing(12)

        # Header: Index and Time
        self.header_layout = QHBoxLayout()
        self.index_label = CaptionLabel(f"#{self.index}", self)
        self.index_label.setStyleSheet("color: #6B7280; font-weight: bold;")
        
        start_time = self._format_time(self.data.get("start", 0))
        end_time = self._format_time(self.data.get("end", 0))
        self.time_label = CaptionLabel(f"{start_time} --> {end_time}", self)
        self.time_label.setStyleSheet("color: #6B7280; font-family: Consolas, monospace;")
        
        self.header_layout.addWidget(self.index_label)
        self.header_layout.addSpacing(12)
        self.header_layout.addWidget(self.time_label)
        
        # Overflow Badge
        self.overflow_badge = QLabel("字数溢出", self)
        self.overflow_badge.setStyleSheet("""
            QLabel {
                color: #E8A87C;
                background-color: transparent;
                border: none;
                border-radius: 0px;
                padding: 2px 8px;
                font-size: 12px;
                font-weight: bold;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        self.overflow_badge.hide()
        self.header_layout.addSpacing(12)
        self.header_layout.addWidget(self.overflow_badge)
        
        self.header_layout.addStretch(1)
        
        # Original Text (Editable)
        self.original_edit = LineEdit(self)
        self.original_edit.setPlaceholderText("原文")
        self.original_edit.setStyleSheet("""
            QLineEdit { 
                color: #6B7280; 
                background: transparent; 
                border: none; 
                font-size: 13px;
                padding: 4px 0;
            } 
            QLineEdit:hover { 
                background: rgba(255, 255, 255, 0.05); 
                border-radius: 0px;
            } 
            QLineEdit:focus { 
                background: rgba(255, 255, 255, 0.08); 
                color: #8A9099;
                border-radius: 0px;
            }
        """)
        
        # Translation Text (Editable)
        self.trans_edit = TextEdit(self)
        self.trans_edit.setPlaceholderText("译文")
        self.default_trans_style = """
            QTextEdit {
                color: #3FA266;
                background: transparent;
                border: none;
                font-size: 15px;
                font-weight: 500;
                selection-background-color: #3FA266;
                selection-color: #FFFFFF;
            }
            QTextEdit:hover {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 0px;
            }
            QTextEdit:focus {
                background: rgba(255, 255, 255, 0.08);
                border-radius: 0px;
            }
        """
        self.overflow_trans_style = """
            QTextEdit {
                color: #E8A87C;
                background: transparent;
                border: none;
                font-size: 15px;
                font-weight: 500;
                selection-background-color: #3FA266;
                selection-color: #FFFFFF;
            }
            QTextEdit:hover {
                background: rgba(255, 255, 255, 0.05);
                border-radius: 0px;
            }
            QTextEdit:focus {
                background: rgba(255, 255, 255, 0.08);
                border-radius: 0px;
            }
        """
        self.trans_edit.setStyleSheet(self.default_trans_style)

        # ── 先断开信号，批量设置文本后再连接，避免 setText 触发不必要的 update_status ──
        # Adjust height based on content
        self.trans_edit.document().documentLayout().documentSizeChanged.connect(
            lambda: self._adjust_height(self.trans_edit)
        )

        # 先设置文本（不触发 textChanged → update_status）
        self.original_edit.setText(self.data.get("text", ""))
        self.trans_edit.setText(self.data.get("translated_text", ""))
        self._adjust_height(self.trans_edit)

        # Overflow Indicator / Repair Section
        self.repair_container = QWidget()
        self.repair_layout = QVBoxLayout(self.repair_container)
        self.repair_layout.setContentsMargins(0, 12, 0, 0)
        self.repair_layout.setSpacing(10)
        self.repair_container.hide() # Hidden by default
        
        # Divider with Label (Optional, using simple divider for now)
        self.divider = QFrame()
        self.divider.setFrameShape(QFrame.Shape.HLine)
        self.divider.setStyleSheet("color: rgba(255, 255, 255, 0.15);")
        self.repair_layout.addWidget(self.divider)
        
        # Container for split cards
        self.split_cards_container = QWidget()
        self.split_cards_layout = QVBoxLayout(self.split_cards_container)
        self.split_cards_layout.setContentsMargins(0, 0, 0, 0)
        self.split_cards_layout.setSpacing(8)
        self.repair_layout.addWidget(self.split_cards_container)
        
        self.v_layout.addLayout(self.header_layout)
        self.v_layout.addWidget(self.original_edit)
        self.v_layout.addWidget(self.trans_edit)
        self.v_layout.addWidget(self.repair_container)
        
        # ── 文本设置完毕后再连接信号，避免初始化时多次触发 ──
        self.trans_edit.textChanged.connect(self._on_text_changed)
        self.original_edit.textChanged.connect(self._on_original_changed)

    def _adjust_height(self, edit_widget):
        doc_height = edit_widget.document().size().height()
        margins = edit_widget.contentsMargins()
        height = int(doc_height + margins.top() + margins.bottom() + 10)
        edit_widget.setFixedHeight(height)

    def _on_text_changed(self):
        # Update internal data and check overflow
        self.data["translated_text"] = self.trans_edit.toPlainText()
        self.update_status()

    def _on_original_changed(self, text):
        self.data["text"] = text

    def update_threshold(self, threshold: int):
        self.threshold = threshold
        self.update_status()

    def set_show_repair(self, show: bool):
        self.show_repair_details = show
        self.update_status()

    def update_status(self):
        text = self.data.get("translated_text", "")
        self.is_overflow = len(text) > self.threshold
        
        if self.is_overflow:
            self.overflow_badge.show()
            # Change card background to red and text to white
            self.setStyleSheet("""
                SubtitleCard { 
                    background-color: transparent; 
                    border: none; 
                    border-radius: 0px; 
                }
            """)
            self.trans_edit.setStyleSheet(self.overflow_trans_style)
            
            if self.show_repair_details:
                self.repair_container.show()
                self._update_repair_previews(text)
            else:
                self.repair_container.hide()
        else:
            self.overflow_badge.hide()
            # Reset to default card style and text color
            self.setStyleSheet("SubtitleCard { background-color: transparent; border: none; border-radius: 0px; }") 
            self.trans_edit.setStyleSheet(self.default_trans_style)
            self.repair_container.hide()

    def _update_repair_previews(self, text):
        pass # No longer needed as we repair directly into the list
        
        # Clear existing previews
        # for i in reversed(range(self.split_cards_layout.count())):
        #     widget = self.split_cards_layout.itemAt(i).widget()
        #     if widget:
        #         widget.deleteLater()
        
        # Removed placeholder as requested since repair logic is now active via button
        # label = QLabel("字幕还未启用溢出修复", self.split_cards_container)
        # label.setStyleSheet("color: #aaa; font-style: italic; padding: 10px 0;")
        # self.split_cards_layout.addWidget(label)

        # splits = TextSplitter.split_subtitle(
        #     text_cn=text,
        #     text_origin=self.data.get("text", ""),
        #     start=self.data.get("start", 0),
        #     end=self.data.get("end", 0),
        #     max_chars=self.threshold
        # )
        # 
        # for i, split_data in enumerate(splits):
        #     card = RepairPreviewCard(i + 1, split_data, self.split_cards_container)
        #     self.split_cards_layout.addWidget(card)

    def _format_time(self, seconds: float) -> str:
        # Convert seconds to HH:mm:ss,ms
        msecs = int(seconds * 1000)
        hours = msecs // 3600000
        mins = (msecs % 3600000) // 60000
        secs = (msecs % 60000) // 1000
        ms = msecs % 1000
        return f"{hours:02}:{mins:02}:{secs:02},{ms:03}"


class SubtitleListWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.cards: List[SubtitleCard] = []
        self.current_threshold = cfg.max_line_count.value
        self.filter_overflow = False
        
        self._init_ui()

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Toolbar
        self.toolbar = QHBoxLayout()
        self.toolbar.setContentsMargins(20, 0, 20, 0)
        
        self.export_btn = DropDownPushButton("导出 SRT", self)
        self.export_btn.setFixedWidth(120)
        
        self.export_menu = RoundMenu(parent=self)
        self.export_menu.addAction(Action("译文 + 原文 (译文在上)", triggered=lambda: self._export_srt("trans_first")))
        self.export_menu.addAction(Action("仅原文", triggered=lambda: self._export_srt("orig_only")))
        self.export_menu.addAction(Action("原文 + 译文 (原文在上)", triggered=lambda: self._export_srt("orig_first")))
        self.export_menu.addSeparator()
        self.export_menu.addAction(Action("按说话人分离导出", triggered=self._export_by_speaker))
        
        self.export_btn.setMenu(self.export_menu)
        self.toolbar.addWidget(self.export_btn)
        
        self.toolbar.addStretch(1)
        self.filter_switch = SwitchButton(parent=self)
        self.filter_switch.setOnText("仅显示溢出")
        self.filter_switch.setOffText("显示全部 (编辑模式)")
        self.filter_switch.checkedChanged.connect(self._on_filter_changed)
        
        self.toolbar.addStretch(1)
        self.toolbar.addWidget(self.filter_switch)
        self.main_layout.addLayout(self.toolbar)

        # Scroll Area（SmoothScrollArea 减轻滚动卡顿）
        self.scroll_area = SmoothScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("background-color: transparent; border: none;")
        
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(10)
        self.scroll_layout.addStretch(1) # Push cards to top
        
        self.scroll_area.setWidget(self.scroll_content)
        self.main_layout.addWidget(self.scroll_area)

    def set_data(self, subtitles: List[Dict[str, Any]]):
        # ── 冻结布局，防止每次 add/remove 都触发重排 ──
        self.scroll_content.setUpdatesEnabled(False)

        # Clear existing
        for card in self.cards:
            self.scroll_layout.removeWidget(card)
            card.deleteLater()
        self.cards.clear()
        
        if not subtitles:
            self.scroll_content.setUpdatesEnabled(True)
            return

        # Create new cards in batches to avoid UI freeze
        # Remove stretch temporarily
        self.scroll_layout.takeAt(self.scroll_layout.count() - 1)
        
        remove_punct = cfg.remove_punctuation.value
        batch_size = 15
        total = len(subtitles)
        
        def add_batch(start_idx):
            end_idx = min(start_idx + batch_size, total)
            for i in range(start_idx, end_idx):
                sub = subtitles[i]
                if remove_punct:
                    txt = (sub.get("translated_text") or "")
                    if txt:
                        sub["translated_text"] = clean_punctuation_text(txt)

                card = SubtitleCard(i + 1, sub, self.current_threshold, self.scroll_content)
                self.scroll_layout.addWidget(card)
                self.cards.append(card)
            
            if end_idx < total:
                # 给主线程更多喘息时间（50ms），避免连续创建复杂 widget 导致画面撕裂
                QTimer.singleShot(50, lambda: add_batch(end_idx))
            else:
                self.scroll_layout.addStretch(1)
                # ── 全部创建完毕后再解冻，一次性刷新 ──
                self.scroll_content.setUpdatesEnabled(True)
                self.update_filter()

        add_batch(0)

    def update_threshold(self, threshold: int):
        self.current_threshold = threshold
        self.scroll_content.setUpdatesEnabled(False)
        try:
            for card in self.cards:
                card.update_threshold(threshold)
        finally:
            self.scroll_content.setUpdatesEnabled(True)
            self.scroll_content.update()
        if self.filter_overflow:
            self.update_filter()

    def _on_filter_changed(self, is_checked):
        self.filter_overflow = is_checked
        self.update_filter()

    def update_filter(self):
        self.scroll_content.setUpdatesEnabled(False)
        try:
            for card in self.cards:
                card.set_show_repair(self.filter_overflow)
                if self.filter_overflow:
                    card.setVisible(card.is_overflow)
                else:
                    card.show()
        finally:
            self.scroll_content.setUpdatesEnabled(True)
            self.scroll_content.update()

    def set_remove_punctuation(self, remove: bool):
        """ Apply punctuation removal to all cards """
        if not remove:
            return
        self.scroll_content.setUpdatesEnabled(False)
        try:
            for card in self.cards:
                current_text = card.trans_edit.toPlainText()
                if current_text:
                    new_text = clean_punctuation_text(current_text)
                    if new_text != current_text:
                        card.trans_edit.setText(new_text)
        finally:
            self.scroll_content.setUpdatesEnabled(True)
            self.scroll_content.update()

    def _export_srt(self, mode="trans_only"):
        file_path, _ = QFileDialog.getSaveFileName(
            self, 
            "导出 SRT 字幕", 
            "subtitle.srt", 
            "SRT Files (*.srt)"
        )
        
        if not file_path:
            return
            
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                for i, card in enumerate(self.cards):
                    start = card.data.get("start", 0)
                    end = card.data.get("end", 0)
                    
                    original = card.data.get("text", "")
                    if original is None: original = ""
                    original = original.strip()
                    
                    trans = card.data.get("translated_text", "")
                    if trans is None: trans = ""
                    trans = trans.strip()
                    
                    content = ""
                    if mode == "trans_first":
                        content = f"{trans}\n{original}"
                    elif mode == "orig_only":
                        content = f"{original}"
                    elif mode == "trans_only":
                        content = f"{trans}"
                    elif mode == "orig_first":
                        content = f"{original}\n{trans}"
                    
                    f.write(f"{i+1}\n")
                    f.write(f"{self._format_time_srt(start)} --> {self._format_time_srt(end)}\n")
                    f.write(f"{content}\n\n")
                    
        except Exception as e:
            print(f"Export failed: {e}")

    def _export_by_speaker(self):
        """从卡片数据中读取说话人信息并按人分别导出 SRT + 新闻稿 TXT。"""
        all_data = [card.data for card in self.cards]
        has_speaker = any("speaker" in d for d in all_data)
        if not has_speaker:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.warning(
                title="无说话人数据",
                content="当前字幕不含说话人分离信息，请在全局设置中开启 Gladia 说话人分离后重新转录",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        export_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not export_dir:
            return

        try:
            from collections import defaultdict
            speaker_segs = defaultdict(list)
            for d in all_data:
                speaker_segs[d.get("speaker", "unknown")].append(d)

            has_trans = any(d.get("translated_text") for d in all_data)

            def _srt_time(sec):
                return self._format_time_srt(sec or 0)

            def _write_srt_file(path, segs, mode):
                with open(path, "w", encoding="utf-8-sig") as f:
                    idx = 1
                    for d in segs:
                        orig = (d.get("text") or "").strip()
                        cn = (d.get("translated_text") or "").strip()
                        if mode == "orig_only":
                            text = orig
                        elif mode == "trans_only":
                            text = cn
                        elif mode == "trans_first":
                            text = f"{cn}\n{orig}" if cn and orig else (cn or orig)
                        else:  # orig_first
                            text = f"{orig}\n{cn}" if cn and orig else (orig or cn)
                        if text:
                            f.write(f"{idx}\n{_srt_time(d.get('start',0))} --> {_srt_time(d.get('end',0))}\n{text}\n\n")
                            idx += 1

            base_name = "output"
            for spk, segs in sorted(speaker_segs.items()):
                _write_srt_file(os.path.join(export_dir, f"{base_name}_{spk}_仅原文.srt"), segs, "orig_only")
                if has_trans:
                    _write_srt_file(os.path.join(export_dir, f"{base_name}_{spk}_仅译文.srt"), segs, "trans_only")
                    _write_srt_file(os.path.join(export_dir, f"{base_name}_{spk}_双语_译文在上.srt"), segs, "trans_first")
                    _write_srt_file(os.path.join(export_dir, f"{base_name}_{spk}_双语_原文在上.srt"), segs, "orig_first")

            with open(os.path.join(export_dir, f"{base_name}_新闻稿.txt"), "w", encoding="utf-8") as f:
                for d in all_data:
                    ts = _srt_time(d.get("start", 0))[:8]
                    spk = d.get("speaker", "Speaker")
                    orig = (d.get("text") or "").strip()
                    f.write(f"[{ts}] [{spk}] {orig}\n")

            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.success(
                title="导出成功",
                content=f"已导出 {len(speaker_segs)} 位说话人的字幕至：{export_dir}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000
            )
        except Exception as ex:
            from qfluentwidgets import InfoBar, InfoBarPosition
            InfoBar.error(title="导出失败", content=str(ex), parent=self,
                          position=InfoBarPosition.BOTTOM_RIGHT)

    def _format_time_srt(self, seconds: float) -> str:
        msecs = int(seconds * 1000)
        hours = msecs // 3600000
        mins = (msecs % 3600000) // 60000
        secs = (msecs % 60000) // 1000
        ms = msecs % 1000
        return f"{hours:02}:{mins:02}:{secs:02},{ms:03}"

class FileImportCard(CardWidget):
    """ Card for importing external SRT files """
    file_selected = pyqtSignal(str)
    start_translation = pyqtSignal()
    start_repair = pyqtSignal()
    stop_task = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(20, 15, 20, 15)
        self.h_layout.setSpacing(15)

        # Import Button
        self.import_btn = PushButton(FIF.FOLDER, "导入字幕", self)
        self.import_btn.setFixedWidth(130)
        self.import_btn.clicked.connect(self._on_import_clicked)
        
        # Start Translation Button
        self.start_btn = PrimaryPushButton(FIF.LANGUAGE, "开始翻译", self)
        self.start_btn.setFixedWidth(130)
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_translation.emit)

        # Start Repair Button
        self.fix_btn = PrimaryPushButton(FIF.EDIT, "开始修复", self)
        self.fix_btn.setFixedWidth(130)
        self.fix_btn.setEnabled(False) # Enabled only when subs exist
        self.fix_btn.clicked.connect(self.start_repair.emit)

        # Stop Button
        self.stop_btn = PushButton(FIF.CANCEL, "终止任务", self)
        self.stop_btn.setFixedWidth(130)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_task.emit)

        # File Info Container
        self.info_container = QWidget()
        self.info_layout = QVBoxLayout(self.info_container)
        self.info_layout.setContentsMargins(0, 0, 0, 0)
        self.info_layout.setSpacing(4)

        self.filename_label = StrongBodyLabel("未选择文件", self)
        self.path_label = CaptionLabel("请点击按钮导入 SRT 字幕文件", self)
        self.path_label.setStyleSheet("color: #6B7280;")

        self.info_layout.addWidget(self.filename_label)
        self.info_layout.addWidget(self.path_label)

        self.h_layout.addWidget(self.import_btn)
        self.h_layout.addWidget(self.start_btn)
        self.h_layout.addWidget(self.fix_btn)
        self.h_layout.addWidget(self.stop_btn)
        self.h_layout.addWidget(self.info_container)
        self.h_layout.addStretch(1)

    def _on_import_clicked(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 SRT 字幕文件",
            "",
            "SRT Files (*.srt)"
        )
        if file_path:
            self.set_file_path(file_path)
            self.file_selected.emit(file_path)

    def set_file_path(self, file_path):
        """ Update UI with file info """
        filename = os.path.basename(file_path)
        self.filename_label.setText(filename)
        self.path_label.setText(file_path)
        self.start_btn.setEnabled(True)
        self.fix_btn.setEnabled(True)


class TranslationInterface(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("translation_interface")
        self._init_ui()
        
    def _init_ui(self):
        self.setAcceptDrops(True)
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(20)
        
        # 1. Title
        self.title_label = SubtitleLabel("翻译与溢出修复", self)
        self.main_layout.addWidget(self.title_label)

        # 2. File Import Card
        self.import_card = FileImportCard(self)
        self.main_layout.addWidget(self.import_card)

        # 3. Subtitle List
        self.subtitle_list = SubtitleListWidget(self)
        self.main_layout.addWidget(self.subtitle_list, 1)

        # Connect signals
        self.import_card.file_selected.connect(self._on_file_imported)
        self.import_card.start_translation.connect(self._on_start_translation)
        self.import_card.start_repair.connect(self._on_start_repair)
        self.import_card.stop_task.connect(self._on_stop_task)
        cfg.max_line_count.valueChanged.connect(self.subtitle_list.update_threshold)

        # Connect config signal directly
        cfg.remove_punctuation.valueChanged.connect(self.subtitle_list.set_remove_punctuation)

        # Initialize state
        self.current_subtitles = []
        self.translation_thread = None
        self.fix_thread = None
        self._phase_start_ts = None
        # 各环节用时（秒），用于质量报告
        self._last_translation_elapsed_sec = None
        self._last_fix_elapsed_sec = None
        # UI 任务代次：用于屏蔽已过期线程回调，避免串写当前界面状态
        self._active_task_token = 0
        self.current_status_msg = ""
        self.current_source_path = None
        self._suppress_auto_open_export_dir = False
        self._shutting_down = False
        self._stop_pending = False

    def _set_task_running(self, running: bool):
        self.import_card.start_btn.setEnabled(not running)
        self.import_card.import_btn.setEnabled(not running)
        self.import_card.fix_btn.setEnabled(not running)
        self.import_card.stop_btn.setEnabled(running)

    def _safe_disconnect(self, signal, signal_name: str):
        try:
            signal.disconnect()
        except Exception:
            logging.debug("[TranslationInterface] disconnect ignored: %s", signal_name)

    def _safe_stop_worker(self, thread, name: str):
        if not thread:
            return False
        if not thread.isRunning():
            return False
        try:
            thread.stop()
            return True
        except Exception:
            logging.exception("[TranslationInterface] 停止线程失败: %s", name)
            return False

    def _safe_set_subtitle_list_data(self):
        """线程/定时器回调中刷新列表前校验界面仍有效，避免关闭或切换后误触已销毁控件。"""
        if not hasattr(self, "subtitle_list") or self.subtitle_list is None:
            return
        win = self.subtitle_list.window()
        if win is None or not win.isVisible():
            return
        self.subtitle_list.set_data(self.current_subtitles)

    def _next_task_token(self) -> int:
        self._active_task_token += 1
        return self._active_task_token

    def _is_task_active(self, task_token: int | None) -> bool:
        if task_token is None:
            return True
        return (not self._shutting_down) and task_token == self._active_task_token

    def _clear_progress_container(self):
        if hasattr(self, 'progress_container'):
            self.progress_container.deleteLater()
            del self.progress_container

    def _on_worker_thread_finished(self, attr_name: str, task_token=None):
        """线程结束后的统一清理，避免线程对象在仍运行时被销毁。"""
        thread = getattr(self, attr_name, None)
        if thread is not None and not thread.isRunning():
            setattr(self, attr_name, None)

        if self._stop_pending:
            translation_running = bool(self.translation_thread and self.translation_thread.isRunning())
            fix_running = bool(self.fix_thread and self.fix_thread.isRunning())
            if not translation_running and not fix_running:
                self._stop_pending = False
                self._set_task_running(False)
                self._phase_start_ts = None
                self.current_status_msg = ""
                self._clear_progress_container()
                InfoBar.warning(
                    title="任务终止",
                    content="任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )

    def _is_cancel_message(self, message: str) -> bool:
        return isinstance(message, str) and ("取消" in message or "终止" in message)

    def _on_start_repair_if_active(self, task_token=None):
        if not self._is_task_active(task_token):
            return
        self._on_start_repair()

    def _ensure_progress(self, label_text: str, total: int):
        if not hasattr(self, 'progress_container'):
            self.progress_container = QWidget(self)
            self.progress_layout = QVBoxLayout(self.progress_container)
            self.progress_layout.setContentsMargins(0, 0, 0, 0)
            self.progress_layout.setSpacing(5)

            self.progress_label = BodyLabel(label_text, self.progress_container)
            self.progress_bar = ProgressBar(self.progress_container)

            self.progress_layout.addWidget(self.progress_label)
            self.progress_layout.addWidget(self.progress_bar)

            self.main_layout.insertWidget(2, self.progress_container)

        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.progress_label.setText(label_text)
        self._phase_start_ts = time.time()

    def _get_glossary_text(self):
        path = cfg.glossaryPath.value
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    def _term_in_text(self, term: str, text: str):
        if not term or not text:
            return False
        if re.search(r"[A-Za-z0-9_]", term):
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
            return bool(pattern.search(text))
        return term in text

    def _get_glossary_stats(self):
        glossary_text = self._get_glossary_text()
        if not glossary_text:
            return None
        translator = LLMTranslator()
        pairs = translator._parse_glossary_pairs(glossary_text)
        if not pairs:
            return None

        original_blob = "\n".join(sub.get("text", "") for sub in self.current_subtitles)
        translated_blob = "\n".join((sub.get("translated_text") or "") for sub in self.current_subtitles)

        applicable = 0
        hit = 0
        missing = 0
        untranslated = 0

        for src, tgt in pairs:
            if not self._term_in_text(src, original_blob):
                continue
            applicable += 1
            if self._term_in_text(tgt, translated_blob):
                hit += 1
            else:
                missing += 1
                if self._term_in_text(src, translated_blob):
                    untranslated += 1

        if applicable == 0:
            return None

        return {
            "applicable": applicable,
            "hit": hit,
            "missing": missing,
            "untranslated": untranslated
        }

    def _maybe_show_glossary_stats(self):
        stats = self._get_glossary_stats()
        if not stats:
            return
        applicable = stats["applicable"]
        hit = stats["hit"]
        missing = stats["missing"]
        untranslated = stats["untranslated"]
        if missing == 0:
            InfoBar.success(
                title="术语一致性",
                content=f"术语覆盖率 {hit}/{applicable}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000
            )
        else:
            suffix = f"，疑似未译 {untranslated} 条" if untranslated > 0 else ""
            InfoBar.warning(
                title="术语提示",
                content=f"术语覆盖率 {hit}/{applicable}，未命中 {missing} 条{suffix}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=4000
            )

    @staticmethod
    def _norm_text_for_compare(text: str) -> str:
        return re.sub(r"[^\w]", "", (text or "")).lower()

    @staticmethod
    def _translated_view_text(seg: Dict[str, Any]) -> str:
        return ((seg.get("translated_text") or seg.get("text") or "").strip())

    def _clean_translated_segments(self, segments: List[Dict[str, Any]], min_duration_ms: int = 100) -> List[Dict[str, Any]]:
        if not segments:
            return segments

        cleaned = []
        removed_short = 0
        removed_prefix = 0
        n = len(segments)

        for i, seg in enumerate(segments):
            start = float(seg.get("start", 0) or 0)
            end = float(seg.get("end", 0) or 0)
            if (end - start) * 1000 < min_duration_ms:
                removed_short += 1
                print(f"[Translation] 删除零时长段: [{start:.3f}→{end:.3f}] \"{self._translated_view_text(seg)[:40]}\"")
                continue

            cur_norm = self._norm_text_for_compare(self._translated_view_text(seg))
            if i + 1 < n:
                next_norm = self._norm_text_for_compare(self._translated_view_text(segments[i + 1]))
                if cur_norm and next_norm.startswith(cur_norm) and cur_norm != next_norm:
                    removed_prefix += 1
                    print(f"[Translation] 删除前缀重复译文段: \"{self._translated_view_text(seg)[:40]}\"")
                    continue

            cleaned.append(seg)

        deduped = []
        removed_dup = 0
        for seg in cleaned:
            cur_norm = self._norm_text_for_compare(self._translated_view_text(seg))
            if deduped:
                prev = deduped[-1]
                prev_norm = self._norm_text_for_compare(self._translated_view_text(prev))
                if cur_norm and cur_norm == prev_norm:
                    prev["end"] = max(float(prev.get("end", 0) or 0), float(seg.get("end", 0) or 0))
                    removed_dup += 1
                    print(f"[Translation] 合并连续重复译文段: \"{self._translated_view_text(seg)[:40]}\"")
                    continue
            deduped.append(seg)

        hallu_keywords = [
            "请不吝点赞", "订阅", "转发", "感谢观看", "谢谢大家",
            "Subscribe", "Thanks for watching", "Subtitles by"
        ]
        final = []
        removed_hallu = 0
        for seg in deduped:
            trans = (seg.get("translated_text") or "").strip()
            if not trans:
                final.append(seg)
                continue

            duration = float(seg.get("end", 0) or 0) - float(seg.get("start", 0) or 0)
            if any(k in trans for k in hallu_keywords):
                removed_hallu += 1
                print(f"[Translation] 删除疑似幻觉译文段(关键词): \"{trans[:40]}\"")
                continue

            if duration > 0 and duration < 2.0:
                max_reasonable_chars = duration * 45
                if len(trans) > max_reasonable_chars:
                    removed_hallu += 1
                    print(f"[Translation] 删除疑似幻觉译文段(语速异常): [{duration:.2f}s/{len(trans)}字] \"{trans[:40]}\"")
                    continue

            final.append(seg)

        total_removed = removed_short + removed_prefix + removed_dup + removed_hallu
        if total_removed > 0:
            print(f"[Translation] 译文清理完成: 删除 {total_removed} 条（零时长 {removed_short} / 前缀重复 {removed_prefix} / 连续重复 {removed_dup} / 疑似幻觉 {removed_hallu}）")

        return final

    def _post_process_subtitles(self):
        if not self.current_subtitles:
            return

        fix_timestamp_overlaps(self.current_subtitles)

        if cfg.remove_punctuation.value:
            for sub in self.current_subtitles:
                txt = (sub.get("translated_text") or "")
                if txt:
                    sub["translated_text"] = clean_punctuation_text(txt)

        if cfg.translation_hallucination_filter.value:
            cleaned = self._clean_translated_segments(self.current_subtitles)
            if cleaned:
                self.current_subtitles = cleaned
            else:
                print("[Translation] 译文清理后为空，保留原字幕结果。")

        fix_timestamp_overlaps(self.current_subtitles)
        for i, seg in enumerate(self.current_subtitles):
            seg["index"] = i + 1

    def set_data_from_transcription(self, subtitles: List[Dict[str, Any]], filename: str, original_file_path: str = None, auto_start: bool = None, phase_timings: dict = None):
        """ 接收转录+优化断句结果：写入翻译界面并启用开始翻译；仅当 auto_start 为 True 时自动开始翻译；phase_timings 用于质量报告打印各环节用时 """
        if auto_start is None:
            auto_start = cfg.workflow_translate.value
        self.current_subtitles = []
        self._phase_timings = phase_timings or {}

        # Store original path for export
        self.current_source_path = original_file_path
        self._suppress_auto_open_export_dir = False
        
        for i, item in enumerate(subtitles):
            seg = {
                "index": i + 1,
                "start": item.get("start", 0),
                "end": item.get("end", 0),
                "text": item.get("optimized_text", item.get("text", "")),
                "translated_text": ""
            }
            if item.get("speaker") is not None:
                seg["speaker"] = str(item["speaker"])
            self.current_subtitles.append(seg)
            
        # Update UI info - 模拟 FileImportCard 的 set_file_path 行为
        self.import_card.filename_label.setText(filename)
        display_path = original_file_path if original_file_path else f"自动转录: {filename}"
        self.import_card.path_label.setText(display_path)
        
        # 无论是否自动翻译，都启用导入卡上的「开始翻译」「开始修复」按钮
        self.import_card.start_btn.setEnabled(True)
        self.import_card.fix_btn.setEnabled(True)
        
        # 显示字幕列表（让用户看到已传入的字幕，并供随后自动翻译使用）
        self.subtitle_list.set_data(self.current_subtitles)
        
        if auto_start:
            InfoBar.info(
                title="数据接收成功",
                content=f"已接收 {len(self.current_subtitles)} 条字幕，即将自动开始翻译...",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000
            )
            QTimer.singleShot(1000, lambda: self._on_start_translation(interactive=False))
        else:
            InfoBar.info(
                title="数据接收成功",
                content=f"已接收 {len(self.current_subtitles)} 条字幕，可点击「开始翻译」进行翻译。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000
            )

    def begin_realtime_session(self, session_name: str, session_anchor_path: str | None = None):
        """为实时捕获会话准备一条新的累计字幕轨。"""
        self._on_stop_task()
        self.current_subtitles = []
        self._phase_timings = {}
        self.current_source_path = session_anchor_path
        self._suppress_auto_open_export_dir = True
        self.import_card.filename_label.setText(f"{session_name}.wav")
        self.import_card.path_label.setText(session_anchor_path or f"实时捕获会话: {session_name}")
        self.import_card.start_btn.setEnabled(True)
        self.import_card.fix_btn.setEnabled(True)
        self.subtitle_list.set_data(self.current_subtitles)

    def reset_realtime_session(self):
        """清空实时捕获会话在翻译页的累计字幕与状态。"""
        self._on_stop_task()
        self.current_subtitles = []
        self._phase_timings = {}
        self.current_source_path = None
        self._suppress_auto_open_export_dir = False
        self.import_card.filename_label.setText("未选择文件")
        self.import_card.path_label.setText("请点击按钮导入 SRT 字幕文件")
        self.import_card.start_btn.setEnabled(False)
        self.import_card.fix_btn.setEnabled(False)
        self.subtitle_list.set_data(self.current_subtitles)

    def append_processed_subtitles(
        self,
        subtitles: List[Dict[str, Any]],
        session_name: str,
        session_anchor_path: str | None = None,
    ):
        """追加一批已经处理完成的字幕到当前翻译轨道，并保持总时间轴连续。"""
        if not subtitles:
            return

        if session_anchor_path:
            self.current_source_path = session_anchor_path
            self.import_card.path_label.setText(session_anchor_path)

        self.import_card.filename_label.setText(f"{session_name}.wav")

        for item in subtitles:
            seg = {
                "start": item.get("start", 0),
                "end": item.get("end", 0),
                "text": item.get("optimized_text", item.get("text", "")),
                "translated_text": item.get("translated_text", ""),
            }
            if item.get("speaker") is not None:
                seg["speaker"] = str(item["speaker"])
            self.current_subtitles.append(seg)

        self.current_subtitles.sort(key=lambda seg: (seg.get("start", 0), seg.get("end", 0)))
        self._post_process_subtitles()
        self.subtitle_list.set_data(self.current_subtitles)
        self.import_card.start_btn.setEnabled(True)
        self.import_card.fix_btn.setEnabled(True)

        InfoBar.success(
            title="实时片段已并入",
            content=f"已累计 {len(self.current_subtitles)} 条字幕，可继续在本页检查与导出。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500
        )

    def _on_file_imported(self, file_path):
        """ Handle imported SRT file """
        try:
            project_manager.mark_runtime_log_start()

            # Sync UI in FileImportCard (especially for drag-and-drop)
            self.import_card.set_file_path(file_path)

            self.current_subtitles = self._parse_srt(file_path)
            self.subtitle_list.set_data(self.current_subtitles)

            InfoBar.success(
                title="导入成功",
                content=f"已加载字幕文件: {os.path.basename(file_path)}\n共 {len(self.current_subtitles)} 条字幕",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            
        except Exception as e:
            self.current_subtitles = []
            InfoBar.error(
                title="导入失败",
                content=str(e),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _parse_srt(self, file_path: str) -> List[Dict[str, Any]]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        subs = []
        # Normalize line endings
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        
        # Split by empty lines
        blocks = re.split(r'\n\s*\n', content.strip())
        
        for block in blocks:
            if not block.strip():
                continue
                
            lines = block.strip().split('\n')
            if len(lines) < 2:
                continue
                
            # 1. Index
            try:
                index = int(lines[0].strip())
            except ValueError:
                continue # Skip if not a number
                
            # 2. Time
            time_line = lines[1].strip()
            if '-->' not in time_line:
                continue
                
            start_str, end_str = time_line.split('-->')
            start = self._parse_time(start_str.strip())
            end = self._parse_time(end_str.strip())
            
            # 3. Text（支持仅原文 / 双语：两行时视为「原文在上、译文在下」）
            text_lines = [ln.strip() for ln in lines[2:] if ln.strip()]
            if len(text_lines) == 2:
                text = text_lines[0]
                translated_text = text_lines[1]
            elif text_lines:
                text = "\n".join(text_lines)
                translated_text = ""
            else:
                continue
            subs.append({
                "index": index,
                "start": start,
                "end": end,
                "text": text,
                "translated_text": translated_text or ""
            })
            
        return subs

    def _parse_time(self, time_str: str) -> float:
        # Format: 00:00:20,000
        time_str = time_str.replace(',', '.')
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        return 0.0

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].toLocalFile().lower().endswith('.srt'):
                event.accept()
            else:
                event.ignore()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            self._on_file_imported(file_path)

    def _on_start_translation(self, interactive=True):
        if (self.translation_thread and self.translation_thread.isRunning()) or (self.fix_thread and self.fix_thread.isRunning()):
            self._on_stop_task()
            if (self.translation_thread and self.translation_thread.isRunning()) or (self.fix_thread and self.fix_thread.isRunning()):
                InfoBar.warning(
                    title="请稍候",
                    content="上一个任务仍在结束中，请稍后再试",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=2500
                )
                return

        if not self.current_subtitles:
            if interactive:
                InfoBar.warning(
                    title="无法开始",
                    content="请先导入有效的 SRT 文件",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
            return
            
        if not cfg.llm_api_key.value:
            msg = "请先在设置中配置 LLM API Key (翻译功能需要)"
            if interactive:
                InfoBar.error(
                    title="配置缺失",
                    content=msg,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
            else:
                # Auto mode: Show error and restore original subs (without translation)
                InfoBar.error(
                    title="翻译中止",
                    content=msg,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=5000
                )
                # Restore view so user sees something
                self.subtitle_list.set_data(self.current_subtitles)
            return
            
        # Check for Video Context
        if interactive and (not cfg.videoContext.value or not cfg.videoContext.value.strip()):
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, 
                "建议", 
                "检测到您尚未输入视频语境。\n\n去“设置-任务配置”界面输入视频语境（如：这是一部关于量子力学的纪录片），翻译效果更佳！\n\n是否继续翻译（不输入语境）？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        # Check if subtitles already have translation content
        has_translation = any((sub.get("translated_text") or "") for sub in self.current_subtitles)
        if interactive and has_translation:
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, 
                "确认", 
                "字幕预览当中已有字幕，是否继续翻译？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return
            # 用户确认重新翻译，清除已有译文，防止断点续传跳过全部批次
            for sub in self.current_subtitles:
                sub["translated_text"] = ""

        self._set_task_running(True)
        self.current_status_msg = "正在翻译"
        self._stop_pending = False
        self._ensure_progress(f"正在翻译: 0/{len(self.current_subtitles)} (0%)", len(self.current_subtitles))
        task_token = self._next_task_token()

        # Get glossary content from config
        glossary_content = ""
        if cfg.glossaryPath.value and os.path.exists(cfg.glossaryPath.value):
            try:
                with open(cfg.glossaryPath.value, 'r', encoding='utf-8') as f:
                    glossary_content = f.read()
            except Exception as e:
                print(f"Error reading glossary: {e}")
        
        self.translation_thread = TranslationThread(self.current_subtitles, glossary=glossary_content)
        self.translation_thread.progress_update.connect(lambda cur, tot, t=task_token: self._on_progress_update(cur, tot, t))
        self.translation_thread.finished_signal.connect(lambda ok, msg, t=task_token: self._on_translation_finished(ok, msg, t))
        self.translation_thread.status_update.connect(lambda msg, t=task_token: self._on_status_update(msg, t))
        self.translation_thread.finished.connect(lambda t=task_token: self._on_worker_thread_finished("translation_thread", t))
        self.translation_thread.start()
        
        InfoBar.info(
            title="开始翻译",
            content="正在调用 LLM 进行批量翻译...",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT
        )

    def _format_eta(self, seconds):
        if seconds is None or seconds < 0:
            return ""
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02}:{m:02}:{s:02}"
        return f"{m:02}:{s:02}"

    def _on_progress_update(self, current, total, task_token=None):
        if not self._is_task_active(task_token):
            return
        if hasattr(self, 'progress_bar'):
            self.progress_bar.setValue(current)
            percent = int((current / total) * 100) if total > 0 else 0

            phase = getattr(self, "current_status_msg", "正在翻译")
            if hasattr(self, 'fix_thread') and self.fix_thread and self.fix_thread.isRunning():
                phase = "正在修复"
            eta_text = ""
            if self._phase_start_ts and current > 0 and total > 0:
                elapsed = time.time() - self._phase_start_ts
                remaining = elapsed * (total - current) / max(current, 1)
                eta = self._format_eta(remaining)
                if eta:
                    eta_text = f" 预计剩余 {eta}"
            self.progress_label.setText(f"{phase}: {current}/{total} ({percent}%){eta_text}")

    def _on_status_update(self, msg, task_token=None):
        """Update status message from thread"""
        if not self._is_task_active(task_token):
            return
        self.current_status_msg = msg
        if hasattr(self, 'progress_label'):
            current_text = self.progress_label.text()
            if ":" in current_text:
                parts = current_text.split(":")
                suffix = ":".join(parts[1:])
                self.progress_label.setText(f"{msg}{suffix}")

    def _on_translation_finished(self, success, message, task_token=None):
        if not self._is_task_active(task_token):
            return

        if self._stop_pending and self._is_cancel_message(message):
            return
        if self._stop_pending and not self._is_cancel_message(message):
            self._stop_pending = False

        self._set_task_running(False)
        if success and self._phase_start_ts is not None:
            self._last_translation_elapsed_sec = round(time.time() - self._phase_start_ts, 1)
        self._phase_start_ts = None
        
        if success:
            if hasattr(self, 'progress_label'):
                self.progress_label.setText("翻译完成，正在加载结果...")
                self.progress_bar.setValue(self.progress_bar.maximum())

            InfoBar.success(
                title="翻译完成",
                content=message,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            self._post_process_subtitles()
            # 延迟加载列表，让 InfoBar 和进度条先完成渲染，避免同帧大量 widget 操作导致撕裂
            QTimer.singleShot(100, self._safe_set_subtitle_list_data)
            self._maybe_show_glossary_stats()
            
            threshold = cfg.max_line_count.value
            has_overflow = any(len((sub.get("translated_text") or "")) > threshold for sub in self.current_subtitles)
            
            if has_overflow and cfg.workflow_overflow_fix.value:
                InfoBar.info(
                    title="自动修复",
                    content="检测到溢出字幕，正在自动启动修复...",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=2000
                )
                QTimer.singleShot(1000, lambda t=task_token: self._on_start_repair_if_active(t))
            else:
                self._clear_progress_container()
                self._auto_export_all()

        else:
            self._clear_progress_container()

            if self._is_cancel_message(message):
                InfoBar.warning(
                    title="任务终止",
                    content="任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=2500
                )
                return

            InfoBar.error(
                title="翻译失败",
                content=message,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=-1
            )

    def _on_start_repair(self):
        if self.fix_thread and self.fix_thread.isRunning():
            InfoBar.warning(
                title="请稍候",
                content="修复任务仍在进行中",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500
            )
            return

        if not self.current_subtitles:
            InfoBar.warning(
                title="无法开始",
                content="请先导入有效的 SRT 文件",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        # Check for overflows
        threshold = cfg.max_line_count.value
        has_overflow = any(len((sub.get("translated_text") or "")) > threshold for sub in self.current_subtitles)
        if not has_overflow:
            InfoBar.warning(
                title="无需修复",
                content="当前没有检测到溢出的字幕",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        if not cfg.llm_api_key.value:
            InfoBar.error(
                title="配置缺失",
                content="请先在设置中配置 LLM API Key",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        self._set_task_running(True)
        self.current_status_msg = "正在修复"
        self._stop_pending = False
        self._ensure_progress(f"正在修复: 0/{len(self.current_subtitles)} (0%)", len(self.current_subtitles))
        task_token = self._next_task_token()

        # Start Thread
        threshold = cfg.max_line_count.value
        self.fix_thread = OverflowFixThread(self.current_subtitles, threshold)
        self.fix_thread.progress_update.connect(lambda cur, tot, t=task_token: self._on_progress_update(cur, tot, t))
        self.fix_thread.status_update.connect(lambda msg, t=task_token: self._on_status_update(msg, t))
        self.fix_thread.finished_signal.connect(lambda ok, msg, t=task_token: self._on_repair_finished(ok, msg, t))
        self.fix_thread.finished.connect(lambda t=task_token: self._on_worker_thread_finished("fix_thread", t))
        self.fix_thread.start()
        
        InfoBar.info(
            title="开始修复",
            content="正在进行 AI 溢出修复...",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT
        )

    def _on_repair_finished(self, success, message, task_token=None):
        if not self._is_task_active(task_token):
            return

        if self._stop_pending and self._is_cancel_message(message):
            return
        if self._stop_pending and not self._is_cancel_message(message):
            self._stop_pending = False

        self._set_task_running(False)
        if success and self._phase_start_ts is not None:
            self._last_fix_elapsed_sec = round(time.time() - self._phase_start_ts, 1)
        self._phase_start_ts = None
        
        if success:
            if hasattr(self, 'progress_label'):
                self.progress_label.setText("修复完成，正在导出...")
                self.progress_bar.setValue(self.progress_bar.maximum())

            InfoBar.success(
                title="修复完成",
                content=message,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            # Update data with new subtitles
            if self.fix_thread and self.fix_thread.new_subtitles:
                self.current_subtitles = self.fix_thread.new_subtitles
                self._post_process_subtitles()
                # 延迟加载列表，让 InfoBar 先完成渲染，避免同帧大量 widget 操作导致撕裂
                QTimer.singleShot(100, self._safe_set_subtitle_list_data)
            
            threshold = cfg.max_line_count.value
            has_overflow = any(len((sub.get("translated_text") or "")) > threshold for sub in self.current_subtitles)
            if has_overflow:
                InfoBar.warning(
                    title="溢出提示",
                    content="修复后仍存在溢出字幕，建议检查后再导出",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=3000
                )

            self._auto_export_all()
            self._clear_progress_container()

        else:
            self._clear_progress_container()

            if self._is_cancel_message(message):
                InfoBar.warning(
                    title="任务终止",
                    content="任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=2500
                )
                return

            # 断点续传：取消时若有已修复部分，写回列表并刷新，下次「开始修复」只修剩余溢出条
            if self.fix_thread and self.fix_thread.new_subtitles:
                self.current_subtitles = self.fix_thread.new_subtitles
                self._post_process_subtitles()
                QTimer.singleShot(100, self._safe_set_subtitle_list_data)
                InfoBar.info(
                    title="已暂停",
                    content="已保存当前修复进度，再次点击「开始修复」将从剩余溢出条继续",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=3500
                )
            else:
                InfoBar.error(
                    title="修复失败",
                    content=message,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )

    def _on_stop_task(self):
        """ 终止当前任务 """
        self._active_task_token += 1
        self._set_task_running(False)
        self._phase_start_ts = None

        requested = False

        if self.translation_thread and self.translation_thread.isRunning():
            requested = self._safe_stop_worker(self.translation_thread, "translation_thread") or requested

        if self.fix_thread and self.fix_thread.isRunning():
            requested = self._safe_stop_worker(self.fix_thread, "fix_thread") or requested

        if self.translation_thread and not self.translation_thread.isRunning():
            self.translation_thread = None
        if self.fix_thread and not self.fix_thread.isRunning():
            self.fix_thread = None

        if requested:
            self._stop_pending = True
            self.current_status_msg = "正在终止"
            if hasattr(self, 'progress_label'):
                self.progress_label.setText("正在终止任务，请稍候...")
            InfoBar.info(
                title="任务终止中",
                content="已发送终止请求，正在等待后台任务收尾",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500
            )
        else:
            self._stop_pending = False
            self._clear_progress_container()

    def _auto_export_all(self):
        """ Automatically export subtitles in 4 formats to a folder """
        try:
            self._post_process_subtitles()
            threshold = cfg.max_line_count.value
            overflow_count = sum(
                1 for sub in self.current_subtitles
                if len((sub.get("translated_text") or "")) > threshold
            )
            fallback_count = sum(
                1 for sub in self.current_subtitles
                if sub.get("text", "").strip() and (sub.get("translated_text") or "").strip() == sub.get("text", "").strip()
            )
            glossary_stats = self._get_glossary_stats()
            summary_parts = [
                f"溢出 {overflow_count} 条",
                f"疑似回退 {fallback_count} 条"
            ]
            if glossary_stats:
                summary_parts.append(f"术语覆盖率 {glossary_stats['hit']}/{glossary_stats['applicable']}")
                if glossary_stats["missing"] > 0:
                    summary_parts.append(f"未命中 {glossary_stats['missing']} 条")
                if glossary_stats["untranslated"] > 0:
                    summary_parts.append(f"疑似未译 {glossary_stats['untranslated']} 条")
            summary_text = "，".join(summary_parts)
            has_risk = overflow_count > 0 or fallback_count > 0
            if glossary_stats and (glossary_stats["missing"] > 0 or glossary_stats["untranslated"] > 0):
                has_risk = True
            if has_risk:
                InfoBar.warning(
                    title="导出前质量汇总",
                    content=summary_text,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=4000
                )
            else:
                InfoBar.info(
                    title="导出前质量汇总",
                    content=summary_text,
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT,
                    duration=3000
                )
            # Determine base folder and filename
            original_filename = self.import_card.filename_label.text()
            if not original_filename or original_filename == "未选择文件":
                original_filename = "output.srt"
                
            base_name = os.path.splitext(original_filename)[0]
            
            # Determine export location
            # Priority: 1. Directory of dragged/original file. 2. Desktop
            
            export_parent_dir = None
            
            # Check if we have the original source path stored (from auto-flow)
            if hasattr(self, 'current_source_path') and self.current_source_path:
                export_parent_dir = os.path.dirname(self.current_source_path)
            
            # Or try to parse from UI label if it's a real path (from manual import)
            if not export_parent_dir:
                ui_path = self.import_card.path_label.text()
                if os.path.exists(ui_path) and os.path.isfile(ui_path):
                    export_parent_dir = os.path.dirname(ui_path)
            
            # Fallback to Desktop
            if not export_parent_dir or not os.path.exists(export_parent_dir):
                export_parent_dir = os.path.join(os.path.expanduser("~"), "Desktop")

            export_root = os.path.join(export_parent_dir, f"{base_name}_导出")
            os.makedirs(export_root, exist_ok=True)

            # ── 检测是否有说话人分离数据 ──────────────────────────────────────
            has_diarization = any("speaker" in sub for sub in self.current_subtitles)
            has_trans = any((sub.get("translated_text") or "").strip() for sub in self.current_subtitles)

            if has_diarization:
                # 说话人分离模式
                from collections import defaultdict
                speaker_segs = defaultdict(list)
                for sub in self.current_subtitles:
                    speaker_segs[sub.get("speaker", "unknown")].append(sub)

                for spk, segs in sorted(speaker_segs.items()):
                    self._write_srt_segs(os.path.join(export_root, f"{base_name}_{spk}_仅原文.srt"), segs, "orig_only")
                    if has_trans:
                        self._write_srt_segs(os.path.join(export_root, f"{base_name}_{spk}_仅译文.srt"), segs, "trans_only")
                        self._write_srt_segs(os.path.join(export_root, f"{base_name}_{spk}_双语_译文在上.srt"), segs, "trans_first")
                        self._write_srt_segs(os.path.join(export_root, f"{base_name}_{spk}_双语_原文在上.srt"), segs, "orig_first")

                news_path = os.path.join(export_root, f"{base_name}_新闻稿.txt")
                with open(news_path, "w", encoding="utf-8") as f:
                    for sub in self.current_subtitles:
                        ts = self._format_srt_time(sub.get("start", 0))[:8]
                        spk = sub.get("speaker", "Speaker")
                        orig = (sub.get("text") or "").strip()
                        f.write(f"[{ts}] [{spk}] {orig}\n")

            else:
                # 普通模式
                srt_formats = {
                    "trans_first": f"{base_name}_译文在上.srt",
                    "orig_first": f"{base_name}_原文在上.srt",
                    "trans_only": f"{base_name}_仅译文.srt",
                    "orig_only": f"{base_name}_仅原文.srt"
                }
                for mode, fname in srt_formats.items():
                    self._write_srt(os.path.join(export_root, fname), mode)

                # VTT 格式（译文在上 + 仅译文）
                self._write_vtt(os.path.join(export_root, f"{base_name}_译文在上.vtt"), "trans_first")
                self._write_vtt(os.path.join(export_root, f"{base_name}_仅译文.vtt"), "trans_only")

                # ASS 格式（仅译文）
                self._write_ass(os.path.join(export_root, f"{base_name}_仅译文.ass"), "trans_only")
            
            runtime_log_path = os.path.join(export_root, f"{base_name}_运行日志.txt")
            write_runtime_log(
                runtime_log_path,
                file_name=original_filename,
                log_text=project_manager.get_runtime_log_text(),
            )

            report_path = os.path.join(export_root, f"{base_name}_质量报告.txt")
            report_lines = []
            # 各环节用时与总用时（含转录、优化+断句、翻译、溢出修复）
            total_sec = 0
            if getattr(self, "_phase_timings", None):
                pt = self._phase_timings
                if pt.get("transcribe_sec") is not None:
                    report_lines.append(f"转录用时: {pt['transcribe_sec']} 秒")
                    total_sec += pt["transcribe_sec"]
                if pt.get("optimize_split_sec") is not None:
                    report_lines.append(f"优化+断句用时: {pt['optimize_split_sec']} 秒")
                    total_sec += pt["optimize_split_sec"]
            if self._last_translation_elapsed_sec is not None:
                report_lines.append(f"翻译用时: {self._last_translation_elapsed_sec} 秒")
                total_sec += self._last_translation_elapsed_sec
            if self._last_fix_elapsed_sec is not None:
                report_lines.append(f"溢出修复用时: {self._last_fix_elapsed_sec} 秒")
                total_sec += self._last_fix_elapsed_sec
            if total_sec > 0 or report_lines:
                if total_sec > 0:
                    report_lines.append(f"总用时: {round(total_sec, 1)} 秒")
                report_lines.append("")
            report_lines.extend([
                f"溢出: {overflow_count}",
                f"疑似回退: {fallback_count}"
            ])
            if glossary_stats:
                report_lines.append(f"术语覆盖率: {glossary_stats['hit']}/{glossary_stats['applicable']}")
                report_lines.append(f"术语未命中: {glossary_stats['missing']}")
                report_lines.append(f"疑似未译术语: {glossary_stats['untranslated']}")
            report_lines.append(f"运行日志文件: {os.path.basename(runtime_log_path)}")
            report_lines.append(f"汇总: {summary_text}")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write("\n".join(report_lines))

            if project_manager.temp_audio_paths:
                for wav_path in list(project_manager.temp_audio_paths):
                    try:
                        if wav_path and os.path.exists(wav_path):
                            os.remove(wav_path)
                    except Exception:
                        pass
                project_manager.temp_audio_paths = []
                
            InfoBar.success(
                title="自动导出完成",
                content=f"所有格式与质量报告已导出至:\n{export_root}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=5000
            )

            if not self._suppress_auto_open_export_dir:
                os.startfile(export_root)
             
        except Exception as e:
            InfoBar.error(
                title="自动导出失败",
                content=str(e),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _format_srt_time(self, seconds: float) -> str:
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); ms = int((seconds * 1000) % 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _format_vtt_time(self, seconds: float) -> str:
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); ms = int((seconds * 1000) % 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    def _format_ass_time(self, seconds: float) -> str:
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); cs = int((seconds * 100) % 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def _get_sub_content(self, sub, mode):
        text = sub.get("text", "")
        cn = (sub.get("translated_text") or "")
        if mode == "trans_first":
            return f"{cn}\n{text}" if cn and text else (cn or text)
        elif mode == "orig_first":
            return f"{text}\n{cn}" if cn and text else (text or cn)
        elif mode == "trans_only":
            return cn
        elif mode == "orig_only":
            return text
        return text

    def _write_srt_segs(self, path, segs, mode):
        """将指定 segs 列表写入 SRT，供说话人分离导出使用。"""
        with open(path, "w", encoding="utf-8-sig") as f:
            idx = 1
            for sub in segs:
                content = self._get_sub_content(sub, mode)
                if content and content.strip():
                    start = self._format_srt_time(sub.get("start", 0))
                    end = self._format_srt_time(sub.get("end", 0))
                    f.write(f"{idx}\n{start} --> {end}\n{content}\n\n")
                    idx += 1

    def _write_srt(self, path, mode):
        with open(path, "w", encoding="utf-8-sig") as f:
            for i, sub in enumerate(self.current_subtitles):
                start = self._format_srt_time(sub.get("start", 0))
                end = self._format_srt_time(sub.get("end", 0))
                f.write(f"{i+1}\n{start} --> {end}\n{self._get_sub_content(sub, mode)}\n\n")

    def _write_vtt(self, path, mode):
        with open(path, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n")
            for i, sub in enumerate(self.current_subtitles):
                start = self._format_vtt_time(sub.get("start", 0))
                end = self._format_vtt_time(sub.get("end", 0))
                f.write(f"{i+1}\n{start} --> {end}\n{self._get_sub_content(sub, mode)}\n\n")

    def _write_ass(self, path, mode):
        with open(path, "w", encoding="utf-8") as f:
            f.write(
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
            for sub in self.current_subtitles:
                start = self._format_ass_time(sub.get("start", 0))
                end = self._format_ass_time(sub.get("end", 0))
                text = self._get_sub_content(sub, mode).replace("\n", "\\N")
                f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")

    def _load_mock_data(self):
        # Sample data
        mock_subs = [
            {"start": 0.5, "end": 3.0, "text": "This is a normal subtitle.", "translated_text": "这是一个正常的字幕。"},
            {"start": 3.5, "end": 8.0, "text": "This is a very long subtitle that will definitely overflow the limit set by the user in the calibration panel.", 
             "translated_text": "这是一个非常长的字幕，肯定会超过用户在校准面板中设置的限制，因此它应该被标记为溢出并显示修复建议。"},
            {"start": 9.0, "end": 11.0, "text": "Short one.", "translated_text": "短句。"}
        ]
        self.subtitle_list.set_data(mock_subs)
