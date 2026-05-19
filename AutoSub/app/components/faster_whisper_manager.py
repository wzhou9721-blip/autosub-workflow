from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, 
    QHeaderView, QTableWidgetItem
)
from qfluentwidgets import (
    SettingCardGroup, PrimaryPushSettingCard, FluentIcon as FIF, 
    InfoBar, InfoBarPosition, OptionsSettingCard, MessageBoxBase,
    TableWidget, TableItemDelegate, HyperlinkButton, SubtitleLabel,
    BodyLabel, ProgressBar, isDarkTheme, SwitchSettingCard
)
from app.common.config import BIN_PATH, MODEL_PATH, cfg
from app.common.thread import FileDownloadThread, UnzipThread, ModelscopeDownloadThread, PipInstallThread
import shutil
import os
from pathlib import Path

FASTER_WHISPER_MODELS = [
    {
        "label": "Tiny",
        "value": "faster-whisper-tiny",
        "size": "77824",
        "modelScopeLink": "pengzhendong/faster-whisper-tiny",
    },
    {
        "label": "Base",
        "value": "faster-whisper-base",
        "size": "148480",
        "modelScopeLink": "pengzhendong/faster-whisper-base",
    },
    {
        "label": "Small",
        "value": "faster-whisper-small",
        "size": "495616",
        "modelScopeLink": "pengzhendong/faster-whisper-small",
    },
    {
        "label": "Medium",
        "value": "faster-whisper-medium",
        "size": "1572864",
        "modelScopeLink": "pengzhendong/faster-whisper-medium",
    },
    {
        "label": "Large-v1",
        "value": "faster-whisper-large-v1",
        "size": "3145728",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v1",
    },
    {
        "label": "Large-v2",
        "value": "faster-whisper-large-v2",
        "size": "3145728",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v2",
    },
    {
        "label": "Large-v3",
        "value": "faster-whisper-large-v3",
        "size": "3145728",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v3",
    },
    {
        "label": "Large-v3-turbo",
        "value": "faster-whisper-large-v3-turbo",
        "size": "1720320",
        "modelScopeLink": "pengzhendong/faster-whisper-large-v3-turbo",
    },
]

