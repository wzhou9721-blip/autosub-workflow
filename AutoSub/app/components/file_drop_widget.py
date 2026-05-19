from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QFileDialog
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QColor, QPainter, QPen
from qfluentwidgets import SubtitleLabel, BodyLabel, FluentIcon as FIF, IconWidget, isDarkTheme
from qfluentwidgets.common.icon import drawIcon, Theme, writeSvg


class _TintedIconWidget(IconWidget):
    """支持自定义着色的 IconWidget"""

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self.setIcon(icon)
        self._tintColor = None
        self._fluentIcon = icon

    def setTintColor(self, color: QColor):
        self._tintColor = color
        self.update()

    def paintEvent(self, e):
        if self._tintColor and hasattr(self._fluentIcon, 'path'):
            painter = QPainter(self)
            painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
            svg_str = writeSvg(self._fluentIcon.path(Theme.DARK), fill=self._tintColor.name())
            if svg_str:
                from PyQt6.QtSvg import QSvgRenderer
                from PyQt6.QtCore import QRectF
                renderer = QSvgRenderer(svg_str.encode())
                renderer.render(painter, QRectF(self.rect()))
            else:
                super().paintEvent(e)
        else:
            super().paintEvent(e)

class FileDropWidget(QFrame):
    """ 文件拖拽上传控件 """
    
    filesAdded = pyqtSignal(list)  # 发送添加的文件列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(200)
        self._has_file = False  # 是否已有文件，控制边框颜色
        
        # 布局
        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vBoxLayout.setSpacing(10)

        # 图标（默认柔和灰色，有文件后变绿色）
        self.iconWidget = _TintedIconWidget(FIF.FOLDER, self)
        self.iconWidget.setFixedSize(48, 48)
        self.iconWidget.setTintColor(QColor("#8A8F96"))  # 默认柔和灰
        
        self.defaultTitle = "拖拽文件到此处"
        self.defaultSubTitle = "支持视频和音频文件，或点击此处选择"
        
        self.titleLabel = SubtitleLabel(self.defaultTitle, self)
        
        self.subTitleLabel = BodyLabel(self.defaultSubTitle, self)
        self.subTitleLabel.setTextColor(QColor(150, 150, 150), QColor(150, 150, 150))

        self.vBoxLayout.addWidget(self.iconWidget, 0, Qt.AlignmentFlag.AlignCenter)
        self.vBoxLayout.addWidget(self.titleLabel, 0, Qt.AlignmentFlag.AlignCenter)
        self.vBoxLayout.addWidget(self.subTitleLabel, 0, Qt.AlignmentFlag.AlignCenter)

        # 设置鼠标样式
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, e):
        super().paintEvent(e)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 有文件时用绿色边框，否则用灰色
        if self._has_file:
            color = QColor("#07C160")      # 微信绿
        else:
            color = QColor("#4B4D50")      # 默认灰色

        pen = QPen(color)
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.SolidLine)
        
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.drawRoundedRect(rect, 10, 10)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.accept()
            # 拖拽进入时高亮效果（可选）
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        event.acceptProposedAction()
        urls = event.mimeData().urls()
        files = [u.toLocalFile() for u in urls if u.toLocalFile()]
        
        # 过滤文件类型（可选，这里先全部接收，逻辑层再过滤）
        if files:
            self.filesAdded.emit(files)

    def setHint(self, title, subtitle):
        self.titleLabel.setText(title)
        self.subTitleLabel.setText(subtitle)
        self._has_file = True
        self.iconWidget.setTintColor(QColor("#07C160"))  # 有文件 → 主题绿色
        self.update()

    def resetHint(self):
        self.setHint(self.defaultTitle, self.defaultSubTitle)
        self._has_file = False
        self.iconWidget.setTintColor(QColor("#8A8F96"))  # 恢复柔和灰
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.openFileDialog()

    def openFileDialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择文件",
            "",
            "Media Files (*.mp4 *.avi *.mkv *.mov *.mp3 *.wav *.flac *.m4a);;All Files (*.*)"
        )
        if file_path:
            self.filesAdded.emit([file_path])
