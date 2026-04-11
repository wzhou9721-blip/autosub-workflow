from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, QRectF, QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QMouseEvent, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SwitchButton,
    Theme,
    TransparentToolButton,
    setTheme,
    setThemeColor,
)

from .config import load_settings
from .language_options import LANGUAGE_OPTIONS, language_label

DUO_COLORS = {
    "primary": "#3FA266",
    "primary_pressed": "#368C58",
    "primary_soft": "#1E3326",
    "info": "#7AB8FF",
    "info_soft": "#162532",
    "warning": "#FFC800",
    "warning_soft": "#3B2D10",
    "danger": "#FF6B6B",
    "danger_soft": "#331919",
    "bg": "#121212",
    "surface": "#171717",
    "surface_alt": "#1A1A1A",
    "border": "#2E3238",
    "border_strong": "#505050",
    "text": "#D0D4DC",
    "text_secondary": "#9BA1AB",
    "text_on_primary": "#FFFFFF",
}

STATUS_META = {
    "idle": ("空闲", DUO_COLORS["surface_alt"], DUO_COLORS["text_secondary"]),
    "starting": ("启动中", DUO_COLORS["warning_soft"], DUO_COLORS["warning"]),
    "running": ("运行中", DUO_COLORS["danger_soft"], DUO_COLORS["danger"]),
    "stopping": ("停止中", DUO_COLORS["warning_soft"], DUO_COLORS["warning"]),
    "completed": ("已完成", DUO_COLORS["primary_soft"], "#72D18D"),
    "failed": ("失败", DUO_COLORS["danger_soft"], DUO_COLORS["danger"]),
}


class SessionWorker(QThread):
    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    finished_paths = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    partial_text = pyqtSignal(str)
    final_source = pyqtSignal(str, str)
    final_translation = pyqtSignal(str, str)
    audio_level = pyqtSignal(float)

    def __init__(self, runtime_options: Any) -> None:
        super().__init__()
        self.runtime_options = runtime_options
        self.stop_signal = threading.Event()

    def run(self) -> None:
        try:
            from .session_runner import SessionCallbacks, SessionRunner

            settings = load_settings()
            runner = SessionRunner(
                settings,
                SessionCallbacks(
                    on_log=self.log_message.emit,
                    on_status=self.status_changed.emit,
                    on_finished=lambda srt, json_path: self.finished_paths.emit(str(srt), str(json_path)),
                    on_error=self.failed.emit,
                    on_partial=self.partial_text.emit,
                    on_final_source=self.final_source.emit,
                    on_final_translation=self.final_translation.emit,
                    on_audio_level=self.audio_level.emit,
                ),
            )
            runner.run(self.stop_signal, self.runtime_options)
        except Exception as exc:
            self.failed.emit(str(exc))

    def request_stop(self) -> None:
        self.stop_signal.set()


