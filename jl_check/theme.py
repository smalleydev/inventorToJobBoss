"""
Light/dark palettes for the Fusion style.

Fusion has no built-in light or dark mode — both are hand-defined here as
QPalettes, modeled on a typical code editor's themes (near-white vs.
near-black backgrounds). Row status colors (red/yellow/orange) live in
bom_model.STATUS_COLORS, not here — they're hardcoded RGB chosen to read
acceptably against both palettes.
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication, theme: str) -> None:
    """Apply the named theme ('Light' or 'Dark') to the whole application.
    Anything unrecognized falls through to Light."""
    if theme == "Dark":
        app.setPalette(_dark_palette())
    else:
        app.setPalette(_light_palette())


def _dark_palette() -> QPalette:
    palette = QPalette()

    palette.setColor(QPalette.Window, QColor(45, 45, 45))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ToolTipBase, QColor(220, 220, 220))
    palette.setColor(QPalette.ToolTipText, QColor(45, 45, 45))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Link, QColor(100, 160, 220))
    palette.setColor(QPalette.Highlight, QColor(70, 110, 160))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))

    return palette


def _light_palette() -> QPalette:
    palette = QPalette()

    palette.setColor(QPalette.Window, QColor(246, 246, 246))
    palette.setColor(QPalette.WindowText, QColor(20, 20, 20))
    palette.setColor(QPalette.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.AlternateBase, QColor(240, 240, 240))
    palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
    palette.setColor(QPalette.ToolTipText, QColor(20, 20, 20))
    palette.setColor(QPalette.Text, QColor(20, 20, 20))
    palette.setColor(QPalette.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ButtonText, QColor(20, 20, 20))
    palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.Link, QColor(20, 90, 190))
    palette.setColor(QPalette.Highlight, QColor(60, 130, 220))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))

    return palette
