import sys
import os
import logging
import traceback

# Ensure the current directory is in sys.path so 'app' can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QColor
from qfluentwidgets import setTheme, setThemeColor, Theme
from app.view.main_window import MainWindow
from app.common.config import MODEL_PATH, BIN_PATH, RES_ROOT, APP_ROOT
from app.common.runtime_log import install_runtime_log_capture

# ── 全局崩溃日志配置 ────────────────────────────────────────────────────────
_LOG_FILE = APP_ROOT / "error.log"
logging.basicConfig(
    filename=str(_LOG_FILE),
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    encoding="utf-8"
)
install_runtime_log_capture()

def _global_exception_handler(exc_type, exc_value, exc_tb):
    """捕获所有未处理的异常并写入 error.log，同时保持控制台输出正常"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logging.critical("未捕获的异常:\n%s", tb_str)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _global_exception_handler


def apply_theme_color_brightness_patch():
    """修复 qfluentwidgets 暗色模式下强制把主题色亮度拉满的问题。
    原始代码在 isDarkTheme() 时执行 v = 1，导致任何颜色都变得极亮。
    此补丁保留原始颜色的亮度值，只做饱和度微调。
    """
    try:
        from qfluentwidgets.common.style_sheet import ThemeColor
        from qfluentwidgets.common.config import qconfig
        from qfluentwidgets.common.style_sheet import isDarkTheme

        def _color_no_force_bright(self):
            color = qconfig.get(qconfig._cfg.themeColor)
            h, s, v, _ = color.getHsvF()

            if isDarkTheme():
                # 不强制 v=1，保留原始亮度，只做轻微饱和度调整
                s *= 0.84
                if self == self.DARK_1:
                    v *= 0.9
                elif self == self.DARK_2:
                    s *= 0.977
                    v *= 0.82
                elif self == self.DARK_3:
                    s *= 0.95
                    v *= 0.7
                elif self == self.LIGHT_1:
                    s *= 0.92
                    v = min(v * 1.1, 1)
                elif self == self.LIGHT_2:
                    s *= 0.78
                    v = min(v * 1.15, 1)
                elif self == self.LIGHT_3:
                    s *= 0.65
                    v = min(v * 1.2, 1)
            else:
                if self == self.DARK_1:
                    v *= 0.75
                elif self == self.DARK_2:
                    s *= 1.05
                    v *= 0.5
                elif self == self.DARK_3:
                    s *= 1.1
                    v *= 0.4
                elif self == self.LIGHT_1:
                    v *= 1.05
                elif self == self.LIGHT_2:
                    s *= 0.75
                    v *= 1.05
                elif self == self.LIGHT_3:
                    s *= 0.65
                    v *= 1.05

            return QColor.fromHsvF(h, min(s, 1), min(v, 1))

        ThemeColor.color = _color_no_force_bright
    except Exception:
        pass


def apply_flat_no_rectangle_patch():
    """移除矩形底板，仅保留淡灰色下沿细线"""
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPainter, QColor
        from qfluentwidgets.components.settings.setting_card import SettingCard
        from qfluentwidgets.components.widgets.card_widget import CardWidget

        def _bottom_gray_line_paint(self, e):
            painter = QPainter(self)
            painter.setRenderHints(QPainter.RenderHint.Antialiasing)

            w, h = self.width(), self.height()
            if w <= 16 or h <= 4:
                return

            painter.setPen(QColor(138, 138, 138, 58))
            painter.drawLine(8, h - 2, w - 8, h - 2)

        SettingCard.paintEvent = _bottom_gray_line_paint
        CardWidget.paintEvent = _bottom_gray_line_paint
    except Exception:
        # 兜底：若运行环境模块路径不同，则保持原样不崩溃
        pass


def apply_switch_textless_patch():
    """隐藏开关右侧 On/Off 文本，并将开关把手固定为白色"""
    try:
        from PyQt6.QtGui import QColor
        from qfluentwidgets.components.widgets.switch_button import SwitchButton, Indicator

        def _update_textless(self):
            self._text = ''
            self.label.setText('')
            self.label.hide()
            self.setSpacing(0)
            self.adjustSize()

        def _slider_white(self):
            return QColor(255, 255, 255)

        SwitchButton._updateText = _update_textless
        Indicator._sliderColor = _slider_white
    except Exception:
        pass


def apply_label_color_patch():
    """Monkey-patch qfluentwidgets 的 Label 组件，让文字颜色在暗色模式下柔和。
    框架的 FluentLabelBase.setTextColor() 默认 dark=QColor(255,255,255)（纯白），
    此补丁拦截该方法，将过亮的暗色模式颜色替换为柔和色。
    """
    try:
        from PyQt6.QtGui import QColor
        from qfluentwidgets.components.widgets.label import FluentLabelBase

        _SOFT_WHITE = QColor("#9BA1AB")  # 替代纯白

        _orig_setTextColor = FluentLabelBase.setTextColor

        def _patched_setTextColor(self, light=QColor(0, 0, 0), dark=QColor(255, 255, 255)):
            dark = QColor(dark)
            # 如果暗色模式颜色太亮（接近纯白），替换为柔和色
            if dark.lightness() > 200:
                dark = _SOFT_WHITE
            _orig_setTextColor(self, light, dark)

        FluentLabelBase.setTextColor = _patched_setTextColor
    except Exception:
        pass


def apply_primary_button_icon_patch():
    """Monkey-patch PrimaryPushButton._drawIcon，让图标在暗色背景下使用浅色。
    框架默认假设 PrimaryPushButton 背景是亮色（主题色），所以用暗色图标。
    我们把背景改成了深灰，所以图标应该用浅色（Theme.DARK）。
    """
    try:
        from qfluentwidgets.components.widgets.button import PrimaryPushButton, PushButton
        from qfluentwidgets.common.icon import FluentIconBase, isDarkTheme, Theme
        from PyQt6.QtGui import QIcon

        def _patched_drawIcon(self, icon, painter, rect, state=QIcon.State.Off):
            if isinstance(icon, FluentIconBase) and self.isEnabled():
                # 暗色模式下用浅色图标（Theme.DARK = 白色图标）
                icon = icon.icon(Theme.DARK if isDarkTheme() else Theme.LIGHT)
            elif not self.isEnabled():
                painter.setOpacity(0.4)
                if isinstance(icon, FluentIconBase):
                    icon = icon.icon(Theme.DARK)
            PushButton._drawIcon(self, icon, painter, rect, state)

        PrimaryPushButton._drawIcon = _patched_drawIcon
    except Exception:
        pass


def apply_segmented_widget_patch():
    """去掉 SegmentedWidget 选中项的圆角矩形背景，只保留底部指示条。"""
    try:
        from PyQt6.QtCore import Qt, QRectF
        from PyQt6.QtGui import QPainter
        from PyQt6.QtWidgets import QWidget
        from qfluentwidgets.components.navigation.segmented_widget import SegmentedWidget
        from qfluentwidgets.common.color import autoFallbackThemeColor

        def _patched_paintEvent(self, e):
            QWidget.paintEvent(self, e)
            if not self.currentItem():
                return

            painter = QPainter(self)
            painter.setRenderHints(QPainter.RenderHint.Antialiasing)

            # 跳过 drawRoundedRect 背景，只画底部指示条
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(autoFallbackThemeColor(self.lightIndicatorColor, self.darkIndicatorColor))

            x = int(self.currentItem().width() / 2 - 8 + self.slideAni.value())
            painter.drawRoundedRect(QRectF(x, self.height() - 3.5, 16, 3), 1.5, 1.5)

        SegmentedWidget.paintEvent = _patched_paintEvent
    except Exception:
        pass


def apply_qss_override_patch():
    """Monkey-patch qfluentwidgets 的 renderQss，在框架每次渲染 QSS 时
    追加我们的覆盖规则。这样组件级样式也会包含我们的颜色覆盖，
    解决全局 QSS 被组件级 QSS 覆盖的问题。
    """
    try:
        import qfluentwidgets.common.style_sheet as ss_mod

        _OVERRIDE_QSS = """
