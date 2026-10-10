"""Map exercises, classify activities, and independently filter every speech unit."""
import hashlib
import json
import logging
import math
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from lecture_trimmer.processing.job_control import check_cancelled, interruptible_wait, submit_with_context

import lecture_trimmer.config as config
from lecture_trimmer.models import EpisodeMap, RetentionReview, SpanClassification, Unit, UnitLabel
from lecture_trimmer.analysis.prompts import EPISODE_PROMPT, PROMPTS, RETENTION_PROMPTS, VERSION

logger = logging.getLogger("trimmer")


def build_units(transcript, unit_seconds=6):
    """Use verified word boundaries for long speech, retaining old-cache fallback.

    Word text must account for the entire segment before it can be split. The
    original segment text is sliced rather than regenerated from ASR word tokens,
    so equations, punctuation and Hindi combining marks are not silently lost.
    """
    import bisect
    import math
    import re
    import unicodedata

    if not math.isfinite(unit_seconds) or unit_seconds <= 0:
        raise ValueError("Speech unit duration must be positive and finite")
    words = sorted(getattr(transcript, "words", []) or [], key=lambda word: (word.start, word.end))
    word_starts = [word.start for word in words]
    word_target = max(6.0, min(8.0, unit_seconds))

    def text_index(text):
        normalized, positions = [], []
        for index, character in enumerate(text):
            for char in unicodedata.normalize("NFKC", character).casefold():
                # Keep mathematical symbols and Indic marks. Punctuation spacing
                # differs between providers; it is retained in the source slices.
                if unicodedata.category(char)[0] in "LNMS":
                    normalized.append(char)
                    positions.append(index)
        return "".join(normalized), positions

    def split_segment(segment):
        if not words or segment.end - segment.start <= unit_seconds:
            return [segment]
        first = bisect.bisect_left(word_starts, segment.start - .05)
        last = bisect.bisect_left(word_starts, segment.end)
        observed = [word for word in words[first:last] if word.end > segment.start]
        if not observed:
            return [segment]
        source_text, positions = text_index(segment.text)
        normalized_words, aligned = [], []
        for word in observed:
            if (not math.isfinite(word.start) or not math.isfinite(word.end)
                    or word.end <= word.start or word.start < segment.start - .05
                    or word.end > segment.end + .05):
                return [segment]
            normalized, _ = text_index(word.text)
            if normalized:
                aligned.append(word)
                normalized_words.append(normalized)
        if not source_text or "".join(normalized_words) != source_text or len(aligned) < 2:
            return [segment]
        if any(right.start < left.start or right.end < left.end
               for left, right in zip(aligned, aligned[1:])):
            return [segment]

        pieces, text_start, time_start, cursor = [], 0, segment.start, 0
        for index, (word, normalized) in enumerate(zip(aligned[:-1], normalized_words[:-1])):
            cursor += len(normalized)
            following = aligned[index + 1]
            if following.start < word.end or not time_start < following.start < segment.end:
                continue  # Never cut through an overlapping spoken word.
            text_end = positions[cursor]
            content = segment.text[text_start:text_end].strip()
            elapsed = following.start - time_start
            sentence_end = bool(re.search(r"[.!?।॥][\"'”’)]*\s*$", content))
            clause_end = bool(re.search(r"[,;:][\"'”’)]*\s*$", content))
            pause = following.start - word.end >= .5
            if ((sentence_end and elapsed >= word_target * .6)
                    or (pause and elapsed >= word_target * .75)
                    or (clause_end and elapsed >= word_target)
                    or elapsed >= word_target * 1.5):
                pieces.append(Unit(id=0, start=time_start, end=following.start, text=content))
                text_start, time_start = text_end, following.start
        pieces.append(Unit(id=0, start=time_start, end=segment.end,
                           text=segment.text[text_start:].strip()))
        return pieces

    units, pending = [], []
    def flush():
        if pending:
            units.append(Unit(id=len(units), start=pending[0].start, end=pending[-1].end,
                              text=" ".join(s.text for s in pending)))
            pending.clear()
    for seg in transcript.segments:
        if not seg.text.strip() or seg.end <= seg.start:
            continue
        for piece in split_segment(seg):
            if pending and (piece.end-pending[0].start > unit_seconds or piece.start-pending[-1].end > 1):
                flush()
            pending.append(piece)
    flush()
    logger.info("Built %d speech units (target ~%ss; incomplete word timing retains whole utterances)", len(units), unit_seconds)
    return units


