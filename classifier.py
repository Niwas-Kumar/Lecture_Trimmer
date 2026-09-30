"""Stage 3: label each ~45 s transcript unit with an LLM.

The transcript is grouped into units, the units into overlapping 10-minute
windows, and every window is sent to the model in parallel with a
schema-enforced JSON response. Works for any language and any subject because
the rule is stated in natural language, not as keywords.

Backends: "claude" (Anthropic, best accuracy) or "gemini" (Google, free tier).
"""
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import config
from models import Label, Transcript, Unit, UnitLabel, WindowClassification

logger = logging.getLogger("trimmer")

SYSTEM_PROMPT = """You are an editor who trims recorded classroom lectures down to the study material.

You will receive consecutive numbered units of a lecture transcript (each roughly 45 seconds, with timestamps). The transcript comes from automatic speech recognition, so expect misspellings, code-mixed languages (e.g. Hindi + English), and occasional garbage. The lecture may be in any language and on any subject.

Label EVERY unit with exactly one label:

- core_teaching: the teacher is explaining a concept, definition, theory, derivation, proof, formula, process, or reading/explaining a slide or the board. Includes the teacher's own summary of a point made in THIS lecture.
- worked_example: the teacher is solving a question, numerical, example, or exercise step by step.
- doubt_solving: the teacher answers a student's question about the subject matter.
- low_density_teaching: on topic but adds nothing new - repeating a point already made in this lecture, slow rephrasing, "so as I said", filling time while writing. Use sparingly.
- revision_of_previous: the teacher recaps material taught in an EARLIER class ("last class we did...", "pichli class mein...", "let's revise chapter 3 first..."). If the same content is being taught fresh for the first time, use core_teaching instead.
- chatter: greetings, roll call, jokes, motivational talk, personal stories, waiting for students to join, "can you hear me", chit-chat with students that is not about the subject.
- admin_announcement: schedule, test dates, homework, fees, batch/platform information, which notes to download, telegram links, and similar logistics.
- technical_issue: mic, camera, screen-share, pen-tablet or internet problems.
- silence_break: break time, long pauses, or units with no meaningful speech.

Rules:
1. Judge by the CONTENT of the speech, not by how formal it sounds. Teaching delivered casually is still teaching.
2. A unit that mixes teaching with a bit of chatter is teaching. Only label chatter/admin when the unit is mostly non-teaching.
3. When genuinely uncertain, prefer the teaching labels and lower the confidence. Cutting real teaching is far worse than keeping 45 seconds of chatter.
4. Use the neighbouring units for context: a short "okay so" between two derivation steps is core_teaching, not chatter.
5. Return a label for every unit id given, and nothing for ids not given."""

AGGRESSIVE_PROMPT = """You are a ruthless editor who cuts recorded lectures down to ONLY dense, core academic content.

You will receive consecutive numbered units of a lecture transcript (each roughly 20 seconds, with timestamps). The transcript comes from automatic speech recognition, so expect misspellings, code-mixed languages (e.g. Hindi + English), and occasional garbage. The lecture may be in any language and on any subject.

Label EVERY unit with exactly one label. Only the first two labels survive the cut.

KEEP (use only when the unit is actively teaching a hard concept):
- core_teaching: a definition, formula, theorem, law, code syntax, mechanism, or a direct factual explanation of a concept. The unit must contain actual content, not talk about content.
- worked_example: step-by-step solving of a problem, numerical, proof, or exercise where the steps are being stated.

DELETE (everything else):
- doubt_solving: student questions and the answers to them, Q&A sessions, "any doubts?".
- low_density_teaching: on-topic talk with no new content - repeating a point already made, slow rephrasing, "so basically what I said", announcing what will be explained next, syllabus or chapter overviews, "this is important for the exam", filling time while writing.
- revision_of_previous: recap of an earlier class.
- chatter: greetings, roll call, jokes, personal stories, motivational talk, like/subscribe/join requests, waiting for students.
- admin_announcement: schedule, tests, homework, fees, notes links, platform information.
- technical_issue: mic, camera, screen-share, tablet or internet problems.
- silence_break: breaks, long pauses, no meaningful speech.

Rules:
1. Be aggressive. It is better to over-trim than to keep unnecessary context. Target keeping only 20-40% of units.
2. If a unit is not actively stating a definition, formula, fact, code, or solution step, DELETE it - pick the closest delete label.
3. A unit that is half teaching and half filler is core_teaching only if the teaching half contains a concrete fact, formula or step. Otherwise low_density_teaching.
4. Confidence reflects how certain you are of the label.
5. Return a label for every unit id given, and nothing for ids not given."""

PROMPTS = {"balanced": SYSTEM_PROMPT, "aggressive": AGGRESSIVE_PROMPT}


# ----------------------------------------------------------------- unit building
def build_units(transcript: Transcript, unit_seconds: float = config.UNIT_SECONDS) -> list[Unit]:
    units: list[Unit] = []
    cur_text: list[str] = []
    cur_start: float | None = None
    cur_end = 0.0

    def flush():
        nonlocal cur_text, cur_start
        if cur_text and cur_start is not None:
            units.append(Unit(id=len(units), start=cur_start, end=cur_end, text=" ".join(cur_text)))
        cur_text, cur_start = [], None

    for seg in transcript.segments:
        if cur_start is None:
            cur_start = seg.start
        # A gap of >15 s inside a unit means a break; close the unit there.
        if cur_text and seg.start - cur_end > 15:
            flush()
            cur_start = seg.start
        cur_text.append(seg.text)
        cur_end = seg.end
        if cur_end - cur_start >= unit_seconds:
            flush()
    flush()
    logger.info("Built %d units of ~%ds", len(units), unit_seconds)
    return units


