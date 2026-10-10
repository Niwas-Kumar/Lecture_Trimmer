"""Compatibility entry point for existing desktop runtimes; see lecture_trimmer.auth.access_control."""
import sys
import lecture_trimmer.auth.access_control as _implementation

sys.modules[__name__] = _implementation
