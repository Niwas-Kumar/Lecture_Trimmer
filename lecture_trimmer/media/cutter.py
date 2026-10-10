"""Export precise H.264/AAC cuts with audio fades, or fast keyframe stream copies."""
import bisect
import errno
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from fractions import Fraction
from pathlib import Path
from lecture_trimmer.processing.job_control import check_cancelled, submit_with_context

import lecture_trimmer.config as config
from lecture_trimmer.media.audio import probe_duration, run_ffmpeg
from lecture_trimmer.models import KeepRange

logger = logging.getLogger("trimmer")
RENDER_VERSION = "render-v5-fast-join"


ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "19"],
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "4"],
}


def render_worker_count(encoder):
    """Bound GPU sessions; software already uses four encoding threads per clip.

    Measured on an RTX 2050: four NVENC sessions halve the time for many short
    sections (each FFmpeg start costs ~0.5 s) and do not slow long sections.
    """
    requested = os.getenv("TRIMMER_RENDER_WORKERS")
    try:
        workers = int(requested) if requested is not None else (4 if encoder == "h264_nvenc" else 2)
    except ValueError as exc:
        raise ValueError("TRIMMER_RENDER_WORKERS must be an integer from 1 to 4") from exc
    if workers not in range(1, 5):
        raise ValueError("TRIMMER_RENDER_WORKERS must be an integer from 1 to 4")
    return workers if encoder in {"h264_nvenc", "h264_qsv"} else 1


class _RenderAborted(RuntimeError):
    """Stop a sibling encoder after another section has failed."""


@lru_cache(maxsize=1)
def select_encoder():
    """An installed encoder is not necessarily usable; actually encode a sample."""
    requested = os.getenv("TRIMMER_ENCODER", "auto")
    candidates = list(ENCODERS) if requested == "auto" else [requested, "libx264"]
    for name in candidates:
        if name not in ENCODERS:
            raise ValueError("TRIMMER_ENCODER must be auto, h264_nvenc, h264_qsv or libx264")
        try:
            result = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=size=640x360:rate=25", "-t", "0.2", *ENCODERS[name],
                "-pix_fmt", "yuv420p", "-f", "null", "-"], capture_output=True, timeout=15)
            if result.returncode == 0:
                return name
        except (subprocess.TimeoutExpired, OSError):
            continue
    return "libx264"


def video_fps(video):
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True, timeout=30)
    try:
        fps = Fraction(result.stdout.strip())
        if 1 <= float(fps) <= 240:
            return fps
    except (ValueError, ZeroDivisionError):
        pass
    return Fraction(25)


def partition_plans(plans):
    """Split at shared boundaries so the combined video reuses question/theory parts."""
    boundaries = sorted({round(t, 6) for ranges in plans.values() for r in ranges for t in (r.start, r.end)})
    return {kind: [KeepRange(start=a, end=b) for a, b in zip(boundaries, boundaries[1:])
                   if any(r.start <= (a+b)/2 < r.end for r in ranges)]
            for kind, ranges in plans.items()}


def validate_export(path, expected_duration):
    """Decode every audio frame and check its actual sample coverage, not packet duration.

    MP4 can stretch a packet's advertised duration across minutes of missing audio.
    Endpoint duration and successful short playback alone cannot detect that failure.
    """
    raw = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True, timeout=30)
    streams = json.loads(raw.stdout)["streams"]
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    if not audio or not video:
        raise RuntimeError("Export is missing its audio or video stream")
    rate = int(audio["sample_rate"])
    video_duration = float(video["duration"])
    if abs(video_duration-expected_duration) > .15:
        raise RuntimeError("Export duration does not match the frame-aligned cut plan")
    frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_frames", "-show_entries", "frame=pts_time,nb_samples", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True, timeout=max(60, expected_duration/20))
    if frames.stderr.strip():
        raise RuntimeError("Export audio failed to decode: " + frames.stderr[-1000:])
    previous_end, samples = None, 0
    for row in frames.stdout.splitlines():
        values = row.split(",")
        if len(values) < 2:
            continue
        pts, count = float(values[0]), int(values[1])
        if count <= 0 or (previous_end is None and abs(pts) > .1):
            raise RuntimeError("Export audio does not begin at the start of the video")
        if previous_end is not None and abs(pts-previous_end) > .05:
            raise RuntimeError(f"Export audio has a gap or overlap near {pts:.2f}s; refusing to publish")
        previous_end = pts + count/rate
        samples += count
    if previous_end is None or abs(previous_end-video_duration) > .15 or abs(samples/rate-video_duration) > .15:
        raise RuntimeError("Export audio does not cover the entire video; refusing to publish")
    return {"audio_frames_checked": len(frames.stdout.splitlines()), "decoded_audio_seconds": samples/rate,
            "video_seconds": video_duration}


