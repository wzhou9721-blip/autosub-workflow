from __future__ import annotations

from typing import Dict

WECHAT_COLORS: Dict[str, str] = {
    "primary": "#07C160",
    "primary_hover": "#14D36D",
    "primary_pressed": "#05A850",
    "primary_soft": "#123C2B",
    "info": "#5AA7FF",
    "info_soft": "#1F2C3A",
    "warning": "#D8A338",
    "warning_soft": "#3C311D",
    "danger": "#FA5151",
    "danger_soft": "#3D2424",
    "bg": "#1A1A1C",
    "surface": "#2B2C2F",
    "surface_alt": "#363739",
    "surface_hover": "#414244",
    "border": "#3A3B3D",
    "border_strong": "#4B4D50",
    "text": "#EDEDED",
    "text_secondary": "#A7A9AD",
    "text_muted": "#7F8389",
    "text_on_primary": "#07140C",
}

# Backwards-compatible alias for older imports while the visual system moves
# from the previous Duolingo-inspired palette to the WeChat dark palette.
DUO_COLORS = WECHAT_COLORS


def duo_primary_button_style() -> str:
    return f"""
PrimaryPushButton {{
    background-color: {DUO_COLORS['primary']};
    border: 1px solid {DUO_COLORS['primary']};
    border-radius: 6px;
    color: {DUO_COLORS['text_on_primary']};
    font-weight: 600;
    padding: 7px 18px;
}}
PrimaryPushButton:hover {{
    background-color: {DUO_COLORS['primary_hover']};
    border-color: {DUO_COLORS['primary']};
}}
PrimaryPushButton:pressed {{
    background-color: {DUO_COLORS['primary_pressed']};
    border-color: {DUO_COLORS['primary_pressed']};
}}
PrimaryPushButton:disabled {{
    background-color: #2A2C2E;
    border-color: #303236;
    color: #60646A;
}}
"""


def duo_secondary_button_style() -> str:
    return f"""
PushButton {{
    background-color: {DUO_COLORS['surface']};
    border: 1px solid {DUO_COLORS['border']};
    border-radius: 6px;
    color: {DUO_COLORS['text']};
    font-weight: 500;
    padding: 7px 16px;
}}
PushButton:hover {{
    background-color: {DUO_COLORS['surface_hover']};
    border-color: {DUO_COLORS['border_strong']};
}}
PushButton:pressed {{
    background-color: #252629;
    border-color: #323438;
}}
PushButton:disabled {{
    color: #5A5E64;
    border-color: #303236;
}}
"""


def duo_page_style(root_selector: str) -> str:
    """Build unified static WeChat-like dark page style."""
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
    border-radius: 8px;
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
    border-radius: 6px;
    color: {DUO_COLORS['text']};
    selection-background-color: {DUO_COLORS['primary']};
    selection-color: {DUO_COLORS['text_on_primary']};
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
    border-radius: 8px;
    gridline-color: {DUO_COLORS['border']};
}}
{root_selector} QHeaderView::section {{
    background-color: {DUO_COLORS['surface_alt']};
    border: none;
    color: {DUO_COLORS['text_secondary']};
    font-weight: 700;
    padding: 8px 10px;
}}
{root_selector} ProgressBar {{
    border-radius: 4px;
    background-color: #303236;
}}
"""


def duo_badge_style(kind: str = "info") -> str:
    color_map = {
        "info": (DUO_COLORS["info"], DUO_COLORS["info_soft"]),
        "warning": (DUO_COLORS["warning"], DUO_COLORS["warning_soft"]),
        "danger": (DUO_COLORS["danger"], DUO_COLORS["danger_soft"]),
        "success": (DUO_COLORS["primary"], DUO_COLORS["primary_soft"]),
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
    background-color: {DUO_COLORS['surface_hover']};
}}
NavigationWidget[isSelected='true'] {{
    background-color: {DUO_COLORS['primary']};
}}
QStackedWidget {{
    background-color: transparent;
}}
"""


def apply_duolingo_global_style(app) -> None:
    """Apply global static WeChat-like palette tokens to QApplication."""
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
