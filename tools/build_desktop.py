"""Build the desktop runtime and application release from an explicit file allowlist."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_runtime.desktop_update import canonical, version_tuple
from tools.release_manifest import APP_FILES, source_for

def signing_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    private = ROOT / '.release-private' / 'signing-key.bin'
    if private.exists():
        key = Ed25519PrivateKey.from_private_bytes(private.read_bytes())
    else:
        private.parent.mkdir(exist_ok=True)
        key = Ed25519PrivateKey.generate()
        private.write_bytes(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return key, public

def payload(version, archive_url=''):
    version_tuple(version)
    key, public = signing_key()
    release_dir = ROOT / 'releases' / version
    release_dir.mkdir(parents=True, exist_ok=True)
    archive = release_dir / f'LectureTrimmer-{version}-update.zip'
    files = {}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in APP_FILES:
            data = (ROOT / source_for(name)).read_bytes()
            files[name] = hashlib.sha256(data).hexdigest()
            bundle.writestr(name, data)
    release = {'version':version,'runtime':'1','files':files,
               'archive': {'url':archive_url, 'size':archive.stat().st_size,
                           'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}}
    envelope = {'release': release, 'signature': base64.b64encode(key.sign(canonical(release))).decode()}
    (release_dir / 'latest.json').write_text(json.dumps(envelope, indent=2), encoding='utf-8')
    return release_dir, envelope, public

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='1.0.0')
    parser.add_argument('--archive-url', default='')
    parser.add_argument('--manifest-url', default='')
    parser.add_argument('--release-only', action='store_true')
    parser.add_argument('--runtime-only', action='store_true', help='Build the runtime for smoke testing without creating the installer')
    parser.add_argument('--package-only', action='store_true', help='Package an already smoke-tested runtime')
    parser.add_argument('--publish-existing', action='store_true', help='Sign an uploaded ZIP link without rebuilding that ZIP')
    args = parser.parse_args()
    if args.publish_existing:
        from desktop_runtime.desktop_update import download_url
        version_tuple(args.version)
        download_url(args.archive_url)
        release_dir = ROOT / 'releases' / args.version
        envelope = json.loads((release_dir / 'latest.json').read_text())
        archive = release_dir / f'LectureTrimmer-{args.version}-update.zip'
        if hashlib.sha256(archive.read_bytes()).hexdigest() != envelope['release']['archive']['sha256']:
            raise RuntimeError('The ZIP changed after the release was prepared. Re-upload it before publishing.')
        envelope['release']['archive']['url'] = args.archive_url
        key, _ = signing_key()
        envelope['signature'] = base64.b64encode(key.sign(canonical(envelope['release']))).decode()
        (release_dir / 'latest.json').write_text(json.dumps(envelope, indent=2), encoding='utf-8')
        print('Signed channel ready:', release_dir / 'latest.json')
        return
    release_dir, envelope, public = payload(args.version, args.archive_url)
    if args.release_only:
        print(release_dir)
        return
    # A fresh resource tree prevents removed modules from an older layout being
    # silently bundled again. Retain it for build diagnostics and generated specs.
    (ROOT / 'build').mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='desktop-resources-', dir=ROOT / 'build'))
    application = staging / 'application'
    application.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        (application / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source_for(name), application / name)
    (staging / 'release-config.json').write_text(json.dumps({'version':args.version,'runtime':'1',
         'public_key':public, 'manifest_url':args.manifest_url,
         'drive_folder':'https://drive.google.com/drive/folders/1d7fGM93tAx2YuUCjU5-RmxbyLxtr_zBT'}, indent=2))
    oauth = list(ROOT.glob('client_secret*.json'))
    if len(oauth) != 1:
        raise RuntimeError('Exactly one Desktop OAuth client file is required.')
    data = json.loads(oauth[0].read_text())
    installed = data.get('installed', {})
    if not installed.get('client_id') or not installed.get('client_secret') or 'private_key' in data:
        raise RuntimeError('Only installed-app OAuth configuration may be packaged.')
    # Installed OAuth clients are public clients. Never include service-account credentials or .env.
    (staging / 'desktop-oauth.json').write_text(json.dumps({'installed': installed}))
    # The owner's Groq key, by the owner's decision: installer resources only, never the
    # signed update ZIP (APP_FILES) or source control. See lecture_trimmer/runtime/bundled_keys.py.
    from dotenv import dotenv_values
    from lecture_trimmer.runtime import bundled_keys
    groq_key = (os.getenv('GROQ_API_KEY') or dotenv_values(ROOT / '.env').get('GROQ_API_KEY') or '').strip()
    if not groq_key.startswith('gsk_'):
        raise RuntimeError('Put the Groq key to ship (GROQ_API_KEY=gsk_...) in .env before building the installer.')
    (staging / bundled_keys.FILENAME).write_text(bundled_keys.encode(groq_key), encoding='ascii')
    binaries = staging / 'bin'
    binaries.mkdir(exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        candidates = list((Path.home() / 'AppData/Local/Microsoft/WinGet/Packages').glob('Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe'))
        if not candidates:
            raise RuntimeError('Install FFmpeg before building.')
        ffmpeg = str(candidates[0])
    ffroot = Path(ffmpeg).parent
    for name in ('ffmpeg.exe','ffprobe.exe'):
        shutil.copy2(ffroot / name, binaries / name)
    licenses = staging / 'licenses'
    licenses.mkdir(exist_ok=True)
    for filename in ('LICENSE','LICENSE.txt','LICENSE.md'):
        if (ffroot.parent / filename).exists():
            shutil.copy2(ffroot.parent / filename, licenses / ('FFmpeg-' + filename))
    (licenses / 'THIRD_PARTY.txt').write_text('FFmpeg Windows build: https://www.gyan.dev/ffmpeg/builds/\n'
        'FFmpeg license and corresponding source: https://ffmpeg.org/legal.html and https://ffmpeg.org/download.html\n'
        'Python: https://docs.python.org/3/license.html\n'
        'FastAPI and Starlette: MIT https://github.com/fastapi/fastapi\n'
        'React: MIT https://github.com/facebook/react\n'
        'Third-party package licenses are retained with their distribution metadata.\n')
    cmd = [sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onedir','--name','LectureTrimmer',
           '--specpath', str(ROOT / 'build'),
           '--icon', str(ROOT / 'assets/lecture-trimmer.ico'),
           '--add-data',f'{staging};.','--paths',str(ROOT),
           '--hidden-import','desktop_runtime.dependencies',
           '--collect-submodules','uvicorn','--collect-submodules','google.genai',
           '--exclude-module','matplotlib','--exclude-module','IPython','--exclude-module','pytest',
           str(ROOT / 'tools' / 'desktop_launcher.py')]
    if not args.package_only:
        subprocess.run(cmd, cwd=ROOT, check=True)
    if args.runtime_only:
        return
    portable = release_dir / f'LectureTrimmer-{args.version}-Windows.zip'
    with zipfile.ZipFile(portable,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as bundle:
        for path in (ROOT / 'dist' / 'LectureTrimmer').rglob('*'):
            if path.is_file():
                bundle.write(path, 'LectureTrimmer/' + path.relative_to(ROOT / 'dist' / 'LectureTrimmer').as_posix())
    # A single installer EXE extracts the portable runtime once. Code updates skip that runtime.
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onefile',
                    '--name','LectureTrimmer-Setup', '--specpath', str(ROOT / 'build'), '--icon',str(ROOT / 'assets/lecture-trimmer.ico'), '--add-data',f'{portable};payload',
                    str(ROOT / 'tools' / 'desktop_installer.py')], cwd=ROOT, check=True)
    shutil.copy2(ROOT / 'dist' / 'LectureTrimmer-Setup.exe', release_dir / f'LectureTrimmer-{args.version}-Setup.exe')
    print('Release created:', release_dir)

if __name__ == '__main__':
    main()