class FasterWhisperModelDialog(MessageBoxBase):
    """Faster Whisper 模型管理对话框"""

    is_downloading = False

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 内容区使用不透明背景，避免模型管理界面背景缺失（与 Fluent 设置页一致）
        if isDarkTheme():
            self.widget.setStyleSheet(
                "QFrame#centerWidget { background-color: #2B2C2F; border: none; border-radius: 8px; }"
            )
        else:
            self.widget.setStyleSheet(
                "QFrame#centerWidget { background-color: #ffffff; border: none; border-radius: 8px; }"
            )
        self.widget.setMinimumWidth(700)
        self.model_download_thread = None
        self._setup_ui()
        
        # Hide default buttons
        self.yesButton.hide()
        self.cancelButton.setText("关闭")

    def _setup_ui(self):
        layout = QVBoxLayout()
        
        # 标题栏
        title_layout = QHBoxLayout()
        title = SubtitleLabel("模型管理", self)
        title_layout.addWidget(title)
        
        open_folder_btn = HyperlinkButton("", "打开模型文件夹", parent=self)
        open_folder_btn.setIcon(FIF.FOLDER)
        open_folder_btn.clicked.connect(self._open_model_folder)
        title_layout.addStretch()
        title_layout.addWidget(open_folder_btn)
        
        layout.addLayout(title_layout)
        layout.addSpacing(10)
        
        # 模型表格
        self.model_table = self._create_model_table()
        self._populate_model_table()
        layout.addWidget(self.model_table)
        
        # 进度条
        self.progress_bar = ProgressBar(self)
        self.progress_label = BodyLabel("", self)
        self.progress_bar.hide()
        self.progress_label.hide()
        
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.progress_label)
        
        self.viewLayout.addLayout(layout)

    def _create_model_table(self):
        table = TableWidget(self)
        table.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(TableWidget.SelectionMode.NoSelection)
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["模型名称", "大小", "状态", "操作"])
        
        table.setBorderVisible(False)
        table.setBorderRadius(0)
        table.setItemDelegate(TableItemDelegate(table))
        
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        
        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 150)
        
        row_height = 45
        table.verticalHeader().setDefaultSectionSize(row_height)
        
        header_height = 30
        max_visible_rows = 8
        table_height = row_height * max_visible_rows + header_height + 5
        table.setFixedHeight(table_height)
        
        return table

    def _populate_model_table(self):
        self.model_table.setRowCount(len(FASTER_WHISPER_MODELS))
        for i, model in enumerate(FASTER_WHISPER_MODELS):
            self._add_model_row(i, model)

    def _add_model_row(self, row, model):
        # 名称
        name_item = QTableWidgetItem(model["label"])
        name_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_table.setItem(row, 0, name_item)
        
        # 大小
        size_mb = int(model['size']) / 1024
        if size_mb > 1024:
            size_str = f"{size_mb/1024:.2f} GB"
        else:
            size_str = f"{size_mb:.1f} MB"
            
        size_item = QTableWidgetItem(size_str)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.model_table.setItem(row, 1, size_item)
        
        # 状态
        model_path = MODEL_PATH / model["value"]
        model_bin_path = model_path / "model.bin"
        is_downloaded = model_bin_path.exists()
        
        status_text = "已下载" if is_downloaded else "未下载"
        status_item = QTableWidgetItem(status_text)
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if is_downloaded:
            status_item.setForeground(QColor("#07C160"))
        self.model_table.setItem(row, 2, status_item)
        
        # 操作按钮
        button_container = QWidget()
        button_layout = QHBoxLayout(button_container)
        button_layout.setContentsMargins(4, 4, 4, 4)
        
        if is_downloaded:
            action_btn = HyperlinkButton("", "删除", parent=self)
            action_btn.setIcon(FIF.DELETE)
            action_btn.clicked.connect(lambda checked, r=row: self._delete_model(r))
        else:
            action_btn = HyperlinkButton("", "下载", parent=self)
            action_btn.setIcon(FIF.DOWNLOAD)
            action_btn.clicked.connect(lambda checked, r=row: self._download_model(r))
            
        button_layout.addStretch()
        button_layout.addWidget(action_btn)
        button_layout.addStretch()
        self.model_table.setCellWidget(row, 3, button_container)

    def _download_model(self, row):
        if FasterWhisperModelDialog.is_downloading:
            InfoBar.warning(
                "下载进行中",
                "请等待当前下载任务完成",
                duration=3000,
                parent=self.window()
            )
            return
            
        FasterWhisperModelDialog.is_downloading = True
        model = FASTER_WHISPER_MODELS[row]
        
        self.progress_bar.show()
        self.progress_label.show()
        self.progress_label.setText(f"正在下载 {model['label']} 模型...")
        
        # Disable buttons
        self._set_buttons_enabled(False)
        
        save_path = MODEL_PATH / model["value"]
        
        self.model_download_thread = ModelscopeDownloadThread(
            model["modelScopeLink"], str(save_path)
        )
        self.model_download_thread.progress.connect(self._on_download_progress)
        self.model_download_thread.finished.connect(lambda: self._on_download_finished(row))
        self.model_download_thread.error.connect(self._on_download_error)
        self.model_download_thread.start()

    def _delete_model(self, row):
        model = FASTER_WHISPER_MODELS[row]
        model_path = MODEL_PATH / model["value"]
        
        try:
            if model_path.exists():
                shutil.rmtree(model_path)
            
            # Refresh row
            self._add_model_row(row, model)
            
            InfoBar.success(
                "删除成功",
                f"{model['label']} 模型已删除",
                duration=2000,
                parent=self.window()
            )
        except Exception as e:
            InfoBar.error(
                "删除失败",
                str(e),
                duration=3000,
                parent=self.window()
            )

    def _on_download_progress(self, value, msg):
        self.progress_bar.setValue(value)
        self.progress_label.setText(msg)

    def _on_download_finished(self, row):
        FasterWhisperModelDialog.is_downloading = False
        self.progress_bar.hide()
        self.progress_label.hide()
        
        # Refresh row
        self._add_model_row(row, FASTER_WHISPER_MODELS[row])
        self._set_buttons_enabled(True)
        
        InfoBar.success(
            "下载完成",
            "模型下载成功",
            duration=3000,
            parent=self.window()
        )

    def _on_download_error(self, error):
        FasterWhisperModelDialog.is_downloading = False
        self.progress_bar.hide()
        self.progress_label.hide()
        self._set_buttons_enabled(True)
        
        InfoBar.error(
            "下载失败",
            error,
            duration=3000,
            parent=self.window()
        )

    def _set_buttons_enabled(self, enabled):
        for row in range(self.model_table.rowCount()):
            widget = self.model_table.cellWidget(row, 3)
            if widget:
                btn = widget.findChild(HyperlinkButton)
                if btn:
                    btn.setEnabled(enabled)

    def _open_model_folder(self):
        MODEL_PATH.mkdir(parents=True, exist_ok=True)
        os.startfile(MODEL_PATH)

    def closeEvent(self, event):
        if FasterWhisperModelDialog.is_downloading:
            InfoBar.warning(
                "警告",
                "请等待下载完成后再关闭",
                duration=2000,
                parent=self.window()
            )
            event.ignore()
        else:
            super().closeEvent(event)


