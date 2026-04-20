from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QToolButton, QVBoxLayout, QWidget


class CollapsibleSettingSection(QWidget):
    """Simple collapsible wrapper for a settings block."""

    def __init__(self, title: str, content: QWidget, parent=None, expanded: bool = True):
        super().__init__(parent)
        self._content = content

        self.toggleButton = QToolButton(self)
        self.toggleButton.setText(title)
        self.toggleButton.setCheckable(True)
        self.toggleButton.setChecked(expanded)
        self.toggleButton.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggleButton.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggleButton.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.toggleButton.clicked.connect(self._onToggled)
        self.toggleButton.setStyleSheet(
            """
QToolButton {
    border: none;
    padding: 6px 2px;
    font-size: 16px;
    font-weight: 700;
    text-align: left;
}
QToolButton:hover {
    color: #3FA266;
}
"""
        )

        self.contentFrame = QFrame(self)
        self.contentFrame.setObjectName("contentFrame")
        self.contentLayout = QVBoxLayout(self.contentFrame)
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(0)
        self.contentLayout.addWidget(content)
        self.contentFrame.setVisible(expanded)

        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(0, 0, 0, 0)
        self.mainLayout.setSpacing(6)
        self.mainLayout.addWidget(self.toggleButton)
        self.mainLayout.addWidget(self.contentFrame)

    def _onToggled(self, checked: bool) -> None:
        self.toggleButton.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.contentFrame.setVisible(checked)

    def setExpanded(self, expanded: bool) -> None:
        self.toggleButton.setChecked(expanded)
        self._onToggled(expanded)

