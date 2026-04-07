from __future__ import annotations

from typing import Dict

DUO_COLORS: Dict[str, str] = {
    "primary": "#3FA266",
    "primary_pressed": "#368C58",
    "primary_soft": "#E2F0E8",
    "info": "#1CB0F6",
    "info_soft": "#E7F7FF",
    "warning": "#FFC800",
    "warning_soft": "#FFF7D6",
    "danger": "#FF4B4B",
    "danger_soft": "#FFE7E7",
    "bg": "#F7F9F4",
    "surface": "#FFFFFF",
    "surface_alt": "#F1F6E9",
    "border": "#D9E5CC",
    "text": "#2B2D31",
    "text_secondary": "#667085",
    "text_on_primary": "#FFFFFF",
}


def duo_primary_button_style() -> str:
    return f"""
PrimaryPushButton {{
    background-color: {DUO_COLORS['primary']};
    border: 1px solid {DUO_COLORS['primary_pressed']};
    border-radius: 14px;
    color: {DUO_COLORS['text_on_primary']};
    font-weight: 700;
    padding: 7px 18px;
}}
PrimaryPushButton:hover {{
    background-color: #56B379;
    border-color: {DUO_COLORS['primary']};
}}
PrimaryPushButton:pressed {{
    background-color: {DUO_COLORS['primary_pressed']};
    border-color: {DUO_COLORS['primary_pressed']};
}}
PrimaryPushButton:disabled {{
    background-color: #9FCFB2;
    border-color: #9FCFB2;
    color: #F5FAEF;
}}
"""


def duo_secondary_button_style() -> str:
    return f"""
PushButton {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 14px;
    color: {DUO_COLORS['text']};
    font-weight: 600;
    padding: 7px 16px;
}}
PushButton:hover {{
    background-color: {DUO_COLORS['surface_alt']};
    border-color: #C7D8B3;
}}
PushButton:pressed {{
    background-color: #E8F2DD;
    border-color: #B7CBA0;
}}
PushButton:disabled {{
    color: #A6B49A;
    border-color: #DFE8D5;
}}
"""


def duo_page_style(root_selector: str) -> str:
    """Build unified static Duolingo-like page style."""
    return f"""
{root_selector} {{
    background-color: {DUO_COLORS['bg']};
    color: {DUO_COLORS['text']};
}}
{root_selector} #scrollWidget {{
    background-color: transparent;
}}
{root_selector} QFrame,
{root_selector} QWidget,
{root_selector} QLabel,
{root_selector} BodyLabel,
{root_selector} CaptionLabel,
{root_selector} TitleLabel,
{root_selector} SubtitleLabel,
{root_selector} StrongBodyLabel {{
    color: {DUO_COLORS['text_secondary']};
}}
{root_selector} TitleLabel,
{root_selector} SubtitleLabel,
{root_selector} StrongBodyLabel {{
    color: {DUO_COLORS['text']};
    font-weight: 700;
}}
{root_selector} CardWidget,
{root_selector} SettingCard,
{root_selector} SettingCardGroup {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 18px;
}}
{root_selector} QLineEdit,
{root_selector} LineEdit,
{root_selector} TextEdit,
{root_selector} QTextEdit,
{root_selector} ComboBox,
{root_selector} SpinBox,
{root_selector} DoubleSpinBox {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 12px;
    color: {DUO_COLORS['text']};
    selection-background-color: {DUO_COLORS['info']};
}}
{root_selector} QLineEdit:focus,
{root_selector} QTextEdit:focus,
{root_selector} ComboBox:focus,
{root_selector} SpinBox:focus,
{root_selector} DoubleSpinBox:focus {{
    border: 1px solid {DUO_COLORS['primary']};
}}
{root_selector} TableView,
{root_selector} QTableView {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 14px;
    gridline-color: #E7EEDC;
}}
{root_selector} QHeaderView::section {{
    background-color: {DUO_COLORS['surface_alt']};
    border: none;
    color: {DUO_COLORS['text_secondary']};
    font-weight: 700;
    padding: 8px 10px;
}}
{root_selector} ProgressBar {{
    border-radius: 6px;
    background-color: #E5EDD9;
}}
"""


def duo_badge_style(kind: str = "info") -> str:
    color_map = {
        "info": (DUO_COLORS["info"], DUO_COLORS["info_soft"]),
        "warning": ("#B87900", DUO_COLORS["warning_soft"]),
        "danger": ("#C63838", DUO_COLORS["danger_soft"]),
        "success": ("#3A8E5A", DUO_COLORS["primary_soft"]),
    }
    fg, bg = color_map.get(kind, (DUO_COLORS["text_secondary"], DUO_COLORS["surface_alt"]))
    return f"""
QLabel {{
    color: {fg};
    background-color: {bg};
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 2px 10px;
    font-weight: 700;
}}
"""


def duo_shell_style() -> str:
    return f"""
FluentWindow {{
    background-color: {DUO_COLORS['bg']};
}}
NavigationInterface {{
    background-color: {DUO_COLORS['surface']};
    border-right: 1px solid {DUO_COLORS['border']};
}}
NavigationWidget {{
    border-radius: 12px;
}}
NavigationWidget:hover {{
    background-color: {DUO_COLORS['surface_alt']};
}}
NavigationWidget[isSelected='true'] {{
    background-color: {DUO_COLORS['primary_soft']};
}}
QStackedWidget {{
    background-color: transparent;
}}
"""


def apply_duolingo_global_style(app) -> None:
    """Apply global static palette tokens to QApplication."""
    app.setStyleSheet(
        f"""
QWidget {{
    font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
    color: {DUO_COLORS['text']};
}}
QToolTip {{
    border: 1px solid {DUO_COLORS['border']};
    background-color: {DUO_COLORS['surface']};
    color: {DUO_COLORS['text']};
    padding: 4px 8px;
}}
"""
    )
