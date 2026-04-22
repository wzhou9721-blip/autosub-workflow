from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLineEdit, QWidget, QVBoxLayout, QHBoxLayout
from qfluentwidgets import (
    SettingCard, LineEdit, SpinBox, DoubleSpinBox, qconfig,
    CardWidget, SegmentedWidget, Slider, BodyLabel, CaptionLabel
)
from app.common.config import cfg

class PasswordLineEdit(LineEdit):
    """ 禁止复制和上下文菜单的密码输入框 """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def copy(self):
        """ 重写复制函数，禁止复制 """
        pass

    def keyPressEvent(self, event):
        """ 禁止 Ctrl+C 等快捷键 """
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_C:
            return
        super().keyPressEvent(event)

class LineEditSettingCard(SettingCard):
    """ 带输入框的设置卡片 """

    def __init__(self, configItem, icon, title, content=None, parent=None):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        
        self.lineEdit = LineEdit(self)
        self.lineEdit.setText(str(configItem.value))
        self.lineEdit.setFixedWidth(300)
        self.lineEdit.setClearButtonEnabled(True)
        
        # 将输入框添加到右侧布局
        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 连接信号
        self.lineEdit.editingFinished.connect(self._onValueChanged)
        self.lineEdit.textChanged.connect(self._onTextChanged)
        configItem.valueChanged.connect(self.setValue)

    def _onTextChanged(self, text):
        """ 实时更新内存值，防止窗口直接关闭时未保存 """
        self.configItem.value = text

    def _onValueChanged(self):
        self.configItem.value = self.lineEdit.text()
        qconfig.save()

    def setValue(self, text):
        if self.lineEdit.text() != str(text):
            self.lineEdit.setText(str(text))

class PasswordSettingCard(SettingCard):
    """ 密码设置卡片 """
    def __init__(self, configItem, icon, title, content=None, parent=None):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        
        # 使用自定义的 PasswordLineEdit
        self.lineEdit = PasswordLineEdit(self)
        self.lineEdit.setText(str(configItem.value))
        self.lineEdit.setFixedWidth(300)
        self.lineEdit.setClearButtonEnabled(True)
        
        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 连接信号
        self.lineEdit.editingFinished.connect(self._onValueChanged)
        self.lineEdit.textChanged.connect(self._onTextChanged)
        configItem.valueChanged.connect(self.setValue)

    def _onTextChanged(self, text):
        self.configItem.value = text

    def _onValueChanged(self):
        self.configItem.value = self.lineEdit.text()
        qconfig.save()

    def setValue(self, text):
        if self.lineEdit.text() != str(text):
            self.lineEdit.setText(str(text))

class NumberSettingCard(SettingCard):
    """ 带数字微调框的设置卡片 """

    def __init__(self, configItem, icon, title, content=None, parent=None, range=(1, 100)):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        
        self.spinBox = SpinBox(self)
        self.spinBox.setRange(*range)
        self.spinBox.setValue(int(configItem.value))
        self.spinBox.setFixedWidth(160)
        
        # 将输入框添加到右侧布局
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 连接信号
        self.spinBox.valueChanged.connect(self.__onValueChanged)
        configItem.valueChanged.connect(self.setValue)

    def __onValueChanged(self, value):
        self.configItem.value = value
        qconfig.save()

    def setValue(self, value):
        if self.spinBox.value() != int(value):
            self.spinBox.setValue(int(value))

class DoubleNumberSettingCard(SettingCard):
    """ 带浮点数微调框的设置卡片 """

    def __init__(self, configItem, icon, title, content=None, parent=None, range=(0.0, 1.0), step=0.1, decimals=2):
        super().__init__(icon, title, content, parent)
        self.configItem = configItem
        
        self.spinBox = DoubleSpinBox(self)
        self.spinBox.setRange(*range)
        self.spinBox.setSingleStep(step)
        self.spinBox.setDecimals(decimals)
        self.spinBox.setValue(float(configItem.value))
        self.spinBox.setFixedWidth(160)
        
        # 将输入框添加到右侧布局
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 连接信号
        self.spinBox.valueChanged.connect(self.__onValueChanged)
        configItem.valueChanged.connect(self.setValue)

    def __onValueChanged(self, value):
        self.configItem.value = value
        qconfig.save()

    def setValue(self, value):
        if abs(self.spinBox.value() - float(value)) > 0.0001:
            self.spinBox.setValue(float(value))


