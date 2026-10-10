"""Compatibility entry point for existing desktop runtimes; see lecture_trimmer.media.audio."""
import sys
import lecture_trimmer.media.audio as _implementation

sys.modules[__name__] = _implementation
