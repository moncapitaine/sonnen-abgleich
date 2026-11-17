"""Configuration from environment variables.

Secrets can be passed either directly (NAME) or as a file path (NAME_FILE),
which is how Docker Swarm secrets are mounted.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _secret(name: str, default: str = "") -> str:
    path = os.environ.get(name + "_FILE")
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "sonnen.sqlite"
# CSV exports dropped here are imported on startup and before every sync.
IMPORT_DIR = Path(os.environ.get("IMPORT_DIR", str(DATA_DIR / "import")))

# The house is in Germany; all day boundaries are local time there.
TZ = ZoneInfo(os.environ.get("SITE_TZ", "Europe/Berlin"))

# Shown in the page header, e.g. the address of the house.
SITE_NAME = os.environ.get("SITE_NAME", "")

SONNEN_USERNAME = _secret("SONNEN_USERNAME")
SONNEN_PASSWORD = _secret("SONNEN_PASSWORD")
SONNEN_SITE_ID = os.environ.get("SONNEN_SITE_ID", "").strip()

# Shared password for the web UI. Empty = no login (only do that behind a proxy with auth).
APP_PASSWORD = _secret("APP_PASSWORD")

SYNC_ENABLED = _flag("SYNC_ENABLED", True)
# Local time of the daily download, HH:MM.
SYNC_TIME = os.environ.get("SYNC_TIME", "04:20")
