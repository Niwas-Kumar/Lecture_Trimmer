"""Measured audio pauses: what an editor hears as dead air.

Pauses come from the real audio, not from transcript timestamps, so they also
correct approximate model-generated phrase timings. The silence threshold is
relative to each lecture's own loudness; a quiet recording therefore cannot
turn soft speech into "silence".
"""
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.models import KeepRange

logger = logging.getLogger("trimmer")
PAUSE_VERSION = "pauses-v2"


@dataclass(frozen=True)
class Pause:
    start: float
    end: float

    @property
    def length(self):
        return self.end - self.start


def _ffmpeg_stderr(args, timeout):
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-nostdin", *args, "-f", "null", "-"],
                            capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("FFmpeg could not analyse the audio: " + result.stderr[-500:])
    return result.stderr


def detect_pauses(video: Path, duration: float, *, min_seconds=1.0, cache_dir: Path | None = None):
    """Return silences of at least `min_seconds`, cached per immutable source."""
    stat = video.stat()
    cache_dir = cache_dir or config.TEMP_DIR
    key = f"{video.stem}_{stat.st_size}_{stat.st_mtime_ns}_{min_seconds}_{PAUSE_VERSION}"
    cache = cache_dir / f"pauses_{key}.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return [Pause(*p) for p in data["pauses"]], data["threshold_db"]
        except (ValueError, KeyError, TypeError, OSError):
            logger.warning("Ignoring invalid pause cache %s", cache.name)
    timeout = max(120, duration / 10)
    # Loudness from three 2-minute samples is as good as a full pass and ~6x faster.
    levels = []
    for fraction in (0.25, 0.5, 0.75) if duration > 600 else (0.0,):
        level = _ffmpeg_stderr(["-ss", f"{duration * fraction:.1f}", "-t", "120", "-i", str(video),
                                "-map", "0:a:0", "-af", "volumedetect"], timeout)
        match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", level)
        if match and float(match.group(1)) > -90:
            levels.append(float(match.group(1)))
    # The loudest sample avoids mistaking a quiet stretch for the speaking level.
    mean = max(levels) if levels else -20.0
    # About 20 dB below average speech, kept within sane bounds for noisy rooms.
    threshold = max(-50.0, min(-28.0, mean - 20.0))
    log = _ffmpeg_stderr(["-i", str(video), "-map", "0:a:0", "-af",
                          f"silencedetect=n={threshold:.1f}dB:d={min_seconds}"], timeout)
    pauses, start = [], None
    for line in log.splitlines():
        if "silence_start:" in line:
            start = float(line.rsplit("silence_start:", 1)[1].split()[0])
        elif "silence_end:" in line and start is not None:
            end = float(line.rsplit("silence_end:", 1)[1].split()[0])
            pauses.append(Pause(max(0.0, start), min(duration, end)))
            start = None
    if start is not None and duration - start >= min_seconds:
        pauses.append(Pause(start, duration))
    staging = cache.with_suffix(".partial")
    staging.write_text(json.dumps({"pauses": [[p.start, p.end] for p in pauses], "threshold_db": threshold}),
                       encoding="utf-8")
    staging.replace(cache)
    return pauses, threshold


def pause_markers(units, pauses, min_seconds=4.0):
    """Unit ID -> seconds of measured silence that follows or sits inside it (for the editor)."""
    marks = {}
    ordered = sorted(units, key=lambda u: u.start)
    index = 0
    for pause in (p for p in pauses if p.length >= min_seconds):
        while index + 1 < len(ordered) and ordered[index + 1].start <= pause.start:
            index += 1
        unit = ordered[index]
        if unit.text.lstrip().lower().startswith("[no reliable speech"):
            continue  # Already explicit in the transcript.
        marks[unit.id] = round(marks.get(unit.id, 0) + pause.length, 1)
    return marks


def snap_to_pauses(ranges, pauses, *, reach=0.4, lead=0.25):
    """Move each cut point into an adjacent pause so cuts land on silence, not mid-word.

    A start moves to `lead` seconds before speech resumes; an end moves to `lead`
    seconds after speech stops. Points move only through measured silence, so no
    excluded speech can be brought back.
    """
    if not pauses:
        return ranges
    out = []
    for r in ranges:
        start, end = r.start, r.end
        for p in pauses:
            if p.length <= 2 * lead:
                continue
            if p.start <= start < p.end:
                # Head of the range is dead air: begin just before speech resumes.
                start = max(start, p.end - lead)
            elif start - reach <= p.end < start:
                # Speech after this pause began slightly before the transcript said.
                start = p.end - lead
            if p.start < end <= p.end:
                end = min(end, p.start + lead)
            elif end < p.start <= end + reach:
                # Let the last word finish into the following pause.
                end = p.start + lead
        out.append(KeepRange(start=start, end=end) if end - start >= 0.5 else r)
    merged = []
    for r in sorted(out, key=lambda r: r.start):
        if merged and r.start <= merged[-1].end:
            merged[-1] = KeepRange(start=merged[-1].start, end=max(merged[-1].end, r.end))
        else:
            merged.append(r)
    return merged


def tighten_pauses(ranges, pauses, *, min_pause, keep):
    """Shorten long dead air inside kept ranges to `keep` seconds, like an editor's ripple delete."""
    removable = []
    for p in pauses:
        if p.length >= min_pause:
            half = keep / 2
            removable.append(KeepRange(start=p.start + half, end=p.end - half))
    if not removable:
        return ranges, 0.0
    from lecture_trimmer.processing.editor import exclude_ranges
    tightened = exclude_ranges(ranges, removable)
    # Never leave slivers shorter than a second; they read as glitches.
    tightened = [r for r in tightened if r.end - r.start >= 1.0]
    before = sum(r.end - r.start for r in ranges)
    after = sum(r.end - r.start for r in tightened)
    if not tightened:
        return ranges, 0.0
    return tightened, before - after
