"""Erasure and retention — what this system forgets, and when.

`ARCHITECTURE_TARGET.md` §17 and issue D6. Two operations, with one rule in common: **forgetting
somebody must never mean contacting them again.**
"""

from app.services.privacy.erasure import ErasureResult, erase_lead, hashed_email
from app.services.privacy.retention import RetentionReport, run_retention

__all__ = [
    "ErasureResult",
    "RetentionReport",
    "erase_lead",
    "hashed_email",
    "run_retention",
]
