import logging
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem
import os
from qfluentwidgets import (
    TitleLabel,
    SmoothScrollArea,
    PrimaryPushButton,
    PushButton,
    FluentIcon as FIF,
    CardWidget,
    BodyLabel,
    TransparentToolButton,
    InfoBar,
    InfoBarPosition,
    SettingCardGroup,
    ComboBoxSettingCard,
    ProgressBar,
    PushSettingCard,
    SwitchSettingCard,
    ComboBox,
    SwitchButton,
)
from PyQt6.QtWidgets import QFileDialog
from app.common.config import cfg
from app.common.runtime_state import BATCH_SESSION_STATE_FILE, load_json_file, save_json_atomic, safe_unlink

from app.components.file_drop_widget import FileDropWidget
from app.components.setting_cards import LineEditSettingCard, CalibrationPanel
from app.common.thread import BatchTranscriptionThread

_TARGET_LANGS = [lang for lang in cfg.LANGUAGE_LIST if lang != "Auto"]

class FileItemWidget(CardWidget):
    """ 文件列表项组件 """
    delete_requested = pyqtSignal(str)   # 发送要删除的文件路径

    def __init__(self, file_path, src_lang=None, tgt_lang=None, parent=None):
        super().__init__(parent)
        self.file_path = file_path

        _src = src_lang or cfg.sourceLanguage.value
        _tgt = tgt_lang or cfg.targetLanguage.value

        self.hBoxLayout = QHBoxLayout(self)
        self.hBoxLayout.setContentsMargins(16, 8, 12, 8)
        self.hBoxLayout.setSpacing(10)

        self.iconWidget = IconWidget(FIF.VIDEO, self)
        self.iconWidget.setFixedSize(22, 22)

        self.nameLabel = BodyLabel(os.path.basename(file_path), self)
        self.nameLabel.setToolTip(file_path)

        # 源语言下拉
        self.srcLangCombo = ComboBox(self)
        self.srcLangCombo.addItems(cfg.LANGUAGE_LIST)
        self.srcLangCombo.setCurrentText(_src if _src in cfg.LANGUAGE_LIST else "Auto")
        self.srcLangCombo.setFixedWidth(105)
        self.srcLangCombo.setToolTip("源语言")
        self.srcLangCombo.setMaxVisibleItems(6)

        self.arrowLabel = BodyLabel("→", self)
        self.arrowLabel.setTextColor(QColor(150, 150, 150), QColor(150, 150, 150))

        # 目标语言下拉
        self.tgtLangCombo = ComboBox(self)
        self.tgtLangCombo.addItems(_TARGET_LANGS)
        self.tgtLangCombo.setCurrentText(_tgt if _tgt in _TARGET_LANGS else "Chinese")
        self.tgtLangCombo.setFixedWidth(105)
        self.tgtLangCombo.setToolTip("目标语言")
        self.tgtLangCombo.setMaxVisibleItems(5)

        # 多语言模式切换按钮（地球图标，可选中态表示已启用）
        self.multiBtn = TransparentToolButton(FIF.GLOBE, self)
        self.multiBtn.setFixedSize(28, 28)
        self.multiBtn.setCheckable(True)
        self.multiBtn.setChecked(cfg.multilingualMode.value)
        self.multiBtn.setToolTip("多语言模式：开启后可为空白区间补录指定次要语言")
        self.multiBtn.clicked.connect(self._onMultiToggled)

        # 次要语言下拉（默认跟随全局设置，多语言模式关闭时隐藏）
        self.secLangCombo = ComboBox(self)
        self.secLangCombo.addItems(cfg.LANGUAGE_LIST)
        _sec = cfg.secondaryLanguage.value
        self.secLangCombo.setCurrentText(_sec if _sec in cfg.LANGUAGE_LIST else "Auto")
        self.secLangCombo.setFixedWidth(105)
        self.secLangCombo.setToolTip("次要语言（空白区间补录时优先使用）")
        self.secLangCombo.setMaxVisibleItems(6)

        # 删除按钮
        self.deleteBtn = TransparentToolButton(FIF.DELETE, self)
        self.deleteBtn.setFixedSize(28, 28)
        self.deleteBtn.setToolTip("从队列中移除")
        self.deleteBtn.clicked.connect(lambda: self.delete_requested.emit(self.file_path))

        self.hBoxLayout.addWidget(self.iconWidget)
        self.hBoxLayout.addWidget(self.nameLabel)
        self.hBoxLayout.addStretch(1)
        self.hBoxLayout.addWidget(self.srcLangCombo)
        self.hBoxLayout.addWidget(self.arrowLabel)
        self.hBoxLayout.addWidget(self.tgtLangCombo)
        self.hBoxLayout.addWidget(self.multiBtn)
        self.hBoxLayout.addWidget(self.secLangCombo)
        self.hBoxLayout.addWidget(self.deleteBtn)

        # 初始化次要语言可见性
        self._onMultiToggled(self.multiBtn.isChecked())

    def _onMultiToggled(self, checked: bool):
        self.secLangCombo.setVisible(checked)

    def setMultilingualAvailable(self, available: bool, reason: str = ""):
        self.multiBtn.setEnabled(available)
        self.secLangCombo.setEnabled(available)
        tooltip = reason or "多语言模式：开启后可为空白区间补录指定次要语言"
        self.multiBtn.setToolTip(tooltip)
        self.secLangCombo.setToolTip(tooltip if not available else "次要语言（空白区间补录时优先使用）")
        self.secLangCombo.setVisible(available and self.multiBtn.isChecked())

    def setLangControlsVisible(self, visible: bool):
        """单文件时隐藏条目语言控件，多文件时显示。"""
        self.srcLangCombo.setVisible(visible)
        self.arrowLabel.setVisible(visible)
        self.tgtLangCombo.setVisible(visible)
        self.multiBtn.setVisible(visible)
        # 次要语言跟随多语言开关状态，不直接跟 visible 绑定
        self.secLangCombo.setVisible(visible and self.multiBtn.isEnabled() and self.multiBtn.isChecked())

    def get_lang_config(self) -> dict:
        """返回该文件条目的语言设置（含多语言）"""
        multilingual = self.multiBtn.isChecked()
        return {
            "sourceLanguage":    self.srcLangCombo.currentText(),
            "targetLanguage":    self.tgtLangCombo.currentText(),
            "multilingualMode":  multilingual,
            "secondaryLanguage": self.secLangCombo.currentText() if multilingual else None,
        }

