"""Stable desktop host services, separate from replaceable application code.

Runtime v1 executables loaded these services under their old top-level names.
Reuse those exact instances when an older EXE loads a new application payload,
so update locks, processing guards, paths and version state remain shared.
"""
import sys

if getattr(sys, 'frozen', False):
    for _name in ('desktop_paths', 'desktop_update'):
        if _name in sys.modules:
            sys.modules[f'{__name__}.{_name}'] = sys.modules[_name]
