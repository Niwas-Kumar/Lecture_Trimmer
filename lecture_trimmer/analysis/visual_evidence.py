"""Bounded, local-only frame evidence for ambiguous teaching activities.

This module never uploads media, runs OCR, or decides whether a passage is
academic. A changing board and a moving teacher can produce similar pixel
differences; unchanged slides can accompany essential speech. Use these frames
as additional evidence, never as a reason to delete a silent passage by itself.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

import lecture_trimmer.config as config

EVIDENCE_VERSION = "local-frames-v1"
MAX_JPEG_BYTES = 3 * 1024 * 1024


@dataclass(frozen=True)
class EvidenceFrame:
    timestamp: float
    path: Path
    sha256: str
    change_score: float | None = None

    def read_jpeg(self) -> bytes:
        """Read verified bytes for a caller-authorized visual review, if any."""
        if self.path.stat().st_size > MAX_JPEG_BYTES:
            raise ValueError("Cached frame exceeds the evidence size limit")
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("Cached frame changed after evidence collection")
        return data


@dataclass
class VisualEvidence:
    source_digest: str
    duration: float
    frames: list[EvidenceFrame] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    cached_frames: int = 0

    def frames_for(self, start: float, end: float, limit: int = 3) -> list[EvidenceFrame]:
        """Return representative frames inside a source-time interval."""
        available = [frame for frame in self.frames if start <= frame.timestamp <= end]
        if limit <= 0:
            return []
        if len(available) <= limit:
            return available
        if limit == 1:
            return [available[len(available) // 2]]
        return [available[round(i * (len(available) - 1) / (limit - 1))] for i in range(limit)]

    def to_report(self) -> dict:
        return {
            "version": EVIDENCE_VERSION, "source_digest": self.source_digest,
            "duration": self.duration, "elapsed_seconds": round(self.elapsed_seconds, 3),
            "cached_frames": self.cached_frames, "warnings": self.warnings,
            "frames": [{"timestamp": frame.timestamp, "sha256": frame.sha256,
                        "change_score": frame.change_score} for frame in self.frames],
            "interpretation": "Pixel change is supporting evidence, not an academic/nonacademic label.",
        }


def plan_sample_times(duration: float, intervals=None, max_frames: int = 24) -> list[float]:
    """Bound samples before decoding; cover passage centers before their edges.

    Callers should pass uncertain whole activities, in priority order. Sparse
    whole-video samples provide orientation only and cannot prove completeness.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Video duration must be positive and finite")
    if not 1 <= max_frames <= 96:
        raise ValueError("Evidence frame limit must be between 1 and 96")
    latest = max(0.0, duration - min(.1, duration / 2))
    if intervals is None:
        count = max_frames
        return sorted({min(latest, round((i + .5) * latest / count, 3)) for i in range(count)})
    cleaned = []
    for item in intervals:
        start, end = float(item[0]), float(item[1])
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Evidence intervals need finite, increasing boundaries")
        start, end = max(0.0, start), min(duration, end)
        if end > start:
            cleaned.append((start, end))
    selected = []
    for fraction in (.5, .1, .9):
        for start, end in cleaned:
            position = min(latest, round(start + fraction * (end - start), 3))
            if position not in selected:
                selected.append(position)
                if len(selected) >= max_frames:
                    return sorted(selected)
    return sorted(selected)


def _fingerprint(video: Path, deadline: float) -> str:
    digest = hashlib.sha256()
    with video.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            if time.monotonic() >= deadline:
                raise TimeoutError("Visual evidence time budget expired while fingerprinting video")
    return digest.hexdigest()


def _read_frame(path: Path) -> tuple[bytes, Image.Image]:
    if not 0 < path.stat().st_size <= MAX_JPEG_BYTES:
        raise ValueError("Invalid frame size")
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "JPEG" or image.width > 1280 or image.height > 1280:
            raise ValueError("Invalid cached evidence frame")
        thumbnail = image.convert("L").resize((96, 54))
    return data, thumbnail


def _extract_frame(video: Path, timestamp: float, target: Path, timeout: float) -> None:
    # Input seeking decodes only the nearest GOP, rather than the whole lecture.
    # Limit both dimensions so portrait and unusual inputs have bounded memory.
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.jpg")
    try:
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{timestamp:.3f}",
            "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-an",
            "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease",
            "-threads", "1", "-q:v", "3", "-update", "1", str(temporary),
        ], capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-500:])
        _read_frame(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def sample_evidence(video: Path, duration: float, *, intervals=None,
                    source_digest: str | None = None, max_frames: int = 24,
                    budget_seconds: float = 30.0, cache_dir: Path | None = None,
                    progress=None) -> VisualEvidence:
    """Collect timestamped JPEG evidence locally with a strict elapsed budget.

    ``source_digest`` may be the verified SHA-256 from pipeline.snapshot_source;
    otherwise this function fingerprints the file itself. It must never be an
    arbitrary source name. A changed source aborts collection instead of mixing
    evidence from two files. Cached frames are reused across output categories.
    Failed/budget-limited sampling returns explicit warnings, not deletion advice.
    """
    video = Path(video)
    started = time.monotonic()
    if not math.isfinite(budget_seconds) or not .1 <= budget_seconds <= 300:
        raise ValueError("Visual evidence budget must be between .1 and 300 seconds")
    positions = plan_sample_times(duration, intervals, max_frames)
    deadline = started + budget_seconds
    before = video.stat()
    if source_digest is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", source_digest):
        raise ValueError("source_digest must be a verified SHA-256")
    try:
        digest = source_digest.lower() if source_digest else _fingerprint(video, deadline)
    except TimeoutError as error:
        return VisualEvidence("", duration, warnings=[str(error)], elapsed_seconds=time.monotonic() - started)
    evidence = VisualEvidence(digest, duration)
    cache = Path(cache_dir or config.TEMP_DIR / "visual_evidence") / EVIDENCE_VERSION / digest
    cache.mkdir(parents=True, exist_ok=True)
    previous = None
    for index, position in enumerate(positions):
        if time.monotonic() >= deadline:
            evidence.warnings.append(f"Visual evidence time budget reached; sampled {len(evidence.frames)}/{len(positions)} frames.")
            break
        target = cache / f"{round(position * 1000):012d}.jpg"
        cached = False
        try:
            try:
                data, thumbnail = _read_frame(target)
                cached = True
            except (OSError, ValueError):
                _extract_frame(video, position, target, max(.1, deadline - time.monotonic()))
                data, thumbnail = _read_frame(target)
            change = None if previous is None else round(
                ImageStat.Stat(ImageChops.difference(previous, thumbnail)).mean[0] / 255.0, 5)
            evidence.frames.append(EvidenceFrame(position, target, hashlib.sha256(data).hexdigest(), change))
            evidence.cached_frames += int(cached)
            previous = thumbnail
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            evidence.warnings.append(f"Frame at {position:.1f}s unavailable: {type(error).__name__}.")
        if progress:
            progress((index + 1) / max(1, len(positions)), "Checking local visual evidence")
    after = video.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("Video changed during visual evidence collection; retry using the protected source")
    evidence.elapsed_seconds = time.monotonic() - started
    # No paths or pixels in the report: these remain private local cache files.
    manifest = cache / (hashlib.sha256(json.dumps(positions).encode()).hexdigest()[:24] + ".json")
    temporary = manifest.with_name(f".{manifest.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(evidence.to_report(), indent=2), encoding="utf-8")
        os.replace(temporary, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return evidence
