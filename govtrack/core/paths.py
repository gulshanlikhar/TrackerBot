"""
paths.py — Central path registry for GovTrack.

All file and directory paths are defined here so no other module
hard-codes locations. Import from this file wherever a path is needed.
"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
#DOCS_DIR = PROJECT_ROOT / "docs"

ENV_PATH = CONFIG_DIR / ".env"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
TOKEN_PATH = DATA_DIR / "token.json"
DATABASE_PATH = DATA_DIR / "govtrack.db"