def add_untranscribed_gaps(units, duration, min_gap=8):
    """Make substantial untranscribed intervals visible to activity analysis.

    Absence of speech is not evidence of absence of teaching. Gaps are explicit
    uncertain observations, not silence labels, and keep their source boundaries.
    IDs are rebuilt without mutating the supplied units.
    """
    import math

    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Source duration must be positive and finite")
    if not math.isfinite(min_gap) or min_gap <= 0:
        raise ValueError("Untranscribed gap threshold must be positive and finite")
    if not units:
        raise RuntimeError("No reliable speech transcript was found. The original video is preserved. "
                           "Check the audio and selected language, then retry transcription; "
                           "empty study videos were not created.")
    ordered = sorted(units, key=lambda unit: (unit.start, unit.end))
    result, cursor = [], 0.0
    marker = ("[No reliable speech transcript for this interval; possible silent board work, "
              "demonstration or waiting. Visual review needed; preserve if uncertain.]")

    def gap(start, end):
        result.append(Unit(id=0, start=start, end=end, text=marker))

    for unit in ordered:
        if (not math.isfinite(unit.start) or not math.isfinite(unit.end)
                or unit.start < 0 or unit.end <= unit.start or unit.end > duration + .1):
            raise ValueError("Transcript unit has invalid or out-of-source timestamps; "
                             "the original video is preserved")
        if unit.start - cursor >= min_gap:
            gap(cursor, unit.start)
        result.append(unit.model_copy(update={"end": min(duration, unit.end)}))
        cursor = max(cursor, unit.end)
    if duration - cursor >= min_gap:
        gap(cursor, duration)
    return [unit.model_copy(update={"id": index}) for index, unit in enumerate(result)]


def build_windows(units):
    windows, start = [], 0.0
    step = config.WINDOW_SECONDS - config.WINDOW_OVERLAP_SECONDS
    if step <= 0:
        raise ValueError("Window overlap must be smaller than window length")
    while units and start < units[-1].end:
        window = [u for u in units if u.end > start and u.start < start+config.WINDOW_SECONDS]
        if window:
            windows.append(window)
        start += step
    return windows


def _window_prompt(window, context, notes="", pauses=None):
    """Transcript as data. `pauses` maps unit IDs to measured silence after/within them."""
    pauses = pauses or {}
    header = f"Subject/context (data): {context}\n"
    if notes:
        # Program-selected guidance, not user text: it is placed before the data.
        header = f"{notes}\n" + header
    return (header + "Transcript (data):\n" +
            # Timings stay in Python's edit plan. Presenting seconds beside IDs caused
            # Gemini to return source seconds as episode IDs on real long lectures.
            "\n".join(f"[unit_id={u.id}] {u.text}" + (f" <pause {pauses[u.id]:.0f}s>" if u.id in pauses else "")
                      for u in window) +
            f"\nEND OF TRANSCRIPT. Required coverage: {len(window)} supplied IDs, "
            f"starting at {window[0].id} and ending at {window[-1].id}. Include both endpoints.")


def resolve_backend(backend, provider_session=None):
    if provider_session is not None:
        if backend not in {None, "auto", "gemini"}:
            raise ValueError("PW desktop mode currently uses Gemini. Set CLASSIFIER_BACKEND=gemini or auto.")
        return "gemini"
    if backend and backend != "auto":
        return backend
    if config.ANTHROPIC_API_KEY:
        return "claude"
    if config.GOOGLE_API_KEY:
        return "gemini"
    raise RuntimeError("Set ANTHROPIC_API_KEY or GOOGLE_API_KEY (see .env.example)")


