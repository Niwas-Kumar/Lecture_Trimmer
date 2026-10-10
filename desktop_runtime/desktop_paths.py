"""Separate replaceable application files from persistent desktop user data."""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
DATA_ROOT = Path(os.getenv("TRIMMER_DATA_DIR") or (
    Path(os.getenv("LOCALAPPDATA", Path.home())) / "LectureTrimmer" if FROZEN else SOURCE_ROOT))
LOG_DIR = DATA_ROOT / "logs"
VERSION = os.getenv("TRIMMER_APP_VERSION", "1.0.0")

