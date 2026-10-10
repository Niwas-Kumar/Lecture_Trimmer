"""Hierarchical editing: complete learning activities, then safe unrelated passages.

The model proposes edits; deterministic validation owns coverage, dependencies and cuts.
Uncertain teaching is preserved and explicitly reported rather than silently discarded.
"""
import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from lecture_trimmer.processing.job_control import check_cancelled, submit_with_context

import lecture_trimmer.config as config
from lecture_trimmer.models import (ActivityKind, ActivityEdit, Label, LessonEdits, LessonMap,
                    Retention, TeachingActivity, UnitLabel, VisualReviews)
from lecture_trimmer.analysis.prompts import LESSON_MAP_PROMPT, PASSAGE_EDIT_PROMPT, VISUAL_REVIEW_PROMPT, VERSION

logger = logging.getLogger("trimmer")
ACADEMIC = {ActivityKind.THEORY, ActivityKind.QUESTIONS, ActivityKind.UNCERTAIN}
CUT_CATEGORIES = {Retention.NONACADEMIC, Retention.LOGISTICS, Retention.WAITING,
                  Retention.FILLER, Retention.REPETITION}
# Stage-2 polish inside teaching. Code enforces the craft rules by dropping a cut
# that breaks them, so over-eager tightening never fails a whole lecture.
TIGHTENING = {Retention.WAITING, Retention.FILLER, Retention.REPETITION}
MAX_TIGHTENING_SHARE = 0.35
# ~4-second units let stage 2 cut a filler phrase on its own. Windows and batches
# are sized so a 2-hour lecture still needs one map call and about eight edit calls.
MAP_WINDOW_UNITS = 2600
# Shared by every lecture in this process, so parallel lectures cannot flood the provider.
AI_SLOTS = threading.BoundedSemaphore(config.AI_MAX_CONCURRENT_REQUESTS)
EDIT_BATCH_UNITS = 360
MIN_KEPT_PIECE = 5.0


class StudyReviewRequired(ValueError):
    public_message = ("Some passages could not be separated reliably into study and unrelated content. "
                      "No new videos were exported. Saved work is preserved. Enable selected-frame review "
                      "or check the transcript language before retrying.")