class AudioVisualizer(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(14)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._target_level = 0.0
        self._display_level = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_level(self, level: float) -> None:
        self._target_level = max(0.0, min(1.0, float(level)))

    def _tick(self) -> None:
        self._display_level += (self._target_level - self._display_level) * 0.35
        self._display_level *= 0.96
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        rect = self.rect().adjusted(0, 2, 0, -2)
        bars = max(18, min(34, rect.width() // 22))
        gap = 8
        bar_width = max(10, int((rect.width() - gap * (bars - 1)) / bars))
        total_width = bars * bar_width + gap * (bars - 1)
        start_x = rect.x() + (rect.width() - total_width) / 2
        active_bars = int(round(self._display_level * bars))

        for index in range(bars):
            x = start_x + index * (bar_width + gap)
            y = rect.y() + (rect.height() - 5) / 2

            if index < active_bars:
                color = QColor(DUO_COLORS["primary"])
                color.setAlpha(235)
            else:
                color = QColor("#2B3038")
                color.setAlpha(230)

            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, y, bar_width, 5), 2.0, 2.0)


class SubtitleHistoryItem(QFrame):
    def __init__(self, utterance_id: str, zh_text: str, en_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.utterance_id = utterance_id
        self.setObjectName("subtitleHistoryItem")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        self.translationLabel = QLabel(zh_text or "翻译中……", self)
        self.translationLabel.setObjectName("historyTranslationLabel")
        self.translationLabel.setWordWrap(True)

        self.sourceLabel = QLabel(en_text or " ", self)
        self.sourceLabel.setObjectName("historySourceLabel")
        self.sourceLabel.setWordWrap(True)

        layout.addWidget(self.translationLabel)
        layout.addWidget(self.sourceLabel)
        self.setMinimumHeight(76)

    def update_source(self, text: str) -> None:
        self.sourceLabel.setText(text or " ")

    def update_translation(self, text: str) -> None:
        self.translationLabel.setText(text or " ")


class FloatingSubtitleWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.worker: SessionWorker | None = None
        self.last_srt_path = ""
        self.last_json_path = ""
        self.current_utterance_id = ""
        self.translation_cache: dict[str, str] = {}
        self.subtitle_items: dict[str, SubtitleHistoryItem] = {}
        self.placeholder_item: SubtitleHistoryItem | None = None
        self._drag_offset: QPoint | None = None
        self.collapsed_height = 252
        self.expanded_height = 454
        self._closing = False
        self._close_poll_attempts = 0

        self.setObjectName("floatingWindow")
        self.setWindowTitle("实时字幕助手")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(760, self.collapsed_height)
        self.setMinimumSize(700, self.collapsed_height)

        self._build_ui()
        self._apply_initial_values()
        self._update_secondary_language_enabled()
        self._refresh_buttons(running=False)
        self._set_status("idle")

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        self.rootFrame = QFrame(self)
        self.rootFrame.setObjectName("rootFrame")
        outer_layout.addWidget(self.rootFrame)

        root_layout = QVBoxLayout(self.rootFrame)
        root_layout.setContentsMargins(14, 12, 14, 12)
        root_layout.setSpacing(10)

        self.headerFrame = QFrame(self.rootFrame)
        header_layout = QHBoxLayout(self.headerFrame)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title = StrongBodyLabel("实时字幕悬浮窗", self.headerFrame)
        subtitle = CaptionLabel("Windows 系统音频 -> 英文转写 -> 中文字幕", self.headerFrame)
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)

        self.statusBadge = QLabel("空闲", self.headerFrame)
        self.statusBadge.setObjectName("statusBadge")
        self.statusBadge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusBadge.setFixedSize(88, 40)

        self.historyButton = PushButton("历史", self.headerFrame)
        self.historyButton.setToolTip("展开或收起已完成句子列表")
        self.historyButton.clicked.connect(self._toggle_history_panel)
        self.historyButton.setFixedSize(88, 40)
        self.historyButton.setStyleSheet("font-size: 13px; font-weight: 700;")

        self.settingsButton = TransparentToolButton(FIF.SETTING, self.headerFrame)
        self.settingsButton.setToolTip("展开或收起设置")
        self.settingsButton.clicked.connect(self._toggle_settings)

        self.closeButton = TransparentToolButton(FIF.CLOSE, self.headerFrame)
        self.closeButton.setToolTip("关闭窗口")
        self.closeButton.clicked.connect(self.close)

        header_layout.addLayout(title_layout, 1)
        header_layout.addWidget(self.statusBadge)
        header_layout.addWidget(self.historyButton)
        header_layout.addWidget(self.settingsButton)
        header_layout.addWidget(self.closeButton)
        root_layout.addWidget(self.headerFrame)

        self.visualizer = AudioVisualizer(self.rootFrame)
        root_layout.addWidget(self.visualizer)

        self.currentSubtitleCard = QFrame(self.rootFrame)
        self.currentSubtitleCard.setObjectName("currentSubtitleCard")
        self.currentSubtitleCard.setProperty("active", False)
        current_layout = QVBoxLayout(self.currentSubtitleCard)
        current_layout.setContentsMargins(16, 12, 16, 12)
        current_layout.setSpacing(0)

        self.currentSubtitleLabel = QLabel("准备就绪", self.currentSubtitleCard)
        self.currentSubtitleLabel.setObjectName("currentSubtitleLabel")
        self.currentSubtitleLabel.setWordWrap(True)
        current_layout.addWidget(self.currentSubtitleLabel)
        root_layout.addWidget(self.currentSubtitleCard)

        self.metaFrame = QFrame(self.rootFrame)
        meta_layout = QHBoxLayout(self.metaFrame)
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(0)

        self.sessionMetaLabel = CaptionLabel("每来一条新的 final 结果，这里会切到下一句继续听写。", self.metaFrame)
        self.sessionMetaLabel.setObjectName("sessionMetaLabel")
        self.sessionMetaLabel.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        meta_layout.addWidget(self.sessionMetaLabel, 1)
        root_layout.addWidget(self.metaFrame)

        self.historyPanel = QFrame(self.rootFrame)
        self.historyPanel.setObjectName("historyPanel")
        history_layout = QVBoxLayout(self.historyPanel)
        history_layout.setContentsMargins(10, 10, 10, 10)
        history_layout.setSpacing(8)

        self.historyTitleLabel = StrongBodyLabel("已完成句子", self.historyPanel)
        history_layout.addWidget(self.historyTitleLabel)

        self.subtitleScroll = QScrollArea(self.historyPanel)
        self.subtitleScroll.setObjectName("subtitleScroll")
        self.subtitleScroll.setWidgetResizable(True)
        self.subtitleScroll.setFrameShape(QFrame.Shape.NoFrame)
        self.subtitleScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.subtitleScroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.subtitleScroll.setMinimumHeight(170)
        self.subtitleScroll.setMaximumHeight(170)

        self.subtitleViewport = QWidget()
        self.subtitleViewport.setObjectName("subtitleViewport")
        self.subtitleScroll.setWidget(self.subtitleViewport)

        self.subtitleListLayout = QVBoxLayout(self.subtitleViewport)
        self.subtitleListLayout.setContentsMargins(0, 0, 4, 0)
        self.subtitleListLayout.setSpacing(8)
        self.subtitleListLayout.addStretch(1)
        history_layout.addWidget(self.subtitleScroll)
        self.historyPanel.hide()
        root_layout.addWidget(self.historyPanel)

        self.settingsPanel = QFrame(self.rootFrame)
        self.settingsPanel.setObjectName("settingsPanel")
        settings_layout = QGridLayout(self.settingsPanel)
        settings_layout.setContentsMargins(14, 12, 14, 12)
        settings_layout.setHorizontalSpacing(12)
        settings_layout.setVerticalSpacing(10)

        self.primaryLanguageCombo = ComboBox(self.settingsPanel)
        self.secondaryLanguageCombo = ComboBox(self.settingsPanel)
        self.translationFrequencySpin = QSpinBox(self.settingsPanel)
        for label, value in LANGUAGE_OPTIONS:
            self.primaryLanguageCombo.addItem(label, value)
            self.secondaryLanguageCombo.addItem(label, value)
        self.primaryLanguageCombo.setMaxVisibleItems(8)
        self.secondaryLanguageCombo.setMaxVisibleItems(8)
        self.translationFrequencySpin.setRange(1, 10)
        self.translationFrequencySpin.setValue(1)
        self.translationFrequencySpin.setSingleStep(1)

        self.dualLanguageSwitch = SwitchButton(self.settingsPanel)
        self.dualLanguageSwitch.checkedChanged.connect(self._update_secondary_language_enabled)
        self.denoiseSwitch = SwitchButton(self.settingsPanel)

        settings_layout.addWidget(CaptionLabel("主语言", self.settingsPanel), 0, 0)
        settings_layout.addWidget(self.primaryLanguageCombo, 0, 1)
        settings_layout.addWidget(CaptionLabel("双语言", self.settingsPanel), 0, 2)
        settings_layout.addWidget(self.dualLanguageSwitch, 0, 3)
        settings_layout.addWidget(CaptionLabel("第二语言", self.settingsPanel), 1, 0)
        settings_layout.addWidget(self.secondaryLanguageCombo, 1, 1)
        settings_layout.addWidget(CaptionLabel("翻译频率", self.settingsPanel), 1, 2)
        settings_layout.addWidget(self.translationFrequencySpin, 1, 3)
        settings_layout.addWidget(CaptionLabel("降噪增强", self.settingsPanel), 2, 0)
        settings_layout.addWidget(self.denoiseSwitch, 2, 1)
        root_layout.addWidget(self.settingsPanel)
        self.settingsPanel.hide()

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.startButton = PrimaryPushButton("开始", self.rootFrame)
        self.stopButton = PushButton("停止", self.rootFrame)
        self.openSrtButton = PushButton("打开字幕", self.rootFrame)
        self.openFolderButton = PushButton("打开目录", self.rootFrame)

        self.startButton.clicked.connect(self._start_session)
        self.stopButton.clicked.connect(self._stop_session)
        self.openSrtButton.clicked.connect(self._open_srt)
        self.openFolderButton.clicked.connect(self._open_output_folder)

        action_layout.addWidget(self.startButton)
        action_layout.addWidget(self.stopButton)
        action_layout.addStretch(1)
        action_layout.addWidget(self.openSrtButton)
        action_layout.addWidget(self.openFolderButton)
        root_layout.addLayout(action_layout)

    def _apply_initial_values(self) -> None:
        settings = load_settings()
        source_languages = settings.source_languages or [""]
        primary = source_languages[0]
        secondary = source_languages[1] if len(source_languages) > 1 else "zh"
        dual_enabled = len(source_languages) > 1

        self._set_combo_value(self.primaryLanguageCombo, primary)
        self._set_combo_value(self.secondaryLanguageCombo, secondary)
        self.dualLanguageSwitch.setChecked(dual_enabled)
        self.denoiseSwitch.setChecked(settings.audio_enhancer)
        self.translationFrequencySpin.setValue(max(1, settings.translation_frequency))

    def _runtime_options(self) -> Any:
        from .session_runner import RuntimeOptions

        return RuntimeOptions(
            primary_language=str(self.primaryLanguageCombo.currentData() or ""),
            dual_language_enabled=self.dualLanguageSwitch.isChecked(),
            secondary_language=str(self.secondaryLanguageCombo.currentData() or ""),
            denoise_enabled=self.denoiseSwitch.isChecked(),
            translation_frequency=int(self.translationFrequencySpin.value()),
        )

    def _set_combo_value(self, combo: ComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _toggle_settings(self) -> None:
        visible = not self.settingsPanel.isVisible()
        self.settingsPanel.setVisible(visible)
        self._update_window_height()

    def _toggle_history_panel(self) -> None:
        visible = not self.historyPanel.isVisible()
        self.historyPanel.setVisible(visible)
        self.historyButton.setText("收起" if visible else "历史")
        self._update_window_height()

    def _update_secondary_language_enabled(self, *_args: Any) -> None:
        self.secondaryLanguageCombo.setEnabled(self.dualLanguageSwitch.isChecked())

    def _start_session(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        runtime_options = self._runtime_options()
        self.translationCache_clear()
        self.current_utterance_id = ""
        self._clear_subtitle_history()
        self._set_status("starting")
        self._set_current_card_active(True)
        self.currentSubtitleLabel.setText("正在建立会话，准备开始转写……")
        self.sessionMetaLabel.setText(runtime_options.describe())

        self.worker = SessionWorker(runtime_options)
        self.worker.status_changed.connect(self._on_status_changed)
        self.worker.finished_paths.connect(self._on_finished_paths)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.partial_text.connect(self._on_partial_text)
        self.worker.final_source.connect(self._on_final_source)
        self.worker.final_translation.connect(self._on_final_translation)
        self.worker.audio_level.connect(self.visualizer.set_level)
        self.worker.start()

        self._refresh_buttons(running=True)

    def _stop_session(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        self._set_status("stopping")
        self._set_current_card_active(True)
        self.sessionMetaLabel.setText("正在停止并等待最后一句完成转写和翻译。")
        self.worker.request_stop()
        self.stopButton.setEnabled(False)

    def _on_status_changed(self, status: str) -> None:
        self._set_status(status)
        summaries = {
            "starting": "正在初始化音频捕获和云端会话。",
            "running": "正在听写中。",
            "stopping": "停止请求已发出，正在等待最后收尾。",
            "completed": "本次会话完成，字幕文件已经导出。",
        }
        if status in summaries:
            self.sessionMetaLabel.setText(summaries[status])
        if status == "running":
            self.currentSubtitleLabel.setText("正在听写…")
            self._set_current_card_active(True)
        if status in {"completed", "failed", "idle"}:
            self._set_current_card_active(False)

    def _on_partial_text(self, text: str) -> None:
        if not text:
            return
        self.currentSubtitleLabel.setText(text)
        self._set_current_card_active(True)

    def _on_final_source(self, utterance_id: str, text: str) -> None:
        self.current_utterance_id = utterance_id
        self._remove_placeholder_history()
        item = self.subtitle_items.get(utterance_id)
        if item is None:
            item = self._add_history_item(
                utterance_id,
                self.translation_cache.get(utterance_id, "翻译中……"),
                text,
            )
        else:
            item.update_source(text)
        self.currentSubtitleLabel.setText("正在听写…")
        self._set_current_card_active(True)

    def _on_final_translation(self, utterance_id: str, text: str) -> None:
        self.translation_cache[utterance_id] = text
        item = self.subtitle_items.get(utterance_id)
        if item is not None:
            item.update_translation(text)
            if utterance_id == self.current_utterance_id:
                self.sessionMetaLabel.setText("上一句已完成，等待下一句。")
                self._set_current_card_active(True)

    def _on_finished_paths(self, srt_path: str, json_path: str) -> None:
        self.last_srt_path = srt_path
        self.last_json_path = json_path
        self.sessionMetaLabel.setText(f"已导出字幕：{Path(srt_path).name}")
        self.currentSubtitleLabel.setText("本次会话已完成")
        self._set_current_card_active(False)
        self._refresh_buttons(running=False)

    def _on_failed(self, message: str) -> None:
        self._set_status("failed")
        self.sessionMetaLabel.setText("运行失败，请检查配置或把报错发给我。")
        self.currentSubtitleLabel.setText("运行失败")
        self._set_current_card_active(False)
        self._refresh_buttons(running=False)
        QMessageBox.critical(self, "运行失败", message)

    def _on_thread_finished(self) -> None:
        self.worker = None
        self._refresh_buttons(running=False)
        if self._closing:
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return
        if self.statusBadge.text() not in {"已完成", "失败", "空闲"}:
            self._set_status("idle")

    def _refresh_buttons(self, *, running: bool) -> None:
        self.startButton.setEnabled(not running)
        self.stopButton.setEnabled(running)
        self.primaryLanguageCombo.setEnabled(not running)
        self.secondaryLanguageCombo.setEnabled(not running and self.dualLanguageSwitch.isChecked())
        self.translationFrequencySpin.setEnabled(not running)
        self.dualLanguageSwitch.setEnabled(not running)
        self.denoiseSwitch.setEnabled(not running)
        self.openSrtButton.setEnabled(bool(self.last_srt_path))
        self.openFolderButton.setEnabled(bool(self.last_srt_path or self.last_json_path))

    def _set_status(self, status: str) -> None:
        text, bg, fg = STATUS_META.get(status, STATUS_META["idle"])
        self.statusBadge.setText(text)
        self.statusBadge.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border-radius: 10px; padding: 0px; font-weight: 700; font-size: 13px;"
        )

    def _open_srt(self) -> None:
        if self.last_srt_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.last_srt_path).resolve())))

    def _open_output_folder(self) -> None:
        path = self.last_srt_path or self.last_json_path
        if path:
            target = Path(path).resolve().parent
        else:
            target = load_settings().output_dir.resolve()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def translationCache_clear(self) -> None:
        self.translation_cache.clear()
        self.subtitle_items.clear()
        self.placeholder_item = None

    def _clear_subtitle_history(self) -> None:
        while self.subtitleListLayout.count() > 1:
            item = self.subtitleListLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.subtitle_items.clear()
        self.placeholder_item = None

    def _add_history_item(self, utterance_id: str, zh_text: str, en_text: str) -> SubtitleHistoryItem:
        item = SubtitleHistoryItem(utterance_id, zh_text, en_text, self.subtitleViewport)
        self.subtitleListLayout.insertWidget(self.subtitleListLayout.count() - 1, item)
        if utterance_id == "__placeholder__":
            self.placeholder_item = item
        else:
            self.subtitle_items[utterance_id] = item
        return item

    def _remove_placeholder_history(self) -> None:
        if self.placeholder_item is not None:
            self.subtitleListLayout.removeWidget(self.placeholder_item)
            self.placeholder_item.deleteLater()
            self.placeholder_item = None

    def _set_current_card_active(self, active: bool) -> None:
        self.currentSubtitleCard.setProperty("active", active)
        self.currentSubtitleCard.style().unpolish(self.currentSubtitleCard)
        self.currentSubtitleCard.style().polish(self.currentSubtitleCard)
        self.currentSubtitleCard.update()

    def _update_window_height(self) -> None:
        target_height = self.expanded_height if self.historyPanel.isVisible() else self.collapsed_height
        if self.settingsPanel.isVisible():
            target_height += 118
        self.resize(self.width(), target_height)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            header_rect = self.headerFrame.geometry()
            if header_rect.contains(event.position().toPoint()):
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
            if not self._closing:
                self._closing = True
                self._close_poll_attempts = 0
                self.hide()
                self.sessionMetaLabel.setText("正在退出，等待当前转写和翻译安全收尾。")
                self.worker.request_stop()
                QTimer.singleShot(100, self._poll_close_completion)
            return
        super().closeEvent(event)

    def _poll_close_completion(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return

        self._close_poll_attempts += 1
        if self._close_poll_attempts >= 50:
            self.worker.terminate()
            self.worker.wait(1000)
            app = QApplication.instance()
            if app is not None:
                app.quit()
            return

        QTimer.singleShot(100, self._poll_close_completion)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 18.0, 18.0)
        painter.fillPath(path, QColor(8, 8, 8, 1))


