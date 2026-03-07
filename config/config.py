"""
config.py — Centralized configuration loaded from .env
Loads .env first; if missing, loads .env.example so one file is enough.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Project root (parent of config/)
_root = Path(__file__).resolve().parent.parent
# Load .env.example first (defaults), then .env overrides
load_dotenv(_root / ".env.example")
load_dotenv(_root / ".env", override=True)
# If required keys are still empty (e.g. .env had empty values), fill from example file
_required_env_keys = ("GROQ_API_KEY", "SMTP_EMAIL", "SMTP_PASSWORD")
_example_path = _root / ".env.example"
if _example_path.exists() and not all(os.getenv(k) for k in _required_env_keys):
    with open(_example_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                k = k.strip()
                if k in _required_env_keys and not os.getenv(k) and v.strip():
                    os.environ[k] = v.strip()
# Use current Groq model if .env still has decommissioned model id
_decommissioned_models = ("llama3-70b-8192", "llama3-8b-8192")
if os.getenv("GROQ_MODEL", "").strip() in _decommissioned_models:
    os.environ["GROQ_MODEL"] = "llama-3.3-70b-versatile"


class Config:
    # === GROQ LLM ===
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # === SMTP EMAIL ===
    SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_EMAIL: str = os.getenv("SMTP_EMAIL", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_TIMEOUT: float = float(os.getenv("SMTP_TIMEOUT", "90"))
    SMTP_USE_TLS: bool = os.getenv("SMTP_USE_TLS", "").strip().lower() in ("1", "true", "yes")

    # === IMAP EMAIL ===
    IMAP_HOST: str = os.getenv("IMAP_HOST", "imap.gmail.com")
    IMAP_PORT: int = int(os.getenv("IMAP_PORT", "993"))
    IMAP_EMAIL: str = os.getenv("IMAP_EMAIL", "")
    IMAP_PASSWORD: str = os.getenv("IMAP_PASSWORD", "")

    # === CALENDAR ===
    CALENDAR_LINK: str = os.getenv("CALENDAR_LINK", "https://calendly.com/your-link")

    # === DATABASE ===
    DB_PATH: str = os.getenv("DB_PATH", "leads.db")

    # === SYSTEM ===
    MAX_CONCURRENT_LEADS: int = int(os.getenv("MAX_CONCURRENT_LEADS", "5"))
    REPLY_CHECK_INTERVAL_MINUTES: int = int(os.getenv("REPLY_CHECK_INTERVAL_MINUTES", "5"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self):
        """Validate that required env vars are set for full workflow."""
        missing = []
        if not self.GROQ_API_KEY:
            missing.append("GROQ_API_KEY")
        if not self.SMTP_EMAIL:
            missing.append("SMTP_EMAIL")
        if not self.SMTP_PASSWORD:
            missing.append("SMTP_PASSWORD")
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                f"Set them in .env (or .env.example) in the project root."
            )


# Global config instance
config = Config()