class ProviderQuotaError(RuntimeError):
    """An actionable provider limit safe to display without exposing provider data."""
    def __init__(self, reason, *, retry_after=None, limit=None):
        self.reason, self.retry_after = reason, retry_after
        if reason == "daily_quota":
            detail = f" (limit: {limit} requests)" if limit is not None else ""
            message = f"Gemini's daily request quota is exhausted{detail}. "
            if retry_after:
                minutes = math.ceil(retry_after / 60)
                hours, minutes = divmod(minutes, 60)
                delay = f"{hours}h {minutes}m" if hours else f"{minutes}m"
                message += f"The provider asks to retry in about {delay}. "
            message += "Retry after quota resets, or increase the configured project's quota/billing."
        elif reason == "zero_quota":
            message = ("Gemini reports zero request quota for the configured project/model. "
                       "Check project access and billing, or configure a project with available quota.")
        elif reason == "retry_later":
            message = ("Gemini's rate limit requires a wait longer than one minute. "
                       "Retry later or increase the configured project's quota.")
        else:
            message = "Gemini is temporarily rate limited. Retry shortly or increase the configured project's quota."
        self.public_message = message + " Saved analysis is preserved."
        super().__init__(self.public_message)


def _retry_delay_seconds(value):
    """Parse Google RetryInfo durations without trusting the provider's error prose."""
    try:
        if isinstance(value, dict):
            seconds = float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9
        elif isinstance(value, (float, int)):
            seconds = float(value)
        else:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s?\s*", str(value))
            if not match:
                return None
            seconds = float(match.group(1))
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quota_metadata(exc):
    """Extract typed quota facts; never include response bodies in public errors."""
    daily, zero, delay, daily_limit = False, False, None, None
    def visit(value, depth=0):
        nonlocal daily, zero, delay, daily_limit
        if depth > 8:
            return
        if isinstance(value, dict):
            names = " ".join(str(value.get(key, "")) for key in ("quotaId", "quotaMetric", "description"))
            is_daily = bool(re.search(r"per[\s_-]*day|daily", names, flags=re.I))
            daily = daily or is_daily
            quota = value.get("quotaValue", value.get("quota_value"))
            if quota is not None:
                try:
                    amount = float(quota)
                    zero = zero or amount == 0
                    if is_daily and math.isfinite(amount) and 0 <= amount <= 1e12 and amount.is_integer():
                        daily_limit = int(amount)
                except (ValueError, TypeError, OverflowError):
                    pass
            for key, part in value.items():
                if key in {"retryDelay", "retry_delay"}:
                    parsed = _retry_delay_seconds(part)
                    if parsed is not None:
                        delay = max(delay or 0, parsed)
                elif isinstance(part, (dict, list)):
                    visit(part, depth+1)
        elif isinstance(value, list):
            for part in value[:100]:
                visit(part, depth+1)
    visit(getattr(exc, "details", None))
    visit(getattr(exc, "response_json", None))
    # Some transports omit structured details. Recognize narrow quota markers
    # without reflecting any arbitrary provider text into UI messages.
    message = str(getattr(exc, "message", "") or str(exc))
    daily = daily or bool(re.search(r"RequestsPerDay|requests?[_ -]+per[_ -]+day|daily (?:request )?quota", message, re.I))
    zero = zero or bool(re.search(r"quotaValue['\"]?\s*:\s*['\"]?0(?:['\",}\s]|$)|\blimit:\s*0\b", message, re.I))
    if delay is None:
        match = re.search(r"retry(?:Delay['\"]?\s*:\s*['\"]?| in\s+)(\d+(?:\.\d+)?)s", message, re.I)
        if match:
            delay = _retry_delay_seconds(match.group(1))
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    if hasattr(headers, "get"):
        header_delay = _retry_delay_seconds(headers.get("Retry-After", headers.get("retry-after")))
        if header_delay is not None:
            delay = max(delay or 0, header_delay)
    return daily, zero, delay, daily_limit


