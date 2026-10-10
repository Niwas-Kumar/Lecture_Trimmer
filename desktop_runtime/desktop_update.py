"""Signed, application-only updates. The bundled Python/media runtime stays installed."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import uuid
import zipfile
from urllib.parse import parse_qs, urlsplit

from desktop_runtime.desktop_paths import DATA_ROOT, RESOURCE_ROOT, VERSION, FROZEN

RUNTIME_VERSION = '1'
_lock = threading.Lock()
_active = 0
_updating = False

class UpdateError(RuntimeError):
    pass

@contextmanager
def processing_guard():
    global _active
    with _lock:
        if _updating:
            raise UpdateError('An update is in progress. Please wait before starting a lecture.')
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1

def busy():
    with _lock:
        return bool(_active or _updating)

def request_restart():
    global _updating
    with _lock:
        if _active or _updating:
            return 'Finish the current lecture before restarting.'
        if not (DATA_ROOT / 'pending-update.json').exists():
            return 'No downloaded update is waiting.'
        _updating = True
        try:
            (DATA_ROOT / 'restart.request').write_text('restart', encoding='utf-8')
        except OSError:
            _updating = False
            raise
    return 'Restarting Lecture Trimmer. Your browser will reopen; sign in again to continue.'

def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', value):
        raise UpdateError('The update version is invalid.')
    return tuple(map(int, value.split('.')))

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()

def safe_path(name):
    p = PurePosixPath(name)
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or p.is_absolute()
            or any(part in ('', '.', '..') or part.rstrip(' .') != part for part in name.split('/'))
            or any(part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(10)],*[f'LPT{i}' for i in range(10)]} for part in p.parts)):
        raise UpdateError('The update contains an unsafe file path.')
    return p

def verify_manifest(envelope, public_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        release = envelope['release']
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
            base64.b64decode(envelope['signature'], validate=True), canonical(release))
    except Exception:
        raise UpdateError('The update signature could not be verified. Your installed app was not changed.') from None
    version_tuple(release.get('version'))
    if release.get('runtime') != RUNTIME_VERSION:
        raise UpdateError('This release needs a newer desktop runtime. Ask the owner for the new installer.')
    files = release.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 500 or 'app.py' not in files:
        raise UpdateError('The update file list is invalid.')
    seen = set()
    for name, digest in files.items():
        safe_path(name)
        if name.casefold() in seen or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise UpdateError('The update file list is invalid.')
        seen.add(name.casefold())
    return release

def read_release_config():
    path = RESOURCE_ROOT / 'release-config.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

def download_url(value):
    """Accept a Drive share link or a fixed HTTPS release endpoint."""
    p = urlsplit(value.strip())
    if p.scheme != 'https' or p.username or p.password or not p.hostname or p.port not in (None, 443):
        raise UpdateError('Use an HTTPS update-file link from the tool owner.')
    if p.hostname == 'drive.google.com':
        match = re.search(r'/file/d/([A-Za-z0-9_-]+)', p.path)
        file_id = match.group(1) if match else parse_qs(p.query).get('id', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', file_id):
            raise UpdateError('Use the shared latest.json file link, not the Drive folder link.')
        return f'https://drive.google.com/uc?export=download&id={file_id}'
    return value.strip()

def fetch(url, target, max_bytes):
    import requests
    # Validate every redirect, and allow only HTTPS. Downloads never execute directly.
    current = download_url(url)
    for _ in range(8):
        with requests.get(current, stream=True, timeout=(10, 45), allow_redirects=False) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                current = download_url(urljoin(current, response.headers['Location']))
                continue
            response.raise_for_status()
            size = 0
            with target.open('wb') as dst:
                for block in response.iter_content(256 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise UpdateError('The update download is larger than expected.')
                    dst.write(block)
            return
    raise UpdateError('The update link redirected too many times.')

def verify_directory(root, release):
    for name, digest in release['files'].items():
        path = root.joinpath(*safe_path(name).parts)
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise UpdateError('An update file is missing or damaged.')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
              and p.name != 'verified-release.json' and '__pycache__' not in p.parts}
    if actual != set(release['files']):
        raise UpdateError('The update contains unexpected files.')

def stage_archive(archive, envelope, public_key, versions_dir):
    release = verify_manifest(envelope, public_key)
    info = release.get('archive', {})
    if archive.stat().st_size != info.get('size') or hashlib.sha256(archive.read_bytes()).hexdigest() != info.get('sha256'):
        raise UpdateError('The update download is incomplete or damaged.')
    versions_dir.mkdir(parents=True, exist_ok=True)
    staging = versions_dir / ('.staging-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) != len(release['files']) or sum(m.file_size for m in members) > 50_000_000:
                raise UpdateError('The update archive is invalid.')
            seen = set()
            for member in members:
                path = safe_path(member.filename)
                if member.filename not in release['files'] or member.filename.casefold() in seen or stat.S_ISLNK(member.external_attr >> 16):
                    raise UpdateError('The update archive contains an unexpected file.')
                seen.add(member.filename.casefold())
                target = staging.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
        verify_directory(staging, release)
        (staging / 'verified-release.json').write_text(json.dumps(envelope), encoding='utf-8')
        destination = versions_dir / release['version']
        if destination.exists():
            verify_directory(destination, release)
        else:
            staging.rename(destination)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    staging.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(staging, path)

def check_and_stage(channel=''):
    global _updating
    if not FROZEN:
        return 'Updates are available in the installed desktop app. This copy runs from source.'
    with _lock:
        if _active or _updating:
            return 'Finish the current lecture before updating.'
        _updating = True
    try:
        cfg = read_release_config()
        settings_path = DATA_ROOT / 'update-settings.json'
        saved = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        url = channel.strip() or saved.get('manifest_url') or cfg.get('manifest_url')
        if not url:
            return 'The owner has not published the update link yet. Add the latest.json file link in Update settings.'
        work = DATA_ROOT / 'updates'
        work.mkdir(parents=True, exist_ok=True)
        manifest = work / 'download.json'
        fetch(url, manifest, 1_000_000)
        try:
            envelope = json.loads(manifest.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            raise UpdateError('The update file is not readable. Ask the owner to check its Drive sharing settings.') from None
        release = verify_manifest(envelope, cfg['public_key'])
        if channel.strip():
            atomic_json(settings_path, {'manifest_url': channel.strip()})
        if version_tuple(release['version']) <= version_tuple(VERSION):
            return f'You are up to date (v{VERSION}).'
        size = release['archive']['size']
        if not isinstance(size, int) or not 0 < size <= 50_000_000:
            raise UpdateError('The update download size is invalid.')
        archive = work / 'application.zip'
        fetch(release['archive']['url'], archive, size)
        stage_archive(archive, envelope, cfg['public_key'], DATA_ROOT / 'versions')
        atomic_json(DATA_ROOT / 'pending-update.json', {'version': release['version']})
        return f'Update v{release["version"]} is ready. Close Lecture Trimmer and reopen it to apply. Your videos and saved work are preserved.'
    finally:
        with _lock:
            _updating = False

