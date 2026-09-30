"""Stage 5: cut the kept ranges and join them - stream copy, no re-encoding.

Each cut starts at the keyframe at or before the requested time (<= one GOP,
2 s on the sample lecture), which is why editor.py pads the ranges. The whole
step runs at disk speed: seconds, even for hours of video.
"""
import bisect
import logging
import subprocess
from pathlib import Path

import config
from audio import probe_duration, run_ffmpeg
from models import KeepRange

logger = logging.getLogger("trimmer")


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


def snap_to_keyframes(ranges: list[KeepRange], video: Path) -> list[KeepRange]:
    """Widen each range to the keyframe at/before its start and at/after its end.

    A stream-copy cut can only begin on a keyframe, so this makes the planned
    ranges equal what ffmpeg will actually output - and makes the cut points
    deterministic. Touching/overlapping ranges are merged afterwards.
    """
    kf = keyframe_times(video)
    duration = probe_duration(video)
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
                 temp_dir: Path = config.TEMP_DIR) -> Path:
    if not ranges:
        raise ValueError("Nothing to keep - refusing to write an empty video")

    parts: list[Path] = []
    for i, r in enumerate(ranges):
        part = temp_dir / f"{video.stem}_part{i:04d}{video.suffix}"
        # -ss/-to before -i: fast keyframe seek on the input, then copy packets.
        run_ffmpeg(["-ss", f"{r.start:.3f}", "-to", f"{r.end:.3f}", "-i", str(video),
                    "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                    "-avoid_negative_ts", "make_zero", str(part)])
        parts.append(part)

    list_file = temp_dir / f"{video.stem}_concat.txt"
    # concat demuxer wants forward slashes and quoted paths
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")

    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", "-movflags", "+faststart", str(out)])

    for p in parts:
        p.unlink(missing_ok=True)
    list_file.unlink(missing_ok=True)
    logger.info("Wrote %s", out)
    return out
