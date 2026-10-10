"""Stage 2: speech-to-text with timestamps through Groq's hosted Whisper.

Audio is uploaded in sections with bounded parallelism; completed sections are
saved so a retry never repeats them. Word timestamps help place clean edit
boundaries.
"""
import logging
import hashlib
import json
import math
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.media.audio import chunk_audio, probe_duration
from lecture_trimmer.models import Segment, Transcript, Word
from lecture_trimmer.processing.job_control import check_cancelled, interruptible_wait, submit_with_context

logger = logging.getLogger("trimmer")


class TranscriptionServiceError(RuntimeError):
    def __init__(self, message):
        self.public_message = message
        super().__init__(message)


def _field(item, name, default=None):
    """Both Groq SDK objects and verbose-JSON dictionaries occur in practice."""
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def normalise_word_timestamps(items, *, offset=0.0, duration=None) -> list[Word]:
    """Validate ASR timing evidence without inventing missing word boundaries.

    Partial/malformed word metadata must not make otherwise usable speech text
    fail. Overlapping words are left intact (some ASR engines align that way);
    exact duplicate observations and non-finite/out-of-source times are removed.
    Older transcripts with no word metadata continue to use segment boundaries.
    """
    if not math.isfinite(offset) or offset < 0:
        raise ValueError("Word timestamp offset must be finite and nonnegative")
    words, invalid = [], 0
    for item in items or []:
        try:
            local_start = float(_field(item, "start"))
            start = local_start + offset
            end = float(_field(item, "end")) + offset
            text = str(_field(item, "word", _field(item, "text", "")) or "").strip()
            if (not text or not math.isfinite(start) or not math.isfinite(end)
                    or local_start < 0 or end <= start
                    or (duration is not None and (start >= duration or end > duration + .05))):
                raise ValueError("Invalid word timing")
            words.append(Word(start=start, end=min(end, duration) if duration is not None else end, text=text))
        except (TypeError, ValueError):
            invalid += 1
    unique = {(w.start, w.end, w.text): w for w in words}
    if invalid:
        logger.warning("Discarded %d invalid word timestamps; sentence timestamps remain available", invalid)
    return sorted(unique.values(), key=lambda word: (word.start, word.end))


def fit_segments_to_source(segments, duration) -> list[Segment]:
    """Keep sentence timing inside the real audio.

    Whisper pads each upload to a 30s window, so the short final section of a
    lecture can report segments ending up to ~30s past the source (often a
    hallucinated "Thank you." over silence). Overhanging ends are clamped,
    segments that start at or beyond the end are dropped, and malformed
    timings are discarded rather than failing the whole lecture.
    """
    fitted, clamped, dropped = [], 0, 0
    for seg in segments:
        start, end = max(0.0, seg.start), min(duration, seg.end)
        if not math.isfinite(start) or not math.isfinite(end) or start >= duration or end <= start:
            dropped += int(bool(seg.text.strip()))
            continue
        clamped += int(end != seg.end or start != seg.start)
        fitted.append(seg if (start, end) == (seg.start, seg.end) else seg.model_copy(update={"start": start, "end": end}))
    if clamped or dropped:
        logger.warning("Fitted speech timing to the %.1fs source: %d clamped, %d dropped", duration, clamped, dropped)
    return sorted(fitted, key=lambda s: s.start)


# ---------------------------------------------------------------------------- groq
def _groq_chunk(client, offset: float, path: Path, language: str | None, notify=None) -> tuple[str, list[Segment], list[Word]]:
    data = path.read_bytes()
    kwargs = dict(model=config.GROQ_ASR_MODEL, response_format="verbose_json",
                  timestamp_granularities=["word", "segment"])
    if language:
        kwargs["language"] = language

    # Back off on provider rate limits; limits vary by account and model.
    delay = 5.0
    for attempt in range(config.GROQ_MAX_RETRIES + 1):
        check_cancelled()
        try:
            resp = client.audio.transcriptions.create(file=(path.name, data), **kwargs)
            break
        except Exception as e:
            from groq import APIConnectionError
            check_cancelled()
            status = getattr(e, "status_code", None)
            retryable = isinstance(e, (APIConnectionError, TimeoutError)) or status in {408, 409, 429, 500, 502, 503, 504}
            if not retryable:
                if status in {401, 403}:
                    raise TranscriptionServiceError('Speech recognition access was rejected. Ask the owner to check the Groq key.') from e
                raise
            headers = getattr(getattr(e, 'response', None), 'headers', {})
            try:
                retry_after = float(headers.get('retry-after', 0))
                if math.isfinite(retry_after):
                    delay = max(delay, retry_after)
            except (TypeError, ValueError):
                pass
            if attempt == config.GROQ_MAX_RETRIES or delay > 120:
                message = ('The speech service usage limit was reached. Wait for your Groq allowance to reset, then retry. Completed audio sections are saved.'
                           if status == 429 else 'Speech recognition lost its connection or timed out. Check your internet and retry; completed audio sections are saved.')
                raise TranscriptionServiceError(message) from e
            logger.warning('Groq section at %.0fs: %s (HTTP %s), retry %d/%d in %.0fs',
                           offset, type(e).__name__, status, attempt + 1, config.GROQ_MAX_RETRIES, delay)
            if notify:
                notify(f'Speech connection interrupted; retrying an audio section in {delay:.0f}s. Completed sections are saved.')
            interruptible_wait(delay)
            delay = min(delay * 2, 60)
    segs = [
        Segment(start=offset + _field(s, "start"), end=offset + _field(s, "end"), text=_field(s, "text").strip())
        for s in (resp.segments or [])
    ]
    words = normalise_word_timestamps(getattr(resp, "words", None), offset=offset,
                                     duration=offset + config.ASR_CHUNK_SECONDS)
    return getattr(resp, "language", "") or "", segs, words