def _global_style() -> str:
    return f"""
QWidget {{
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    color: {DUO_COLORS['text_secondary']};
}}
QToolTip {{
    border: 1px solid {DUO_COLORS['border']};
    background-color: {DUO_COLORS['surface']};
    color: {DUO_COLORS['text']};
    padding: 4px 8px;
}}
#rootFrame {{
    background-color: {DUO_COLORS['bg']};
    border: none;
    border-radius: 18px;
}}
#currentSubtitleCard, #settingsPanel, #historyPanel, #subtitleHistoryItem {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 14px;
}}
#currentSubtitleCard[active='true'] {{
    background-color: #1B242D;
    border: 1px solid #3F6F55;
}}
#subtitleScroll, #subtitleViewport {{
    background-color: transparent;
    border: none;
}}
#currentSubtitleLabel {{
    color: {DUO_COLORS['text']};
    font-size: 24px;
    font-weight: 700;
    line-height: 1.25;
}}
#sessionMetaLabel {{
    color: {DUO_COLORS['text_secondary']};
    font-size: 12px;
}}
#historyTranslationLabel {{
    color: {DUO_COLORS['text']};
    font-size: 16px;
    font-weight: 700;
    line-height: 1.35;
}}
#historySourceLabel {{
    color: {DUO_COLORS['text_secondary']};
    font-size: 12px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 0 4px 0;
}}
QScrollBar::handle:vertical {{
    background: {DUO_COLORS['border']};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{
    background: {DUO_COLORS['border_strong']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}
TransparentToolButton {{
    border-radius: 10px;
    background-color: transparent;
}}
TransparentToolButton:hover {{
    background-color: {DUO_COLORS['surface_alt']};
}}
ComboBox {{
    background-color: {DUO_COLORS['surface_alt']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 8px;
    color: {DUO_COLORS['text']};
    padding: 4px 8px;
}}
ComboBox:focus, QSpinBox:focus {{
    border: 1px solid {DUO_COLORS['primary']};
}}
QSpinBox {{
    background-color: {DUO_COLORS['surface_alt']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 8px;
    color: {DUO_COLORS['text']};
    padding: 4px 8px;
}}
PushButton, PrimaryPushButton {{
    border-radius: 6px;
    font-weight: 600;
    padding: 6px 16px;
}}
PushButton {{
    background-color: #3A3A3A;
    border: 1px solid {DUO_COLORS['border_strong']};
    color: {DUO_COLORS['text']};
}}
PushButton:hover {{
    background-color: #454545;
    border-color: #606060;
}}
PushButton:pressed {{
    background-color: #2E2E2E;
    border-color: #404040;
}}
PrimaryPushButton {{
    background-color: {DUO_COLORS['primary']};
    border: 1px solid {DUO_COLORS['primary_pressed']};
    color: {DUO_COLORS['text_on_primary']};
}}
PrimaryPushButton:hover {{
    background-color: #56B379;
}}
PrimaryPushButton:pressed {{
    background-color: {DUO_COLORS['primary_pressed']};
    border-color: {DUO_COLORS['primary_pressed']};
}}
"""


def main() -> None:
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    setThemeColor(QColor(DUO_COLORS["primary"]))
    app.setStyleSheet(_global_style())

    window = FloatingSubtitleWindow()
    screen = app.primaryScreen()
    if screen is not None:
        geometry = screen.availableGeometry()
        window.move(geometry.right() - window.width() - 36, geometry.top() + 48)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
