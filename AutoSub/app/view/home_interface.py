import sys
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt
from qfluentwidgets import SubtitleLabel, setFont

class HomeInterface(QFrame):
    def __init__(self, text: str, parent=None):
        super().__init__(parent=parent)
        self.setObjectName(text.replace(' ', '-'))
        self.vBoxLayout = QVBoxLayout(self)
        self.label = SubtitleLabel(text, self)
        
        setFont(self.label, 24)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vBoxLayout.addWidget(self.label, 0, Qt.AlignmentFlag.AlignCenter)
