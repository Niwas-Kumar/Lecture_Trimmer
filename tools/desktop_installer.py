"""Per-user, non-admin installation of the bundled runtime."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox
import zipfile

def main():
    root = tk.Tk()
    root.title('Install Lecture Trimmer')
    root.geometry('460x275')
    root.configure(bg='#f4f6fb')
    root.resizable(False, False)
    tk.Label(root, text='Lecture Trimmer', font=('Segoe UI', 24, 'bold'), bg='#f4f6fb', fg='#312e81').pack(pady=(28,12))
    tk.Label(root, text='Focused study videos, on your desktop.\nPython and FFmpeg are included.', font=('Segoe UI',11), bg='#f4f6fb').pack()
    status = tk.StringVar(value='Installs for your Windows account. No administrator access needed.')
    tk.Label(root, textvariable=status, font=('Segoe UI',9), bg='#f4f6fb', wraplength=400).pack(pady=18)
    result = []
    def install_worker():
        mutex = None
        kernel = None
        try:
            import ctypes
            import hashlib
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            data = Path(os.getenv('TRIMMER_DATA_DIR') or Path(os.environ['LOCALAPPDATA']) / 'LectureTrimmer')
            name = 'Local\\LectureTrimmer-' + hashlib.sha256(str(data).encode()).hexdigest()[:20]
            ctypes.set_last_error(0)
            mutex = kernel.CreateMutexW(None, False, name)
            if not mutex or ctypes.get_last_error() == 183:
                raise RuntimeError('Close the Lecture Trimmer desktop window before installing or repairing it.')
            base = Path(os.environ['LOCALAPPDATA']) / 'Programs'
            target = base / 'LectureTrimmer'
            if target.resolve().parent != base.resolve():
                raise RuntimeError('The installation location is invalid.')
            base.mkdir(parents=True, exist_ok=True)
            payload = next((Path(sys._MEIPASS) / 'payload').glob('*.zip'))
            # The archive is built into this installer from the build allowlist.
            staging = base / 'LectureTrimmer-installing'
            if staging.exists():
                raise RuntimeError('A previous installation is incomplete. Contact the tool owner.')
            staging.mkdir()
            try:
                with zipfile.ZipFile(payload) as bundle:
                    if shutil.disk_usage(base).free < sum(i.file_size for i in bundle.infolist()) + 500_000_000:
                        raise RuntimeError('Free up at least 2 GB of disk space and try again.')
                    for info in bundle.infolist():
                        destination = (staging / info.filename).resolve()
                        if not destination.is_relative_to(staging.resolve()):
                            raise RuntimeError('Invalid installation archive.')
                    bundle.extractall(staging)
                backup = None
                if target.exists():
                    import uuid
                    backup = base / ('LectureTrimmer-backup-' + uuid.uuid4().hex[:8])
                    target.rename(backup)
                try:
                    (staging / 'LectureTrimmer').rename(target)
                except OSError:
                    if backup and not target.exists():
                        backup.rename(target)
                    raise
            finally:
                if staging.exists() and staging.resolve().parent == base.resolve():
                    shutil.rmtree(staging)
            env = os.environ.copy()
            env['LECTURE_INSTALL_EXE'] = str(target / 'LectureTrimmer.exe')
            script = "$w=New-Object -ComObject WScript.Shell; foreach($p in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))) {$s=$w.CreateShortcut((Join-Path $p 'Lecture Trimmer.lnk')); $s.TargetPath=$env:LECTURE_INSTALL_EXE; $s.WorkingDirectory=Split-Path $env:LECTURE_INSTALL_EXE; $s.Save()}"
            subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script], env=env, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            result.append((True, str(target / 'LectureTrimmer.exe')))
        except Exception as exc:
            import json, traceback, uuid
            from datetime import datetime, timezone
            reference = uuid.uuid4().hex[:12].upper()
            try:
                logs = Path(os.environ['LOCALAPPDATA']) / 'LectureTrimmer/logs/errors'
                logs.mkdir(parents=True, exist_ok=True)
                (logs / f'{reference}.json').write_text(json.dumps({
                    'reference': reference, 'time_utc': datetime.now(timezone.utc).isoformat(),
                    'stage':'installer', 'type':type(exc).__name__, 'owner':None,
                    'trace':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                             for f in traceback.extract_tb(exc.__traceback__)]}), encoding='utf-8')
            except OSError:
                pass
            message = str(exc) if isinstance(exc, RuntimeError) else 'Setup could not finish. Please retry or contact the tool owner.'
            result.append((False, f'{message}\nReference: {reference}'))
        finally:
            if mutex and kernel:
                kernel.CloseHandle(ctypes.c_void_p(mutex))
    def poll():
        if not result:
            root.after(250,poll)
            return
        ok, value = result[0]
        if ok:
            status.set('Installed. Your desktop shortcut is ready.')
            button.configure(text='Open Lecture Trimmer', state='normal', command=lambda: (subprocess.Popen([value]), root.destroy()))
        else:
            status.set('Installation could not finish.')
            messagebox.showerror('Lecture Trimmer setup', value)
            button.configure(text='Close', state='normal', command=root.destroy)
    def start():
        button.configure(state='disabled')
        status.set('Installing… This can take a minute.')
        threading.Thread(target=install_worker,daemon=True).start()
        root.after(250,poll)
    button=tk.Button(root,text='Install Lecture Trimmer',command=start,bg='#4f46e5',fg='white',font=('Segoe UI',11,'bold'),relief='flat',padx=20,pady=10)
    button.pack()
    root.mainloop()

if __name__ == '__main__':
    main()
