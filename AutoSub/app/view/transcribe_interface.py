# -*- coding: utf-8 -*-
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Union

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTime, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CommandBar,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    RoundMenu,
    TableView,
    TransparentDropDownPushButton,
    SegmentedWidget,
    IndeterminateProgressRing
)

from app.common.config import cfg
from app.common.thread import OptimizationThread, TranscriptionThread
from app.core.controller import task_controller
from app.core.project import project_manager

class SubtitleTableModel(QAbstractTableModel):
    def __init__(self, data: List[Dict[str, Any]] = None):
        super().__init__()
        self._data = data or []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if isinstance(self._data, list):
            return len(self._data)
        return 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 3

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not self._data or not isinstance(self._data, list):
            return None

        row = index.row()
        if row >= len(self._data):
            return None
            
        col = index.column()
        segment = self._data[row]

        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            if col == 0:
                return self._format_time(segment.get("start", 0))
            elif col == 1:
                return self._format_time(segment.get("end", 0))
            elif col == 2:
                # 优先显示 optimized_text (如果存在且不为空), 否则显示 text
                # 但根据新逻辑，data本身会被替换，所以这里统一读取 text 即可
                # 为了兼容性，我们还是优先读取 optimized_text，但通常这两个在 split 后是一样的
                return segment.get("optimized_text", segment.get("text", ""))
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col in [0, 1]:
                return Qt.AlignmentFlag.AlignCenter
        return None

    def _format_time(self, seconds: Optional[float]) -> str:
        if seconds is None:
            return "00:00:00.000"
        # Convert seconds to HH:mm:ss.zzz
        msecs = int(seconds * 1000)
        return QTime(0, 0).addMSecs(msecs).toString("hh:mm:ss.zzz")[:-1]

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal:
                headers = ["开始时间", "结束时间", "字幕内容"]
                if 0 <= section < len(headers):
                    return headers[section]
            elif orientation == Qt.Orientation.Vertical:
                return str(section + 1)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        # Allow editing text columns
        if index.column() == 2:
            return Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False

        row = index.row()
        col = index.column()
        
        if col == 2:
            self._data[row]["text"] = value
            # 同时更新 optimized_text 以保持一致
            self._data[row]["optimized_text"] = value
        else:
            return False
            
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        return True

    def update_all(self, data: List[Dict[str, Any]]):
        self.beginResetModel()
        if isinstance(data, list):
            self._data = data
        else:
            print(f"[SubtitleTableModel] Error: Received {type(data)} instead of list. Resetting to empty.")
            self._data = []
        self.endResetModel()

