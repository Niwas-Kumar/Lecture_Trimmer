"""Configuration for the lecture trimmer."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from desktop_runtime.desktop_paths import DATA_ROOT, RESOURCE_ROOT, FROZEN
from lecture_trimmer.paths import APPLICATION_ROOT

if not FROZEN:
    load_dotenv(APPLICATION_ROOT / ".env")
_bundled_bin = RESOURCE_ROOT / "bin"
if _bundled_bin.is_dir():
    os.environ["PATH"] = str(_bundled_bin) + os.pathsep + os.environ.get("PATH", "")

# === Ensure FFmpeg is in PATH (winget installs to a non-standard location) ===
if not shutil.which("ffmpeg"):
    _winget = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    for _p in _winget.rglob("ffmpeg.exe"):
        os.environ["PATH"] = str(_p.parent) + os.pathsep + os.environ.get("PATH", "")
        break

# === Paths ===
PROJECT_ROOT = APPLICATION_ROOT
OUTPUT_DIR = DATA_ROOT / "output"
TEMP_DIR = DATA_ROOT / "temp"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# === API keys ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Installed copies fall back to the key the owner packaged into the installer.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
if not GROQ_API_KEY and FROZEN:
    from lecture_trimmer.runtime.bundled_keys import read as _read_bundled_key
    GROQ_API_KEY = _read_bundled_key(RESOURCE_ROOT)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# === Audio ===
AUDIO_SAMPLE_RATE = 16000
ASR_CHUNK_SECONDS = 120  # bounded uploads; completed cloud chunks survive retries

# === Speech recognition (Groq Whisper) ===
GROQ_ASR_MODEL = "whisper-large-v3-turbo"
GROQ_PARALLEL_REQUESTS = 2          # avoid saturating slower upload connections
GROQ_MAX_RETRIES = 3               # explicit retries; SDK retries are disabled

# === Classification ===
# "auto" uses Claude if ANTHROPIC_API_KEY is set, otherwise Gemini if GOOGLE_API_KEY is set.
CLASSIFIER_BACKEND = os.getenv("CLASSIFIER_BACKEND", "auto")   # auto | claude | gemini

CLAUDE_MODEL = "claude-opus-5-5"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")  # semantic quality; fewer whole-activity requests
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
GEMINI_PARALLEL_REQUESTS = 3        # free tier is ~10-15 requests/min; backoff handles 429
GEMINI_MAX_RETRIES = 5
UNIT_SECONDS = 8           # evidence units; editing is on complete teaching activities
WINDOW_SECONDS = 600       # each API call sees a 10-minute window of units...
WINDOW_OVERLAP_SECONDS = 60  # ...overlapping the previous one so context isn't lost
CLASSIFY_PARALLEL_REQUESTS = 8
SPEECH_SNAP_SECONDS = 3.0  # move cut points up to this far to land on a sentence boundary

# === Trimming modes ===
# Both modes independently remove nonacademic speech, including inside exercises.
# Both protect academic reinforcement; concise mode permits shorter unrelated interruptions.
MODES = {
    "balanced": dict(
        # ~4 s evidence units: short enough that filler phrases are separate units.
        unit_seconds=4,
        keep_labels={"core_teaching", "worked_example", "doubt_solving", "low_density_teaching"},
        min_gap_to_cut=2.0, min_keep_island=0.0, edge_padding=0.15,
        # Dead air longer than pause_min is shortened to pause_keep seconds.
        pause_min=3.0, pause_keep=1.2,
    ),
    "aggressive": dict(
        unit_seconds=4,
        keep_labels={"core_teaching", "worked_example", "doubt_solving"},
        min_gap_to_cut=1.0, min_keep_island=0.0, edge_padding=0.1,
        pause_min=2.0, pause_keep=0.8,
    ),
}
# Put teaching illustrations in the questions video as well as the theory video.
ILLUSTRATIONS_IN_QUESTIONS = os.getenv("TRIMMER_ILLUSTRATIONS_IN_QUESTIONS", "").lower() in {"1", "true", "yes"}
# Background from the other category (e.g. a derivation needed by a question) is
# copied into a single-category video only when it is at most this long; longer
# background stays in its own video and the review panel says where to find it.
MAX_CONTEXT_SECONDS = float(os.getenv("TRIMMER_MAX_CONTEXT_SECONDS", "180"))
# Batch processing: lectures analysed by the AI at the same time, and the total
# number of AI requests in flight across all of them (keeps PW within its limits).
PARALLEL_ANALYSES = max(1, int(os.getenv("TRIMMER_PARALLEL_ANALYSES", "3")))
AI_MAX_CONCURRENT_REQUESTS = max(1, int(os.getenv("TRIMMER_AI_MAX_CONCURRENT_REQUESTS", "6")))
# Set to 0 to keep every pause exactly as recorded.
TIGHTEN_PAUSES = os.getenv("TRIMMER_TIGHTEN_PAUSES", "1").lower() not in {"0", "false", "no"}
DEFAULT_MODE = os.getenv("TRIM_MODE", "balanced")

# Legacy helper defaults. The shared pipeline passes mode settings per run.
KEEP_LABELS = MODES[DEFAULT_MODE]["keep_labels"]

# === Edit-list smoothing ===
MIN_GAP_TO_CUT = MODES[DEFAULT_MODE]["min_gap_to_cut"]    # removed stretches shorter than this are kept
MIN_KEEP_ISLAND = MODES[DEFAULT_MODE]["min_keep_island"]  # kept stretches shorter than this are dropped
EDGE_PADDING = MODES[DEFAULT_MODE]["edge_padding"]        # seconds added before/after every kept range