def build_windows(units: list[Unit]) -> list[list[Unit]]:
    """Overlapping time windows so the model always has context on both sides."""
    windows, start_t = [], 0.0
    step = config.WINDOW_SECONDS - config.WINDOW_OVERLAP_SECONDS
    last_end = units[-1].end if units else 0
    while start_t < last_end:
        end_t = start_t + config.WINDOW_SECONDS
        w = [u for u in units if u.end > start_t and u.start < end_t]
        if w:
            windows.append(w)
        start_t += step
    return windows


def _fmt(t: float) -> str:
    h, m, s = int(t // 3600), int(t % 3600 // 60), int(t % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _window_prompt(window: list[Unit], context: str) -> str:
    lines = [f"[unit {u.id}] {_fmt(u.start)}-{_fmt(u.end)}: {u.text}" for u in window]
    header = f"Lecture context: {context}\n\n" if context else ""
    return header + "Transcript units:\n\n" + "\n".join(lines)


# --------------------------------------------------------------- backend: claude
def _claude_client():
    import anthropic

    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _classify_claude(client, window: list[Unit], context: str, system_prompt: str) -> list[UnitLabel]:
    response = client.messages.parse(
        model=config.CLAUDE_MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _window_prompt(window, context)}],
        output_format=WindowClassification,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined window starting at unit {window[0].id}")
    return response.parsed_output.labels


# --------------------------------------------------------------- backend: gemini
def _gemini_client():
    from google import genai

    if not config.GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set (see .env.example)")
    return genai.Client(api_key=config.GOOGLE_API_KEY)


def _classify_gemini(client, window: list[Unit], context: str, system_prompt: str) -> list[UnitLabel]:
    from google.genai import types

    prompt = _window_prompt(window, context)
    models = [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]
    last_err: Exception | None = None
    for model in models:
        delay = 10.0
        for attempt in range(config.GEMINI_MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema=WindowClassification,
                        temperature=0.0,  # as repeatable as the API allows
                    ),
                )
                parsed = response.parsed
                if not isinstance(parsed, WindowClassification):
                    parsed = WindowClassification.model_validate_json(response.text)
                return parsed.labels
            except Exception as e:  # noqa: BLE001 - classify by HTTP code below
                last_err = e
                code = getattr(e, "code", None)
                if code == 404 or (code == 503 and attempt >= 1):
                    # Model missing, or overloaded twice in a row: the fallback models answer instantly.
                    logger.warning("Gemini %s on %s, switching to next model", code, model)
                    break
                if code == 503:
                    time.sleep(5)
                    continue
                if code == 429 and attempt < config.GEMINI_MAX_RETRIES:
                    logger.warning("Gemini 429 on %s, retry %d/%d in %.0fs", model,
                                   attempt + 1, config.GEMINI_MAX_RETRIES, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
    raise RuntimeError(f"All Gemini models failed: {last_err}")


# ------------------------------------------------------------------------ entry
def resolve_backend(backend: str | None) -> str:
    """'auto' picks Claude if its key is present, else Gemini."""
    if backend and backend != "auto":
        return backend
    if config.ANTHROPIC_API_KEY:
        return "claude"
    if config.GOOGLE_API_KEY:
        return "gemini"
    raise RuntimeError("Set ANTHROPIC_API_KEY or GOOGLE_API_KEY (see .env.example)")


def _cache_path(units: list[Unit], context: str, model: str, system_prompt: str):
    """Labels are cached per (units, prompt, context, model) so repeat runs are identical."""
    h = hashlib.sha1()
    for u in units:
        h.update(f"{u.start:.2f}|{u.end:.2f}|{u.text}\n".encode("utf-8"))
    h.update(f"{context}|{model}|{system_prompt}".encode("utf-8"))
    return config.TEMP_DIR / f"labels_{h.hexdigest()[:16]}.json"


def classify(units: list[Unit], context: str = "", backend: str = "auto", mode: str = "balanced",
             use_cache: bool = True) -> dict[int, UnitLabel]:
    """Return {unit_id: label}. One pass over the windows; units the model missed default to kept."""
    backend = resolve_backend(backend)
    system_prompt = PROMPTS[mode]
    if backend == "claude":
        client, fn, workers, model = _claude_client(), _classify_claude, config.CLASSIFY_PARALLEL_REQUESTS, config.CLAUDE_MODEL
    elif backend == "gemini":
        client, fn, workers, model = _gemini_client(), _classify_gemini, config.GEMINI_PARALLEL_REQUESTS, config.GEMINI_MODEL
    else:
        raise ValueError(f"Unknown classifier backend: {backend}")

    cache = _cache_path(units, context, model, system_prompt)
    if use_cache and cache.exists():
        logger.info("Using cached labels %s", cache.name)
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {int(k): UnitLabel.model_validate(v) for k, v in data.items()}

    windows = build_windows(units)
    logger.info("Classifying %d windows with %s (%s), mode=%s", len(windows), backend, model, mode)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda w: fn(client, w, context, system_prompt), windows))

    valid_ids = {u.id for u in units}
    labels: dict[int, UnitLabel] = {}
    for window_labels in results:
        for ul in window_labels:
            if ul.unit_id not in valid_ids:
                continue
            # Overlapping windows label the same unit twice - keep the more confident answer.
            if ul.unit_id not in labels or ul.confidence > labels[ul.unit_id].confidence:
                labels[ul.unit_id] = ul

    for u in units:
        if u.id not in labels:
            labels[u.id] = UnitLabel(unit_id=u.id, label=Label.CORE_TEACHING, confidence=0.0,
                                     reason="not returned by model; kept by default")

    cache.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in labels.items()},
                                ensure_ascii=False), encoding="utf-8")
    return labels