def _request(client, backend, prompt, system, schema):
    check_cancelled()
    if backend == "claude":
        response = client.messages.parse(
            model=config.CLAUDE_MODEL, max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}], output_format=schema)
        if response.stop_reason in {"refusal", "max_tokens"} or response.parsed_output is None:
            raise RuntimeError(f"Incomplete classifier response: {response.stop_reason}")
        return response.parsed_output
    from google.genai import types
    last_error = None
    waited = 0.0
    for model in [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]:
        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            check_cancelled()
            try:
                response = client.models.generate_content(model=model, contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=system,
                        # The SDK otherwise retries 429 internally before we can
                        # inspect a daily/zero quota. One retry policy owns delays.
                        http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
                        response_mime_type="application/json", response_schema=schema,
                        temperature=0.0, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
                return response.parsed if isinstance(response.parsed, schema) else schema.model_validate_json(response.text)
            except Exception as exc:
                last_error = exc
                code = getattr(exc, "code", None)
                try:
                    code = int(code)
                except (ValueError, TypeError):
                    pass
                retry_delay = None
                if code == 429:
                    daily, zero, retry_delay, limit = _quota_metadata(exc)
                    if zero:
                        raise ProviderQuotaError("zero_quota", retry_after=retry_delay) from None
                    if daily:
                        raise ProviderQuotaError("daily_quota", retry_after=retry_delay, limit=limit) from None
                    if retry_delay is not None and retry_delay > 60:
                        raise ProviderQuotaError("retry_later", retry_after=retry_delay) from None
                    if attempt >= config.GEMINI_MAX_RETRIES:
                        raise ProviderQuotaError("transient_exhausted", retry_after=retry_delay) from None
                if code == 404 or (code == 503 and attempt >= 1):
                    break
                if code in {429, 503} and attempt < config.GEMINI_MAX_RETRIES:
                    if code == 503:
                        retry_delay = _quota_metadata(exc)[2]
                    delay = retry_delay if retry_delay is not None else min(5 * 2**attempt, 30)
                    if delay > 60 or waited + delay > 60:
                        if code == 429:
                            raise ProviderQuotaError("transient_exhausted", retry_after=retry_delay) from None
                        raise RuntimeError("Gemini is temporarily unavailable. Retry later; saved analysis is preserved.") from None
                    interruptible_wait(delay)
                    waited += delay
                    continue
                raise
    raise RuntimeError(f"All classifier models failed: {last_error}")


def expand_spans(result, window):
    """Reject missing/duplicate/foreign IDs instead of quietly calling them theory."""
    valid = {u.id for u in window}
    labels = {}
    if isinstance(result, RetentionReview):
        for decision in result.decisions:
            if decision.unit_id not in valid or decision.unit_id in labels:
                raise ValueError(f"Duplicate or out-of-window editorial ID {decision.unit_id}")
            labels[decision.unit_id] = decision
        missing = sorted(valid-labels.keys())
        if missing:
            raise ValueError(f"Editor omitted {len(missing)} speech units: IDs {missing[:60]}")
        return labels
    for span in result.spans:
        if span.first_id > span.last_id:
            raise ValueError("Reversed classification span")
        if span.first_id not in valid or span.last_id not in valid:
            raise ValueError(f"Classification span {span.first_id}-{span.last_id} uses IDs outside this window")
        ids = set(range(span.first_id, span.last_id+1))
        if not ids <= valid or ids & labels.keys():
            raise ValueError("Overlapping or out-of-window classification span")
        for uid in ids:
            labels[uid] = UnitLabel(unit_id=uid, label=span.label, confidence=span.confidence, reason=span.reason)
    if labels.keys() != valid:
        missing = sorted(valid-labels.keys())
        raise ValueError(f"Classifier omitted {len(missing)} speech units: IDs {missing[:60]}")
    return labels


def validate_episodes(mapped, units):
    """Normalize harmless ordering/duplicates; reject, never guess, ambiguous boundaries.

    The exercise map is advisory context. Conflicting episodes are all excluded rather
    than arbitrarily choosing one or merging potentially separate problems.
    """
    valid = {u.id for u in units}
    unique, issues = {}, []
    for episode in mapped.episodes:
        start, end = episode.first_id, episode.last_id
        if start > end or start not in valid or end not in valid:
            issues.append(f"invalid IDs {start}-{end}")
            continue
        if end-start+1 != sum(start <= uid <= end for uid in valid):
            issues.append(f"unavailable IDs inside {start}-{end}")
            continue
        unique.setdefault((start, end), episode)
    ordered = sorted(unique.values(), key=lambda e: (e.first_id, e.last_id))
    conflicts = set()
    for i, episode in enumerate(ordered):
        for j in range(i+1, len(ordered)):
            other = ordered[j]
            if other.first_id > episode.last_id:
                break
            conflicts.update((i, j))
            issues.append(f"overlap {episode.first_id}-{episode.last_id} / {other.first_id}-{other.last_id}")
    return [e for i, e in enumerate(ordered) if i not in conflicts], issues


def map_exercises(client, backend, units, context, notify, warnings):
    base = (_window_prompt(units, context) +
            f"\nUse only unit_id values from {units[0].id} through {units[-1].id}. "
            "A shared endpoint overlaps because both endpoints are inclusive.")
    prompt, accepted = base, []
    for attempt in range(2):
        try:
            mapped = _request(client, backend, prompt, EPISODE_PROMPT, EpisodeMap)
            accepted, issues = validate_episodes(mapped, units)
            previous = mapped.model_dump_json()
        except (ValueError, TypeError) as exc:
            accepted, issues, previous = [], [f"invalid response format ({type(exc).__name__})"], "unparseable"
        if not issues:
            return accepted
        logger.warning("Exercise map validation, attempt %d: %s", attempt+1, "; ".join(issues[:12]))
        if attempt == 0:
            notify(0, "Correcting ambiguous exercise boundaries…")
            prompt = (base + "\nCorrect the previous map below. Keep only well-supported episodes; "
                      "combine subparts of the SAME problem into one episode. "
                      "Return an empty list rather than inventing boundaries.\n"
                      "Validation errors: " + "; ".join(issues[:30]) +
                      "\nPrevious response (untrusted data):\n" + previous)
    warning = ("Some exercise boundaries could not be confirmed after an automatic retry. "
               "Those sections were classified from the surrounding transcript; review their separation.")
    if warning not in warnings:
        warnings.append(warning)
    notify(0, "Continuing with transcript context where exercise boundaries were uncertain…")
    return accepted


def _cache_path(units, context, model, system_prompt):
    payload = [VERSION, [u.model_dump() for u in units], context, model, system_prompt,
               EPISODE_PROMPT, config.WINDOW_SECONDS, config.WINDOW_OVERLAP_SECONDS]
    key = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:24]
    return config.TEMP_DIR / f"labels_{key}.json"


