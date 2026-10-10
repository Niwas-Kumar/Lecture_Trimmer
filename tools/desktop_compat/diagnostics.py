"""Compatibility entry point for existing desktop runtimes; see lecture_trimmer.runtime.diagnostics."""
import sys
import lecture_trimmer.runtime.diagnostics as _implementation

sys.modules[__name__] = _implementation
