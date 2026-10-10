"""The owner's speech key, packaged into the desktop installer at build time.

The owner chose (10 Oct 2026) to ship their Groq key so colleagues need no setup.
It lives only in the installer's resources: never in source control and never in
the signed code-update ZIPs. The encoding keeps it out of plain-text secret
scanners; it is NOT protection, since anyone with the installer can recover it.
To rotate: revoke the key at console.groq.com, put the new one in .env and
build a new installer.
"""
import base64
from pathlib import Path

FILENAME = "speech-key.dat"
_MASK = b"lecture-trimmer/speech"


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(data))


def encode(key: str) -> str:
    return base64.b64encode(_xor(key.encode("utf-8"))).decode("ascii")


def decode(text: str) -> str:
    return _xor(base64.b64decode(text.strip())).decode("utf-8")


def read(resource_root) -> str:
    """The packaged key, or "" when this copy has none (source runs, older installers)."""
    try:
        return decode((Path(resource_root) / FILENAME).read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeError):
        return ""
