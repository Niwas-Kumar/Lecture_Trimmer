"""Personal launcher: supply the owner's existing Groq key to the desktop app.

Reads the private project-root .env; this launcher is not a release file.
Close an already running desktop app before launching with a different provider.
"""
import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify Groq access without uploading audio')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    key = (dotenv_values(project / '.env').get('GROQ_API_KEY') or '').strip()
    if not key:
        raise SystemExit('Set GROQ_API_KEY in the private project .env first.')
    executable = Path(os.environ['LOCALAPPDATA']) / 'Programs/LectureTrimmer/LectureTrimmer.exe'
    if not executable.is_file():
        raise SystemExit('The Lecture Trimmer desktop installation was not found.')
    if args.check:
        from groq import Groq
        try:
            with Groq(api_key=key, timeout=20, max_retries=0) as client:
                models = {item.id for item in client.models.list().data}
            if 'whisper-large-v3-turbo' not in models:
                raise SystemExit('Groq connected, but Whisper Turbo is unavailable.')
        except Exception as exc:
            raise SystemExit(f'Groq connection failed ({type(exc).__name__}). No audio was uploaded.') from None
        print('Groq connected; whisper-large-v3-turbo is available. No audio was uploaded.')
        return
    environment = os.environ.copy()
    environment['GROQ_API_KEY'] = key
    environment['PW_ASR_BACKEND'] = 'groq'  # read by installed copies released before speech became Groq-only
    data = Path(os.getenv('TRIMMER_DATA_DIR') or Path(os.environ['LOCALAPPDATA']) / 'LectureTrimmer')
    mutex_name = 'Local\\LectureTrimmer-' + hashlib.sha256(str(data).encode()).hexdigest()[:20]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenMutexW(0x00100000, False, mutex_name)
    if handle:
        kernel.CloseHandle(handle)
        raise SystemExit('Close the existing Lecture Trimmer window, then open this Groq launcher again.')
    if ctypes.get_last_error() != 2:
        raise SystemExit('Could not check the running app. Close Lecture Trimmer before trying again.')
    subprocess.Popen([str(executable)], env=environment, cwd=str(executable.parent),
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                     close_fds=True)
    print('Opened Lecture Trimmer with Groq Whisper Turbo.')


if __name__ == '__main__':
    try:
        main()
    except SystemExit as exc:
        # A desktop shortcut uses pythonw, which has no console for these messages.
        import sys
        if isinstance(exc.code, str) and sys.stderr is None:
            ctypes.windll.user32.MessageBoxW(None, exc.code, 'Lecture Trimmer', 0x40)
        raise
