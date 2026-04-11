from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import QUrl

from .config import load_settings
from .language_options import LANGUAGE_OPTIONS

if TYPE_CHECKING:
    from .session_runner import RuntimeOptions


STATUS_LABELS = {
    "starting": "启动中",
    "running": "运行中",
    "stopping": "停止中",
    "completed": "已完成",
}


class SessionWorker(QThread):
    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    finished_paths = pyqtSignal(str, str)
    failed = pyqtSignal(str)

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
                ),
            )
            runner.run(self.stop_signal, self.runtime_options)
        except Exception:
            # Errors are already sent through the callback path when possible.
            pass

    def request_stop(self) -> None:
        self.stop_signal.set()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.worker: SessionWorker | None = None
        self.last_srt_path = ""
        self.last_json_path = ""

        self.setWindowTitle("实时字幕助手")
        self.resize(920, 720)

        central = QWidget(self)
        self.setCentralWidget(central)

        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(16)

        root_layout.addWidget(self._build_settings_group())
        root_layout.addWidget(self._build_status_group())
        root_layout.addWidget(self._build_log_group(), 1)

        self._apply_initial_values()
        self._refresh_secondary_language_state()
        self._refresh_button_state(running=False)

    def _build_settings_group(self) -> QGroupBox:
        group = QGroupBox("识别选项", self)
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(12)

        self.primary_language_combo = QComboBox(group)
        self.secondary_language_combo = QComboBox(group)
        for label, code in LANGUAGE_OPTIONS:
            self.primary_language_combo.addItem(label, code)
            self.secondary_language_combo.addItem(label, code)

        self.dual_language_check = QCheckBox("开启双语言识别", group)
        self.denoise_check = QCheckBox("开启降噪增强", group)
        self.dual_language_check.toggled.connect(self._refresh_secondary_language_state)

        target_label = QLabel("翻译目标：简体中文", group)
        target_label.setStyleSheet("color: #666;")

        layout.addWidget(QLabel("主语言", group), 0, 0)
        layout.addWidget(self.primary_language_combo, 0, 1)
        layout.addWidget(QLabel("第二语言", group), 0, 2)
        layout.addWidget(self.secondary_language_combo, 0, 3)
        layout.addWidget(self.dual_language_check, 1, 0, 1, 2)
        layout.addWidget(self.denoise_check, 1, 2, 1, 2)
        layout.addWidget(target_label, 2, 0, 1, 4)

        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("控制台", self)
        layout = QVBoxLayout(group)
        layout.setSpacing(12)

        self.status_label = QLabel("空闲", group)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setStyleSheet("font-size: 16px; font-weight: 600;")

        button_row = QHBoxLayout()
        button_row.setSpacing(10)

        self.start_button = QPushButton("开始", group)
        self.stop_button = QPushButton("停止", group)
        self.open_srt_button = QPushButton("打开字幕", group)
        self.open_output_button = QPushButton("打开输出目录", group)

        self.start_button.clicked.connect(self._start_session)
        self.stop_button.clicked.connect(self._stop_session)
        self.open_srt_button.clicked.connect(self._open_srt)
        self.open_output_button.clicked.connect(self._open_output_folder)

        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.open_srt_button)
        button_row.addWidget(self.open_output_button)
        button_row.addStretch(1)

        layout.addWidget(self.status_label)
        layout.addLayout(button_row)
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("运行日志", self)
        layout = QVBoxLayout(group)
        self.log_view = QPlainTextEdit(group)
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view)
        return group

    def _apply_initial_values(self) -> None:
        settings = load_settings()
        primary = settings.source_languages[0] if settings.source_languages else ""
        secondary = settings.source_languages[1] if len(settings.source_languages) > 1 else "zh"
        dual_enabled = len(settings.source_languages) > 1

        self._set_combo_value(self.primary_language_combo, primary)
        self._set_combo_value(self.secondary_language_combo, secondary)
        self.dual_language_check.setChecked(dual_enabled)
        self.denoise_check.setChecked(settings.audio_enhancer)

    def _set_combo_value(self, combo: QComboBox, code: str) -> None:
        index = combo.findData(code)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _refresh_secondary_language_state(self) -> None:
        enabled = self.dual_language_check.isChecked()
        self.secondary_language_combo.setEnabled(enabled)

    def _runtime_options(self) -> Any:
        from .session_runner import RuntimeOptions

        return RuntimeOptions(
            primary_language=str(self.primary_language_combo.currentData() or ""),
            dual_language_enabled=self.dual_language_check.isChecked(),
            secondary_language=str(self.secondary_language_combo.currentData() or ""),
            denoise_enabled=self.denoise_check.isChecked(),
        )

    def _start_session(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return

        runtime_options = self._runtime_options()
        self.log_view.clear()
        self._append_log(f"[INFO] 已从界面发起启动：{runtime_options.describe()}")
        self.status_label.setText("启动中")

        self.worker = SessionWorker(runtime_options)
        self.worker.log_message.connect(self._append_log)
        self.worker.status_changed.connect(self._handle_status_change)
        self.worker.finished_paths.connect(self._handle_finished)
        self.worker.failed.connect(self._handle_failure)
        self.worker.finished.connect(self._handle_thread_finished)
        self.worker.start()
        self._refresh_button_state(running=True)

    def _stop_session(self) -> None:
        if self.worker is None or not self.worker.isRunning():
            return
        self._append_log("[INFO] 已从界面请求停止")
        self.status_label.setText("停止中")
        self.worker.request_stop()
        self.stop_button.setEnabled(False)

    def _handle_status_change(self, status: str) -> None:
        self.status_label.setText(STATUS_LABELS.get(status, status.title()))

    def _handle_finished(self, srt_path: str, json_path: str) -> None:
        self.last_srt_path = srt_path
        self.last_json_path = json_path
        self._append_log(f"[INFO] 已完成：{srt_path}")

    def _handle_failure(self, message: str) -> None:
        self.status_label.setText("失败")
        self._append_log(f"[ERROR] {message}")
        QMessageBox.critical(self, "运行失败", message)

    def _handle_thread_finished(self) -> None:
        self.worker = None
        self._refresh_button_state(running=False)
        if self.status_label.text() not in {"已完成", "空闲", "失败"}:
            self.status_label.setText("已停止")

    def _refresh_button_state(self, *, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.primary_language_combo.setEnabled(not running)
        self.secondary_language_combo.setEnabled(not running and self.dual_language_check.isChecked())
        self.dual_language_check.setEnabled(not running)
        self.denoise_check.setEnabled(not running)
        self.open_srt_button.setEnabled(bool(self.last_srt_path))
        self.open_output_button.setEnabled(True)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def _open_srt(self) -> None:
        if self.last_srt_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.last_srt_path))

    def _open_output_folder(self) -> None:
        path = self.last_srt_path or self.last_json_path
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve().parent)))
        else:
            settings = load_settings()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(settings.output_dir.resolve())))


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
