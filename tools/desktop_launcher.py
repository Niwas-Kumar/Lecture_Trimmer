"""Frozen desktop host. Keeps the runtime stable while signed application files update."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time
import webbrowser

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Build-time dependency discovery; application sources are in a separately signed payload.
if False:
    import desktop_runtime.dependencies

def roots():
    resource = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    data = Path(os.getenv('TRIMMER_DATA_DIR') or Path(os.getenv('LOCALAPPDATA', Path.home())) / 'LectureTrimmer')
    data.mkdir(parents=True, exist_ok=True)
    return resource, data

def select_payload(resource, data):
    from desktop_runtime.desktop_update import atomic_json, verify_directory, verify_manifest
    cfg = json.loads((resource / 'release-config.json').read_text())
    bundled = resource / 'application'
    statefile = data / 'installed-state.json'
    try:
        state = json.loads(statefile.read_text())
    except (OSError, ValueError):
        state = {'current': 'bundled', 'previous': 'bundled'}
    pending = data / 'pending-update.json'
    if pending.exists():
        version = json.loads(pending.read_text())['version']
        from desktop_runtime.desktop_update import version_tuple
        version_tuple(version)
        path = data / 'versions' / version
        envelope = json.loads((path / 'verified-release.json').read_text())
        release = verify_manifest(envelope, cfg['public_key'])
        verify_directory(path, release)
        state = {'current': version, 'previous': state['current'], 'trial': True}
        atomic_json(statefile, state)
        pending.unlink()
    if state.get('trial'):
        # The parent clears trial only after the new process responds successfully.
        pass
    chosen = state.get('current', 'bundled')
    if chosen == 'bundled':
        payload, version = bundled, cfg['version']
    else:
        from desktop_runtime.desktop_update import version_tuple
        version_tuple(chosen)
        payload, version = data / 'versions' / chosen, chosen
        envelope = json.loads((payload / 'verified-release.json').read_text())
        release = verify_manifest(envelope, cfg['public_key'])
        verify_directory(payload, release)
    return payload, version, state

def rollback(data):
    from desktop_runtime.desktop_update import atomic_json
    statefile = data / 'installed-state.json'
    try:
        state = json.loads(statefile.read_text())
        if state.get('trial') and state['current'] != state['previous']:
            atomic_json(statefile, {'current': state['previous'], 'previous': 'bundled'})
            return True
    except (OSError, ValueError, KeyError):
        pass
    return False

def configure(resource, data, payload, version):
    os.environ['TRIMMER_DATA_DIR'] = str(data)
    os.environ['TRIMMER_APP_VERSION'] = version
    os.environ['TRIMMER_AUTH'] = 'pw-desktop'
    os.environ['TRIMMER_DESKTOP_OAUTH_FILE'] = str(resource / 'desktop-oauth.json')
    os.environ['PATH'] = str(resource / 'bin') + os.pathsep + os.environ.get('PATH', '')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(payload))
    from desktop_runtime import desktop_paths, desktop_update
    desktop_paths.VERSION = version
    desktop_update.VERSION = version
    if os.name == 'nt':
        original_popen = subprocess.Popen
        class HiddenProcess(original_popen):
            def __init__(self, *args, **kwargs):
                kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
                super().__init__(*args, **kwargs)
        subprocess.Popen = HiddenProcess

def serve(port):
    resource, data = roots()
    payload, version, _ = select_payload(resource, data)
    configure(resource, data, payload, version)
    (data / 'logs').mkdir(exist_ok=True)
    stream = (data / 'logs' / 'desktop-startup.log').open('w', encoding='utf-8', buffering=1)
    sys.stdout = sys.stderr = stream
    sys.argv = ['app.py', '--auth', 'pw-desktop', '--host', '127.0.0.1', '--port', str(port), '--no-browser']
    try:
        runpy.run_path(str(payload / 'app.py'), run_name='__main__')
    except BaseException as exc:
        import importlib
        diag = importlib.import_module('diagnostics')
        diag.record_error(exc, stage='startup')
        raise

def smoke_test(target):
    resource, data = roots()
    payload, version, _ = select_payload(resource, data)
    configure(resource, data, payload, version)
    import importlib
    app = importlib.import_module('app')
    from pw_desktop_auth import DesktopSettings, make_desktop_server
    from fastapi.testclient import TestClient
    from lecture_trimmer.server.api import mount_web, signed_in_resolver
    from lecture_trimmer.server.jobs import JobStore, Scheduler
    settings = DesktopSettings.from_file(resource, 18763)
    server = make_desktop_server(settings)
    scheduler = Scheduler(JobStore(data / 'smoke-jobs'))
    mount_web(server, scheduler, resolve_user=signed_in_resolver(server.state.trimmer_auth),
              base_url=settings.base_url, mode='pw-desktop')
    with TestClient(server, base_url=settings.base_url) as client:
        home = client.get('/')
        assert home.status_code == 200 and 'Make time for learning' in home.text
        assert client.get('/api/me').status_code == 401
        assert client.get('/auth/login', follow_redirects=False).status_code == 303
        studio = client.get('/studio/')
        assert studio.status_code == 200 and '<div id="root">' in studio.text
    scheduler.shutdown()
    import shutil
    for binary in ('ffmpeg', 'ffprobe'):
        assert shutil.which(binary)
        subprocess.run([binary, '-version'], check=True, capture_output=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    import groq  # noqa: F401  speech recognition is Groq-only; the key is supplied per user, never bundled
    speech_check = 'Groq client import passed'
    from audio import extract_audio
    fixture = data / 'smoke-media.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                    '-f','lavfi','-i','color=c=blue:s=160x90:r=25',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                    '-t','1','-c:v','libx264','-c:a','aac',str(fixture)],check=True,capture_output=True)
    assert extract_audio(fixture,out_dir=data).is_file()
    # The owner's Groq key is packaged on purpose; no other provider key may be.
    assert app.config.GROQ_API_KEY.startswith('gsk_') and not app.config.GOOGLE_API_KEY
    Path(target).write_text(json.dumps({'passed': True, 'version': version, 'auth': 'required',
                                      'media_tools': 'AAC decode and conversion passed', 'speech_engine': speech_check,
                                      'provider_keys': 'owner Groq key packaged; no other keys'}), encoding='utf-8')

def desktop():
    import ctypes
    import tkinter as tk
    from tkinter import messagebox
    resource, data = roots()
    # One launcher per Windows user/data directory; prevents simultaneous upgrades.
    mutex_name = 'Local\\LectureTrimmer-' + hashlib.sha256(str(data).encode()).hexdigest()[:20]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, mutex_name)
    if ctypes.get_last_error() == 183:
        try:
            webbrowser.open((data / 'app-url.txt').read_text())
        except OSError:
            pass
        return
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    (data / 'app-url.txt').write_text(url)
    (data / 'restart.request').unlink(missing_ok=True)
    root = tk.Tk()
    root.title('Lecture Trimmer')
    try:
        payload, _, _ = select_payload(resource, data)
        root.iconbitmap(str(payload / 'assets' / 'lecture-trimmer.ico'))
    except (OSError, tk.TclError):
        pass
    root.geometry('440x300')
    root.resizable(False, False)
    root.configure(bg='#f4f6fb')
    tk.Label(root, text='▶  Lecture Trimmer', font=('Segoe UI', 21, 'bold'), bg='#f4f6fb', fg='#312e81').pack(pady=(28, 8))
    status = tk.StringVar(value='Starting your study workspace…')
    tk.Label(root, textvariable=status, font=('Segoe UI', 11), bg='#f4f6fb', wraplength=380).pack(pady=8)
    tk.Button(root, text='Open workspace', font=('Segoe UI', 11, 'bold'), bg='#4f46e5', fg='white', relief='flat',
              padx=22, pady=9, command=lambda: webbrowser.open(url)).pack(pady=10)
    tk.Label(root, text='Keep this window open while a lecture is processing.', font=('Segoe UI', 9), bg='#f4f6fb', fg='#64748b').pack(pady=8)
    child = None
    started = 0
    healthy = False
    rolled_back = False
    def launch():
        nonlocal child, started, healthy
        child = subprocess.Popen([sys.executable, '--serve', '--port', str(port)],
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        started, healthy = time.monotonic(), False
    def stop_child():
        if child and child.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
            else:
                child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    def close():
        if not messagebox.askyesno('Close Lecture Trimmer?', 'Closing stops any lecture that is still processing. Close the app?'):
            return
        stop_child()
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    def tick():
        nonlocal healthy, rolled_back
        if child.poll() is not None:
            if not healthy and not rolled_back and rollback(data):
                rolled_back = True
                status.set('The update could not start. Restoring the previous version…')
                launch()
            else:
                status.set('The app could not continue. Reopen it or send the saved support report to the owner.')
            root.after(2000, tick)
            return
        if not healthy:
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=.5) as response:
                    ready = response.status == 200 and b'Lecture Trimmer' in response.read(20000)
                if ready:
                    healthy = True
                    statepath = data / 'installed-state.json'
                    if statepath.exists():
                        from desktop_runtime.desktop_update import atomic_json
                        state = json.loads(statepath.read_text())
                        state.pop('trial', None)
                        atomic_json(statepath, state)
                    status.set('Your workspace is ready in your browser.')
                    webbrowser.open(url)
            except Exception:
                if time.monotonic() - started > 120:
                    stop_child()
        request = data / 'restart.request'
        if healthy and request.exists():
            request.unlink()
            status.set('Applying your update…')
            stop_child()
            launch()
        root.after(1500, tick)
    launch()
    root.after(1500, tick)
    root.mainloop()
    kernel.CloseHandle(ctypes.c_void_p(handle))

def main():
    import multiprocessing
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=7861)
    parser.add_argument('--smoke-test')
    args = parser.parse_args()
    if args.smoke_test:
        try:
            smoke_test(args.smoke_test)
        except BaseException as exc:
            import traceback
            Path(args.smoke_test).write_text(json.dumps({'passed': False, 'error': type(exc).__name__,
                                                        'trace': traceback.format_exc()}), encoding='utf-8')
            raise
    elif args.serve:
        serve(args.port)
    else:
        desktop()

if __name__ == '__main__':
    main()