class ActivityLabels(dict):
    def __init__(self, *args, activities=(), edits=(), evidence=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.activities = list(activities)
        self.edits = list(edits)
        self.evidence = evidence or {}
        self.subject = None
        self.class_type = None


def validate_map(mapped, units, previous=()):
    valid = {u.id for u in units}
    covered, known = set(), {a.first_id: a for a in previous}
    ordered = sorted(mapped.activities, key=lambda a: a.first_id)
    for index, activity in enumerate(ordered):
        if activity.first_id not in valid or activity.last_id not in valid or activity.last_id < activity.first_id:
            raise ValueError("Activity boundaries must be supplied unit IDs")
        ids = set(range(activity.first_id, activity.last_id + 1))
        if not ids <= valid or covered & ids:
            raise ValueError("Overlapping or unavailable activity IDs")
        if activity.continues_previous and (index != 0 or not previous):
            raise ValueError("Only the first activity may continue the previous window")
        for dependency in activity.depends_on:
            if dependency not in known or known[dependency].kind not in ACADEMIC or dependency >= activity.first_id:
                raise ValueError("Dependencies must refer to earlier academic activities")
        covered.update(ids)
        known[activity.first_id] = activity
    if covered != valid:
        raise ValueError(f"Activity map must cover every supplied ID; missing {sorted(valid-covered)[:30]}")
    return ordered


def require_roles(activities):
    """The model must say what happened; the program maps that to a video."""
    missing = [a.first_id for a in activities if a.role is None]
    if missing:
        raise ValueError(f"Every activity needs a role; missing for activities starting at {missing[:20]}")
    return activities


def _sanitize_tightening(edit, activity, unit_by_id):
    """Drop stage-2 cuts inside teaching that break the rules of a clean cut.

    Applies only where the effective category is academic; confirming a wholly
    unrelated activity keeps the strict stage-1 validation below.
    """
    effective = edit.subactivities or [activity]
    academic = {uid for child in effective if child.kind in ACADEMIC
                for uid in range(child.first_id, child.last_id + 1)}
    essential = set(edit.essential_ids)
    kept, dropped = [], []
    for cut in edit.cuts:
        ids = set(range(cut.first_id, cut.last_id + 1))
        if cut.category in TIGHTENING and not cut.standalone:
            dropped.append(cut)  # The model itself says the remainder would not read cleanly.
        elif (cut.category in TIGHTENING and ids <= academic and
                (ids & essential or any(
                    unit_by_id[uid].text.lstrip().lower().startswith("[no reliable speech")
                    for uid in ids if uid in unit_by_id))):
            dropped.append(cut)
        else:
            kept.append(cut)
    edit.cuts = kept

    def seconds(ids):
        return sum(unit_by_id[uid].end - unit_by_id[uid].start for uid in ids if uid in unit_by_id)

    for child in effective:
        if child.kind not in ACADEMIC:
            continue
        span = set(range(child.first_id, child.last_id + 1))
        polish = [c for c in edit.cuts if c.category in TIGHTENING and set(range(c.first_id, c.last_id + 1)) <= span]
        if not polish:
            continue
        removed = {uid for c in polish for uid in range(c.first_id, c.last_id + 1)}
        if seconds(removed) > MAX_TIGHTENING_SHARE * max(seconds(span), 1e-6):
            edit.cuts = [c for c in edit.cuts if c not in polish]
            dropped.extend(polish)
            continue
        # A polish cut must not strand a short piece of teaching between cuts.
        for cut in sorted(polish, key=lambda c: c.first_id):
            others = {uid for c in edit.cuts if c is not cut for uid in range(c.first_id, c.last_id + 1)}
            cut_ids = others | set(range(cut.first_id, cut.last_id + 1))
            runs, run = [], []
            for uid in sorted(span):
                if uid in cut_ids:
                    if run:
                        runs.append(run)
                    run = []
                else:
                    run.append(uid)
            if run:
                runs.append(run)
            if any(seconds(r) < MIN_KEPT_PIECE and len(runs) > 1 for r in runs):
                edit.cuts = [c for c in edit.cuts if c is not cut]
                dropped.append(cut)
    return dropped


def validate_edits(response, activities, units, known_activities=None):
    expected = {a.first_id: a for a in activities}
    unit_by_id, seen = {u.id: u for u in units}, set()
    for edit in response.activities:
        if edit.activity_id not in expected or edit.activity_id in seen:
            raise ValueError("Missing, duplicate or foreign activity edit")
        seen.add(edit.activity_id)
        activity = expected[edit.activity_id]
        valid = set(range(activity.first_id, activity.last_id + 1))
        if edit.subactivities:
            if any(child.continues_previous for child in edit.subactivities):
                raise ValueError("Editorial subactivities cannot continue an analysis window")
            earlier = [a for a in (known_activities or activities) if a.first_id < activity.first_id]
            children = validate_map(LessonMap(activities=edit.subactivities),
                                    [unit_by_id[uid] for uid in sorted(valid)], earlier)
            edit.subactivities = children
        if not set(edit.essential_ids) <= valid:
            raise ValueError("Essential IDs outside their activity")
        if any(not (activity.first_id <= c.first_id <= c.last_id <= activity.last_id) for c in edit.cuts):
            raise ValueError("Cut endpoints outside their activity")
        _sanitize_tightening(edit, activity, unit_by_id)
        cut_ids = set()
        for cut in edit.cuts:
            if cut.first_id not in valid or cut.last_id not in valid or cut.last_id < cut.first_id:
                raise ValueError("Cut endpoints outside their activity")
            ids = set(range(cut.first_id, cut.last_id + 1))
            if ids & cut_ids or ids & set(edit.essential_ids):
                raise ValueError("Cuts overlap each other or essential teaching")
            if cut.category not in CUT_CATEGORIES or not cut.standalone:
                raise ValueError("Only self-contained unrelated passages can be cut")
            cut_ids.update(ids)
        # Reject a micro-cut that strands the rest of an explanation. Preserve a
        # genuinely short WHOLE activity (e.g. a final answer) without a duration rule.
        # A gap inside confirmed teaching is protected. A gap inside an activity
        # independently mapped AND edited as wholly nonacademic is not board work
        # by default. Refined academic children restore that protection locally.
        effective = edit.subactivities or [activity]
        academic_ids = {uid for child in effective if child.kind != ActivityKind.NONACADEMIC
                        for uid in range(child.first_id, child.last_id + 1)}
        protected = {uid for uid in academic_ids
                     if unit_by_id[uid].text.lstrip().lower().startswith("[no reliable speech")}
        crossing = [c for c in edit.cuts if set(range(c.first_id, c.last_id + 1)) & protected
                    and set(range(c.first_id, c.last_id + 1)) - protected]
        if crossing:
            # Keep the protected gap and still remove the unrelated speech beside it:
            # trim each such cut to its runs of ordinary speech. Never fail the lecture.
            trimmed = []
            for c in edit.cuts:
                if c not in crossing:
                    trimmed.append(c)
                    continue
                run = []
                for uid in range(c.first_id, c.last_id + 2):
                    if uid <= c.last_id and uid not in protected:
                        run.append(uid)
                    elif run:
                        trimmed.append(c.model_copy(update={"first_id": run[0], "last_id": run[-1]}))
                        run = []
            edit.cuts = trimmed
            edit.review_required = True
            edit.review_reason = "Untranscribed board work preserved; unrelated speech beside it was removed."
            cut_ids = {uid for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
        if any(set(range(c.first_id, c.last_id + 1)) & protected for c in edit.cuts):
            edit.cuts = [c for c in edit.cuts if not set(range(c.first_id, c.last_id + 1)) & protected]
            edit.review_required = True
            edit.review_reason = "Untranscribed board work preserved; unrelated passages can still be removed."
            cut_ids = {uid for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
        remaining = [i for i in range(activity.first_id, activity.last_id + 1) if i not in cut_ids]
        if activity.kind == ActivityKind.UNCERTAIN and not edit.subactivities:
            edit.cuts = []
            edit.review_required = True
            edit.review_reason = edit.review_reason or "Uncertain speech/board work preserved."
        elif any(child.kind in ACADEMIC for child in effective) and edit.cuts and not remaining:
            raise StudyReviewRequired(
                f"Activity {activity.first_id}: the editor would delete all mapped teaching. "
                "Preserve its actual study, or explicitly correct a wholly nonacademic map with "
                "a full-range nonacademic subactivity and its confirmed cut.")
        if edit.cuts and remaining:
            runs = []
            for uid in remaining:
                if runs and uid == runs[-1][-1]+1:
                    runs[-1].append(uid)
                else:
                    runs.append([uid])
            if any(unit_by_id[r[-1]].end-unit_by_id[r[0]].start < 5
                   and not any(child.first_id == r[0] and child.last_id == r[-1]
                               and child.kind in ACADEMIC for child in edit.subactivities)
                   for r in runs):
                raise StudyReviewRequired(
                    f"Activity {activity.first_id}: the cut strands a short explanation fragment. "
                    "Keep short academic content intact and recheck the boundaries. If it is a complete "
                    "short answer/explanation, identify it as a complete subactivity; do not restore chatter.")
        if edit.subactivities:
            child_edits = _divide_edit(edit, edit.subactivities)
            checked = validate_edits(LessonEdits(activities=child_edits), edit.subactivities,
                                     [unit_by_id[uid] for uid in sorted(valid)],
                                     known_activities=known_activities or activities)
            before = {uid for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
            after = {uid for child in checked for c in child.cuts for uid in range(c.first_id, c.last_id + 1)}
            if before != after:
                raise ValueError("Subactivity boundaries conflict with approved cuts; preserve coherent passages "
                                 "and mark wholly unrelated cut activities nonacademic")
    if seen != expected.keys():
        raise ValueError("Editor must review every requested activity exactly once")
    return sorted(response.activities, key=lambda e: e.activity_id)


def validate_study_edits(response, activities, units, known_activities=None):
    """Force explicit map/editor agreement instead of exporting rescued chatter."""
    checked = validate_edits(response, activities, units, known_activities)
    by_id = {a.first_id: a for a in activities}
    for edit in checked:
        cut_ids = {uid for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
        for child in edit.subactivities or [by_id[edit.activity_id]]:
            if child.kind == ActivityKind.UNCERTAIN:
                raise StudyReviewRequired(
                    f"Activity {child.first_id}: resolve its study purpose using corrected subactivities "
                    "(theory including explanatory examples; questions including related theory; or "
                    "nonacademic with cuts). Uncertain categories cannot be exported to both videos.")
            if child.kind == ActivityKind.NONACADEMIC:
                missing = set(range(child.first_id, child.last_id + 1)) - cut_ids
                if missing:
                    raise StudyReviewRequired(
                        f"Activity {child.first_id}: nonacademic IDs {sorted(missing)[:40]} remain uncut. "
                        "Confirm removal of the unrelated passage, or correct its map with subactivities "
                        "that preserve actual study and separately cut every nonacademic portion.")
    return checked


def _divide_edit(edit, children):
    """Partition validated source decisions; category refinement cannot add cuts."""
    divided = []
    for child in children:
        cuts = []
        for cut in edit.cuts:
            first, last = max(child.first_id, cut.first_id), min(child.last_id, cut.last_id)
            if first <= last:
                cuts.append(cut.model_copy(update={"first_id": first, "last_id": last}))
        divided.append(ActivityEdit(activity_id=child.first_id, cuts=cuts,
            essential_ids=[uid for uid in edit.essential_ids if child.first_id <= uid <= child.last_id],
            review_required=edit.review_required or child.needs_visual_review or child.kind == ActivityKind.UNCERTAIN,
            review_reason=edit.review_reason))
    return divided


def refine_activities(units, activities, edits):
    """Apply optional editorial map corrections with strict whole-graph validation.

    An external dependency on an old parent still means its full academic context,
    so splitting that parent cannot silently shorten another output's prerequisite.
    A dependency on an earlier sibling refers precisely to that complete sibling.
    """
    original = validate_map(LessonMap(activities=activities), units)
    checked = validate_edits(LessonEdits(activities=[edit.model_copy(deep=True) for edit in edits]),
                             original, units, known_activities=original)
    by_edit = {edit.activity_id: edit for edit in checked}
    families = {a.first_id: [child.model_copy(deep=True) for child in (
        by_edit[a.first_id].subactivities or [a])] for a in original}
    refined, child_edits = [], []
    for parent in original:
        children = families[parent.first_id]
        sibling_ids = {child.first_id for child in children}
        for child in children:
            dependencies = []
            for dependency in dict.fromkeys([*parent.depends_on, *child.depends_on]):
                if dependency in sibling_ids:
                    if dependency >= child.first_id:
                        raise ValueError("Refined dependencies must refer to an earlier sibling")
                    dependencies.append(dependency)
                elif dependency in families and dependency < parent.first_id:
                    context = [a.first_id for a in families[dependency] if a.kind in ACADEMIC]
                    if not context:
                        raise ValueError("Refinement removed all academic context required by another activity")
                    dependencies.extend(context)
                else:
                    raise ValueError("Refinement has an unavailable or forward dependency anchor")
            child.depends_on = list(dict.fromkeys(dependencies))
            refined.append(child)
        child_edits.extend(_divide_edit(by_edit[parent.first_id], children))
    refined = validate_map(LessonMap(activities=refined), units)
    final_edits = validate_edits(LessonEdits(activities=child_edits), refined, units,
                                 known_activities=refined)
    before = {uid for edit in checked for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
    after = {uid for edit in final_edits for c in edit.cuts for uid in range(c.first_id, c.last_id + 1)}
    if before != after:
        raise ValueError("Refinement must preserve every validated cut")
    return refined, final_edits


def _keep_rejected(digest, attempt, exc, reply):
    """Keep a rejected model reply locally so a failure can be diagnosed without paying again."""
    try:
        folder = config.TEMP_DIR / "rejected_replies"
        folder.mkdir(exist_ok=True)
        _save(folder / f"{digest}_attempt{attempt + 1}.json",
              {"error": f"{type(exc).__name__}: {exc}", "reply": reply})
    except OSError:
        logger.warning("Could not keep the rejected reply for diagnosis")


def _save(path, data):
    staging = path.with_suffix(f".{uuid.uuid4().hex}.partial")
    try:
        staging.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


MODE_NOTES = {
    "balanced": ("Mode: full explanations. In stage 2 cut only filler, waiting and word-for-word "
                 "mechanical repeats. Keep pedagogical restatements and recaps."),
    "aggressive": ("Mode: study essentials. In stage 2 also cut re-explanations and repeated calculations "
                   "once a point is clearly established, unless they add a step, insight, condition or "
                   "correction. Keep every solution complete and learnable."),
}


def classify_activities(units, context="", backend="auto", mode="balanced", use_cache=True,
                        progress=None, warnings=None, video=None, visual_review=False, provider_session=None,
                        pauses=None):
    from lecture_trimmer.analysis.classifier import ProviderQuotaError, _request, _window_prompt, resolve_backend
    from lecture_trimmer.analysis.subjects import subject_brief
    if not units:
        return ActivityLabels()
    subject, class_type, notes = subject_brief(context, units)
    pauses = pauses or {}

    def window_prompt(window):
        return _window_prompt(window, context, notes=notes, pauses=pauses)
    warnings = [] if warnings is None else warnings
    notify = progress or (lambda fraction, desc: logger.info(desc))
    backend = resolve_backend(backend, provider_session=provider_session)
    model = config.CLAUDE_MODEL if backend == "claude" else config.GEMINI_MODEL
    signature = [VERSION, backend, model, context, mode, notes, sorted(pauses.items()), [u.model_dump() for u in units],
                 bool(visual_review), str(video) if visual_review else None,
                 LESSON_MAP_PROMPT, PASSAGE_EDIT_PROMPT, VISUAL_REVIEW_PROMPT]
    key = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:24]
    cache = config.TEMP_DIR / f"lessons_{key}.json"
    if use_cache and cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            mapped = LessonMap.model_validate({"activities": saved["activities"]})
            activities = validate_map(mapped, units)
            edits = validate_study_edits(LessonEdits.model_validate({"activities": saved["edits"]}), activities, units)
            warnings.extend(w for w in saved.get("warnings", []) if w not in warnings)
            notify(1, "Using saved complete-lesson analysis")
            result = make_labels(units, activities, edits, saved.get("evidence"))
            result.subject, result.class_type = subject, class_type
            return result
        except (ValueError, KeyError, TypeError, OSError):
            logger.warning("Ignoring invalid complete-lesson cache %s", cache.name)
    if provider_session is not None:
        client = provider_session.client(backend)
        workers = config.GEMINI_PARALLEL_REQUESTS
    elif backend == "gemini":
        from google import genai
        client = genai.Client(api_key=config.GOOGLE_API_KEY, http_options={"timeout": 180000})
        workers = config.GEMINI_PARALLEL_REQUESTS
    elif backend == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=180)
        workers = min(config.CLASSIFY_PARALLEL_REQUESTS, 3)
    else:
        raise ValueError(f"Unknown classifier backend: {backend}")

    cancelled = threading.Event()
    def request(prompt, instructions, schema, validate, extra_key=None):
        check_cancelled()
        if cancelled.is_set():
            raise RuntimeError("Analysis stopped after an earlier provider error; completed work is saved.")
        key_data = [VERSION, backend, model, instructions, extra_key or prompt, schema.__name__]
        digest = hashlib.sha256(json.dumps(key_data, ensure_ascii=False).encode()).hexdigest()[:24]
        saved = config.TEMP_DIR / f"activity_{digest}.json"
        if use_cache and saved.exists():
            try:
                return validate(schema.model_validate_json(saved.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError, OSError):
                logger.warning("Invalid activity cache %s", saved.name)
        current = prompt
        for attempt in range(2):
            if cancelled.is_set():
                raise RuntimeError("Analysis stopped after an earlier provider error; completed work is saved.")
            result = None
            try:
                with AI_SLOTS:
                    result = _request(client, backend, current, instructions, schema)
                raw = result.model_dump(mode="json")  # Before validation repairs it in place.
                validated = validate(result)
            except (ValueError, TypeError) as exc:
                _keep_rejected(digest, attempt, exc, raw if result is not None else None)
                if attempt:
                    raise
                correction = f"\nCorrect the invalid response: {exc}. Use supplied IDs and preserve all essential content."
                current = prompt + correction if isinstance(prompt, str) else [*prompt, correction]
                continue
            _save(saved, result.model_dump(mode="json"))
            return validated

    try:
        activities = []
        # Bound token size; preceding whole-activity summaries let the first activity
        # continue across a computational window without creating an edit boundary.
        for first in range(0, len(units), MAP_WINDOW_UNITS):
            chunk = units[first:first+MAP_WINDOW_UNITS]
            notify(.05, "Mapping whole explanations and complete exercises…")
            previous = json.dumps([a.model_dump(mode="json") for a in activities[-30:]], ensure_ascii=False)
            prompt = (window_prompt(chunk) + "\nPreceding activities (context only):\n" + previous +
                      "\nIf the first target activity continues the preceding final activity, set continues_previous=true. "
                      "Do not split a problem merely because this input window ends.")
            mapped = request(prompt, LESSON_MAP_PROMPT, LessonMap,
                             lambda result: require_roles(validate_map(result, chunk, activities)))
            if mapped[0].continues_previous:
                prior, continuation = activities[-1], mapped.pop(0)
                if prior.kind != continuation.kind:
                    prior.kind = ActivityKind.UNCERTAIN
                    prior.needs_visual_review = True
                    warnings.append("A teaching activity crosses an analysis window with uncertain category; preserved for review.")
                prior.last_id = continuation.last_id
                prior.summary += " " + continuation.summary
                prior.needs_visual_review |= continuation.needs_visual_review
                prior.depends_on = list(dict.fromkeys(
                    dependency for dependency in [*prior.depends_on, *continuation.depends_on]
                    if dependency not in {prior.first_id, continuation.first_id}))
                # Dependencies within the continued activity become internal context.
                for a in mapped:
                    a.depends_on = list(dict.fromkeys(prior.first_id if d == continuation.first_id else d for d in a.depends_on))
            activities.extend(mapped)
        validate_map(LessonMap(activities=activities), units)
        evidence = {}
        candidates = [a for a in activities if a.needs_visual_review or a.kind == ActivityKind.UNCERTAIN]
        if visual_review and video and backend == "gemini" and candidates:
            from google.genai import types
            from lecture_trimmer.analysis.visual_evidence import sample_evidence
            by_id = {u.id: u for u in units}
            # Explicit fixed budget: never submit a full lecture or one image per unit.
            selected = sorted(candidates, key=lambda a: by_id[a.last_id].end-by_id[a.first_id].start, reverse=True)[:8]
            notify(.22, "Checking uncertain teaching sections against selected video frames…")
            def evidence_interval(activity):
                missing = [u for u in units if activity.first_id <= u.id <= activity.last_id
                           and u.text.lstrip().lower().startswith("[no reliable speech")]
                if missing:
                    gap = max(missing, key=lambda u: u.end-u.start)
                    return (max(by_id[activity.first_id].start, gap.start-2),
                            min(by_id[activity.last_id].end, gap.end+2))
                return by_id[activity.first_id].start, by_id[activity.last_id].end
            local = sample_evidence(video, units[-1].end,
                intervals=[evidence_interval(a) for a in selected],
                max_frames=24, budget_seconds=30)
            evidence = local.to_report()
            evidence["reviews"] = []
            contents, cache_inputs, included = [], [], {}
            for activity in selected:
                frames = local.frames_for(by_id[activity.first_id].start, by_id[activity.last_id].end, limit=3)
                if not frames:
                    continue
                text = window_prompt([u for u in units if activity.first_id <= u.id <= activity.last_id])
                text += "\nActivity: " + activity.model_dump_json()
                contents.append(text)
                cache_inputs.append([text, [frame.sha256 for frame in frames]])
                included[activity.first_id] = activity
                for frame in frames:
                    contents.extend([f"Activity {activity.first_id}, source timestamp {frame.timestamp:.2f} seconds",
                                     types.Part.from_bytes(data=frame.read_jpeg(), mime_type="image/jpeg")])
            contents.append("Return one review for EACH supplied activity_id. The photos after each activity belong to that activity only.")
            def validate_visual(result):
                ids = [r.activity_id for r in result.reviews]
                if len(ids) != len(set(ids)) or set(ids) != set(included):
                    raise ValueError("Visual reviews must cover exactly the requested activities")
                return result
            if included:
                try:
                    reviewed = request(contents, VISUAL_REVIEW_PROMPT, VisualReviews,
                                       validate_visual, extra_key=cache_inputs)
                    evidence["reviews"] = [r.model_dump(mode="json") for r in reviewed.reviews]
                    for review in reviewed.reviews:
                        activity = included[review.activity_id]
                        # Visuals may clarify/rescue teaching, never introduce a deletion.
                        if review.kind in ACADEMIC:
                            activity.kind = review.kind
                            activity.needs_visual_review = review.kind == ActivityKind.UNCERTAIN
                        elif review.preserve:
                            # A mixed closing passage may contain two useful doubts
                            # amid logistics. Preserve those parts; do not protect
                            # the entire unrelated speech from the passage editor.
                            activity.needs_visual_review = True
                except ProviderQuotaError:
                    # A known exhausted quota also prevents the editorial pass;
                    # don't send more doomed requests or silently lower quality.
                    raise
                except Exception as exc:
                    logger.warning("Visual review unavailable: %s", type(exc).__name__)
                    warnings.append("Optional visual verification was unavailable; uncertain teaching was preserved for review.")
        elif visual_review and backend != "gemini":
            warnings.append("Frame review currently supports Gemini; transcript analysis and preservation remain active.")

        # Pack whole activities, not overlapping tiny transcript windows. Common
        # two-hour lectures require one map plus roughly 10-15 parallel edit calls.
        batches, batch, count = [], [], 0
        for activity in activities:
            size = activity.last_id-activity.first_id+1
            if batch and count+size > EDIT_BATCH_UNITS:
                batches.append(batch)
                batch, count = [], 0
            batch.append(activity)
            count += size
        if batch:
            batches.append(batch)
        def edit_batch(batch):
            window = [u for u in units if batch[0].first_id <= u.id <= batch[-1].last_id]
            prompt = window_prompt(window) + "\nActivities to review:\n" + json.dumps(
                [a.model_dump(mode="json") for a in batch], ensure_ascii=False)
            prompt += ("\nFor an activity mapped nonacademic, independently confirm its ENTIRE passage is "
                       "unrelated and return a whole-activity cut, or correct the map with subactivities "
                       "that retain actual study and cut unrelated portions. Resolve uncertain categories "
                       "from context instead of copying them into both outputs. In an activity flagged for visual "
                       "review, preserve untranscribed/uncertain units but still remove clearly unrelated "
                       "self-contained passages whose boundaries and independence are established. "
                       + MODE_NOTES.get(mode, MODE_NOTES["balanced"]) + " Never trade coherent teaching for brevity.")
            prompt += "\nEarlier academic dependency anchors (context only):\n" + json.dumps(
                [{"first_id": a.first_id, "kind": a.kind.value, "title": a.title, "summary": a.summary}
                 for a in activities if a.first_id < batch[0].first_id and a.kind in ACADEMIC], ensure_ascii=False)
            def validate_batch(response):
                require_roles([child for edit in response.activities for child in edit.subactivities])
                return validate_study_edits(response, batch, window, known_activities=activities)
            try:
                return request(prompt, PASSAGE_EDIT_PROMPT, LessonEdits, validate_batch)
            except ValueError:
                # A big batch that fails twice is usually one hard activity. Retry the
                # halves (new, smaller requests) instead of abandoning a paid analysis.
                if len(batch) == 1 or cancelled.is_set():
                    raise
                middle = len(batch) // 2
                logger.warning("Edit batch %s-%s failed validation twice; retrying it in two halves",
                               batch[0].first_id, batch[-1].last_id)
                return edit_batch(batch[:middle]) + edit_batch(batch[middle:])
        edits = []
        with ThreadPoolExecutor(max_workers=min(workers, 3)) as pool:
            futures = [submit_with_context(pool, edit_batch, batch) for batch in batches]
            for done, future in enumerate(as_completed(futures), 1):
                try:
                    edits.extend(future.result())
                except Exception:
                    cancelled.set()
                    for pending in futures:
                        pending.cancel()
                    raise
                notify(.3 + .7*done/len(batches), f"Reviewed complete teaching sections {done}/{len(batches)}")
        edits.sort(key=lambda e: e.activity_id)
        activities, edits = refine_activities(units, activities, edits)
        validate_study_edits(LessonEdits(activities=edits), activities, units)
        # Uncertain inputs cannot be safely removed even if an editor disregards the prompt.
        for a, e in zip(activities, edits):
            if a.kind == ActivityKind.UNCERTAIN:
                e.cuts = []
                e.review_required = True
                e.review_reason = e.review_reason or "Uncertain speech/board work preserved."
            elif a.needs_visual_review:
                e.review_required = True
                e.review_reason = e.review_reason or "Uncertain parts preserved; self-contained unrelated passages removed."
        result = make_labels(units, activities, edits, evidence)
        result.subject, result.class_type = subject, class_type
        _save(cache, {"activities": [a.model_dump(mode="json") for a in result.activities],
                      "edits": [e.model_dump(mode="json") for e in edits], "warnings": warnings,
                      "evidence": evidence})
        return result
    finally:
        client.close()


def make_labels(units, activities, edits, evidence=None):
    by_id = {e.activity_id: e for e in edits}
    result = ActivityLabels(activities=activities, edits=edits, evidence=evidence)
    for a in activities:
        edit = by_id[a.first_id]
        cuts = {uid: c for c in edit.cuts for uid in range(c.first_id, c.last_id+1)}
        uncertain_remainder = a.kind == ActivityKind.NONACADEMIC and len(cuts) < a.last_id-a.first_id+1
        if uncertain_remainder:
            raise StudyReviewRequired("Nonacademic content must be cut or explicitly reclassified before export.")
        label = Label.WORKED_EXAMPLE if a.kind == ActivityKind.QUESTIONS else Label.CORE_TEACHING
        if a.kind == ActivityKind.NONACADEMIC and not uncertain_remainder:
            label = Label.CHATTER
        for uid in range(a.first_id, a.last_id+1):
            cut = cuts.get(uid)
            result[uid] = UnitLabel(unit_id=uid, label=label,
                confidence=.5 if a.kind == ActivityKind.UNCERTAIN or uncertain_remainder else .9,
                reason=a.summary, retention=Retention(cut.category.value) if cut else Retention.ACADEMIC,
                retention_reason=cut.reason if cut else "Complete teaching activity",
                activity_id=a.first_id, activity_title=a.title,
                review_required=edit.review_required or a.needs_visual_review or a.kind == ActivityKind.UNCERTAIN)
    return result
