"""Stage 4: turn per-unit labels into a smooth list of ranges to keep."""
import logging

import bisect

import config
from models import KeepRange, Transcript, Unit, UnitLabel

logger = logging.getLogger("trimmer")


def snap_to_speech(ranges: list[KeepRange], transcript: Transcript,
                   max_shift: float = config.SPEECH_SNAP_SECONDS) -> list[KeepRange]:
    """Move each cut to the nearest sentence boundary so no word is clipped.

    Range starts move to the nearest transcript-segment start, range ends to the
    nearest segment end, but never further than `max_shift` seconds.
    """
    starts = sorted(s.start for s in transcript.segments)
    ends = sorted(s.end for s in transcript.segments)

    def nearest(values, t, prefer_before: bool):
        i = bisect.bisect_left(values, t)
        cands = [v for v in values[max(0, i - 1):i + 1] if abs(v - t) <= max_shift]
        if not cands:
            return t
        # On a tie, start a little early / end a little late rather than the reverse.
        return min(cands, key=lambda v: (abs(v - t), v if prefer_before else -v))

    out = []
    for r in ranges:
        s, e = nearest(starts, r.start, True), nearest(ends, r.end, False)
        if e - s < 1.0:
            s, e = r.start, r.end
        if out and s <= out[-1].end:
            out[-1].end = max(out[-1].end, e)
        else:
            out.append(KeepRange(start=s, end=e))
    return out


def _merge(ranges: list[KeepRange]) -> list[KeepRange]:
    out: list[KeepRange] = []
    for r in sorted(ranges, key=lambda r: r.start):
        if out and r.start <= out[-1].end:
            out[-1].end = max(out[-1].end, r.end)
        else:
            out.append(KeepRange(start=r.start, end=r.end))
    return out


def build_keep_list(
    units: list[Unit],
    labels: dict[int, UnitLabel],
    duration: float,
    keep_labels: set[str] = config.KEEP_LABELS,
) -> list[KeepRange]:
    kept = _merge([KeepRange(start=u.start, end=u.end)
                   for u in units if labels[u.id].label.value in keep_labels])

    # Close small gaps: a 15 s joke inside a derivation is not worth a visible jump cut.
    closed: list[KeepRange] = []
    for r in kept:
        if closed and r.start - closed[-1].end < config.MIN_GAP_TO_CUT:
            closed[-1].end = r.end
        else:
            closed.append(r)

    # Drop tiny islands: 20 s of "teaching" surrounded by chatter is usually a mislabel.
    solid = [r for r in closed if r.end - r.start >= config.MIN_KEEP_ISLAND]

    # Pad edges so words are not clipped, then clamp and re-merge.
    padded = [KeepRange(start=max(0.0, r.start - config.EDGE_PADDING),
                        end=min(duration, r.end + config.EDGE_PADDING)) for r in solid]
    final = _merge(padded)

    kept_s = sum(r.end - r.start for r in final)
    logger.info("Keep list: %d ranges, %.1f min of %.1f min (%.0f%%)",
                len(final), kept_s / 60, duration / 60, 100 * kept_s / max(duration, 1))
    return final
