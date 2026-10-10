"""One analysis shared by the UI and CLI, with isolated files for each export job."""
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.media.audio import extract_audio, probe_duration
from lecture_trimmer.analysis.classifier import build_units, classify
from lecture_trimmer.media.cutter import RENDER_VERSION, cut_and_join, keyframe_times, partition_plans, select_encoder, snap_to_keyframes
from lecture_trimmer.processing.editor import build_keep_list, exclude_ranges, snap_to_speech
from lecture_trimmer.models import KeepRange, Transcript
from lecture_trimmer.media.transcriber import (BACKEND as SPEECH_BACKEND, fit_segments_to_source,
                                               normalise_language, transcribe)
from lecture_trimmer.analysis.prompts import VERSION
from lecture_trimmer.processing.job_control import check_cancelled, ProcessingCancelled

logger = logging.getLogger("trimmer")
OUTPUT_TITLES = {"questions": "Question solving", "theory": "Theory", "combined": "Theory + questions"}
QUESTION_LABELS = {"worked_example", "doubt_solving"}
THEORY_LABELS = {"core_teaching", "low_density_teaching"}


def snapshot_source(video, notify):
    """Own a stable copy: uploads can be replaced or removed during long jobs."""
    root = config.TEMP_DIR / "sources"
    root.mkdir(exist_ok=True)
    staging = root / f"{uuid.uuid4().hex}.partial"
    before = video.stat()
    digest = hashlib.sha256()
    copied = 0
    try:
        with video.open("rb") as src, staging.open("wb") as dst:
            while block := src.read(8 * 1024 * 1024):
                digest.update(block)
                dst.write(block)
                copied += len(block)
                notify(.02, f"Securing source video — {copied/max(1, before.st_size)*100:.0f}%")
        after = video.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Source changed while being copied. Please wait for the upload to finish and retry.")
        target = root / f"{digest.hexdigest()}{video.suffix.lower()}"
        if not target.exists():
            staging.replace(target)
        return target
    finally:
        staging.unlink(missing_ok=True)


# Fields the cache key has always contained, from engines that have since been
# removed. Kept byte-identical so transcripts saved earlier are still found and
# an old lecture is never transcribed (and re-analysed) again.
_LEGACY_KEY_FIELDS = ["saaras:v4", None, "unknown", "auto", "large-v3-turbo"]


def transcript_cache(video: Path, language: str | None) -> Path:
    stat = video.stat()
    signature = [str(video.resolve()), stat.st_size, stat.st_mtime_ns, SPEECH_BACKEND,
                 normalise_language(language), config.GROQ_ASR_MODEL, *_LEGACY_KEY_FIELDS,
                 os.getenv("WHISPER_MODEL_CPU", "large-v3-turbo" if config.FROZEN else "small")]
    digest = hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:24]
    return config.TEMP_DIR / f"transcript_{digest}.json"


def get_transcript(video: Path, language: str | None, fresh: bool, progress=None) -> Transcript:
    notify = progress or (lambda fraction, desc: logger.info(desc))
    cache = transcript_cache(video, language)
    if cache.exists() and not fresh:
        notify(0.1, "Using the saved transcript…")
        saved = Transcript.model_validate_json(cache.read_text(encoding="utf-8"))
        # Transcripts saved before segment timing was fitted to the source can overhang its end.
        return saved.model_copy(update={"segments": fit_segments_to_source(saved.segments, saved.duration)})
    with tempfile.TemporaryDirectory(prefix="audio_", dir=config.TEMP_DIR) as work:
        notify(0.08, "Extracting lecture audio…")
        wav = extract_audio(video, out_dir=Path(work),
                            progress=lambda f, desc: notify(.08 + .07*f, desc))
        notify(0.15, "Transcribing the lecture. Long recordings can take several minutes…")
        transcript = transcribe(wav, language, cache_dir=cache.with_suffix(''), use_cache=not fresh,
                                progress=lambda f, desc: notify(None if f is None else .15 + .20*f, desc))
    staging = cache.with_name(f"{cache.name}.{uuid.uuid4().hex}.partial")
    try:
        staging.write_text(transcript.model_dump_json(indent=1), encoding="utf-8")
        staging.replace(cache)
    finally:
        staging.unlink(missing_ok=True)
    return transcript


