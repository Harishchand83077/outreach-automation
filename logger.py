"""
utils/logger.py — Rich logging setup for the entire system
"""

import logging
import sys
from rich.logging import RichHandler
from rich.console import Console

console = Console()


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger with Rich formatting.
    All loggers share the same root config.
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = RichHandler(
            console=console,
            rich_tracebacks=True,
            show_time=True,
            show_path=False,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger


def set_log_level(level: str):
    """Set log level globally."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger().setLevel(numeric)