/* ── 需要灰色背景的按钮（不包含 Transparent* / ToolButton / SegmentedItem） ── */
PrimaryPushButton,
PushButton,
QPushButton,
QPushButton#primaryButton,
DropDownPushButton,
PrimaryDropDownPushButton {
    background-color: #3A3A3A;
    border: 1px solid #505050;
    color: #D0D4DC;
    border-radius: 6px;
}
PrimaryPushButton:hover,
PushButton:hover,
QPushButton:hover,
QPushButton#primaryButton:hover,
DropDownPushButton:hover,
PrimaryDropDownPushButton:hover {
    background-color: #454545;
    border-color: #606060;
}
PrimaryPushButton:pressed,
PushButton:pressed,
QPushButton:pressed,
QPushButton#primaryButton:pressed,
DropDownPushButton:pressed,
PrimaryDropDownPushButton:pressed {
    background-color: #2E2E2E;
    border-color: #404040;
}
PrimaryPushButton:disabled,
PushButton:disabled,
QPushButton:disabled,
QPushButton#primaryButton:disabled,
DropDownPushButton:disabled,
PrimaryDropDownPushButton:disabled {
    background-color: #252525;
    border-color: #333333;
    color: #484848;
}
/* ── SegmentedWidget / PivotItem 恢复透明，不受 PushButton 规则影响 ── */
SegmentedItem,
SegmentedItem:hover,
SegmentedItem:pressed,
SegmentedItem[isSelected=true],
SegmentedItem[isSelected=false],
PivotItem,
PivotItem:hover,
PivotItem:pressed {
    background-color: transparent;
    border: none;
}
SegmentedWidget,
SegmentedToolWidget {
    background-color: transparent;
    border: none;
}
/* ── SettingCard 内部文字颜色柔和化 ── */
SettingCard QLabel {
    color: #9BA1AB;
}
SettingCard QLabel#contentLabel {
    color: #6B7280;
}
SettingCardGroup QLabel {
    color: #9BA1AB;
}
"""

        _orig_renderQss = ss_mod.renderQss

        def _patched_renderQss(qss: str):
            result = _orig_renderQss(qss)
            return result + _OVERRIDE_QSS

        ss_mod.renderQss = _patched_renderQss
    except Exception:
        pass


def apply_pro_gray_black_style(app: QApplication):
    """全局高级灰黑风格：仅保留按钮形态，其余矩形容器弱化为无边框"""
    app.setStyleSheet("""