class NoWheelSlider(Slider):
    """忽略鼠标滚轮，避免滚动页面时误改滑块数值。"""

    def wheelEvent(self, event):
        event.ignore()


class CalibrationPanel(CardWidget):
    """ 字幕溢出检测参数面板（模式切换 + 字号 + 最大不换行字数）"""

    max_count_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._init_ui()
        self._connect_signals()
        self._load_initial_state()

    def _init_ui(self):
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(20, 10, 20, 10)
        self.h_layout.setSpacing(20)

        # 1. 模式选择
        self.mode_container = QWidget()
        self.mode_layout = QVBoxLayout(self.mode_container)
        self.mode_label = BodyLabel("模式选择", self)
        self.mode_switch = SegmentedWidget(self)
        self.mode_switch.addItem("PR模式", "PR模式")
        self.mode_switch.addItem("剪映模式", "剪映模式")
        self.mode_layout.addWidget(self.mode_label)
        self.mode_layout.addWidget(self.mode_switch)

        # 2. 字号大小
        self.font_container = QWidget()
        self.font_layout = QVBoxLayout(self.font_container)
        self.font_header_layout = QHBoxLayout()
        self.font_label = BodyLabel("字号大小", self)
        self.font_value_label = CaptionLabel("60", self)
        self.font_header_layout.addWidget(self.font_label)
        self.font_header_layout.addStretch(1)
        self.font_header_layout.addWidget(self.font_value_label)
        self.font_slider = NoWheelSlider(Qt.Orientation.Horizontal, self)
        self.font_slider.setRange(1, 100)
        self.font_slider.setFixedWidth(200)
        self.font_layout.addLayout(self.font_header_layout)
        self.font_layout.addWidget(self.font_slider)

        # 3. 最大不换行字数（溢出阈值）
        self.threshold_container = QWidget()
        self.threshold_layout = QVBoxLayout(self.threshold_container)
        self.threshold_label = BodyLabel("最大不换行字数", self)
        self.threshold_spin = SpinBox(self)
        self.threshold_spin.setRange(1, 2000)
        self.threshold_spin.setFixedWidth(160)
        self.threshold_layout.addWidget(self.threshold_label)
        self.threshold_layout.addWidget(self.threshold_spin)

        self.h_layout.addWidget(self.mode_container)
        self.h_layout.addWidget(self.font_container)
        self.h_layout.addWidget(self.threshold_container)
        self.h_layout.addStretch(1)

    def _connect_signals(self):
        self.mode_switch.currentItemChanged.connect(self._on_mode_changed)
        self.font_slider.valueChanged.connect(self._on_font_size_changed)
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)

    def _load_initial_state(self):
        current_mode = cfg.overflow_mode.value
        self.mode_switch.blockSignals(True)
        self.mode_switch.setCurrentItem(current_mode)
        self.mode_switch.blockSignals(False)
        if current_mode == "剪映模式":
            self.font_slider.setRange(1, 20)
        else:
            self.font_slider.setRange(1, 100)
        self.font_slider.setValue(cfg.font_size.value)
        self.threshold_spin.setValue(cfg.max_line_count.value)
        self.font_value_label.setText(str(cfg.font_size.value))

    def _on_mode_changed(self, mode_name):
        cfg.overflow_mode.value = mode_name
        if mode_name == "PR模式":
            self.font_slider.setRange(1, 100)
            new_font, new_max = 60, 23
        else:
            self.font_slider.setRange(1, 20)
            new_font, new_max = 8, 16
        self.font_slider.setValue(new_font)
        self.threshold_spin.setValue(new_max)

    def _on_font_size_changed(self, value):
        cfg.font_size.value = value
        self.font_value_label.setText(str(value))
        mode = self.mode_switch.currentItem().text()
        if value > 0:
            factor = 1380 if mode == "PR模式" else 128
            self.threshold_spin.setValue(int(factor / value))

    def _on_threshold_changed(self, value):
        cfg.max_line_count.value = value
        self.max_count_changed.emit(value)
