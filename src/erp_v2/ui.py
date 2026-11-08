"""ERP's composition boundary for the shared Dotmac UI contract."""

from __future__ import annotations

from typing import Final

from dotmac_ui.assets import static_dir, stylesheet_url
from dotmac_ui.contract import THEME_ATTRIBUTE, UI_CONTRACT_VERSION
from dotmac_ui.theme import DEFAULT_THEME

UI_STYLESHEET_URL: Final[str] = stylesheet_url()
UI_THEME_ATTRIBUTE: Final[str] = THEME_ATTRIBUTE
UI_THEME: Final[str] = DEFAULT_THEME
UI_STATIC_DIR = static_dir()


def template_globals() -> dict[str, str | int]:
    return {
        "dotmac_ui_contract_version": UI_CONTRACT_VERSION,
        "dotmac_ui_stylesheet_url": UI_STYLESHEET_URL,
        "dotmac_ui_theme_attribute": UI_THEME_ATTRIBUTE,
        "dotmac_ui_theme": UI_THEME,
    }


__all__ = [
    "UI_STATIC_DIR",
    "UI_STYLESHEET_URL",
    "UI_THEME",
    "UI_THEME_ATTRIBUTE",
    "template_globals",
]
