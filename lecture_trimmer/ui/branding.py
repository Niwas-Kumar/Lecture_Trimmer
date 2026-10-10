"""Lecture Trimmer's book-and-play identity, shared by login and workspace."""
from lecture_trimmer.paths import ASSET_ROOT

ASSETS = ASSET_ROOT
ICON_PATH = ASSETS / 'lecture-trimmer.ico'


def logo():
    return (ASSETS / 'lecture-trimmer.svg').read_text(encoding='utf-8')
