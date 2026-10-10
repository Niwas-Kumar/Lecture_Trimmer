"""Compatibility entry point for existing desktop runtimes; see lecture_trimmer.ui.app."""
import sys
import lecture_trimmer.ui.app as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
