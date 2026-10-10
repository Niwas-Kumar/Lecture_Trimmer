"""Check a signed update with an existing EXE without changing its active data.

Example (run from the repository root):
    python tools/smoke_desktop.py --version 1.0.4
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop_runtime.desktop_update import atomic_json, stage_archive, version_tuple


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--exe', type=Path, default=(
        Path(os.environ.get('LOCALAPPDATA', Path.home()))
        / 'Programs' / 'LectureTrimmer' / 'LectureTrimmer.exe'
    ))
    args = parser.parse_args()
    version_tuple(args.version)
    executable = args.exe.resolve()
    if not executable.is_file():
        parser.error('Install the desktop runtime first, or provide --exe.')
    resource = executable.parent / '_internal'
    release = ROOT / 'releases' / args.version
    envelope = json.loads((release / 'latest.json').read_text(encoding='utf-8'))
    runtime = json.loads((resource / 'release-config.json').read_text(encoding='utf-8'))

    # Retain this isolated folder and its logs so a failed startup is diagnosable.
    (ROOT / 'temp').mkdir(exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix='desktop-smoke-', dir=ROOT / 'temp'))
    stage_archive(release / f'LectureTrimmer-{args.version}-update.zip',
                  envelope, runtime['public_key'], data / 'versions')
    atomic_json(data / 'installed-state.json', {
        'current': args.version, 'previous': 'bundled',
    })
    environment = os.environ.copy()
    for key in ('GROQ_API_KEY', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
                'SARVAM_API_KEY', 'ANTHROPIC_API_KEY'):
        environment.pop(key, None)
    environment['TRIMMER_DATA_DIR'] = str(data)
    result_path = data / 'smoke-result.json'
    completed = subprocess.run(
        [str(executable), '--smoke-test', str(result_path)],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        timeout=120,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if completed.returncode or not result_path.exists():
        raise RuntimeError(f'Desktop smoke test failed; inspect {data}')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if not result.get('passed') or result.get('version') != args.version:
        raise RuntimeError(f'Desktop returned an unexpected result; inspect {data}')
    print(json.dumps(result, indent=2))
    print(f'Isolated diagnostic files: {data}')


if __name__ == '__main__':
    main()
