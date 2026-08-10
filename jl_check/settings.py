"""
Persistent app settings for JL Check, backed by QSettings — Qt's standard
mechanism for durable preferences (Windows registry under
HKCU\\Software\\JL\\JL Check; a config file on other platforms).

Settings survive across sessions and app updates because they're keyed by
organization/app name, not tied to the install location. New settings
should follow the same pattern: a KEY constant, a default, and a small
typed get/set pair — callers never touch QSettings directly.
"""

from PySide6.QtCore import QSettings

ORG_NAME = "JL"
APP_NAME = "JL Check"

THEME_KEY = "appearance/theme"
DEFAULT_THEME = "Dark"

VALID_THEMES = ("Light", "Dark")


def get_theme() -> str:
    """Return the saved theme name, falling back to the default if the
    stored value is missing or unrecognized."""
    settings = QSettings(ORG_NAME, APP_NAME)
    theme = settings.value(THEME_KEY, DEFAULT_THEME)
    return theme if theme in VALID_THEMES else DEFAULT_THEME


def set_theme(theme: str) -> None:
    """Persist the theme choice. Ignores unrecognized values rather than
    writing garbage that get_theme() would then have to fall back from."""
    if theme in VALID_THEMES:
        settings = QSettings(ORG_NAME, APP_NAME)
        settings.setValue(THEME_KEY, theme)