FluentWindow {
    background-color: #121212;
    border: 1px solid #505050;
}

FluentTitleBar, TitleBar, TitleBarBase {
    background-color: #1A1A1A;
}

QStackedWidget, QScrollArea, QWidget#scrollWidget {
    background-color: #121212;
    border: none;
}

QWidget {
    color: #8A9099;
}

NavigationInterface {
    background-color: #121212;
    border-right: none;
}

NavigationWidget {
    background-color: transparent;
    border: none;
    border-radius: 0px;
}

NavigationWidget:hover {
    background-color: transparent;
}

NavigationWidget[isSelected='true'] {
    background-color: transparent;
    border-left: 2px solid #3FA266;
}

CardWidget, SettingCard, SettingCardGroup,
TableView, QTableView, QListWidget, QListView, QTreeView,
QFrame#centerWidget, QHeaderView::section,
ProgressBar, QToolTip {
    background-color: transparent;
    border: none;
    border-radius: 0px;
    color: #8A9099;
}

QLineEdit, QTextEdit, QPlainTextEdit,
LineEdit, TextEdit,
ComboBox, SpinBox, DoubleSpinBox {
    background-color: #171717;
    border: 1px solid #2E3238;
    border-radius: 0px;
    color: #B8BDC6;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
LineEdit:focus, TextEdit:focus,
ComboBox:focus, SpinBox:focus, DoubleSpinBox:focus {
    border: 1px solid #3A4048;
}

SettingCard, CardWidget {
    margin-top: 1px;
    margin-bottom: 6px;
}

QHeaderView::section {
    background-color: transparent;
}

PrimaryPushButton, PushButton, QPushButton,
DropDownPushButton, PrimaryDropDownPushButton {
    border-radius: 6px;
    background-color: #3A3A3A;
    border: 1px solid #505050;
    color: #D0D4DC;
}
PrimaryPushButton:hover, PushButton:hover, QPushButton:hover,
DropDownPushButton:hover, PrimaryDropDownPushButton:hover {
    background-color: #454545;
    border-color: #606060;
}
PrimaryPushButton:pressed, PushButton:pressed, QPushButton:pressed,
DropDownPushButton:pressed, PrimaryDropDownPushButton:pressed {
    background-color: #2E2E2E;
    border-color: #404040;
}
PrimaryPushButton:disabled, PushButton:disabled, QPushButton:disabled,
DropDownPushButton:disabled, PrimaryDropDownPushButton:disabled {
    background-color: #252525;
    border-color: #333333;
    color: #484848;
}
""")


if __name__ == '__main__':
    # Ensure critical directories exist
    MODEL_PATH.mkdir(parents=True, exist_ok=True)
    BIN_PATH.mkdir(parents=True, exist_ok=True)

    # Enable High DPI scaling
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    os.environ["QT_SCALE_FACTOR"] = "1"

    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(RES_ROOT / 'logo.ico')))
    
    # Patch QSS 渲染，必须在 setTheme 之前，这样框架加载样式时就会包含覆盖规则
    apply_qss_override_patch()
    apply_label_color_patch()
    apply_primary_button_icon_patch()
    apply_segmented_widget_patch()

    # Force Dark Theme by default
    setTheme(Theme.DARK)
    apply_theme_color_brightness_patch()
    setThemeColor(QColor("#3FA266"))
    apply_flat_no_rectangle_patch()
    apply_switch_textless_patch()
    apply_pro_gray_black_style(app)

    w = MainWindow()
    w.setMicaEffectEnabled(False)
    w.setCustomBackgroundColor("#121212", "#121212")
    w.show()
    sys.exit(app.exec())
