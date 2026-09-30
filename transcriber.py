"""Stage 2: speech-to-text with timestamps.

Two interchangeable backends:
  - "local": faster-whisper on this machine (GPU if CUDA libs are present, else CPU).
  - "groq":  Groq's hosted whisper-large-v3-turbo; audio is chunked and uploaded in
             parallel, so a 4-hour lecture transcribes in well under a minute.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config
from audio import chunk_audio, probe_duration
from models import Segment, Transcript

logger = logging.getLogger("trimmer")


# --------------------------------------------------------------------------- local
def _load_whisper():
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    attempts = []
    if config.WHISPER_DEVICE in ("auto", "cuda"):
        attempts.append(("cuda", "int8_float16", config.WHISPER_MODEL_GPU))
    if config.WHISPER_DEVICE in ("auto", "cpu"):
        attempts.append(("cpu", "int8", config.WHISPER_MODEL_CPU))

    last_err = None
    for device, compute, size in attempts:
        try:
            logger.info("Loading whisper '%s' on %s (%s)", size, device, compute)
            model = WhisperModel(size, device=device, compute_type=compute)
            # Force a tiny inference so missing CUDA libraries fail here, not mid-lecture.
            import numpy as np
            list(model.transcribe(np.zeros(16000, dtype=np.float32))[0])
            return BatchedInferencePipeline(model=model), device
        except Exception as e:  # noqa: BLE001 - any CUDA/library error means "try next device"
            logger.warning("Could not use %s: %s", device, str(e).splitlines()[0])
            last_err = e
    raise RuntimeError(f"No usable whisper device: {last_err}")


def _collapse_repeats(text: str, max_run: int = 2) -> str:
    """Whisper sometimes loops ("तो तो तो तो ..."); keep at most `max_run` repeats."""
    out: list[str] = []
    for w in text.split():
        if len(out) >= max_run and all(x == w for x in out[-max_run:]):
            continue
        out.append(w)
    return " ".join(out)


def transcribe_local(wav: Path, language: str | None = None) -> Transcript:
    pipeline, device = _load_whisper()
    segments_gen, info = pipeline.transcribe(
        str(wav),
        batch_size=config.WHISPER_BATCH_SIZE,
        language=language,
        vad_filter=True,
        beam_size=1,             # greedy is ~2x faster; accuracy is fine for topic detection
        condition_on_previous_text=False,
        repetition_penalty=1.2,  # both settings damp the repetition loops small models fall into
        no_repeat_ngram_size=3,
    )
    segments = [Segment(start=s.start, end=s.end, text=_collapse_repeats(s.text.strip()))
                for s in segments_gen if s.text.strip()]
    logger.info("Local ASR (%s): %d segments, language=%s", device, len(segments), info.language)
    return Transcript(language=info.language, duration=info.duration, segments=segments)


# ---------------------------------------------------------------------------- groq
def _groq_chunk(client, offset: float, path: Path, language: str | None) -> tuple[str, list[Segment]]:
    data = path.read_bytes()
    kwargs = dict(model=config.GROQ_ASR_MODEL, response_format="verbose_json",
                  timestamp_granularities=["segment"])
    if language:
        kwargs["language"] = language

    # Free tier: 20 requests/min and 7,200 audio-seconds/hour. Back off on 429 instead of failing.
    delay = 5.0
    for attempt in range(config.GROQ_MAX_RETRIES + 1):
        try:
            resp = client.audio.transcriptions.create(file=(path.name, data), **kwargs)
            break
        except Exception as e:  # noqa: BLE001 - only retry rate limits
            if getattr(e, "status_code", None) != 429 or attempt == config.GROQ_MAX_RETRIES:
                raise
            logger.warning("Groq 429 on %s, retry %d/%d in %.0fs", path.name, attempt + 1,
                           config.GROQ_MAX_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    segs = [
        Segment(start=offset + s["start"], end=offset + s["end"], text=s["text"].strip())
        for s in (resp.segments or [])
    ]
    return getattr(resp, "language", "") or "", segs


def transcribe_groq(wav: Path, language: str | None = None) -> Transcript:
    from groq import Groq

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set (see .env.example)")
    client = Groq(api_key=config.GROQ_API_KEY)
    chunks = chunk_audio(wav, config.ASR_CHUNK_SECONDS)

    try:
        with ThreadPoolExecutor(max_workers=config.GROQ_PARALLEL_REQUESTS) as pool:
            results = list(pool.map(lambda c: _groq_chunk(client, c[0], c[1], language), chunks))
    finally:
        for _, p in chunks:
            p.unlink(missing_ok=True)

    segments = [s for _, segs in results for s in segs]
    segments.sort(key=lambda s: s.start)
    lang = next((l for l, _ in results if l), language or "unknown")
    logger.info("Groq ASR: %d segments, language=%s", len(segments), lang)
    return Transcript(language=lang, duration=probe_duration(wav), segments=segments)


# -------------------------------------------------------------------------- sarvam
def sarvam_client():
    from sarvamai import SarvamAI

    if not config.SARVAM_API_KEY:
        raise RuntimeError("SARVAM_API_KEY is not set (see .env.example)")
    return SarvamAI(api_subscription_key=config.SARVAM_API_KEY)


def _sarvam_chunk(client, offset: float, path: Path, language: str | None) -> tuple[str, Segment | None]:
    kwargs = dict(model=config.SARVAM_ASR_MODEL, with_timestamps=True,
                  language_code=language or config.SARVAM_LANGUAGE)
    if config.SARVAM_ASR_MODE and config.SARVAM_ASR_MODEL == "saaras:v3":
        kwargs["mode"] = config.SARVAM_ASR_MODE
    data = path.read_bytes()

    # Sarvam's sync endpoint has a low requests-per-second limit; back off on 429.
    delay = 2.0
    for attempt in range(config.SARVAM_MAX_RETRIES + 1):
        try:
            resp = client.speech_to_text.transcribe(file=(path.name, data, "audio/flac"), **kwargs)
            break
        except Exception as e:  # noqa: BLE001 - only retry rate limits, re-raise everything else
            if getattr(e, "status_code", None) != 429 or attempt == config.SARVAM_MAX_RETRIES:
                raise
            logger.warning("Sarvam 429 on %s, retry %d/%d in %.0fs", path.name, attempt + 1,
                           config.SARVAM_MAX_RETRIES, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)

    text = (resp.transcript or "").strip()
    if not text:
        return resp.language_code or "", None
    ts = resp.timestamps
    if ts and ts.start_time_seconds and ts.end_time_seconds:
        start, end = offset + ts.start_time_seconds[0], offset + ts.end_time_seconds[-1]
    else:
        start, end = offset, offset + config.SARVAM_CHUNK_SECONDS
    return resp.language_code or "", Segment(start=start, end=end, text=text)


def transcribe_sarvam(wav: Path, language: str | None = None) -> Transcript:
    client = sarvam_client()
    chunks = chunk_audio(wav, config.SARVAM_CHUNK_SECONDS)

    try:
        with ThreadPoolExecutor(max_workers=config.SARVAM_PARALLEL_REQUESTS) as pool:
            results = list(pool.map(lambda c: _sarvam_chunk(client, c[0], c[1], language), chunks))
    finally:
        for _, p in chunks:
            p.unlink(missing_ok=True)

    segments = [seg for _, seg in results if seg]
    langs = [l for l, _ in results if l]
    lang = max(set(langs), key=langs.count) if langs else (language or "unknown")
    logger.info("Sarvam ASR (%s): %d segments, language=%s", config.SARVAM_ASR_MODEL, len(segments), lang)
    return Transcript(language=lang, duration=probe_duration(wav), segments=segments)


# --------------------------------------------------------------------------- entry
def auto_backend() -> str:
    """Fastest backend we have a key for; falls back to running on this machine."""
    if config.GROQ_API_KEY:
        return "groq"
    if config.SARVAM_API_KEY:
        return "sarvam"
    return "local"


_LANGUAGE_CODES = {
    "hindi": "hi", "english": "en", "bengali": "bn", "bangla": "bn", "tamil": "ta", "telugu": "te",
    "marathi": "mr", "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "urdu": "ur",
    "odia": "od", "oriya": "od", "assamese": "as", "nepali": "ne", "hinglish": "hi",
}


def normalise_language(language: str | None, backend: str) -> str | None:
    """Accept 'Hindi', 'hi' or 'hi-IN' and return what the chosen backend expects."""
    if not language:
        return None
    code = _LANGUAGE_CODES.get(language.strip().lower(), language.strip()).split("-")[0].lower()
    if backend == "sarvam":
        return f"{code}-IN"
    return "or" if code == "od" else code  # whisper uses 'or' for Odia


def transcribe(wav: Path, backend: str = "auto", language: str | None = None) -> Transcript:
    if backend == "auto":
        backend = auto_backend()
    language = normalise_language(language, backend)
    if backend == "sarvam":
        return transcribe_sarvam(wav, language)
    if backend == "groq":
        return transcribe_groq(wav, language)
    if backend == "local":
        return transcribe_local(wav, language)
    raise ValueError(f"Unknown ASR backend: {backend}")
