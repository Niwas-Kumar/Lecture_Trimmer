"""Locations of application files, independent of the process working directory."""
from pathlib import Path

# Both source checkouts and signed desktop payloads contain this package at root.
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = APPLICATION_ROOT / "assets"