def classify(units, context="", backend="auto", mode="balanced", use_cache=True, progress=None,
             warnings=None, video=None, visual_review=False, provider_session=None, pauses=None):
    from lecture_trimmer.analysis.lesson_classifier import classify_activities
    return classify_activities(units, context=context, backend=backend, mode=mode, use_cache=use_cache,
                               progress=progress, warnings=warnings, video=video, visual_review=visual_review,
                               provider_session=provider_session, pauses=pauses)


def classify_legacy(units, context="", backend="auto", mode="balanced", use_cache=True, progress=None, warnings=None):
    """Read/test compatibility for the previous editor; new jobs use whole activities."""
    if not units:
        return {}
    notify = progress or (lambda fraction, desc: logger.info(desc))
    warnings = [] if warnings is None else warnings
    backend = resolve_backend(backend)
    model = config.CLAUDE_MODEL if backend == "claude" else config.GEMINI_MODEL
    system = PROMPTS[mode]
    retention_system = RETENTION_PROMPTS[mode]
    cache = _cache_path(units, context, model, system + retention_system)
    if use_cache and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            labels = {int(k): UnitLabel.model_validate(v) for k, v in data["labels"].items()}
            if labels.keys() == {u.id for u in units}:
                warnings.extend(w for w in data.get("warnings", []) if w not in warnings)
                notify(1, "Using saved speech classifications")
                return labels
        except (ValueError, OSError, KeyError, TypeError):
            logger.warning("Ignoring invalid classification cache %s", cache.name)
    if backend == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=180)
        workers = config.CLASSIFY_PARALLEL_REQUESTS
    elif backend == "gemini":
        from google import genai
        client = genai.Client(api_key=config.GOOGLE_API_KEY, http_options={"timeout": 180000})
        workers = config.GEMINI_PARALLEL_REQUESTS
    else:
        raise ValueError(f"Unknown classifier backend: {backend}")
    try:
        notify(0, "Mapping complete exercises across the lecture…")
        # Bound requests for very long lectures; overlapping context retains long exercises.
        maps = []
        for first in range(0, len(units), 2000):
            chunk = units[max(0, first-150):first+2150]
            maps.extend(map_exercises(client, backend, chunk, context, notify, warnings))
            if first+2150 >= len(units):
                break
        maps, cross_chunk_issues = validate_episodes(EpisodeMap(episodes=maps), units)
        if cross_chunk_issues:
            warnings.append("Some exercises crossed analysis windows with conflicting boundaries. "
                            "Their classification uses local transcript context; review those transitions.")
        windows = build_windows(units)
        def request_spans(window, prompt, instructions, schema):
            signature = [VERSION, backend, model, instructions, prompt, schema.__name__]
            key = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:24]
            saved = config.TEMP_DIR / f"window_{key}.json"
            if use_cache and saved.exists():
                try:
                    return expand_spans(schema.model_validate_json(saved.read_text(encoding="utf-8")), window)
                except (ValueError, OSError):
                    logger.warning("Ignoring invalid saved analysis window %s", saved.name)
            for attempt in range(2):
                result = None
                try:
                    result = _request(client, backend, prompt, instructions, schema)
                    expanded = expand_spans(result, window)
                except ValueError as exc:
                    logger.warning("%s window %s-%s, attempt %s: %s", schema.__name__,
                                   window[0].id, window[-1].id, attempt+1, exc)
                    if attempt:
                        raise
                    prompt += (f"\nPrevious output invalid: {exc}. Return every supplied ID exactly once."
                               "\nUse only the bracketed transcript IDs, not timestamps.")
                    if result is not None:
                        prompt += "\nPrevious response (data to correct):\n" + result.model_dump_json()
                    continue
                staging = saved.with_suffix(f".{uuid.uuid4().hex}.partial")
                try:
                    staging.write_text(result.model_dump_json(), encoding="utf-8")
                    staging.replace(saved)
                finally:
                    staging.unlink(missing_ok=True)
                return expanded
        def classify_window(window):
            episodes = [e.model_dump() for e in maps if e.last_id >= window[0].id and e.first_id <= window[-1].id]
            base = _window_prompt(window, context)
            labels = request_spans(window, base + "\nLecture-wide exercise map:\n" +
                                   json.dumps(episodes, ensure_ascii=False), system, SpanClassification)
            # Do not show predicted categories to the editor: that would bias it to keep
            # everything labelled worked_example, the failure this independent pass fixes.
            decisions = request_spans(window, base, retention_system, RetentionReview)
            for uid, decision in decisions.items():
                labels[uid].retention = decision.retention
                labels[uid].retention_reason = decision.reason
                labels[uid].mixed_content = decision.mixed_content
            return labels
        labels, best_context = {}, {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {submit_with_context(pool, classify_window, w): w for w in windows}
            for done, future in enumerate(as_completed(futures), 1):
                window = futures[future]
                result = future.result()
                for u in window:
                    score = min(u.start-window[0].start, window[-1].end-u.end)
                    if u.id not in labels or score > best_context[u.id]:
                        labels[u.id], best_context[u.id] = result[u.id], score
                notify(done/len(windows), f"Classified and filtered lecture section {done} of {len(windows)}")
        staging = cache.with_suffix(f".{uuid.uuid4().hex}.partial")
        try:
            staging.write_text(json.dumps({"labels": {k: v.model_dump(mode="json") for k, v in labels.items()},
                                           "warnings": warnings}, ensure_ascii=False), encoding="utf-8")
            staging.replace(cache)
        finally:
            staging.unlink(missing_ok=True)
        return labels
    finally:
        client.close()