def keyframe_times(video: Path) -> list[float]:
    """Timestamps of all video keyframes, read from the container index (no decoding)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags",
         "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    )
    times = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            times.append(float(parts[0]))
    times.sort()
    logger.info("Found %d keyframes (avg interval %.2fs)", len(times),
                (times[-1] - times[0]) / max(len(times) - 1, 1) if len(times) > 1 else 0)
    return times


def snap_to_keyframes(ranges: list[KeepRange], video: Path, *,
                      keyframes: list[float] | None = None,
                      duration: float | None = None) -> list[KeepRange]:
    """Widen each range to the keyframe at/before its start and at/after its end.

    Stream-copy cuts begin on keyframes. Packet timing can still make the actual
    duration differ slightly from the plan. Touching/overlapping ranges are merged.
    """
    kf = keyframe_times(video) if keyframes is None else keyframes
    duration = probe_duration(video) if duration is None else duration
    if not kf:
        return ranges
    snapped: list[KeepRange] = []
    for r in ranges:
        i = bisect.bisect_right(kf, r.start) - 1
        start = kf[i] if i >= 0 else 0.0
        j = bisect.bisect_left(kf, r.end)
        end = kf[j] if j < len(kf) else duration
        if snapped and start <= snapped[-1].end:
            snapped[-1].end = max(snapped[-1].end, end)
        else:
            snapped.append(KeepRange(start=start, end=end))
    return snapped


def cut_and_join(video: Path, ranges: list[KeepRange], out: Path,
                 temp_dir: Path = config.TEMP_DIR, *, style: str = "smooth", progress=None,
                 cache_dir: Path | None = None, encoder: str | None = None) -> Path:
    if not ranges:
        raise ValueError("Nothing to keep - refusing to write an empty video")
    if style not in {"smooth", "fast"}:
        raise ValueError(f"Unknown cut style: {style}")
    if any(r.start < 0 or r.end <= r.start for r in ranges):
        raise ValueError("Cut ranges must have nonnegative starts and positive lengths")
    out.parent.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda fraction, desc: logger.info(desc))
    encoder = encoder or select_encoder() if style == "smooth" else "copy"
    fps = video_fps(video) if style == "smooth" else None
    total_length = sum(r.end-r.start for r in ranges)
    encoded_length = 0.0
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    # Relative, generated part names also avoid concat escaping problems with source names.
    with tempfile.TemporaryDirectory(prefix="cuts_", dir=temp_dir) as work:
        work = Path(work)
        parts, jobs = [], []
        source_stat = video.stat()
        for i, r in enumerate(ranges):
            start = round(r.start * fps) / float(fps) if fps else r.start
            end = round(r.end * fps) / float(fps) if fps else r.end
            length = end - start
            if length <= 0:
                continue
            encoded_length += length
            part = work / f"part{i:04d}.mkv"
            # Immutable source + encoding settings make saved parts safe to reuse on retry.
            signature = [RENDER_VERSION, str(video.resolve()), source_stat.st_size, source_stat.st_mtime_ns,
                         start, end, style, encoder, str(fps)]
            key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
            cached = cache_dir / f"{key}.mkv" if cache_dir else None
            if cached and cached.is_file() and cached.stat().st_size > 0:
                parts.append(cached)
                continue
            parts.append(cached or part)
            jobs.append((i, start, length, part, cached))
        if not parts:
            raise ValueError("Selected ranges are shorter than a video frame; no video can be exported")

        workers = min(render_worker_count(encoder), max(1, len(jobs)))
        pending_length = sum(job[2] for job in jobs)
        # Each worker reports only its own progress. The lock keeps UI callbacks
        # serialized and the aggregate monotonic even when sections finish out of order.
        amounts = {job[0]: 0.0 for job in jobs}
        progress_lock = threading.Lock()
        aborted = threading.Event()

        def render_part(job):
            check_cancelled()
            i, start, length, part, cached = job
            if aborted.is_set():
                raise _RenderAborted("Rendering stopped after another section failed")

            def part_progress(fraction, desc):
                if aborted.is_set():
                    raise _RenderAborted("Rendering stopped after another section failed")
                with progress_lock:
                    amounts[i] = max(amounts[i], min(1.0, max(0.0, fraction)) * length)
                    done = encoded_length - pending_length + sum(amounts.values())
                    notify(.9 * done / encoded_length,
                           f"Rendering {len(jobs)} sections with {workers} worker(s) — "
                           f"section {i+1}/{len(ranges)} {desc} ({encoder})")

            part_progress(0, "starting")
            args = ["-ss", f"{start:.6f}", "-i", str(video), "-t", f"{length:.6f}",
                    "-map", "0:v:0", "-map", "0:a:0"]
            if style == "smooth":
                fade = min(0.04, length / 4)
                args += ["-vf", f"setpts=PTS-STARTPTS,fps={fps},pad=ceil(iw/2)*2:ceil(ih/2)*2",
                         "-af", (f"asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad,atrim=duration={length:.6f},"
                                 f"afade=t=in:d={fade:.6f},"
                                 f"afade=t=out:st={length-fade:.6f}:d={fade:.6f}"),
                         *ENCODERS[encoder],
                         # FLAC chooses different block sizes for uneven clips. Concat
                         # reuses the first header and can silently discard later audio.
                         "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
                         "-ar", "48000", "-ac", "2"]
            else:
                args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            run_ffmpeg([*args, str(part)], progress=part_progress, duration=length)
            if aborted.is_set():
                raise _RenderAborted("Rendering stopped after another section failed")
            if cached:
                try:
                    # The usual cache/work directories share a volume. Renaming
                    # the completed part is atomic and avoids copying large PCM
                    # intermediates and retaining a second copy until the join ends.
                    part.replace(cached)
                except OSError as exc:
                    if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                        raise
                    staging = cached.with_suffix(f".{uuid.uuid4().hex}.partial")
                    try:
                        shutil.copyfile(part, staging)
                        staging.replace(cached)
                    finally:
                        staging.unlink(missing_ok=True)
            part_progress(1, "complete")

        notify(.9 * (encoded_length-pending_length) / encoded_length,
               f"Preparing {len(jobs)} sections ({encoder}, {workers} worker(s))…")
        try:
            if workers == 1:
                for job in jobs:
                    render_part(job)
            else:
                pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="video-render")
                futures = []
                try:
                    futures = [submit_with_context(pool, render_part, job) for job in jobs]
                    for future in as_completed(futures):
                        future.result()
                finally:
                    aborted.set()
                    for future in futures:
                        future.cancel()
                    # Running FFmpeg processes stop at their next progress callback.
                    # Wait before removing the temporary directory or starting fallback.
                    pool.shutdown(wait=True, cancel_futures=True)
        except RuntimeError:
            if encoder in {"copy", "libx264"}:
                raise
            # Do not mix hardware/software codec headers. All active workers are
            # stopped before a consistent software retry; cached successes survive.
            logger.warning("Hardware export failed; retrying with libx264", exc_info=True)
            return cut_and_join(video, ranges, out, temp_dir, style=style, progress=progress,
                                cache_dir=cache_dir, encoder="libx264")
        list_file = work / "concat.txt"
        # Cache paths are server-generated but the workspace may contain an apostrophe.
        def concat_path(path):
            return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        list_file.write_text("".join(f"file '{concat_path(p)}'\n" for p in parts), encoding="utf-8")
        # Encode audio only once, avoiding accumulated AAC encoder padding at every join.
        # The fast AAC coder is ~3x quicker than the default and, at 128 kbps, still
        # well above typical lecture sources (~60 kbps AAC); it dominated join time.
        codecs = (["-c:v", "copy", "-c:a", "aac", "-aac_coder", "fast", "-b:a", "128k"]
                  if style == "smooth" else ["-c", "copy"])
        pending = work / "result.mp4"
        notify(0.9, "Joining sections and preparing the final video…")
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file),
                    *codecs, "-movflags", "+faststart", str(pending)], duration=total_length,
                    progress=lambda f, d: notify(.9 + .085*f, "Joining video — " + d))
        if abs(probe_duration(pending)-total_length) > max(1.0, .05*len(ranges)) and style == "smooth":
            raise RuntimeError("Export duration did not match the cut plan; refusing to publish a truncated video")
        if style == "smooth":
            notify(.99, "Checking audio continuity throughout the entire video…")
            validate_export(pending, encoded_length)
        # Use a sibling staging file so publishing also works across Windows drives.
        staging = out.with_name(out.name + ".partial")
        try:
            shutil.copyfile(pending, staging)
            staging.replace(out)
        finally:
            staging.unlink(missing_ok=True)
    logger.info("Wrote %s", out)
    notify(1.0, "Video saved")
    return out
