"""Local, bounded diagnostics; exported reports exclude credentials and lecture content."""
from __future__ import annotations
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import threading
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from desktop_runtime.desktop_paths import DATA_ROOT, LOG_DIR, VERSION

_lock = threading.Lock()

def redact(text):
    text = str(text)
    for name, value in os.environ.items():
        if len(value) >= 8 and any(k in name.upper() for k in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            text = text.replace(value, "[redacted]")
    text = re.sub(r'(?i)(bearer\s+)[\w.+/=-]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)([?&](?:code|key|token|access_token|id_token|state)=)[^\s&]+', r'\1[redacted]', text)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b', '[account]', text)
    return text

class PrivateFormatter(logging.Formatter):
    def formatException(self, exc_info):
        # No locals, exception bodies, provider responses or source lines.
        frames = traceback.extract_tb(exc_info[2])
        return '\n'.join([f'{exc_info[0].__name__}: details retained as diagnostic metadata'] +
                         [f'  {Path(f.filename).name}:{f.lineno} in {f.name}' for f in frames])
    def format(self, record):
        return redact(super().format(record))

def configure_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("trimmer")
    if any(getattr(h, '_lecture_file', False) for h in logger.handlers):
        return
    handler = RotatingFileHandler(LOG_DIR / 'app.log', maxBytes=2_000_000, backupCount=4, encoding='utf-8')
    handler._lecture_file = True
    handler.setFormatter(PrivateFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def record_error(exc, *, stage='processing', owner=None):
    reference = uuid.uuid4().hex[:12].upper()
    item = {'reference': reference, 'time_utc': datetime.now(timezone.utc).isoformat(),
            'version': VERSION, 'stage': stage, 'type': type(exc).__name__,
            'platform': platform.system(), 'python': platform.python_version(), 'owner': owner,
            'trace': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                      for f in traceback.extract_tb(exc.__traceback__)]}
    # Fixed category/status are useful for diagnosing providers, without response bodies.
    category = getattr(exc, 'category', None)
    if isinstance(category, str) and re.fullmatch(r'[a-z_]{1,40}', category):
        item['category'] = category
    code = getattr(exc, 'code', None)
    if isinstance(code, int):
        item['http_status'] = code
    causes, current = [], exc
    for _ in range(5):
        if current is None:
            break
        cause = {'type': type(current).__name__}
        for attr in ('errno', 'winerror', 'returncode'):
            value = getattr(current, attr, None)
            if isinstance(value, int):
                cause[attr] = value
        # Map media/network failures to fixed useful reasons; never export raw bodies.
        text = str(current).lower()
        for needle, reason in (('non-monotonic', 'overlapping_media_timestamps'),
                               ('no space left', 'disk_full'), ('disk full', 'disk_full'),
                               ('no audio', 'no_audio_stream'), ('matches no streams', 'no_audio_stream'),
                               ('invalid data found', 'invalid_media'), ('timed out', 'timeout'),
                               ('no progress', 'encoder_stalled'), ('cuda', 'gpu_runtime'),
                               ('quota', 'provider_quota'), ('rate limit', 'provider_rate_limit')):
            if needle in text:
                cause['reason'] = reason
                break
        causes.append(cause)
        current = current.__cause__
    item['causes'] = causes
    root = LOG_DIR / 'errors'
    try:
        with _lock:
            root.mkdir(parents=True, exist_ok=True)
            (root / f'{reference}.json').write_text(json.dumps(item, indent=2), encoding='utf-8')
            old = sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[200:]
            for path in old:
                path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger('trimmer').error('Unable to save diagnostic %s', reference)
    logging.getLogger('trimmer').error('%s failed: %s [reference %s]', stage, type(exc).__name__, reference)
    return reference

def export_support(owner=None):
    """Only errors belonging to this authenticated user; no cross-account logs."""
    root = DATA_ROOT / 'support'
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'LectureTrimmer-support-{uuid.uuid4().hex[:12]}.zip'
    with _lock, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('system.json', json.dumps({'app_version': VERSION, 'system': platform.system(),
                                                   'architecture': platform.machine(), 'python': platform.python_version()}))
        for path in (LOG_DIR / 'errors').glob('*.json'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                if item.pop('owner', None) == owner:
                    bundle.writestr(f'errors/{path.name}', json.dumps(item, indent=2))
            except (OSError, ValueError):
                continue
    return target

