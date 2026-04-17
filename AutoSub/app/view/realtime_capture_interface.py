# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget, QHBoxLayout, QMessageBox
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SmoothScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
)

from app.common.batch_utils import load_glossary_text
from app.common.config import APP_ROOT, cfg
from app.common.thread import TaskCancelledError, TranscriptionThread
from app.core.realtime_loopback import AudioChunkConfig, SystemAudioLoopbackCapture
from app.view.translation_interface import OverflowFixThread, TranslationThread


@dataclass(slots=True)
class SegmentJob:
    segment_id: int
    wav_path: str
    global_start_sec: float
    duration_sec: float
    status: str = "排队中"
    optimized: list | None = None


class RealtimeCaptureWorker(QThread):
    started_ok = pyqtSignal(str, str)
    stats_changed = pyqtSignal(float, float, float)
    failed = pyqtSignal(str)

    def __init__(self, sample_rate: int = 16000, chunk_duration_ms: int = 100) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.chunk_duration_ms = chunk_duration_ms
        self._stop_event = threading.Event()
        self._buffer_lock = threading.Lock()
        self._segment_bytes = bytearray()
        self._segment_frames = 0
        self._session_frames = 0
        self.capture: Optional[SystemAudioLoopbackCapture] = None

    def run(self) -> None:
        self.capture = SystemAudioLoopbackCapture(
            AudioChunkConfig(
                capture_sample_rate=48000,
                capture_channels=2,
                target_sample_rate=self.sample_rate,
                chunk_duration_ms=self.chunk_duration_ms,
            )
        )
        try:
            self.capture.open()
            self.started_ok.emit(self.capture.backend, self.capture.device_name)
            while not self._stop_event.is_set():
                chunk = self.capture.read_chunk()
                if not chunk:
                    if self._stop_event.is_set():
                        break
                    continue
                frames = len(chunk) // 2
                with self._buffer_lock:
                    self._segment_bytes.extend(chunk)
                    self._segment_frames += frames
                    self._session_frames += frames
                    current_seg_sec = self._segment_frames / self.sample_rate
                    session_sec = self._session_frames / self.sample_rate
                self.stats_changed.emit(current_seg_sec, session_sec, float(self.capture.last_level))
        except Exception as exc:
            logging.exception("[RealtimeCaptureWorker] capture failed")
            self.failed.emit(str(exc))
        finally:
            if self.capture is not None:
                self.capture.close()
                self.capture = None

    def request_stop(self) -> None:
        self._stop_event.set()
        if self.capture is not None:
            self.capture.request_stop()

    def take_segment(self) -> tuple[bytes, int]:
        with self._buffer_lock:
            data = bytes(self._segment_bytes)
            frames = self._segment_frames
            self._segment_bytes.clear()
            self._segment_frames = 0
            return data, frames