def transcribe_groq(wav: Path, language: str | None = None, *, cache_dir=None, use_cache=True, progress=None) -> Transcript:
    from groq import Groq
    import httpx

    if not config.GROQ_API_KEY:
        raise TranscriptionServiceError('Speech recognition needs a Groq API key. Set GROQ_API_KEY '
                                        '(see .env.example), then retry; nothing was uploaded.')
    client = Groq(api_key=config.GROQ_API_KEY, max_retries=0,
                  timeout=httpx.Timeout(connect=15, read=60, write=45, pool=15))
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda fraction, message: logger.info(message))
    def work(chunk):
        check_cancelled()
        offset, path = chunk
        cached = None
        if cache_dir:
            key = hashlib.sha256(path.read_bytes() + json.dumps(
                ['groq-chunks-v1', config.GROQ_ASR_MODEL, language, offset, config.ASR_CHUNK_SECONDS]).encode()).hexdigest()
            cached = cache_dir / (key + '.json')
            if use_cache and cached.exists():
                try:
                    saved = Transcript.model_validate_json(cached.read_text(encoding='utf-8'))
                    return (saved.language, saved.segments, saved.words), True
                except (OSError, ValueError):
                    logger.warning('Ignoring invalid saved speech section')
        result = _groq_chunk(client, offset, path, language,
                             notify=lambda message: notify(None, message))
        if cached:
            saved = Transcript(language=result[0], duration=offset + config.ASR_CHUNK_SECONDS,
                               segments=result[1], words=result[2])
            staging = cached.with_suffix(f'.{uuid.uuid4().hex}.partial')
            try:
                staging.write_text(saved.model_dump_json(), encoding='utf-8')
                staging.replace(cached)
            finally:
                staging.unlink(missing_ok=True)
        return result, False
    chunks = []
    results = []
    try:
        chunks = chunk_audio(wav, config.ASR_CHUNK_SECONDS, out_dir=wav.parent)
        pool = ThreadPoolExecutor(max_workers=config.GROQ_PARALLEL_REQUESTS)
        pending, iterator, completed, reused = {}, iter(chunks), 0, 0
        try:
            for chunk in list(chunks)[:config.GROQ_PARALLEL_REQUESTS]:
                next(iterator)
                pending[submit_with_context(pool, work, chunk)] = chunk[0]
            while pending:
                future = next(as_completed(pending))
                offset = pending.pop(future)
                result, hit = future.result()
                results.append((offset, result))
                completed += 1
                reused += int(hit)
                notify(completed / len(chunks), f'Transcribing audio: {completed}/{len(chunks)} sections complete ({reused} reused).')
                check_cancelled()
                chunk = next(iterator, None)
                if chunk is not None:
                    pending[submit_with_context(pool, work, chunk)] = chunk[0]
        finally:
            for future in pending:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
    finally:
        for _, p in chunks:
            p.unlink(missing_ok=True)
        client.close()

    check_cancelled()
    results = [result for _, result in sorted(results)]
    duration = probe_duration(wav)
    segments = fit_segments_to_source([s for _, segs, _ in results for s in segs], duration)
    words = normalise_word_timestamps([w for _, _, chunk_words in results for w in chunk_words], duration=duration)
    lang = next((l for l, _, _ in results if l), language or "unknown")
    logger.info("Groq ASR: %d segments, language=%s", len(segments), lang)
    return Transcript(language=lang, duration=duration, segments=segments, words=words)


# --------------------------------------------------------------------------- entry
BACKEND = "groq"

_LANGUAGE_CODES = {
    "hindi": "hi", "english": "en", "bengali": "bn", "bangla": "bn", "tamil": "ta", "telugu": "te",
    "marathi": "mr", "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "urdu": "ur",
    "odia": "or", "oriya": "or", "assamese": "as", "nepali": "ne", "hinglish": "hi",
}


def normalise_language(language: str | None) -> str | None:
    """Accept 'Hindi', 'hi' or 'hi-IN' and return the Whisper language code."""
    if not language:
        return None
    code = _LANGUAGE_CODES.get(language.strip().lower(), language.strip()).split("-")[0].lower()
    return "or" if code == "od" else code


def transcribe(wav: Path, language: str | None = None, *, cache_dir=None, use_cache=True, progress=None) -> Transcript:
    check_cancelled()
    return transcribe_groq(wav, normalise_language(language), cache_dir=cache_dir, use_cache=use_cache, progress=progress)
