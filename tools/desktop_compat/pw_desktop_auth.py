"""Compatibility entry point for existing desktop runtimes; see lecture_trimmer.auth.pw_desktop_auth."""
import sys
import lecture_trimmer.auth.pw_desktop_auth as _implementation

sys.modules[__name__] = _implementation