class FasterWhisperManager(SettingCardGroup):
    def __init__(self, parent=None, title="Faster Whisper 管理"):
        super().__init__(title, parent)
        
        self.cpu_url = "https://modelscope.cn/models/bkfengg/whisper-cpp/resolve/master/whisper-faster.exe"
        self.gpu_url = "https://modelscope.cn/models/bkfengg/whisper-cpp/resolve/master/Faster-Whisper-XXL_r245.2_windows.7z"
        
        # 状态卡片
        self.statusCard = PrimaryPushSettingCard(
            "刷新",
            FIF.INFO,
            "环境状态",
            "正在检测本地 Faster Whisper 环境...",
            self
        )
        self.statusCard.button.clicked.connect(self.checkEnvironment)
        self.addSettingCard(self.statusCard)
        
        # 设备选择 (CPU/GPU)
        self.deviceCard = OptionsSettingCard(
            cfg.fasterWhisperDevice,
            FIF.SPEED_HIGH,
            "运行设备",
            "选择 Faster Whisper 的运行设备 (需要已安装对应版本)",
            ["CPU", "GPU"],
            self
        )
        self.addSettingCard(self.deviceCard)
        
        # 按钮：安装 CPU 版本
        self.cpuInstallCard = PrimaryPushSettingCard(
            "安装",
            FIF.DOWNLOAD,
            "CPU 版本",
            "下载并安装 Faster Whisper (仅 CPU)",
            self
        )
        self.cpuInstallCard.button.clicked.connect(self.installCpu)
        self.addSettingCard(self.cpuInstallCard)
        
        # 按钮：安装 GPU 版本
        self.gpuInstallCard = PrimaryPushSettingCard(
            "安装",
            FIF.DOWNLOAD,
            "GPU 版本",
            "下载并安装 Faster Whisper (支持 NVIDIA GPU)",
            self
        )
        self.gpuInstallCard.button.clicked.connect(self.installGpu)
        self.addSettingCard(self.gpuInstallCard)

        # 模型管理
        self.modelCard = PrimaryPushSettingCard(
            "管理模型",
            FIF.FOLDER,
            "模型管理",
            "下载和管理 Faster Whisper 模型",
            self
        )
            
        self.modelCard.button.clicked.connect(self.showModelDialog)
        self.addSettingCard(self.modelCard)
        
        self.downloadThread = None
        self.unzipThread = None
        self.pipThread = None
        
        self.checkEnvironment()

    def checkEnvironment(self):
        cpu_path = BIN_PATH / "faster-whisper.exe"
        gpu_path = BIN_PATH / "Faster-Whisper-XXL" / "faster-whisper-xxl.exe"
        
        status = []
        if cpu_path.exists():
            status.append("CPU已安装")
        else:
            status.append("CPU未安装")
            
        if gpu_path.exists():
            status.append("GPU已安装")
        else:
            status.append("GPU未安装")
            
        self.statusCard.setContent(" | ".join(status))
        
        # 更新按钮状态
        if cpu_path.exists():
            self.cpuInstallCard.button.setText("重新安装")
        else:
            self.cpuInstallCard.button.setText("安装")
            
        if gpu_path.exists():
            self.gpuInstallCard.button.setText("重新安装")
        else:
            self.gpuInstallCard.button.setText("安装")

    def installCpu(self):
        self._startDownload(self.cpu_url, BIN_PATH / "faster-whisper.exe", is_zip=False)

    def installGpu(self):
        self._startDownload(self.gpu_url, BIN_PATH / "temp_gpu.7z", is_zip=True)

    def showModelDialog(self):
        w = FasterWhisperModelDialog(self.window())
        w.exec()

    def _startDownload(self, url, save_path, is_zip):
        if self.downloadThread and self.downloadThread.isRunning():
            InfoBar.warning(
                title="正在下载",
                content="当前有正在进行的下载任务，请稍候...",
                parent=self.window(),
                position=InfoBarPosition.TOP_RIGHT
            )
            return

        self.cpuInstallCard.button.setEnabled(False)
        self.gpuInstallCard.button.setEnabled(False)
        self.statusCard.setContent("正在下载...")

        self.downloadThread = FileDownloadThread(url, str(save_path))
        self.downloadThread.progress.connect(self._updateDownloadProgress)
        self.downloadThread.finished.connect(lambda path: self._onDownloadFinished(path, is_zip))
        self.downloadThread.error.connect(self._onDownloadError)
        self.downloadThread.start()
        
        InfoBar.info(
            title="开始下载",
            content="正在后台下载，请耐心等待...",
            parent=self.window(),
            position=InfoBarPosition.TOP_RIGHT,
            duration=3000
        )

    def _updateDownloadProgress(self, percent, msg):
        self.statusCard.setContent(f"下载中: {percent:.1f}% - {msg}")

    def _onDownloadFinished(self, path, is_zip):
        if is_zip:
            self.statusCard.setContent("下载完成，正在解压...")
            # 解压到 bin 目录 (假设压缩包内包含 Faster-Whisper-XXL 文件夹)
            self.unzipThread = UnzipThread(path, str(BIN_PATH))
            self.unzipThread.finished.connect(lambda: self._onUnzipFinished(is_gpu=True))
            self.unzipThread.error.connect(self._onDownloadError)
            self.unzipThread.start()
        else:
            self._onInstallFinished()

    def _onUnzipFinished(self, is_gpu=False):
        if is_gpu:
            # 如果是打包环境，跳过 pip install，假设压缩包中已包含所需 DLL
            if getattr(sys, 'frozen', False):
                self._onInstallFinished()
                InfoBar.info(
                    title="提示",
                    content="GPU 组件已安装。如果无法启用 GPU，请重启程序。",
                    parent=self.window(),
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=5000
                )
                return

            self.statusCard.setContent("正在安装 GPU 依赖库 (NVIDIA CUDA)...")
            # GPU 版本需要安装额外的 Python 库来提供 DLL
            packages = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
            self.pipThread = PipInstallThread(packages)
            self.pipThread.finished.connect(self._onInstallFinished)
            self.pipThread.progress.connect(lambda msg: self.statusCard.setContent(f"正在安装: {msg[:30]}..."))
            self.pipThread.error.connect(self._onDownloadError)
            self.pipThread.start()
        else:
            self._onInstallFinished()

    def _onInstallFinished(self):
        self.cpuInstallCard.button.setEnabled(True)
        self.gpuInstallCard.button.setEnabled(True)
        self.checkEnvironment()
        InfoBar.success(
            title="安装完成",
            content="Faster Whisper 组件已安装成功！",
            parent=self.window(),
            position=InfoBarPosition.TOP_RIGHT
        )

    def _onDownloadError(self, error_msg):
        self.cpuInstallCard.button.setEnabled(True)
        self.gpuInstallCard.button.setEnabled(True)
        self.checkEnvironment()
        InfoBar.error(
            title="安装失败",
            content=f"错误详情: {error_msg}",
            parent=self.window(),
            position=InfoBarPosition.TOP_RIGHT,
            duration=-1
        )