from qfluentwidgets import IconWidget
from PyQt6.QtGui import QColor, QPainter, QPen

class SettingTaskInterface(SmoothScrollArea):
    """ 设置任务界面 """
    start_transcription_signal = pyqtSignal(list, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingTaskInterface")
        self.setAcceptDrops(True)
        
        self.scrollWidget = QWidget()
        self.scrollWidget.setObjectName("scrollWidget")  # 设置 ObjectName 以便应用样式
        self.setWidget(self.scrollWidget)
        self.setWidgetResizable(True)
        
        # 设置背景透明，适配深色模式
        self.setStyleSheet("SettingTaskInterface, #scrollWidget { background-color: transparent; border: none; }")
        
        self.vBoxLayout = QVBoxLayout(self.scrollWidget)
        self.vBoxLayout.setContentsMargins(36, 20, 36, 36)
        self.vBoxLayout.setSpacing(20)
        
        # 1. 标题 + 批量开关
        self.titleLayout = QHBoxLayout()
        self.titleLabel = TitleLabel("设置任务", self.scrollWidget)
        self.titleLayout.addWidget(self.titleLabel)
        self.titleLayout.addSpacing(16)
        
        self.batchLabel = BodyLabel("批量模式", self.scrollWidget)
        self.batchLabel.setStyleSheet("color: #6B7280; font-size: 13px;")
        self.batchSwitch = SwitchButton(self.scrollWidget)
        self.batchSwitch.setChecked(False)
        self.batchSwitch.checkedChanged.connect(self._onBatchModeChanged)
        self.titleLayout.addWidget(self.batchLabel)
        self.titleLayout.addWidget(self.batchSwitch)
        self.titleLayout.addStretch(1)
        self.vBoxLayout.addLayout(self.titleLayout)
        
        # 2. 拖拽区域
        self.dropWidget = FileDropWidget(self.scrollWidget)
        self.dropWidget.filesAdded.connect(self.onFilesAdded)
        self.vBoxLayout.addWidget(self.dropWidget)

        # 3. 文件列表标题和操作栏（紧跟拖拽区域下方）
        self.listHeaderLayout = QHBoxLayout()
        self.listLabel = BodyLabel("待处理文件列表", self.scrollWidget)
        self.listHeaderLayout.addWidget(self.listLabel)
        self.listHeaderLayout.addStretch(1)
        self.vBoxLayout.addLayout(self.listHeaderLayout)

        # 4. 文件列表容器
        self.fileListLayout = QVBoxLayout()
        self.fileListLayout.setSpacing(8)
        self.vBoxLayout.addLayout(self.fileListLayout)

        # 2.5 任务配置
        self.initSettings()
        
        self.vBoxLayout.addStretch(1)
        
        # 4.5 进度显示区域
        self.progressContainer = QWidget(self.scrollWidget)
        self.progressLayout = QVBoxLayout(self.progressContainer)
        self.progressLayout.setContentsMargins(0, 0, 0, 10)
        
        self.progressLabel = BodyLabel("", self.progressContainer)
        self.progressLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progressLabel.hide()
        
        self.progressBar = ProgressBar(self.progressContainer)
        self.progressBar.setMinimum(0)
        self.progressBar.setMaximum(100)
        self.progressBar.setValue(0)
        self.progressBar.hide()
        
        self.progressLayout.addWidget(self.progressLabel)
        self.progressLayout.addWidget(self.progressBar)
        self.vBoxLayout.addWidget(self.progressContainer)
        
        # 5. 底部操作栏
        self.actionLayout = QHBoxLayout()
        self.actionLayout.setSpacing(10)

        # 终止批量按钮（初始隐藏，批量开始时显示）
        self.stopBatchBtn = PushButton("终止批量", self.scrollWidget, FIF.PAUSE)
        self.stopBatchBtn.hide()
        self.stopBatchBtn.clicked.connect(self._on_stop_batch)

        self.nextBtn = PrimaryPushButton("开始处理", self.scrollWidget)
        self.nextBtn.clicked.connect(self.onNextStep)
        self.actionLayout.addStretch(1)
        self.actionLayout.addWidget(self.stopBatchBtn)
        self.actionLayout.addWidget(self.nextBtn)
        self.vBoxLayout.addLayout(self.actionLayout)
        
        # 数据存储
        self.files = []
        self._restore_session_state_if_any()

    def initSettings(self):
        self.settingGroup = SettingCardGroup("任务配置", self.scrollWidget)
        
        # 视频语境 (新增)
        self.contextCard = LineEditSettingCard(
            cfg.videoContext,
            FIF.CHAT,
            "视频语境",
            "输入视频的背景信息或专业术语（可选），有助于提高转录准确率",
            self.settingGroup
        )
        
        # 转录方式 (新增：云端/本地)
        self.modeCard = ComboBoxSettingCard(
            cfg.transcribeMode,
            FIF.CLOUD,
            "转录方式",
            "选择使用本地模型还是云端API进行转录",
            ["本地", "云端"],
            self.settingGroup
        )
        
        # ASR Model (仅在本地模式显示)
        self.modelCard = ComboBoxSettingCard(
            cfg.asrModel,
            FIF.SPEED_HIGH,
            "语音模型",
            "选择用于转录的 Whisper 模型大小",
            ["tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo"],
            self.settingGroup
        )
        
        # Source Language (扩展了全语言)
        self.langCard = ComboBoxSettingCard(
            cfg.sourceLanguage,
            FIF.LANGUAGE,
            "源语言",
            "视频/音频的原始语言",
            cfg.LANGUAGE_LIST, # 使用 config 中定义的全语言列表
            self.settingGroup
        )
        # 限制源语言下拉列表的最大可见项数，超过则滚动
        self.langCard.comboBox.setMaxVisibleItems(5)

        self.gapFillCard = SwitchSettingCard(
            FIF.SYNC,
            "空白区间补录",
            "检测到较长未转录空白时，自动裁剪音频并二次转录。多语言模式下优先使用次要语言，单语言模式下沿用主语言",
            configItem=cfg.gap_fill_enabled,
            parent=self.settingGroup
        )
        
        # 多语言模式开关
        self.multilingualCard = SwitchSettingCard(
            FIF.GLOBE,
            "多语言模式",
            "视频包含多种语言时开启，用于为空白区间补录指定次要语言",
            configItem=cfg.multilingualMode,
            parent=self.settingGroup
        )

        # 次要源语言（仅多语言模式开启时显示）
        self.secondaryLangCard = ComboBoxSettingCard(
            cfg.secondaryLanguage,
            FIF.LANGUAGE,
            "次要源语言",
            "空白区间补录时优先使用的第二语言",
            cfg.LANGUAGE_LIST,
            self.settingGroup
        )
        self.secondaryLangCard.comboBox.setMaxVisibleItems(5)

        # Target Language
        self.targetLangCard = ComboBoxSettingCard(
            cfg.targetLanguage,
            FIF.LANGUAGE,
            "目标语言",
            "翻译的目标语言",
            _TARGET_LANGS,
            self.settingGroup
        )
        self.targetLangCard.comboBox.setMaxVisibleItems(5)
        
        # 术语表 (新增)
        self.glossaryCard = PushSettingCard(
            "选择文件",
            FIF.EDIT,
            "术语表",
            "上传术语表文件 (*.txt) 以提高专有名词翻译准确度",
            self.settingGroup
        )
        self.glossaryCard.clicked.connect(self._on_select_glossary)
        
        # Add Help Button to Glossary Card
        self.glossaryHelpBtn = TransparentToolButton(FIF.HELP, self.glossaryCard)
        self.glossaryHelpBtn.setToolTip("查看案例")
        self.glossaryHelpBtn.clicked.connect(self._open_glossary_example)
        
        # Insert help button BEFORE the "Select File" button
        # Layout: [...Title...] [HelpBtn] [Space] [Button] [Space]
        idx = self.glossaryCard.hBoxLayout.indexOf(self.glossaryCard.button)
        if idx >= 0:
            self.glossaryCard.hBoxLayout.insertWidget(idx, self.glossaryHelpBtn)
            self.glossaryCard.hBoxLayout.insertSpacing(idx + 1, 10)
        else:
            # Fallback if button not found
            self.glossaryCard.hBoxLayout.addWidget(self.glossaryHelpBtn)
        
        # Initialize content
        if cfg.glossaryPath.value:
            self.glossaryCard.setContent(os.path.basename(cfg.glossaryPath.value))
        
        self.settingGroup.addSettingCard(self.contextCard)
        self.settingGroup.addSettingCard(self.modeCard)
        self.settingGroup.addSettingCard(self.modelCard)
        self.settingGroup.addSettingCard(self.langCard)
        self.settingGroup.addSettingCard(self.gapFillCard)
        self.settingGroup.addSettingCard(self.multilingualCard)
        self.settingGroup.addSettingCard(self.secondaryLangCard)
        self.settingGroup.addSettingCard(self.targetLangCard)
        self.settingGroup.addSettingCard(self.glossaryCard)

        # 溢出检测参数面板：直接插入 settingGroup 内部 layout，避免 addSettingCard 的高度限制
        self.calibrationPanel = CalibrationPanel(self.scrollWidget)
        self.settingGroup.vBoxLayout.addWidget(self.calibrationPanel)

        self.vBoxLayout.addWidget(self.settingGroup)

        self.workflowGroup = SettingCardGroup("流程控制", self.scrollWidget)
        
        self.workflowOptimizeCard = SwitchSettingCard(
            FIF.EDUCATION,
            "自动优化",
            "转录完成后自动进行 AI 优化",
            configItem=cfg.workflow_optimize,
            parent=self.workflowGroup
        )
        self.workflowSplitCard = SwitchSettingCard(
            FIF.EDIT,
            "自动断句",
            "优化后自动进行语义断句",
            configItem=cfg.workflow_split,
            parent=self.workflowGroup
        )
        self.workflowTranslateCard = SwitchSettingCard(
            FIF.LANGUAGE,
            "自动翻译",
            "断句完成后自动进入翻译流程",
            configItem=cfg.workflow_translate,
            parent=self.workflowGroup
        )
        self.workflowOverflowFixCard = SwitchSettingCard(
            FIF.SETTING,
            "自动溢出修复",
            "翻译后自动进行溢出修复",
            configItem=cfg.workflow_overflow_fix,
            parent=self.workflowGroup
        )
        self.workflowGroup.addSettingCard(self.workflowOptimizeCard)
        self.workflowGroup.addSettingCard(self.workflowSplitCard)
        self.workflowGroup.addSettingCard(self.workflowTranslateCard)
        self.workflowGroup.addSettingCard(self.workflowOverflowFixCard)

        self.vBoxLayout.addWidget(self.workflowGroup)


        cfg.transcribeMode.valueChanged.connect(self.onTranscribeModeChanged)
        self.onTranscribeModeChanged(cfg.transcribeMode.value)
        cfg.asr_provider.valueChanged.connect(
            lambda _: self.onTranscribeModeChanged(cfg.transcribeMode.value)
        )
        cfg.gap_fill_enabled.valueChanged.connect(self._updateMultilingualAvailability)
        cfg.workflow_translate.valueChanged.connect(self.onTranslateWorkflowChanged)
        self.onTranslateWorkflowChanged(cfg.workflow_translate.value)

        # 多语言开关联动：控制次要语言卡的显示
        cfg.multilingualMode.valueChanged.connect(self._onMultilingualToggled)
        self._onMultilingualToggled(cfg.multilingualMode.value)

    def onTranscribeModeChanged(self, value):
        """根据转录模式显示/隐藏本地模型选择，并在云端模式下展示当前提供商。"""
        if value == "本地":
            self.modelCard.setVisible(True)
            self.modeCard.setContent("选择使用本地模型还是云端API进行转录")
        else:
            self.modelCard.setVisible(False)
            provider = cfg.asr_provider.value
            self.modeCard.setContent(f"当前云端提供商: {provider}（可在全局设置中切换）")
        self._updateMultilingualAvailability()

    def _onMultilingualToggled(self, enabled: bool):
        """多语言模式开关联动：显示/隐藏次要语言下拉。"""
        self.secondaryLangCard.setVisible(bool(enabled) and self._isMultilingualAvailable())

    def _isGladiaMode(self) -> bool:
        return cfg.transcribeMode.value == "云端" and cfg.asr_provider.value == "Gladia"

    def _isMultilingualAvailable(self) -> bool:
        return bool(cfg.gap_fill_enabled.value or self._isGladiaMode())

    def _updateMultilingualAvailability(self, *_):
        available = self._isMultilingualAvailable()
        if available:
            hint = "视频包含多种语言时开启，用于为空白区间补录指定次要语言"
        elif self._isGladiaMode():
            hint = "视频包含多种语言时开启，Gladia 主转录会直接参考次要语言"
        else:
            hint = "当前模式下需先开启空白区间补录；仅 Gladia 云端转录可在关闭补录时继续使用多语言模式"

        self.multilingualCard.setEnabled(available)
        self.multilingualCard.setContent(hint)
        self.secondaryLangCard.setEnabled(available)
        self._onMultilingualToggled(cfg.multilingualMode.value)

        file_hint = hint.replace("视频包含多种语言时开启，", "") if hint.startswith("视频包含多种语言时开启，") else hint
        for i in range(self.fileListLayout.count()):
            widget = self.fileListLayout.itemAt(i).widget()
            if isinstance(widget, FileItemWidget):
                widget.setMultilingualAvailable(available, file_hint)

    def onTranslateWorkflowChanged(self, value):
        enable = bool(value)
        self.workflowOverflowFixCard.setEnabled(enable)
        if not enable:
            cfg.workflow_overflow_fix.value = False

    def _updateLangCardsVisibility(self):
        """
        单文件：显示全局语言设置（含多语言卡），隐藏条目语言控件。
        多文件：隐藏全局语言设置，每个条目独立显示语言控件。
        """
        show_global = len(self.files) <= 1
        # 全局语言卡（含多语言）
        self.langCard.setVisible(show_global)
        self.targetLangCard.setVisible(show_global)
        self.multilingualCard.setVisible(show_global)
        self.secondaryLangCard.setVisible(show_global and cfg.multilingualMode.value and self._isMultilingualAvailable())
        # 每个文件条目的语言控件
        for i in range(self.fileListLayout.count()):
            w = self.fileListLayout.itemAt(i).widget()
            if isinstance(w, FileItemWidget):
                w.setLangControlsVisible(not show_global)
        self._updateMultilingualAvailability()

    def onFilesAdded(self, files):
        """ 文件添加回调（支持多文件批量加入队列） """
        if not files:
            return

        is_batch = self.batchSwitch.isChecked()

        if not is_batch:
            # 单文件模式：只取第一个文件，替换现有文件
            new_file = files[0]
            if not os.path.isfile(new_file):
                return
            # 清空现有文件列表
            self._clearAllFiles()
            self.files.append(new_file)
            self.addFileItem(new_file)
            self.dropWidget.setHint("已添加文件", os.path.basename(new_file))
            self._updateLangCardsVisibility()
            self._save_session_state(stage="queued", current_file_index=0)
            InfoBar.success(
                title="文件已添加",
                content=os.path.basename(new_file),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500
            )
            return

        # 批量模式：允许添加多个文件
        added = []
        for f in files:
            if os.path.isfile(f) and f not in self.files:
                self.files.append(f)
                self.addFileItem(f)
                added.append(os.path.basename(f))

        if not added:
            return

        self._updateLangCardsVisibility()
        self._save_session_state(stage="queued", current_file_index=0)

        n = len(added)
        if n == 1:
            self.dropWidget.setHint("已添加文件", added[0])
            content = added[0]
        else:
            self.dropWidget.setHint(f"已添加 {n} 个文件", f"队列共 {len(self.files)} 个文件")
            content = f"共添加 {n} 个文件，队列合计 {len(self.files)} 个"

        InfoBar.success(
            title="文件已添加",
            content=content,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=2500
        )

    def addFileItem(self, file_path):
        item = FileItemWidget(
            file_path,
            src_lang=cfg.sourceLanguage.value,
            tgt_lang=cfg.targetLanguage.value,
            parent=self.scrollWidget
        )
        item.delete_requested.connect(self._on_delete_file_item)
        self.fileListLayout.addWidget(item)
        self._updateMultilingualAvailability()

    def _on_delete_file_item(self, file_path):
        """ 响应文件项删除按钮 """
        for i in range(self.fileListLayout.count()):
            widget = self.fileListLayout.itemAt(i).widget()
            if isinstance(widget, FileItemWidget) and widget.file_path == file_path:
                self.fileListLayout.removeWidget(widget)
                widget.deleteLater()
                break
        if file_path in self.files:
            self.files.remove(file_path)
        self._updateLangCardsVisibility()
        n = len(self.files)
        if n == 0:
            self.dropWidget.resetHint()
        elif n == 1:
            self.dropWidget.setHint("已添加文件", os.path.basename(self.files[0]))
        else:
            self.dropWidget.setHint(f"队列共 {n} 个文件", "可继续拖入更多文件")
        self._save_session_state(stage="queued", current_file_index=0)
        InfoBar.info(
            title="已移除",
            content=os.path.basename(file_path),
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=1500
        )

    def _clearAllFiles(self, clear_session_state: bool = True):
        """ 清空所有文件列表项 """
        while self.fileListLayout.count():
            item = self.fileListLayout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.files.clear()
        self.dropWidget.resetHint()
        if clear_session_state:
            self._clear_session_state()

    def _onBatchModeChanged(self, checked):
        """ 批量模式开关切换 """
        if not checked and len(self.files) > 1:
            # 从批量切回单文件模式，只保留第一个文件
            keep = self.files[0]
            self._clearAllFiles()
            self.files.append(keep)
            self.addFileItem(keep)
            self.dropWidget.setHint("已添加文件", os.path.basename(keep))
            self._updateLangCardsVisibility()
            InfoBar.info(
                title="已切换为单文件模式",
                content=f"仅保留: {os.path.basename(keep)}",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=2500
            )

    def removeFile(self, widget, file_path):
        if file_path in self.files:
            self.files.remove(file_path)
            self.fileListLayout.removeWidget(widget)
            widget.deleteLater()

    def clearFiles(self):
        self.files.clear()
        self.dropWidget.resetHint()
        # 清空 Layout
        while self.fileListLayout.count():
            item = self.fileListLayout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        files = [u.toLocalFile() for u in urls if u.toLocalFile()]
        
        if files:
            self.onFilesAdded(files)

    def _build_global_config_data(self) -> dict:
        """构建全局流程配置，供单文件与批量入口复用。"""
        return {
            "transcribeMode": cfg.transcribeMode.value,
            "asrModel": cfg.asrModel.value,
            "sourceLanguage": cfg.sourceLanguage.value,
            "targetLanguage": cfg.targetLanguage.value,
            "videoContext": cfg.videoContext.value,
            "wordLevelTimestamps": cfg.wordLevelTimestamps.value,
            "workflowOptimize": cfg.workflow_optimize.value,
            "workflowSplit": cfg.workflow_split.value,
            "workflowTranslate": cfg.workflow_translate.value,
            "workflowOverflowFix": cfg.workflow_overflow_fix.value,
            "gapFillEnabled": cfg.gap_fill_enabled.value,
            "multilingualMode": cfg.multilingualMode.value,
            "secondaryLanguage": cfg.secondaryLanguage.value if cfg.multilingualMode.value else None,
            "CloudMultilingualGapThresholdSec": cfg.cloud_multilingual_gap_threshold_sec.value,
        }

    def _get_session_state_path(self):
        return BATCH_SESSION_STATE_FILE

    def _save_session_state(self, stage: str = "queued", current_file_index: int = 0):
        try:
            if not self.files:
                return
            entries = self._collect_file_entries(include_lang_overrides=True)
            if not entries:
                return
            data = {
                "files": entries,
                "config": self._build_global_config_data(),
                "stage": stage,
                "current_file_index": int(current_file_index),
            }
            save_json_atomic(self._get_session_state_path(), data)
        except Exception:
            logging.exception("[SettingTaskInterface] 保存会话状态失败")

    def _clear_session_state(self):
        try:
            safe_unlink(self._get_session_state_path())
        except Exception:
            logging.exception("[SettingTaskInterface] 清理会话状态失败")

    def _restore_session_state_if_any(self):
        path = self._get_session_state_path()
        if not path.exists():
            return
        try:
            data = load_json_file(path, default={}) or {}
            files = data.get("files") or []
            if not files:
                return

            existing_paths = []
            existing_map = {}
            for item in files:
                p = item.get("path")
                if p and os.path.isfile(p):
                    existing_paths.append(p)
                    existing_map[p] = item
            if not existing_paths:
                self._clear_session_state()
                return

            self.batchSwitch.setChecked(len(existing_paths) > 1)
            self._clearAllFiles(clear_session_state=False)
            for p in existing_paths:
                self.files.append(p)
                saved = existing_map.get(p, {})
                item = FileItemWidget(
                    p,
                    src_lang=saved.get("sourceLanguage") or cfg.sourceLanguage.value,
                    tgt_lang=saved.get("targetLanguage") or cfg.targetLanguage.value,
                    parent=self.scrollWidget
                )
                item.multiBtn.setChecked(bool(saved.get("multilingualMode", False)))
                sec = saved.get("secondaryLanguage")
                if sec and sec in cfg.LANGUAGE_LIST:
                    item.secLangCombo.setCurrentText(sec)
                item.delete_requested.connect(self._on_delete_file_item)
                self.fileListLayout.addWidget(item)

            self._updateLangCardsVisibility()
            if len(existing_paths) == 1:
                self.dropWidget.setHint("已恢复上次任务", os.path.basename(existing_paths[0]))
            else:
                self.dropWidget.setHint("已恢复上次批量任务", f"共 {len(existing_paths)} 个文件")

            stage = data.get("stage", "queued")
            idx = int(data.get("current_file_index", 0)) + 1
            InfoBar.info(
                title="已恢复上次会话",
                content=f"阶段: {stage}，建议从文件 {idx}/{len(existing_paths)} 继续",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=4500
            )
        except Exception:
            logging.exception("[SettingTaskInterface] 恢复会话状态失败")

    def _collect_file_entries(self, include_lang_overrides: bool) -> list:
        """收集文件队列；批量模式带条目级语言配置，单文件使用全局配置。"""
        if not include_lang_overrides:
            return [{"path": self.files[0]}]

        file_entries = []
        for i in range(self.fileListLayout.count()):
            widget = self.fileListLayout.itemAt(i).widget()
            if isinstance(widget, FileItemWidget):
                entry = {"path": widget.file_path}
                entry.update(widget.get_lang_config())
                file_entries.append(entry)
        return file_entries

    def onNextStep(self):
        if not self.files:
            InfoBar.error(
                title="无法开始",
                content="请至少添加一个文件",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
            return

        config_data = self._build_global_config_data()

        if len(self.files) > 1:
            file_entries = self._collect_file_entries(include_lang_overrides=True)
            self._start_batch(file_entries, config_data)
            return

        self.start_transcription_signal.emit(self.files, config_data)

    def _start_batch(self, file_entries, config_data):
        """启动批量处理线程"""
        self._save_session_state(stage="batch_started", current_file_index=0)
        self.nextBtn.setEnabled(False)
        self.nextBtn.setText("批量处理中...")
        self.stopBatchBtn.show()
        self.progressLabel.show()
        self.progressBar.show()
        self.progressBar.setValue(0)

        self.batch_thread = BatchTranscriptionThread(file_entries, config_data)
        self.batch_thread.batch_reports = []
        self.batch_thread.file_progress.connect(self._on_batch_progress)
        self.batch_thread.file_done.connect(self._on_batch_file_done)
        self.batch_thread.all_done.connect(self._on_batch_all_done)
        self.batch_thread.error.connect(self._on_batch_error)
        self.batch_thread.finished.connect(self._on_batch_thread_finished)
        self.batch_thread.start()

    def _on_stop_batch(self):
        """终止批量处理"""
        if hasattr(self, 'batch_thread') and self.batch_thread.isRunning():
            self.batch_thread.stop()
            self.stopBatchBtn.setEnabled(False)
            self.stopBatchBtn.setText("正在终止...")
            self.progressLabel.setText("正在终止批量处理，请稍候...")
            InfoBar.warning(
                title="批量处理终止中",
                content="当前文件处理完毕后将停止，后续文件不再处理",
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT,
                duration=3000
            )

    def _on_batch_thread_finished(self):
        if hasattr(self, 'batch_thread') and self.batch_thread and not self.batch_thread.isRunning():
            self.batch_thread = None

    def _on_batch_progress(self, current, total, msg):
        pct = int((current - 1) / total * 100)
        self.progressBar.setValue(pct)
        self.progressLabel.setText(msg)
        self.progressLabel.show()
        self._save_session_state(stage="file_processing", current_file_index=max(0, current - 1))

    def _on_batch_file_done(self, current, total, src_path, export_path):
        pct = int(current / total * 100)
        self.progressBar.setValue(pct)
        name = os.path.basename(src_path)
        self.progressLabel.setText(f"[{current}/{total}] 完成: {name}")
        export_dir = os.path.basename(os.path.dirname(export_path))
        content = f"已导出至 {export_dir}/"
        if hasattr(self, 'batch_thread') and self.batch_thread:
            summary_path = getattr(self.batch_thread, 'batch_summary_path', '')
            if summary_path:
                content += f"\n批量摘要: {os.path.basename(summary_path)}"
        InfoBar.success(
            title=f"文件 {current}/{total} 完成",
            content=content,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=3000
        )

    def _on_batch_all_done(self, success, total):
        self.nextBtn.setEnabled(True)
        self.nextBtn.setText("开始处理")
        self.stopBatchBtn.hide()
        self.stopBatchBtn.setEnabled(True)
        self.stopBatchBtn.setText("终止批量")
        self.progressBar.setValue(100)
        label = f"批量处理完成：{success}/{total} 个文件成功"
        if success < total:
            label = f"批量处理结束：{success}/{total} 个文件成功（{total-success} 个被终止或失败）"
        self.progressLabel.setText(label)

        summary_hint = ""
        last_export_dir = ""
        if hasattr(self, 'batch_thread') and self.batch_thread:
            summary_path = getattr(self.batch_thread, 'batch_summary_path', '')
            if summary_path:
                summary_hint = f"\n批量摘要：{os.path.basename(summary_path)}"
            batch_reports = getattr(self.batch_thread, 'batch_reports', []) or []
            for report in reversed(batch_reports):
                export_path = report.get("export", "")
                if report.get("status") == "success" and export_path:
                    last_export_dir = os.path.dirname(export_path) or export_path
                    break

        content = (
            f"成功处理 {success}/{total} 个文件，字幕已导出至各文件旁的「_导出」文件夹"
            f"\n每个导出目录均包含质量报告.txt"
            f"{summary_hint}"
        )

        InfoBar.success(
            title="批量处理完成",
            content=content,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT,
            duration=5000
        )

        if last_export_dir and os.path.isdir(last_export_dir):
            try:
                os.startfile(last_export_dir)
            except Exception:
                logging.exception("[SettingTaskInterface] Failed to open final batch export dir: %s", last_export_dir)

    def _on_batch_error(self, msg):
        self.nextBtn.setEnabled(True)
        self.nextBtn.setText("开始处理")
        self.stopBatchBtn.hide()
        self.stopBatchBtn.setEnabled(True)
        self.stopBatchBtn.setText("终止批量")
        InfoBar.error(
            title="批量处理出错",
            content=msg,
            parent=self,
            position=InfoBarPosition.BOTTOM_RIGHT
        )

    def _on_select_glossary(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择术语表文件",
            "",
            "Text Files (*.txt)"
        )
        if file_path:
            cfg.glossaryPath.value = file_path
            cfg.save()
            
            filename = os.path.basename(file_path)
            self.glossaryCard.setContent(filename)
            
            InfoBar.success(
                title="已选择术语表",
                content=filename,
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )

    def _open_glossary_example(self):
        example_path = os.path.abspath("glossary_example.txt")
        if not os.path.exists(example_path):
             try:
                 with open(example_path, "w", encoding="utf-8") as f:
                     f.write("# 术语表示例文件\n原文 = 译文\nQuantum Mechanics = 量子力学")
             except Exception:
                 logging.exception("[SettingTaskInterface] 创建术语表示例失败")
             
        try:
            os.startfile(example_path)
        except Exception as e:
            InfoBar.error(
                title="无法打开文件",
                content=str(e),
                parent=self,
                position=InfoBarPosition.BOTTOM_RIGHT
            )