class RealtimeCaptureInterface(SmoothScrollArea):
    open_translation_requested = pyqtSignal()

    def __init__(self, translate_interface, task_interface, parent=None):
        super().__init__(parent)
        self.setObjectName("RealtimeCaptureInterface")
        self.translateInterface = translate_interface
        self.taskInterface = task_interface

        self.capture_worker: RealtimeCaptureWorker | None = None
        self.processing_job: SegmentJob | None = None
        self.transcription_thread: TranscriptionThread | None = None
        self.translation_thread: TranslationThread | None = None
        self.fix_thread: OverflowFixThread | None = None

        self.segment_queue: deque[SegmentJob] = deque()
        self.segment_jobs: list[SegmentJob] = []
        self.segment_items: dict[int, QListWidgetItem] = {}
        self.timeline_cursor_sec = 0.0
        self.session_dir: Path | None = None
        self.session_anchor_path: Path | None = None
        self.session_name = ""
        self._segment_counter = 0
        self._session_running = False
        self.current_task_progress = 0

        self.scrollWidget = QWidget()
        self.scrollWidget.setObjectName("scrollWidget")
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        self.setStyleSheet("RealtimeCaptureInterface, #scrollWidget { background-color: transparent; border: none; }")

        self.vBoxLayout = QVBoxLayout(self.scrollWidget)
        self.vBoxLayout.setContentsMargins(36, 20, 36, 36)
        self.vBoxLayout.setSpacing(16)

        self._build_ui()
        self._refresh_buttons()

    def _build_ui(self) -> None:
        self.titleLabel = SubtitleLabel("实时捕获", self.scrollWidget)
        self.descLabel = BodyLabel(
            "持续采集系统音频。点击“提交当前片段”后，断点前音频会进入 AutoSub 既有转录与翻译链路，断点后继续录制。\n该界面设置沿用设置任务界面的全部参数和开关。",
            self.scrollWidget,
        )
        self.descLabel.setWordWrap(True)
        self.vBoxLayout.addWidget(self.titleLabel)
        self.vBoxLayout.addWidget(self.descLabel)

        self.statusCard = CardWidget(self.scrollWidget)
        statusLayout = QVBoxLayout(self.statusCard)
        statusLayout.setContentsMargins(20, 18, 20, 18)
        statusLayout.setSpacing(10)

        self.statusHeaderLayout = QHBoxLayout()
        self.statusHeaderLayout.setContentsMargins(0, 0, 0, 0)
        self.statusHeaderLayout.setSpacing(10)

        self.statusLabel = StrongBodyLabel("准备开始实时捕获", self.statusCard)
        self.statusRing = IndeterminateProgressRing(self.statusCard)
        self.statusRing.setFixedSize(18, 18)
        self.statusRing.hide()
        self.percentLabel = BodyLabel("片段进度 --", self.statusCard)
        self.deviceLabel = BodyLabel("当前仅支持系统全局音频 loopback 捕获。", self.statusCard)
        self.deviceLabel.setWordWrap(True)
        self.durationLabel = BodyLabel("当前片段: 0.0 秒 | 会话累计: 0.0 秒", self.statusCard)
        self.queueLabel = BodyLabel("队列: 0 段待处理", self.statusCard)
        self.levelBar = ProgressBar(self.statusCard)
        self.levelBar.setRange(0, 100)
        self.levelBar.setValue(0)

        self.statusHeaderLayout.addWidget(self.statusLabel)
        self.statusHeaderLayout.addStretch(1)
        self.statusHeaderLayout.addWidget(self.statusRing)
        self.statusHeaderLayout.addWidget(self.percentLabel)

        statusLayout.addLayout(self.statusHeaderLayout)
        statusLayout.addWidget(self.deviceLabel)
        statusLayout.addWidget(self.durationLabel)
        statusLayout.addWidget(self.queueLabel)
        statusLayout.addWidget(self.levelBar)
        self.vBoxLayout.addWidget(self.statusCard)

        self.actionCard = CardWidget(self.scrollWidget)
        actionLayout = QHBoxLayout(self.actionCard)
        actionLayout.setContentsMargins(20, 18, 20, 18)
        actionLayout.setSpacing(10)

        self.startBtn = PrimaryPushButton("开始捕获", self.actionCard)
        self.commitBtn = PushButton("提交当前片段", self.actionCard)
        self.stopBtn = PushButton("停止捕获", self.actionCard)
        self.resetBtn = PushButton("重置", self.actionCard)
        self.openTranslationBtn = PushButton("打开翻译页", self.actionCard)

        self.startBtn.clicked.connect(self._start_capture_session)
        self.commitBtn.clicked.connect(self._commit_current_segment)
        self.stopBtn.clicked.connect(self._stop_capture_session)
        self.resetBtn.clicked.connect(self._reset_capture_session)
        self.openTranslationBtn.clicked.connect(self.open_translation_requested.emit)

        actionLayout.addWidget(self.startBtn)
        actionLayout.addWidget(self.commitBtn)
        actionLayout.addWidget(self.stopBtn)
        actionLayout.addWidget(self.resetBtn)
        actionLayout.addStretch(1)
        actionLayout.addWidget(self.openTranslationBtn)
        self.vBoxLayout.addWidget(self.actionCard)

        self.segmentCard = CardWidget(self.scrollWidget)
        segmentLayout = QVBoxLayout(self.segmentCard)
        segmentLayout.setContentsMargins(20, 18, 20, 18)
        segmentLayout.setSpacing(10)

        self.segmentTitle = StrongBodyLabel("片段队列", self.segmentCard)
        self.segmentList = QListWidget(self.segmentCard)
        self.segmentList.setMinimumHeight(320)
        self.segmentList.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.segmentList.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.segmentList.setStyleSheet(
            """
            QListWidget {
                background: transparent;
                border: none;
                color: #9BA1AB;
            }
            QListWidget::item {
                border-bottom: 1px solid rgba(255, 255, 255, 0.06);
                padding: 10px 6px;
            }
            QScrollBar:vertical {
                background: rgba(255, 255, 255, 0.03);
                width: 10px;
                margin: 4px 0;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: rgba(63, 162, 102, 0.85);
                min-height: 36px;
                border-radius: 5px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            """
        )

        segmentLayout.addWidget(self.segmentTitle)
        segmentLayout.addWidget(self.segmentList)
        self.vBoxLayout.addWidget(self.segmentCard)
        self.vBoxLayout.addStretch(1)

    def _build_runtime_config(self) -> dict:
        return self.taskInterface._build_global_config_data()

    def _start_capture_session(self) -> None:
        if self._session_running:
            return
        if self._has_active_processing():
            InfoBar.warning(
                title="请稍候",
                content="上一轮片段仍在处理，请等待后台任务完成后再开始新会话。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
            )
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.session_name = f"实时捕获_{timestamp}"
        self.session_dir = APP_ROOT / "realtime_capture_sessions" / self.session_name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_anchor_path = self.session_dir / "session_anchor.txt"
        self.session_anchor_path.write_text("realtime capture session\n", encoding="utf-8")

        self.segment_queue.clear()
        self.segment_jobs.clear()
        self.segment_items.clear()
        self.segmentList.clear()
        self.timeline_cursor_sec = 0.0
        self._segment_counter = 0
        self._session_running = True
        self.current_task_progress = 0
        self.translateInterface.begin_realtime_session(self.session_name, str(self.session_anchor_path))

        self.capture_worker = RealtimeCaptureWorker()
        self.capture_worker.started_ok.connect(self._on_capture_started)
        self.capture_worker.stats_changed.connect(self._on_capture_stats)
        self.capture_worker.failed.connect(self._on_capture_failed)
        self.capture_worker.finished.connect(self._on_capture_thread_finished)
        self.capture_worker.start()

        self.statusLabel.setText("正在初始化系统音频捕获…")
        self.deviceLabel.setText("正在打开 Windows loopback 设备。")
        self._refresh_buttons()

    def _commit_current_segment(self) -> None:
        if self.capture_worker is None:
            return
        data, frames = self.capture_worker.take_segment()
        if frames <= 0 or not data:
            InfoBar.info(
                title="暂无可提交音频",
                content="当前片段还没有采集到足够的系统音频。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
            )
            return

        duration_sec = frames / self.capture_worker.sample_rate
        if duration_sec < 0.4:
            InfoBar.info(
                title="片段过短",
                content="当前片段太短，建议再录一点再提交。",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2000,
            )
            return

        self._segment_counter += 1
        wav_path = self._write_segment_wav(self._segment_counter, data)
        job = SegmentJob(
            segment_id=self._segment_counter,
            wav_path=str(wav_path),
            global_start_sec=self.timeline_cursor_sec,
            duration_sec=duration_sec,
        )
        self.timeline_cursor_sec += duration_sec
        self.segment_jobs.append(job)
        self.segment_queue.append(job)
        self._add_segment_item(job)
        self._update_queue_label()
        self._update_progress_indicator()
        self._start_next_job_if_idle()

    def _stop_capture_session(self) -> None:
        if not self._session_running:
            return

        if self.capture_worker is not None:
            data, frames = self.capture_worker.take_segment()
            if frames > 0 and data:
                duration_sec = frames / self.capture_worker.sample_rate
                if duration_sec >= 0.4:
                    self._segment_counter += 1
                    wav_path = self._write_segment_wav(self._segment_counter, data)
                    job = SegmentJob(
                        segment_id=self._segment_counter,
                        wav_path=str(wav_path),
                        global_start_sec=self.timeline_cursor_sec,
                        duration_sec=duration_sec,
                    )
                    self.timeline_cursor_sec += duration_sec
                    self.segment_jobs.append(job)
                    self.segment_queue.append(job)
                    self._add_segment_item(job)
                    self._update_queue_label()
                    self._update_progress_indicator()

            self.capture_worker.request_stop()
            self.statusLabel.setText("正在停止捕获，后台会继续处理已提交片段…")
            self.deviceLabel.setText("采集结束后，会按顺序处理并并入翻译页总时间轴。")

        self._session_running = False
        self._refresh_buttons()
        self._start_next_job_if_idle()

    def _reset_capture_session(self) -> None:
        if self._session_running or self.capture_worker is not None or self._has_active_processing() or self.segment_jobs:
            reply = QMessageBox.question(
                self,
                "确认重置",
                "重置后会清空当前实时捕获的时间码、切片列表和过程记录，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.stop_all()

        session_dir = self.session_dir
        self.segment_queue.clear()
        self.segment_jobs.clear()
        self.segment_items.clear()
        self.segmentList.clear()
        self.timeline_cursor_sec = 0.0
        self._segment_counter = 0
        self.current_task_progress = 0
        self.processing_job = None
        self.session_dir = None
        self.session_anchor_path = None
        self.session_name = ""
        self._session_running = False

        self.statusLabel.setText("准备开始实时捕获")
        self.deviceLabel.setText("当前仅支持系统全局音频 loopback 捕获。")
        self.durationLabel.setText("当前片段: 0.0 秒 | 会话累计: 0.0 秒")
        self.queueLabel.setText("队列: 0 段待处理")
        self.levelBar.setValue(0)
        self._refresh_buttons()

        try:
            self.translateInterface.reset_realtime_session()
        except Exception:
            logging.exception("[RealtimeCaptureInterface] reset translation session failed")

        if session_dir and session_dir.exists():
            try:
                shutil.rmtree(session_dir, ignore_errors=True)
            except Exception:
                logging.exception("[RealtimeCaptureInterface] remove session dir failed: %s", session_dir)

        InfoBar.success(
            title="已重置",
            content="本轮实时捕获的时间码、切片列表和过程记录已清空，可重新开始制作。",
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500,
        )

    def _on_capture_started(self, backend: str, device_name: str) -> None:
        self.statusLabel.setText("正在实时捕获系统音频")
        self.deviceLabel.setText(f"捕获后端: {backend} | 设备: {device_name}")
        self._refresh_buttons()

    def _on_capture_stats(self, current_seg_sec: float, session_sec: float, level: float) -> None:
        self.durationLabel.setText(
            f"当前片段: {current_seg_sec:.1f} 秒 | 会话累计: {session_sec:.1f} 秒"
        )
        self.levelBar.setValue(int(max(0.0, min(1.0, level)) * 100))

    def _on_capture_failed(self, message: str) -> None:
        self._session_running = False
        self.statusLabel.setText("实时捕获启动失败")
        self.deviceLabel.setText(message)
        self._refresh_buttons()
        InfoBar.error(
            title="实时捕获失败",
            content=message,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000,
        )

    def _on_capture_thread_finished(self) -> None:
        self.capture_worker = None
        self.levelBar.setValue(0)
        if self._has_active_processing() or self.segment_queue:
            self.statusLabel.setText("采集已结束，后台仍在处理剩余片段…")
        else:
            self.statusLabel.setText("实时捕获会话已结束")
        self._update_progress_indicator()
        self._refresh_buttons()

    def _write_segment_wav(self, segment_id: int, pcm_bytes: bytes) -> Path:
        assert self.session_dir is not None
        segment_dir = self.session_dir / "segments"
        segment_dir.mkdir(parents=True, exist_ok=True)
        wav_path = segment_dir / f"segment_{segment_id:03d}.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_bytes)
        return wav_path

    def _add_segment_item(self, job: SegmentJob) -> None:
        item = QListWidgetItem(self._format_job_line(job))
        self.segmentList.addItem(item)
        self.segmentItems_update(job, item)
        self._apply_item_style(job, item)

    def segmentItems_update(self, job: SegmentJob, item: QListWidgetItem) -> None:
        self.segment_items[job.segment_id] = item

    def _update_segment_item(self, job: SegmentJob) -> None:
        item = self.segment_items.get(job.segment_id)
        if item is not None:
            item.setText(self._format_job_line(job))
            self._apply_item_style(job, item)

    def _apply_item_style(self, job: SegmentJob, item: QListWidgetItem) -> None:
        if job.status == "已完成":
            item.setForeground(QBrush(QColor("#3FA266")))
            item.setBackground(QBrush(QColor(63, 162, 102, 30)))
        else:
            item.setForeground(QBrush(QColor("#E6E9EF")))
            item.setBackground(QBrush(Qt.GlobalColor.transparent))

    def _format_job_line(self, job: SegmentJob) -> str:
        start = self._format_time(job.global_start_sec)
        end = self._format_time(job.global_start_sec + job.duration_sec)
        return (
            f"#{job.segment_id:03d}  {start} -> {end}  |  时长 {job.duration_sec:.1f}s  |  状态: {job.status}"
        )

    @staticmethod
    def _format_time(seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds * 1000) % 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    def _start_next_job_if_idle(self) -> None:
        if self.processing_job is not None or not self.segment_queue:
            return

        job = self.segment_queue.popleft()
        self.processing_job = job
        job.status = "转录中"
        self._update_segment_item(job)
        self._update_queue_label()

        config = self._build_runtime_config()
        self.transcription_thread = TranscriptionThread([job.wav_path], config)
        self.transcription_thread.progress.connect(self._on_transcription_progress)
        self.transcription_thread.finished.connect(self._on_transcription_finished)
        self.transcription_thread.error.connect(self._on_transcription_error)
        self.transcription_thread.finished.connect(self.transcription_thread.deleteLater)
        self.transcription_thread.start()
        self.statusLabel.setText(f"正在处理片段 #{job.segment_id:03d}")
        self.current_task_progress = 0
        self._update_progress_indicator()

    def _on_transcription_progress(self, value: int, message: str) -> None:
        if self.processing_job is not None:
            self.current_task_progress = max(0, min(100, int(value)))
            self.deviceLabel.setText(f"片段 #{self.processing_job.segment_id:03d}：{message}")
            self._update_progress_indicator()

    def _on_transcription_finished(self, result: dict) -> None:
        thread = self.transcription_thread
        self.transcription_thread = None
        if thread and not thread.isRunning():
            pass
        if self.processing_job is None:
            return

        optimized = result.get("optimized") or []
        if not optimized:
            self.processing_job.status = "转录为空"
            self._update_segment_item(self.processing_job)
            self._finish_current_job()
            return

        offset = self.processing_job.global_start_sec
        offset_segments = []
        for seg in optimized:
            new_seg = dict(seg)
            new_seg["start"] = float(seg.get("start", 0.0)) + offset
            new_seg["end"] = float(seg.get("end", 0.0)) + offset
            offset_segments.append(new_seg)
        self.processing_job.optimized = offset_segments

        if cfg.workflow_translate.value:
            self._start_translation_for_current_job()
        else:
            self.processing_job.status = "待人工翻译"
            self._update_segment_item(self.processing_job)
            self.translateInterface.append_processed_subtitles(
                offset_segments,
                self.session_name,
                str(self.session_anchor_path) if self.session_anchor_path else None,
            )
            self._finish_current_job()

    def _on_transcription_error(self, message: str) -> None:
        self.transcription_thread = None
        if self.processing_job is not None:
            self.processing_job.status = f"转录失败: {message}"
            self._update_segment_item(self.processing_job)
        self.current_task_progress = 0
        InfoBar.error(
            title="片段转录失败",
            content=message,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000,
        )
        self._finish_current_job()

    def _start_translation_for_current_job(self) -> None:
        assert self.processing_job is not None and self.processing_job.optimized is not None
        glossary = ""
        try:
            glossary = load_glossary_text()
        except Exception:
            logging.exception("[RealtimeCaptureInterface] load glossary failed")

        self.processing_job.status = "翻译中"
        self._update_segment_item(self.processing_job)
        self.current_task_progress = 0
        self.translation_thread = TranslationThread(self.processing_job.optimized, glossary=glossary)
        self.translation_thread.progress_update.connect(self._on_translation_progress)
        self.translation_thread.finished_signal.connect(self._on_translation_finished)
        self.translation_thread.status_update.connect(self._on_translation_status)
        self.translation_thread.finished.connect(self.translation_thread.deleteLater)
        self.translation_thread.start()

    def _on_translation_status(self, message: str) -> None:
        if self.processing_job is not None:
            self.deviceLabel.setText(f"片段 #{self.processing_job.segment_id:03d}：{message}")

    def _on_translation_progress(self, current: int, total: int) -> None:
        if self.processing_job is not None and total > 0:
            self.current_task_progress = max(0, min(100, int(current * 100 / total)))
            self._update_progress_indicator()

    def _on_translation_finished(self, success: bool, message: str) -> None:
        self.translation_thread = None
        if self.processing_job is None or self.processing_job.optimized is None:
            return

        if not success and "终止" not in message and "取消" not in message:
            self.processing_job.status = "翻译失败，已回退原文"
            self._update_segment_item(self.processing_job)
        elif cfg.workflow_overflow_fix.value and self._segment_has_overflow(self.processing_job.optimized):
            self._start_overflow_fix_for_current_job()
            return

        self.processing_job.status = "已完成"
        self.current_task_progress = 100
        self._update_segment_item(self.processing_job)
        self.translateInterface.append_processed_subtitles(
            self.processing_job.optimized,
            self.session_name,
            str(self.session_anchor_path) if self.session_anchor_path else None,
        )
        self._finish_current_job()

    def _segment_has_overflow(self, subtitles: list[dict]) -> bool:
        threshold = cfg.max_line_count.value
        return any(len((sub.get("translated_text") or "")) > threshold for sub in subtitles)

    def _start_overflow_fix_for_current_job(self) -> None:
        assert self.processing_job is not None and self.processing_job.optimized is not None
        self.processing_job.status = "溢出修复中"
        self._update_segment_item(self.processing_job)
        self.current_task_progress = 0
        self.fix_thread = OverflowFixThread(self.processing_job.optimized, cfg.max_line_count.value)
        self.fix_thread.progress_update.connect(self._on_fix_progress)
        self.fix_thread.finished_signal.connect(self._on_fix_finished)
        self.fix_thread.finished.connect(self.fix_thread.deleteLater)
        self.fix_thread.start()

    def _on_fix_progress(self, current: int, total: int) -> None:
        if self.processing_job is not None and total > 0:
            self.current_task_progress = max(0, min(100, int(current * 100 / total)))
            self._update_progress_indicator()

    def _on_fix_finished(self, success: bool, message: str) -> None:
        if self.processing_job is None:
            self.fix_thread = None
            return

        if success and self.fix_thread and self.fix_thread.new_subtitles:
            self.processing_job.optimized = self.fix_thread.new_subtitles
        self.fix_thread = None
        self.processing_job.status = "已完成"
        self.current_task_progress = 100
        self._update_segment_item(self.processing_job)
        self.translateInterface.append_processed_subtitles(
            self.processing_job.optimized or [],
            self.session_name,
            str(self.session_anchor_path) if self.session_anchor_path else None,
        )
        self._finish_current_job()

    def _finish_current_job(self) -> None:
        self.processing_job = None
        self._update_queue_label()
        if self.segment_queue:
            self.current_task_progress = 0
            self._update_progress_indicator()
            QTimer.singleShot(0, self._start_next_job_if_idle)
            return

        if self.capture_worker is None and not self._session_running:
            self.statusLabel.setText("实时捕获会话已完成，字幕已并入翻译页。")
            self.deviceLabel.setText("可以前往“翻译+溢出修复”页继续检查与导出。")
            self.current_task_progress = 100 if self.segment_jobs else 0
        else:
            self.statusLabel.setText("正在实时捕获系统音频")
            self.current_task_progress = 0
        self._update_progress_indicator()
        self._refresh_buttons()

    def _update_queue_label(self) -> None:
        processing = 1 if self.processing_job is not None else 0
        self.queueLabel.setText(
            f"队列: {len(self.segment_queue)} 段待处理 | 当前处理中: {processing} 段 | 已提交总数: {len(self.segment_jobs)}"
        )

    def _update_progress_indicator(self) -> None:
        processing_active = self.processing_job is not None or any(
            thread is not None and thread.isRunning()
            for thread in (self.transcription_thread, self.translation_thread, self.fix_thread)
        )
        if processing_active:
            self.percentLabel.setText(f"片段进度 {self.current_task_progress}%")
            self.statusRing.show()
            self.statusRing.start()
        else:
            if self.current_task_progress > 0:
                self.percentLabel.setText(f"片段进度 {self.current_task_progress}%")
            else:
                self.percentLabel.setText("片段进度 --")
            self.statusRing.stop()
            self.statusRing.hide()

    def _has_active_processing(self) -> bool:
        return any(
            thread is not None and thread.isRunning()
            for thread in (self.transcription_thread, self.translation_thread, self.fix_thread)
        ) or self.processing_job is not None or bool(self.segment_queue)

    def _refresh_buttons(self) -> None:
        capturing = self.capture_worker is not None and self.capture_worker.isRunning()
        self.startBtn.setEnabled(not capturing and not self._session_running)
        self.commitBtn.setEnabled(capturing)
        self.stopBtn.setEnabled(capturing or self._session_running)
        self._update_progress_indicator()

    def stop_all(self) -> None:
        self._session_running = False
        if self.capture_worker is not None and self.capture_worker.isRunning():
            self.capture_worker.request_stop()
            self.capture_worker.wait(3000)
            self.capture_worker = None

        if self.transcription_thread is not None and self.transcription_thread.isRunning():
            self.transcription_thread.stop()
            self.transcription_thread.wait(3000)
            self.transcription_thread = None

        if self.translation_thread is not None and self.translation_thread.isRunning():
            self.translation_thread.stop()
            self.translation_thread.wait(3000)
            self.translation_thread = None

        if self.fix_thread is not None and self.fix_thread.isRunning():
            self.fix_thread.stop()
            self.fix_thread.wait(3000)
            self.fix_thread = None
