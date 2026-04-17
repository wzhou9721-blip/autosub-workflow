import logging
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QCloseEvent
from qfluentwidgets import FluentWindow, NavigationItemPosition, FluentIcon as FIF, qconfig
from app.view.home_interface import HomeInterface
from app.view.setting_task_interface import SettingTaskInterface
from app.view.setting_interface import SettingInterface
from app.view.realtime_capture_interface import RealtimeCaptureInterface
from app.view.transcribe_interface import TranscribeInterface
from app.view.translation_interface import TranslationInterface

from app.common.config import cfg

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        
        # Clear Video Context on startup as requested
        cfg.videoContext.value = ""
        
        self.initWindow()

        # Create sub interfaces
        # 1. 设置任务 (Setting Task)
        self.taskInterface = SettingTaskInterface(self)
        self.taskInterface.start_transcription_signal.connect(self._on_start_transcription)
        
        # 2. 转录+初始优化 (Transcribe + Initial Optimization)
        self.transcribeInterface = TranscribeInterface(self)
        # 3. 翻译+溢出修复 (Translate + Overflow Fix)
        self.translateInterface = TranslationInterface(self)
        # 4. 实时捕获
        self.realtimeInterface = RealtimeCaptureInterface(self.translateInterface, self.taskInterface, self)
        self.realtimeInterface.open_translation_requested.connect(lambda: self.switchTo(self.translateInterface))
        
        # Settings Interface (Bottom)
        self.settingInterface = SettingInterface(self)

        # Initialize navigation
        self.initNavigation()

    def initNavigation(self):
        # 1. 设置任务 - 使用 EDIT 图标 (代表新建/编辑任务)
        self.addSubInterface(self.taskInterface, FIF.EDIT, '设置项目任务')
        
        # 2. 实时捕获
        self.addSubInterface(self.realtimeInterface, FIF.VIDEO, '实时捕获')

        # 3. 转录+初始优化 - 使用 MICROPHONE 图标 (代表语音识别)
        self.addSubInterface(self.transcribeInterface, FIF.MICROPHONE, '转录+初始优化')
        
        # 4. 翻译+溢出修复 - 使用 LANGUAGE 图标 (代表翻译)
        self.addSubInterface(self.translateInterface, FIF.LANGUAGE, '翻译+溢出修复')

        # self.navigationInterface.addSeparator()  # 去掉导航分隔线

        # 4. 设置 - 底部保留
        self.addSubInterface(self.settingInterface, FIF.SETTING, '设置', NavigationItemPosition.BOTTOM)
        
        # --- Signal Connections for Automated Workflow ---
        # When TranscribeInterface finishes (or auto-triggers), pass data to TranslationInterface and switch
        self.transcribeInterface.transcription_finished.connect(self._on_transcription_finished)

    def _on_start_transcription(self, files, config):
        """ Handle start signal from SettingTaskInterface """
        # Switch to TranscribeInterface
        self.switchTo(self.transcribeInterface)
        # Start the task
        self.transcribeInterface.start_transcription(files, config)

    def _on_transcription_finished(self, data, filename, original_path=None):
        """ 转录+优化断句完成后：始终把字幕传给翻译界面并切过去；是否自动开始翻译由「自动翻译」配置决定 """
        auto_start = cfg.workflow_translate.value
        phase_timings = getattr(self.transcribeInterface, "_last_phase_timings", None)
        # 1. 始终把数据传给翻译界面（含环节用时，供质量报告打印）
        self.translateInterface.set_data_from_transcription(
            data, filename, original_path, auto_start=auto_start, phase_timings=phase_timings
        )
        # 2. 切到翻译界面
        self.switchTo(self.translateInterface)

    def initWindow(self):
        self.resize(900, 700)
        self.setMinimumWidth(760)
        self.setMinimumHeight(520)
        self.setWindowTitle('AutoSub Pro - 智能字幕助手')
        
        # Center the window
        desktop = QApplication.primaryScreen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w//2 - self.width()//2, h//2 - self.height()//2)

    def closeEvent(self, event: QCloseEvent):
        """ 关闭窗口前停止所有运行中的线程，防止后台崩溃 """
        # 停止转录界面的线程
        try:
            self.transcribeInterface._on_stop_task()
        except Exception:
            logging.exception("[MainWindow] 停止转录线程失败")
        try:
            self.realtimeInterface.stop_all()
        except Exception:
            logging.exception("[MainWindow] 停止实时捕获线程失败")
        # 停止翻译界面的线程
        try:
            self.translateInterface._on_stop_task()
        except Exception:
            logging.exception("[MainWindow] 停止翻译线程失败")
        # 停止批量任务界面的线程
        try:
            if hasattr(self.taskInterface, '_on_stop_batch'):
                self.taskInterface._on_stop_batch()
        except Exception:
            logging.exception("[MainWindow] 停止批量线程失败")

        # 关闭前再等待一次，确保线程对象不会在运行中被销毁
        def _wait_thread(thread, name, timeout_ms=5000):
            if thread and thread.isRunning():
                if not thread.wait(timeout_ms):
                    logging.warning("[MainWindow] 线程未及时退出: %s", name)
                    return False
            return True

        all_stopped = True
        all_stopped = _wait_thread(getattr(self.realtimeInterface, "capture_worker", None), "realtime.capture_worker") and all_stopped
        all_stopped = _wait_thread(getattr(self.realtimeInterface, "transcription_thread", None), "realtime.transcription_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.realtimeInterface, "translation_thread", None), "realtime.translation_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.realtimeInterface, "fix_thread", None), "realtime.fix_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.transcribeInterface, "thread", None), "transcribe.thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.transcribeInterface, "work_thread", None), "transcribe.work_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.translateInterface, "translation_thread", None), "translate.translation_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.translateInterface, "fix_thread", None), "translate.fix_thread") and all_stopped
        all_stopped = _wait_thread(getattr(self.taskInterface, "batch_thread", None), "task.batch_thread") and all_stopped

        if not all_stopped:
            event.ignore()
            return

        qconfig.save()
        super().closeEvent(event)
