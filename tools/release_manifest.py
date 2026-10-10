"""Explicit public-file allowlist for signed application updates.

Keep this list independent of build output, environment files and user data.
Tests verify that new application modules are included deliberately.
"""
from pathlib import Path

APP_FILES = [
    # Stable adapters used by existing frozen runtime version 1.
    "app.py",
    "audio.py",
    "access_control.py",
    "pw_desktop_auth.py",
    "diagnostics.py",
    # Supplied access kit stays replaceable independently of our integration.
    "vendor/pw_access.py",
    "desktop_runtime/__init__.py",
    "desktop_runtime/desktop_paths.py",
    "desktop_runtime/desktop_update.py",
    "lecture_trimmer/__init__.py",
    "lecture_trimmer/__main__.py",
    "lecture_trimmer/cli.py",
    "lecture_trimmer/config.py",
    "lecture_trimmer/models.py",
    "lecture_trimmer/paths.py",
    "lecture_trimmer/analysis/__init__.py",
    "lecture_trimmer/analysis/classifier.py",
    "lecture_trimmer/analysis/lesson_classifier.py",
    "lecture_trimmer/analysis/prompts.py",
    "lecture_trimmer/analysis/subjects.py",
    "lecture_trimmer/analysis/visual_evidence.py",
    "lecture_trimmer/auth/__init__.py",
    "lecture_trimmer/auth/access_control.py",
    "lecture_trimmer/auth/pw_desktop_auth.py",
    "lecture_trimmer/integrations/__init__.py",
    "lecture_trimmer/integrations/ai_usage.py",
    "lecture_trimmer/integrations/pw_diagnostics.py",
    "lecture_trimmer/integrations/pw_proxy.py",
    "lecture_trimmer/media/__init__.py",
    "lecture_trimmer/media/audio.py",
    "lecture_trimmer/media/cutter.py",
    "lecture_trimmer/media/transcriber.py",
    "lecture_trimmer/media/pauses.py",
    "lecture_trimmer/media/transcript_export.py",
    "lecture_trimmer/processing/__init__.py",
    "lecture_trimmer/processing/editor.py",
    "lecture_trimmer/processing/job_control.py",
    "lecture_trimmer/processing/lesson_planner.py",
    "lecture_trimmer/processing/pipeline.py",
    "lecture_trimmer/runtime/__init__.py",
    "lecture_trimmer/server/__init__.py",
    "lecture_trimmer/server/api.py",
    "lecture_trimmer/server/links.py",
    "lecture_trimmer/server/jobs.py",
    "lecture_trimmer/runtime/bundled_keys.py",
    "lecture_trimmer/runtime/diagnostics.py",
    "lecture_trimmer/runtime/messages.py",
    "lecture_trimmer/ui/__init__.py",
    "lecture_trimmer/ui/app.py",
    "lecture_trimmer/ui/branding.py",
    "lecture_trimmer/ui/review.py",
    "lecture_trimmer/ui/ui_style.py",
    "assets/lecture-trimmer.svg",
    "assets/lecture-trimmer.png",
    "assets/lecture-trimmer.ico",
]

# The built web interface (npm run build in frontend/) has hashed file names, so its
# files are listed from the build folder. Source in frontend/ is never shipped.
_WEB_ROOT = Path(__file__).resolve().parents[1]
APP_FILES += sorted(path.relative_to(_WEB_ROOT).as_posix()
                    for path in (_WEB_ROOT / "lecture_trimmer" / "web" / "dist").rglob("*") if path.is_file())

# The repository keeps adapters with build tools; old EXEs need them at the
# payload root. Archive paths and source locations are intentionally distinct.
PAYLOAD_SOURCES = {
    name: f"tools/desktop_compat/{name}"
    for name in ("audio.py", "access_control.py", "pw_desktop_auth.py", "diagnostics.py")
}


def source_for(name):
    return PAYLOAD_SOURCES.get(name, name)