class TranscribeInterface(QWidget):
    transcription_finished = pyqtSignal(list, str, str) # Signals completion to MainWindow (data, filename, original_path)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("transcribe_interface")
        self.thread = None
        self.work_thread = None
        self._task_running = False
        self._user_cancelled = False
        self._stop_pending = False
        self._transcribe_run_id = 0  # 每次开始新转录自增，用于忽略过期批次的 raw_ready/finished/error
        self._init_ui()
        
    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setSpacing(10)
        self.main_layout.setContentsMargins(20, 20, 20, 20)

        # 1. Command Bar
        self._setup_command_bar()

        # 2. Subtitle Table
        self._setup_table()
        
        # 3. Bottom Status/Progress
        self._setup_bottom_bar()

    def _setup_command_bar(self):
        self.command_bar = CommandBar(self)
        # self.command_bar.setFrameVisible(False)  # 移除不支持的方法
        
        # Start Task Action
        self.start_action = Action(FIF.PLAY, "开始任务", triggered=self._on_start_task)
        self.command_bar.addAction(self.start_action)
        
        # Stop Task Action
        self.stop_action = Action(FIF.PAUSE, "终止任务", triggered=self._on_stop_task)
        self.stop_action.setEnabled(False)
        self.command_bar.addAction(self.stop_action)
        
        # 切换按钮 (原始/优化)
        self.view_switch = SegmentedWidget(self)
        self.view_switch.addItem("raw", "原始字幕")
        self.view_switch.addItem("optimized", "优化字幕")
        self.view_switch.setCurrentItem("optimized")
        self.view_switch.currentItemChanged.connect(self._on_view_changed)
        self.view_switch.setEnabled(False) # 初始禁用，直到有数据
        
        self.command_bar.addWidget(self.view_switch)
        self.command_bar.addSeparator()
        
        # Save Action
        self.save_menu = RoundMenu(parent=self)
        self.save_menu.addAction(Action("导出为 SRT", triggered=lambda: self._on_save("srt")))
        self.save_menu.addAction(Action("导出为 VTT", triggered=lambda: self._on_save("vtt")))
        self.save_menu.addAction(Action("导出为 ASS", triggered=lambda: self._on_save("ass")))
        self.save_menu.addAction(Action("导出为 TXT", triggered=lambda: self._on_save("txt")))
        self.save_menu.addSeparator()
        self.save_menu.addAction(Action("按说话人分离导出", triggered=self._on_save_by_speaker))
        
        self.save_btn = TransparentDropDownPushButton("导出字幕", self, FIF.SAVE)
        self.save_btn.setMenu(self.save_menu)
        self.command_bar.addWidget(self.save_btn)
        
        self.main_layout.addWidget(self.command_bar)

    def _setup_table(self):
        self.table = TableView(self)
        self.model = SubtitleTableModel([])
        self.table.setModel(self.model)
        
        # Styling
        self.table.setBorderVisible(False)
        self.table.setBorderRadius(0)
        self.table.setWordWrap(True)
        
        # Headers
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 120)
        
        self.main_layout.addWidget(self.table)

    def _set_task_running(self, running: bool):
        self._task_running = running
        self.start_action.setEnabled(not running)
        self.stop_action.setEnabled(running)
        self.stop_transcribe_btn.setEnabled(running)
        self.save_btn.setEnabled(not running)

    def _safe_stop_thread(self, thread_attr: str):
        thread = getattr(self, thread_attr, None)
        if not thread:
            return False
        requested = False
        try:
            if thread.isRunning():
                thread.stop()
                requested = True
        except Exception:
            logging.exception("[TranscribeInterface] 停止线程失败: %s", thread_attr)
        finally:
            # 无论底层线程是否已经完全退出，界面层都立即清理引用，
            # 避免因为 thread.isRunning() 仍为 True 而阻塞下一次任务启动。
            # 线程自身会通过 _is_running 标志和 task_scope 取消逻辑尽快安全退出。
            setattr(self, thread_attr, None)
        return requested

    def _setup_bottom_bar(self):
        self.bottom_layout = QHBoxLayout()
        
        self.status_label = BodyLabel("请加载音频/视频文件或字幕文件", self)
        self.status_label.setMinimumWidth(120)
        
        # Progress Ring (旋转动画)
        self.progress_ring = IndeterminateProgressRing(self)
        self.progress_ring.setFixedSize(20, 20)
        self.progress_ring.hide()
        
        # Progress Bar（给最小宽度避免被挤没）
        self.progress_bar = ProgressBar(self)
        self.progress_bar.setMinimumWidth(200)
        self.progress_bar.hide()
        
        # 终止转录：任务运行时可见，便于用户快速终止后处理新任务
        self.stop_transcribe_btn = PushButton(FIF.PAUSE, "终止转录", self)
        self.stop_transcribe_btn.setEnabled(False)
        self.stop_transcribe_btn.clicked.connect(self._on_stop_task)
        
        self.bottom_layout.addWidget(self.status_label)
        self.bottom_layout.addStretch(1)
        self.bottom_layout.addWidget(self.progress_ring)
        self.bottom_layout.addSpacing(8)
        self.bottom_layout.addWidget(self.progress_bar)
        self.bottom_layout.addSpacing(12)
        self.bottom_layout.addWidget(self.stop_transcribe_btn)
        
        self.main_layout.addLayout(self.bottom_layout)
        
        # 数据存储
        self.raw_data = []
        self.optimized_data = []

    def update_data(self, data: Union[List, Dict]):
        """ 外部调用用于更新数据 (例如从 SettingTaskInterface) """
        if isinstance(data, dict):
            self.raw_data = data.get("raw", [])
            self.optimized_data = data.get("optimized", [])
            # 默认显示 optimized
            target_data = self.optimized_data
            
            self.view_switch.setEnabled(True)
            self.view_switch.setCurrentItem("optimized")
            
            self.status_label.setText("任务处理完成")
            
        elif isinstance(data, list):
            self.raw_data = data
            self.optimized_data = data
            target_data = data
            self.view_switch.setEnabled(True)
            self.status_label.setText("任务处理完成")
            
        else:
            target_data = []
            
        self.model.update_all(target_data)

    def start_transcription(self, files, config=None):
        """ Start new transcription task (Called from SettingTaskInterface) """
        self.raw_data = []
        self.optimized_data = []
        self.model.update_all([])
        self._user_cancelled = False
        self._stop_pending = False
        
        # Store file_path for filename reference (Use first file)
        self.current_file_path = files[0] if files else ""
        
        self.progress_bar.show()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.progress_ring.show()
        self.progress_ring.start()

        self.status_label.setText("正在转录中...")
        self._set_task_running(True)

        # Start thread；用 run_id 区分当前批次，避免终止后旧线程晚到的结果覆盖新任务
        self._transcribe_run_id += 1
        run_id = self._transcribe_run_id
        self.thread = TranscriptionThread(files, config)
        self.thread.raw_ready.connect(lambda segs, r=run_id: self._on_raw_transcription_ready(segs, r))
        self.thread.finished.connect(lambda result, r=run_id: self._on_transcription_done(result, r))
        self.thread.error.connect(lambda msg, r=run_id: self._on_task_error(msg, r))
        self.thread.progress.connect(self._on_task_progress)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def _on_raw_transcription_ready(self, raw_segments, run_id=None):
        """语音识别完成后立即保存原始结果，优化/断句失败时可直接重新优化而无需再次转录"""
        if run_id is not None and run_id != self._transcribe_run_id:
            return
        if not self._user_cancelled and raw_segments:
            self.raw_data = [s.copy() for s in raw_segments]
            self.model.update_all(self.raw_data)
            self.view_switch.setEnabled(True)
            self.view_switch.setCurrentItem("raw")
            self.status_label.setText("转录文本已保存，正在优化与断句…")

    def _on_transcription_done(self, result, run_id=None):
        if run_id is not None and run_id != self._transcribe_run_id:
            return
        t = self.thread
        self.thread = None
        if t and not t.isRunning():
            pass  # deleteLater 已由 finished 信号触发

        if self._user_cancelled:
            show_cancelled = self._stop_pending
            self._stop_pending = False
            self._user_cancelled = False
            self.progress_bar.hide()
            self.progress_ring.stop()
            self.progress_ring.hide()
            self._set_task_running(False)
            self.status_label.setText("转录任务已终止")
            if show_cancelled:
                InfoBar.warning(
                    title="任务终止",
                    content="当前任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
            return

        self.raw_data = result.get("raw", [])
        self.optimized_data = result.get("optimized", [])
        self._last_phase_timings = result.get("phase_timings") or {}

        self.progress_bar.hide()
        self.progress_ring.stop()
        self.progress_ring.hide()

        self._set_task_running(False)

        if self.optimized_data:
            self.model.update_all(self.optimized_data)
            self.view_switch.setEnabled(True)
            self.view_switch.setCurrentItem("optimized")
            self.status_label.setText("转录完成")
            timing_parts = []
            if self._last_phase_timings.get("transcribe_sec") is not None:
                timing_parts.append(f"转录用时 {self._last_phase_timings['transcribe_sec']} 秒")
            if self._last_phase_timings.get("optimize_split_sec") is not None:
                timing_parts.append(f"优化+断句用时 {self._last_phase_timings['optimize_split_sec']} 秒")
            timing_str = "，".join(timing_parts) if timing_parts else ""
            if timing_str:
                print(f"[Transcribe] 环节用时: {timing_str}")
            InfoBar.success(
                title="成功",
                content="转录及优化任务已完成" + (f"（{timing_str}）" if timing_str else ""),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

            # Emit signal to notify completion
            file_path = self.current_file_path if hasattr(self, 'current_file_path') else ""
            filename = os.path.basename(file_path) if file_path else "unknown"
            self.transcription_finished.emit(self.optimized_data, filename, file_path)
            
        else:
            self.status_label.setText("未生成有效的字幕结果")
            InfoBar.warning(
                title="提示",
                content="未生成有效的字幕结果",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _on_load_file(self):
        fname, _ = QFileDialog.getOpenFileName(
            self,
            "选择文件",
            "",
            "字幕文件 (*.srt *.txt);;媒体文件 (*.mp3 *.wav *.mp4 *.mkv)"
        )
        if not fname:
            return

        ext = os.path.splitext(fname)[1].lower()
        self.current_file_path = fname

        if ext in [".mp3", ".wav", ".mp4", ".mkv"]:
            self.status_label.setText(f"已选择媒体文件: {os.path.basename(fname)}")
            InfoBar.info(
                title="已选择媒体文件",
                content="请返回「设置项目任务」页面点击“开始处理”执行完整流程",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000
            )
            return

        try:
            if ext == ".srt":
                parsed = self._parse_srt_segments(fname)
            elif ext == ".txt":
                parsed = self._parse_txt_segments(fname)
            else:
                InfoBar.warning(
                    title="不支持的文件类型",
                    content="仅支持导入 .srt / .txt 字幕文件",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
                return

            if not parsed:
                InfoBar.warning(
                    title="导入失败",
                    content="文件中未解析到有效字幕内容",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
                return

            self.raw_data = parsed
            self.optimized_data = [dict(seg) for seg in parsed]
            self.model.update_all(self.optimized_data)
            self.view_switch.setEnabled(True)
            self.view_switch.setCurrentItem("optimized")
            self.save_btn.setEnabled(True)
            self.status_label.setText(f"已加载字幕: {os.path.basename(fname)}")
            InfoBar.success(
                title="导入成功",
                content=f"已导入 {len(parsed)} 条字幕",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
        except Exception as e:
            logging.exception("[TranscribeInterface] 加载字幕文件失败: %s", fname)
            InfoBar.error(
                title="导入失败",
                content=f"读取字幕文件出错: {e}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _parse_srt_segments(self, file_path: str) -> List[Dict[str, Any]]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()

        blocks = re.split(r"\n\s*\n", content)
        segments: List[Dict[str, Any]] = []
        for idx, block in enumerate(blocks, start=1):
            lines = [line.strip("\ufeff ") for line in block.splitlines() if line.strip()]
            if len(lines) < 2:
                continue

            if "-->" in lines[0]:
                time_line = lines[0]
                text_lines = lines[1:]
            elif len(lines) >= 3 and "-->" in lines[1]:
                time_line = lines[1]
                text_lines = lines[2:]
            else:
                continue

            start_str, end_str = [p.strip() for p in time_line.split("-->", 1)]
            start = self._parse_timestamp_to_seconds(start_str)
            end = self._parse_timestamp_to_seconds(end_str)
            text = "\n".join(text_lines).strip()
            if not text:
                continue

            if end < start:
                end = start

            segments.append({
                "index": idx,
                "start": start,
                "end": end,
                "text": text,
                "optimized_text": text
            })

        return segments

    def _parse_txt_segments(self, file_path: str) -> List[Dict[str, Any]]:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        segments: List[Dict[str, Any]] = []
        for idx, line in enumerate(lines, start=1):
            start = float(idx - 1)
            end = float(idx)
            segments.append({
                "index": idx,
                "start": start,
                "end": end,
                "text": line,
                "optimized_text": line
            })

        return segments

    @staticmethod
    def _parse_timestamp_to_seconds(ts: str) -> float:
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(ts)

    def _on_save(self, fmt):
        """ 导出字幕 """
        # 1. 根据当前视图选择数据源
        # Use tracked view key instead of currentItem() object
        current_key = getattr(self, "current_view_key", "optimized")
        if current_key == "raw":
            data_to_save = self.raw_data
            mode_name = "原始字幕"
        else:
            data_to_save = self.optimized_data
            mode_name = "优化字幕"

        if not data_to_save:
            InfoBar.warning(
                title="导出失败",
                content=f"当前{mode_name}为空，无法导出",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        # 2. 选择保存路径
        ext_map = {"srt": ".srt", "vtt": ".vtt", "ass": ".ass", "txt": ".txt"}
        filter_map = {
            "srt": "SRT Subtitle (*.srt)",
            "vtt": "WebVTT Subtitle (*.vtt)",
            "ass": "ASS Subtitle (*.ass)",
            "txt": "Text File (*.txt)"
        }
        default_ext = ext_map.get(fmt, ".txt")
        filters = filter_map.get(fmt, "All Files (*.*)")

        file_path, _ = QFileDialog.getSaveFileName(
            self, 
            f"导出 {mode_name}", 
            f"output{default_ext}", 
            filters
        )
        
        if not file_path:
            return
            
        # 3. 写入文件
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                def get_text(item):
                    if current_key == "raw":
                        return item.get("text", "")
                    return item.get("optimized_text", item.get("text", ""))

                if fmt == "srt":
                    for i, item in enumerate(data_to_save):
                        start = self._format_srt_time(item.get("start", 0))
                        end = self._format_srt_time(item.get("end", 0))
                        f.write(f"{i+1}\n{start} --> {end}\n{get_text(item)}\n\n")

                elif fmt == "vtt":
                    f.write("WEBVTT\n\n")
                    for i, item in enumerate(data_to_save):
                        start = self._format_vtt_time(item.get("start", 0))
                        end = self._format_vtt_time(item.get("end", 0))
                        f.write(f"{i+1}\n{start} --> {end}\n{get_text(item)}\n\n")

                elif fmt == "ass":
                    f.write(self._build_ass_header())
                    for item in data_to_save:
                        start = self._format_ass_time(item.get("start", 0))
                        end = self._format_ass_time(item.get("end", 0))
                        text = get_text(item).replace("\n", "\\N")
                        f.write(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n")

                else:  # txt
                    for item in data_to_save:
                        f.write(f"{get_text(item)}\n")
                        
            InfoBar.success(
                title="导出成功",
                content=f"已导出{mode_name}到 {file_path}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
        except Exception as e:
            InfoBar.error(
                title="导出失败",
                content=f"保存文件时出错: {str(e)}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _on_save_by_speaker(self):
        """按说话人分离导出：每个说话人 3 个 SRT + 全员新闻稿 TXT。"""
        data = self.optimized_data or self.raw_data
        if not data:
            InfoBar.warning(title="导出失败", content="当前没有字幕数据",
                            parent=self, position=InfoBarPosition.BOTTOM_RIGHT)
            return

        has_speaker = any("speaker" in seg for seg in data)
        if not has_speaker:
            InfoBar.warning(
                title="无说话人数据",
                content="当前字幕不含说话人分离信息，请在全局设置中开启 Gladia 说话人分离后重新转录",
                parent=self, position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        # 选择导出目录
        export_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not export_dir:
            return

        file_path = self.current_file_path if hasattr(self, "current_file_path") else ""
        base_name = os.path.splitext(os.path.basename(file_path))[0] if file_path else "output"
        has_trans = any(seg.get("translated_text") for seg in data)

        try:
            from collections import defaultdict
            speaker_segs = defaultdict(list)
            for seg in data:
                speaker_segs[seg.get("speaker", "unknown")].append(seg)

            def _srt_time(sec):
                return self._format_srt_time(sec or 0)

            def _write_srt(path, segs, mode):
                with open(path, "w", encoding="utf-8") as f:
                    for i, seg in enumerate(segs):
                        s, e = _srt_time(seg.get("start", 0)), _srt_time(seg.get("end", 0))
                        orig = seg.get("optimized_text") or seg.get("text", "")
                        cn = seg.get("translated_text") or ""
                        if mode == "orig_only":
                            text = orig
                        elif mode == "trans_only":
                            text = cn
                        elif mode == "orig_first":
                            text = f"{orig}\n{cn}" if cn and orig else (orig or cn)
                        else:  # trans_first (双语)
                            text = f"{cn}\n{orig}" if cn and orig else (cn or orig)
                        if text:
                            f.write(f"{i+1}\n{s} --> {e}\n{text}\n\n")

            exported = []
            for spk, spk_segs in sorted(speaker_segs.items()):
                _write_srt(os.path.join(export_dir, f"{base_name}_{spk}_仅原文.srt"),
                           spk_segs, "orig_only")
                exported.append(f"{spk}_仅原文.srt")
                if has_trans:
                    _write_srt(os.path.join(export_dir, f"{base_name}_{spk}_仅译文.srt"),
                               spk_segs, "trans_only")
                    _write_srt(os.path.join(export_dir, f"{base_name}_{spk}_双语_译文在上.srt"),
                               spk_segs, "trans_first")
                    _write_srt(os.path.join(export_dir, f"{base_name}_{spk}_双语_原文在上.srt"),
                               spk_segs, "orig_first")

            # 新闻稿 TXT
            news_path = os.path.join(export_dir, f"{base_name}_新闻稿.txt")
            with open(news_path, "w", encoding="utf-8") as f:
                for seg in data:
                    ts = _srt_time(seg.get("start", 0))[:8]
                    spk = seg.get("speaker", "Speaker")
                    orig = seg.get("optimized_text") or seg.get("text", "")
                    f.write(f"[{ts}] [{spk}] {orig}\n")
            exported.append("新闻稿.txt")

            InfoBar.success(
                title="导出成功",
                content=f"已导出 {len(speaker_segs)} 位说话人的字幕文件到：{export_dir}",
                parent=self, position=InfoBarPosition.BOTTOM_RIGHT, duration=5000
            )
        except Exception as ex:
            InfoBar.error(title="导出失败", content=str(ex),
                          parent=self, position=InfoBarPosition.BOTTOM_RIGHT)

    def _format_srt_time(self, seconds: float) -> str:
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); ms = int((seconds * 1000) % 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _format_vtt_time(self, seconds: float) -> str:
        """ WebVTT 时间格式 HH:MM:SS.mmm """
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); ms = int((seconds * 1000) % 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    def _format_ass_time(self, seconds: float) -> str:
        """ ASS 时间格式 H:MM:SS.cs（厘秒） """
        if seconds is None: seconds = 0
        h = int(seconds // 3600); m = int((seconds % 3600) // 60)
        s = int(seconds % 60); cs = int((seconds * 100) % 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def _build_ass_header(self) -> str:
        return (
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

    def _on_start_task(self):
        """ 仅重新运行优化和断句流程 (基于现有原始字幕) """
        if not self.raw_data:
            InfoBar.warning(
                title="提示",
                content="没有可优化的原始字幕，请先在'设置任务'界面开始新的转录任务",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        # 获取最新配置
        config_data = {
            "transcribeMode": cfg.transcribeMode.value,
            "asrModel": cfg.asrModel.value,
            "sourceLanguage": cfg.sourceLanguage.value,
            "targetLanguage": cfg.targetLanguage.value,
            "videoContext": cfg.videoContext.value,
            "wordLevelTimestamps": cfg.wordLevelTimestamps.value
        }

        self.progress_bar.show()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_ring.show()
        self.progress_ring.start()
        self.status_label.setText("正在初始化优化任务...")
        self._set_task_running(True)

        # 启动优化线程 (只做优化+断句)
        self._user_cancelled = False
        self._stop_pending = False
        self.work_thread = OptimizationThread(self.raw_data, config_data)
        self.work_thread.progress.connect(self._on_task_progress)
        self.work_thread.finished.connect(self._on_task_finished)
        self.work_thread.error.connect(self._on_task_error)
        self.work_thread.finished.connect(self.work_thread.deleteLater)
        self.work_thread.start()

    def _on_task_progress(self, val, msg):
        self.progress_bar.setValue(val)
        self.status_label.setText(msg)

    def _on_task_finished(self, results):
        t = self.work_thread
        self.work_thread = None
        if t and not t.isRunning():
            pass  # deleteLater 已由 finished 信号触发

        if self._user_cancelled:
            show_cancelled = self._stop_pending
            self._stop_pending = False
            self._user_cancelled = False
            self.progress_bar.hide()
            self.progress_ring.stop()
            self.progress_ring.hide()
            self._set_task_running(False)
            self.status_label.setText("优化任务已终止")
            if show_cancelled:
                InfoBar.warning(
                    title="任务终止",
                    content="当前任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
            return

        self.progress_bar.hide()
        self.progress_ring.stop()
        self.progress_ring.hide()
        self._set_task_running(False)

        if results:
            self._last_phase_timings = results.get("phase_timings") or {}
            self.update_data(results)

            self.status_label.setText("优化重新处理完成！")
            timing_str = ""
            if self._last_phase_timings.get("optimize_split_sec") is not None:
                timing_str = f"（优化+断句用时 {self._last_phase_timings['optimize_split_sec']} 秒）"
                print(f"[Transcribe] 环节用时: 优化+断句 {self._last_phase_timings['optimize_split_sec']} 秒")
            InfoBar.success(
                title="成功",
                content="字幕已根据最新设置重新优化完成" + timing_str,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

            # Emit signal to notify completion (passing optimized data and a placeholder filename)
            # In a real scenario, we might want to track the original filename source
            file_path = self.current_file_path if hasattr(self, 'current_file_path') else ""
            filename = os.path.basename(file_path) if file_path else "current_task"
            self.transcription_finished.emit(self.optimized_data, filename, file_path)
            
        else:
            self.status_label.setText("未生成有效的字幕结果")
            InfoBar.warning(
                title="提示",
                content="未生成有效的字幕结果",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _on_view_changed(self, key):
        """ 切换显示原始/优化字幕 """
        self.current_view_key = key
        if key == "raw":
            self.model.update_all(self.raw_data)
            InfoBar.info(
                title="切换视图",
                content="当前显示：原始转录字幕",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=1500
            )
        else:
            self.model.update_all(self.optimized_data)
            InfoBar.info(
                title="切换视图",
                content="当前显示：AI优化字幕",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=1500
            )

    def _on_stop_task(self):
        """ 终止当前任务 """
        self._user_cancelled = True
        self._stop_pending = False
        requested = False

        if self._safe_stop_thread("thread"):
            self.status_label.setText("转录任务终止中...")
            requested = True

        if self._safe_stop_thread("work_thread"):
            self.status_label.setText("优化任务终止中...")
            requested = True

        if requested:
            self._stop_pending = True
            self._set_task_running(False)
            InfoBar.info(
                title="任务终止中",
                content="已发送终止请求，正在等待后台任务收尾",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
        else:
            self._user_cancelled = False
            self._set_task_running(False)

    def _on_task_error(self, err_msg, run_id=None):
        if run_id is not None and run_id != self._transcribe_run_id:
            return
        self.thread = None
        self.work_thread = None

        # 用户主动取消时，将取消态单独提示，避免被错误态覆盖
        if getattr(self, '_user_cancelled', False) and ("取消" in err_msg or "终止" in err_msg):
            show_cancelled = self._stop_pending
            self._user_cancelled = False
            self._stop_pending = False
            self.progress_bar.hide()
            self.progress_ring.stop()
            self.progress_ring.hide()
            self._set_task_running(False)
            self.status_label.setText("任务已终止")
            if show_cancelled:
                InfoBar.warning(
                    title="任务终止",
                    content="当前任务已终止",
                    parent=self,
                    position=InfoBarPosition.BOTTOM_RIGHT
                )
            return

        self._user_cancelled = False
        self._stop_pending = False
        self.progress_bar.hide()
        self.progress_ring.stop()
        self.progress_ring.hide()
        self.status_label.setText(f"处理失败: {err_msg}")
        self._set_task_running(False)
        InfoBar.error(
            title="处理出错",
            content=err_msg,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=-1
        )