def plan_outputs(units, labels, transcript, duration, mode="balanced", *, min_gap=None,
                 padding=None, combined_labels=None, pauses=None, tighten=False, tightened=None):
    """Plan kept ranges. With measured `pauses`, cuts land on silence and, if
    `tighten`, long dead air inside kept speech is shortened (seconds saved per
    output are written into `tightened`)."""
    settings = config.MODES[mode]
    if hasattr(labels, "activities"):
        from lecture_trimmer.processing.lesson_planner import plan_activities
        planned = plan_activities(units, labels, duration,
            settings["edge_padding"] if padding is None else padding, combined_labels)[0]
        if pauses:
            from lecture_trimmer.media.pauses import snap_to_pauses, tighten_pauses
            for kind, ranges in planned.items():
                ranges = snap_to_pauses(ranges, pauses)
                if tighten:
                    ranges, saved = tighten_pauses(ranges, pauses, min_pause=settings["pause_min"],
                                                   keep=settings["pause_keep"])
                    if tightened is not None:
                        tightened[kind] = saved
                planned[kind] = [KeepRange(start=max(0, r.start), end=min(duration, r.end))
                                 for r in ranges if min(duration, r.end) > max(0, r.start)]
        return planned
    theory_labels = THEORY_LABELS & settings["keep_labels"]
    label_sets = {"questions": QUESTION_LABELS, "theory": theory_labels,
                  "combined": (QUESTION_LABELS | theory_labels) if combined_labels is None else combined_labels}
    result = {}
    for kind, keep in label_sets.items():
        # Padding and short-gap smoothing must not restore explicitly excluded speech.
        opposite = {label.label.value for label in labels.values()} - keep
        ranges = build_keep_list(
            units, labels, duration, keep,
            min_gap=settings["min_gap_to_cut"] if min_gap is None else min_gap,
            # Short answers and definitions are real content, not necessarily mislabels.
            min_island=0,
            padding=settings["edge_padding"] if padding is None else padding,
            blocked_labels=opposite,
        )
        ranges = snap_to_speech(ranges, transcript)
        blocked = [KeepRange(start=u.start, end=u.end) for u in units
                   if not labels[u.id].keep or labels[u.id].label.value in opposite]
        ranges = exclude_ranges(ranges, blocked)
        result[kind] = [KeepRange(start=max(0, r.start), end=min(duration, r.end))
                        for r in ranges if min(duration, r.end) > max(0, r.start)]
    return result


def complete_export_key(source_digest, encoder, cut_style, ranges):
    """Only identical source bytes and ordered render plans may share finished media."""
    if cut_style != "smooth" or not encoder:
        return None
    signature = {"cache_version": 1, "source_sha256": source_digest,
                 "render_version": RENDER_VERSION, "encoder": encoder, "cut_style": cut_style,
                 "ranges": [[r.start, r.end] for r in ranges]}
    return hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _copy_with_digest(source, destination):
    digest = hashlib.sha256()
    before = source.stat()
    with source.open("rb") as src, destination.open("wb") as dst:
        while block := src.read(8 * 1024 * 1024):
            digest.update(block)
            dst.write(block)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("Completed video changed while being copied")
    return digest.hexdigest()


