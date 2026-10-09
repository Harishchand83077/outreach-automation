"""Backwards-compatible shim.

Settings now live in `app.core.settings`, which is where the target layout puts them
(ARCHITECTURE_TARGET.md §4). The legacy flat modules at the repository root still import
`from config.config import config`, so this re-exports the same objects rather than defining a
second settings class — one source of truth while both layouts coexist.

This file is deleted along with the last legacy module that imports it.
"""

from app.core.settings import (
    FEATURE_KEYS,
    REQUIRED_KEYS,
    ConfigError,
    Settings,
    config,
    looks_like_placeholder,
)

__all__ = [
    "FEATURE_KEYS",
    "REQUIRED_KEYS",
    "ConfigError",
    "Settings",
    "config",
    "looks_like_placeholder",
]
