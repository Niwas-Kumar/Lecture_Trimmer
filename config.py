"""Configuration for the lecture trimmer."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# === Ensure FFmpeg is in PATH (winget installs to a non-standard location) ===
if not shutil.which("ffmpeg"):
    _winget = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    for _p in _winget.rglob("ffmpeg.exe"):
        os.environ["PATH"] = str(_p.parent) + os.pathsep + os.environ.get("PATH", "")
        break

# === Paths ===
PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = PROJECT_ROOT / "output"
TEMP_DIR = PROJECT_ROOT / "temp"
OUTPUT_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

# === API keys ===
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# === Audio ===
AUDIO_SAMPLE_RATE = 16000
ASR_CHUNK_SECONDS = 600  # 10-minute chunks for the cloud ASR backend

# === Local ASR (faster-whisper) ===
# "auto" tries CUDA first and falls back to CPU if the CUDA libraries are missing.
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")
WHISPER_MODEL_GPU = "large-v3-turbo"  # fits in 4 GB VRAM with int8_float16
WHISPER_MODEL_CPU = "small"           # realistic on CPU; use "medium" if you can wait
WHISPER_BATCH_SIZE = 8

# === Cloud ASR (Groq) ===
GROQ_ASR_MODEL = "whisper-large-v3-turbo"
GROQ_PARALLEL_REQUESTS = 4          # free tier: 20 req/min, 2 h of audio per hour
GROQ_MAX_RETRIES = 6

# === Cloud ASR (Sarvam AI - best for Indian languages / Hinglish) ===
SARVAM_ASR_MODEL = "saaras:v4"      # or "saaras:v3"
SARVAM_ASR_MODE = None              # saaras:v3 only: "transcribe" | "codemix" | "verbatim" | "translit"
SARVAM_LANGUAGE = "unknown"         # auto-detect; or e.g. "hi-IN", "en-IN"
SARVAM_CHUNK_SECONDS = 30           # the synchronous endpoint is meant for short clips
SARVAM_PARALLEL_REQUESTS = 2        # free tier rate-limits quickly; raise if your plan allows
SARVAM_MAX_RETRIES = 6              # exponential backoff on 429 (2s, 4s, 8s ... max 30s)

# === Classification ===
# "auto" uses Claude if ANTHROPIC_API_KEY is set, otherwise Gemini if GOOGLE_API_KEY is set.
CLASSIFIER_BACKEND = os.getenv("CLASSIFIER_BACKEND", "auto")   # auto | claude | gemini

CLAUDE_MODEL = "claude-opus-5-5"

GEMINI_MODEL = "gemini-3.5-flash-lite"   # fast and reliable on the free tier; labels tested fine
GEMINI_FALLBACK_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash"]  # tried on 404 / repeated 503
GEMINI_PARALLEL_REQUESTS = 3        # free tier is ~10-15 requests/min; backoff handles 429
GEMINI_MAX_RETRIES = 5
UNIT_SECONDS = 45          # default unit length; MODES below override it per run
WINDOW_SECONDS = 600       # each API call sees a 10-minute window of units...
WINDOW_OVERLAP_SECONDS = 60  # ...overlapping the previous one so context isn't lost
CLASSIFY_PARALLEL_REQUESTS = 8
SPEECH_SNAP_SECONDS = 3.0  # move cut points up to this far to land on a sentence boundary

# === Trimming modes ===
# balanced:   keep everything that teaches, cut chatter/admin/tech/revision. Safe default.
# aggressive: keep only dense definitions, formulas, code and step-by-step solving;
#             also cuts Q&A, overviews, repetition. Targets 60-80% reduction.
MODES = {
    "balanced": dict(
        unit_seconds=45,
        keep_labels={"core_teaching", "worked_example", "doubt_solving", "low_density_teaching"},
        min_gap_to_cut=20.0, min_keep_island=30.0, edge_padding=1.5,
    ),
    "aggressive": dict(
        unit_seconds=20,
        keep_labels={"core_teaching", "worked_example"},
        min_gap_to_cut=8.0, min_keep_island=15.0, edge_padding=1.0,
    ),
}
DEFAULT_MODE = os.getenv("TRIM_MODE", "balanced")

# Defaults below mirror the balanced mode; the CLI/UI override them per run.
KEEP_LABELS = MODES[DEFAULT_MODE]["keep_labels"]

# === Edit-list smoothing ===
MIN_GAP_TO_CUT = MODES[DEFAULT_MODE]["min_gap_to_cut"]    # removed stretches shorter than this are kept
MIN_KEEP_ISLAND = MODES[DEFAULT_MODE]["min_keep_island"]  # kept stretches shorter than this are dropped
EDGE_PADDING = MODES[DEFAULT_MODE]["edge_padding"]        # seconds added before/after every kept range