def reuse_complete_export(cache_root, key, out):
    """Copy a previously validated immutable export into this job's own directory.

    Hashing during the copy detects corruption even if file size and mtime were
    restored. Missing, unfinished or altered cache entries are ordinary misses.
    """
    if not key:
        return None
    staging = out.with_name(f"{uuid.uuid4().hex}.partial")
    try:
        manifest = json.loads((cache_root / f"{key}.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return None
        digest = manifest.get("sha256", "")
        if (manifest.get("key") != key or manifest.get("validation") != "full_decoded_audio_v1"
                or manifest.get("render_version") != RENDER_VERSION
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            return None
        cached = cache_root / f"{digest}.mp4"
        stat = cached.stat()
        if stat.st_size <= 0 or (stat.st_size, stat.st_mtime_ns) != (manifest.get("size"), manifest.get("mtime_ns")):
            return None
        duration = manifest.get("duration")
        if type(duration) not in (int, float) or not 0 < duration < float("inf"):
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        if _copy_with_digest(cached, staging) != digest:
            logger.warning("Completed export cache checksum mismatch; rendering again")
            return None
        # Never return a shared cache path or another user's job path.
        staging.replace(out)
        return {"duration": duration, "encoder": manifest.get("encoder"), "reused": True}
    except (OSError, ValueError, TypeError):
        return None
    finally:
        staging.unlink(missing_ok=True)


def _finished_export_encoder(out):
    import subprocess
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream_tags=encoder", "-of", "json", str(out)],
        capture_output=True, text=True, check=True, timeout=30)
    streams = json.loads(result.stdout).get("streams", [])
    tag = streams[0].get("tags", {}).get("encoder", "") if streams else ""
    return next((encoder for encoder in ("h264_nvenc", "h264_qsv", "libx264")
                 if encoder in tag), None)


def save_complete_export(cache_root, key, out, duration, encoder):
    """Call only after cut_and_join's full smooth-export audio gate succeeds."""
    if not key:
        return
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = cache_root / f"{uuid.uuid4().hex}.partial"
    manifest_staging = cache_root / f"{uuid.uuid4().hex}.json.partial"
    try:
        digest = _copy_with_digest(out, staging)
        cached = cache_root / f"{digest}.mp4"
        # Content-addressed blobs make concurrent manifests refer to their own
        # validated bytes. A manifest is never published before its media exists.
        staging.replace(cached)
        stat = cached.stat()
        manifest = {"key": key, "sha256": digest, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                    "duration": duration, "encoder": encoder, "render_version": RENDER_VERSION,
                    "validation": "full_decoded_audio_v1"}
        manifest_staging.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_staging.replace(cache_root / f"{key}.json")
    finally:
        staging.unlink(missing_ok=True)
        manifest_staging.unlink(missing_ok=True)


def process_video(video: Path, *, outputs=None, mode="balanced", context="", language=None,
                  classifier=config.CLASSIFIER_BACKEND, cut_style="smooth",
                  fresh=False, dry_run=False, min_gap=None, padding=None,
                  combined_labels=None, combined_out: Path | None = None, progress=None, on_result=None,
                  owner_id=None, visual_review=False, provider_session=None, stage_gate=None):
    """`stage_gate(name)` (optional) is a context manager that admits this lecture into
    the 'prepare', 'transcribe', 'analyse' and 'render' stages; the job scheduler uses
    it to run several lectures as an assembly line."""
    from contextlib import nullcontext
    gate = stage_gate or (lambda name: nullcontext())
    selected = list(OUTPUT_TITLES) if outputs is None else list(dict.fromkeys(outputs))
    if not selected or any(k not in OUTPUT_TITLES for k in selected):
        raise ValueError("Choose at least one valid output video")
    if mode not in config.MODES or cut_style not in {"smooth", "fast"}:
        raise ValueError("Unknown trim mode or cut style")
    if combined_labels and not set(combined_labels) <= (QUESTION_LABELS | THEORY_LABELS):
        raise ValueError("Study exports can include only academic categories")
    if combined_out and ("combined" not in selected or combined_out.suffix.lower() != ".mp4"):
        raise ValueError("--out requires the combined output and an .mp4 filename")
    if combined_out and combined_out.resolve() == video.resolve():
        raise ValueError("The output must not overwrite the original video")
    if not video.is_file():
        raise ValueError("Video file does not exist")
    started = time.perf_counter()
    callback = progress or (lambda fraction, desc: logger.info(desc))
    def notify(fraction, desc):
        check_cancelled()
        callback(fraction, desc)
    original = video
    legacy_cache = transcript_cache(original, language)
    with gate("prepare"):
        video = snapshot_source(original, notify)
        duration = probe_duration(video)
    # Measure real audio pauses alongside transcription; they guide the editor
    # and let every cut land on silence. Failure only disables that polish.
    from concurrent.futures import ThreadPoolExecutor
    from lecture_trimmer.media.pauses import detect_pauses, pause_markers
    pause_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pause-detect")
    pause_future = pause_pool.submit(detect_pauses, video, duration)
    pause_pool.shutdown(wait=False)
    # Retain valid pre-upgrade transcription work when the original upload still matches.
    cache = transcript_cache(video, language)
    if not fresh and not cache.exists() and legacy_cache.exists():
        try:
            old = Transcript.model_validate_json(legacy_cache.read_text(encoding="utf-8"))
            if abs(old.duration-duration) < 2:
                shutil.copyfile(legacy_cache, cache)
        except (ValueError, OSError):
            logger.warning("Could not migrate older transcript cache")
    job_dir = config.OUTPUT_DIR / uuid.uuid4().hex
    job_dir.mkdir()
    report_path = job_dir / "analysis.json"
    report = {"video": original.name, "source": str(video.resolve()), "duration": duration,
              "mode": mode, "cut_style": cut_style, "classification_version": VERSION, "context": context,
              "render_version": RENDER_VERSION, "owner_id": owner_id,
              "visual_review_requested": bool(visual_review), "timings": {}, "analysis_only": bool(dry_run),
              "status": "analysing", "outputs": {}, "units": [], "warnings": []}
    def save_report():
        report["elapsed"] = time.perf_counter() - started
        if provider_session is not None:
            report["ai_usage"] = provider_session.usage.snapshot()
        staging = report_path.with_suffix(".partial")
        staging.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        staging.replace(report_path)
        if on_result:
            on_result({**report, "report_path": str(report_path.resolve())})
    save_report()
    try:
        try:
            stage_started = time.perf_counter()
            reused_transcript = cache.exists() and not fresh
            report["transcription"] = {"backend": SPEECH_BACKEND, "model": config.GROQ_ASR_MODEL,
                                       "reused": reused_transcript}
            save_report()
            with gate("transcribe"):
                transcript = get_transcript(video, language, fresh, progress=notify)
            report["timings"]["transcription_seconds"] = time.perf_counter()-stage_started
            from lecture_trimmer.media.transcript_export import write_transcript_files
            report['transcript_files'] = write_transcript_files(
                transcript, job_dir, video_name=original.name, backend=SPEECH_BACKEND, model=config.GROQ_ASR_MODEL)
            save_report()
            notify(0.34, "Measuring pauses in the audio…")
            units = build_units(transcript, unit_seconds=config.MODES[mode]["unit_seconds"])
            if not units:
                raise RuntimeError("No intelligible speech was transcribed. Try a different language or speech provider; the source is preserved.")
            from lecture_trimmer.analysis.classifier import add_untranscribed_gaps
            units = add_untranscribed_gaps(units, duration)
            try:
                pauses, threshold = pause_future.result(timeout=max(180, duration / 5))
                report["pauses"] = {"count": len(pauses), "threshold_db": threshold,
                                    "total_seconds": round(sum(p.length for p in pauses), 1)}
            except Exception:
                logger.warning("Pause detection unavailable", exc_info=True)
                pauses = []
                report["warnings"].append("Audio pause detection was unavailable; cuts use transcript timing only.")
            stage_started = time.perf_counter()
            with gate("analyse"):
                notify(0.35, "Identifying theory and complete question-solving sections…")
                labels = classify(units, context=context, backend=classifier, mode=mode, use_cache=not fresh,
                                  progress=lambda f, desc: notify(.35 + .2*f, desc),
                                  warnings=report["warnings"], video=video, visual_review=visual_review,
                                  pauses=pause_markers(units, pauses),
                                  **({"provider_session": provider_session} if provider_session is not None else {})) if units else {}
            report["timings"]["analysis_seconds"] = time.perf_counter()-stage_started
        except Exception as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = record_error(exc, stage='analysis', owner=owner_id)
            exc.diagnostic_reference = reference
            report.update(status="failed", error="Analysis failed", error_reference=reference)
            save_report()
            raise
        tightened = {}
        planned = plan_outputs(units, labels, transcript, duration, mode,
                               min_gap=min_gap, padding=padding, combined_labels=combined_labels,
                               pauses=pauses, tightened=tightened,
                               tighten=config.TIGHTEN_PAUSES and cut_style == "smooth")
        if cut_style == "fast":
            keyframes = keyframe_times(video)
            snapped = {k: snap_to_keyframes(r, video, keyframes=keyframes, duration=duration)
                       for k, r in planned.items()}
            if any(exclude_ranges(snapped[k], planned[k]) for k in selected):
                # Stream copy cannot honor a non-keyframe content boundary. A faster export
                # must not silently bring back the very speech the user asked to remove.
                cut_style = "smooth"
                report["cut_style"] = "smooth"
                report["warnings"].append("Used precise cuts because fast keyframe cuts would restore excluded speech.")
            else:
                planned = snapped
        report.update({
            "language": transcript.language, "status": "analysed" if dry_run else "exporting",
            "units": [{**u.model_dump(), **labels[u.id].model_dump(mode="json", exclude={"unit_id"})}
                      for u in units],
            "outputs": {k: {"keep": [r.model_dump() for r in planned[k]],
                            "status": "no_matching_content" if not planned[k] else "planned",
                            "path": None, "duration": sum(r.end-r.start for r in planned[k])}
                        for k in selected},
        })
        if hasattr(labels, "activities"):
            from lecture_trimmer.processing.lesson_planner import plan_activities
            import statistics
            _, contexts = plan_activities(units, labels, duration,
                config.MODES[mode]["edge_padding"] if padding is None else padding, combined_labels)
            report["activities"] = [a.model_dump(mode="json") for a in labels.activities]
            report["passage_edits"] = [e.model_dump(mode="json") for e in labels.edits]
            report["visual_evidence"] = labels.evidence
            report["output_context"] = contexts
            report["subject"] = getattr(labels, "subject", None)
            report["class_type"] = getattr(labels, "class_type", None)
            for kind, item in report["outputs"].items():
                lengths = [r.end-r.start for r in planned[kind]]
                item["quality"] = {"continuous_sections": len(lengths),
                                   "sections_under_5_seconds": sum(s < 5 for s in lengths),
                                   "median_section_seconds": statistics.median(lengths) if lengths else 0}
            review_activities = [a.first_id for a in labels.activities if
                                 any(labels[i].review_required for i in range(a.first_id, a.last_id+1))]
            report["review_activity_ids"] = review_activities
            if review_activities:
                report["warnings"].append(f"{len(review_activities)} complete sections were preserved for review rather than risk losing teaching.")
        long_units = sum(u.end-u.start > 15 for u in units)
        if long_units:
            report["warnings"].append(f"{long_units} speech units exceed 15 seconds; boundaries inside these utterances may need review.")
        mixed = [u.id for u in units if labels[u.id].keep and labels[u.id].mixed_content]
        report["review_unit_ids"] = [u.id for u in units if labels[u.id].review_required or labels[u.id].mixed_content]
        if mixed:
            report["warnings"].append(f"{len(mixed)} retained speech units mix academic and other speech; exact internal cuts need finer transcript timestamps.")
        removed = {}
        for u in units:
            if not labels[u.id].keep:
                reason = labels[u.id].retention.value
                removed[reason] = removed.get(reason, 0) + u.end-u.start
        report["filtering"] = {"removed_seconds_by_reason": removed, "mixed_units": len(mixed),
                               "dead_air_shortened_seconds": {k: round(v, 1) for k, v in tightened.items()}}
        if report.get("activities"):
            try:
                from lecture_trimmer.ui.review import build_sections, write_review_page
                report["review_path"] = str(write_review_page(report, job_dir / "review.html").resolve())
                flagged = sum(bool(s["checks"]) for s in build_sections(report))
                if flagged:
                    report["warnings"].append(f"{flagged} section(s) are flagged for your judgement in the AI decisions review.")
            except Exception:
                logger.warning("Could not write the review page", exc_info=True)
        render_plans = partition_plans({k: planned[k] for k in selected}) if cut_style == "smooth" else planned
        encoder = select_encoder() if cut_style == "smooth" and not dry_run else None
        report["encoder"] = encoder
        render_cache = config.TEMP_DIR / "render_cache" / video.stem
        completed_cache = config.TEMP_DIR / "completed_exports" / video.stem
        save_report()
        with gate("render") if not dry_run else nullcontext():
            for i, kind in enumerate(selected):
                item = report["outputs"][kind]
                if dry_run or not planned[kind]:
                    continue
                notify(0.55 + 0.4 * i / len(selected), f"Exporting {OUTPUT_TITLES[kind]}…")
                out = combined_out if kind == "combined" and combined_out else job_dir / f"{original.stem}_{kind}.mp4"
                export_started = time.perf_counter()
                try:
                    def export_progress(fraction, desc):
                        notify(0.55 + 0.4 * (i + fraction) / len(selected), f"{OUTPUT_TITLES[kind]} — {desc}")
                    item["status"] = "rendering"
                    save_report()
                    export_key = complete_export_key(video.stem, encoder, cut_style, render_plans[kind])
                    reused = reuse_complete_export(completed_cache, export_key, out)
                    if reused:
                        export_progress(1, "Reused the previously verified video")
                        item.update(path=str(out.resolve()), duration=reused["duration"], status="ready",
                                    export_cache="reused", encoder=reused["encoder"])
                    else:
                        cut_and_join(video, render_plans[kind], out, style=cut_style, progress=export_progress,
                                     cache_dir=render_cache, encoder=encoder)
                        item.update(path=str(out.resolve()), duration=probe_duration(out), status="ready",
                                    export_cache="not_reused")
                        if cut_style == "smooth":
                            try:
                                # Hardware rendering can fall back to software. Cache the
                                # actual encoder instead of claiming the requested encoder.
                                actual_encoder = _finished_export_encoder(out)
                                item["encoder"] = actual_encoder
                                actual_key = complete_export_key(video.stem, actual_encoder, cut_style, render_plans[kind])
                                save_complete_export(completed_cache, actual_key, out, item["duration"], actual_encoder)
                            except Exception:
                                # A cache write is an optimization, not a reason to hide a
                                # successfully validated output from its owner.
                                logger.warning("Could not cache completed export; the video is ready", exc_info=True)
                    item["audio_continuity"] = "passed" if cut_style == "smooth" else "not_checked_stream_copy"
                except Exception as exc:
                    from lecture_trimmer.runtime.diagnostics import record_error
                    reference = record_error(exc, stage=f'export-{kind}', owner=owner_id)
                    item.update(status="failed", error="Video export failed", error_reference=reference)
                item["render_seconds"] = time.perf_counter()-export_started
                save_report()
        failed = sum(item["status"] == "failed" for item in report["outputs"].values())
        report["status"] = ("failed" if failed == len(selected) else "partial") if failed else "analysed" if dry_run else "complete"
        report["timings"]["render_seconds"] = sum(i.get("render_seconds", 0) for i in report["outputs"].values())
        save_report()
        notify(1.0, "Some exports failed; completed videos are ready below" if failed else
               "Analysis complete" if dry_run else "Exports complete")
        return {**report, "report_path": str(report_path.resolve())}

    except ProcessingCancelled:
        report["status"] = "cancelled"
        for item in report["outputs"].values():
            if item["status"] in {"planned", "rendering"}:
                item["status"] = "cancelled"
        save_report()
        raise
