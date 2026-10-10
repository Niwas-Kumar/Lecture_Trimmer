"""Stage 4: turn per-unit labels into a smooth list of ranges to keep."""
import logging

import bisect

import lecture_trimmer.config as config
from lecture_trimmer.models import KeepRange, Transcript, Unit, UnitLabel

logger = logging.getLogger("trimmer")


def snap_to_speech(ranges: list[KeepRange], transcript: Transcript,
                   max_shift: float = config.SPEECH_SNAP_SECONDS) -> list[KeepRange]:
    """Expand cuts to nearby ASR boundaries, without trimming away selected speech.

    Starts only move earlier and ends only move later, by at most `max_shift`.
    """
    starts = sorted(s.start for s in transcript.segments)
    ends = sorted(s.end for s in transcript.segments)

    def nearest(values, t, prefer_before: bool):
        i = bisect.bisect_left(values, t)
        cands = [v for v in values[max(0, i - 1):i + 1] if abs(v - t) <= max_shift
                 and (v <= t if prefer_before else v >= t)]
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
    *,
    min_gap: float | None = None,
    min_island: float | None = None,
    padding: float | None = None,
    blocked_labels: set[str] | None = None,
) -> list[KeepRange]:
    min_gap = config.MIN_GAP_TO_CUT if min_gap is None else min_gap
    min_island = config.MIN_KEEP_ISLAND if min_island is None else min_island
    padding = config.EDGE_PADDING if padding is None else padding
    blocked = [KeepRange(start=u.start, end=u.end) for u in units
               if not labels[u.id].keep or labels[u.id].label.value in (blocked_labels or set())]
    kept = _merge([KeepRange(start=u.start, end=u.end)
                   for u in units if labels[u.id].keep and labels[u.id].label.value in keep_labels])

    # Close only unlabeled gaps; never restore speech explicitly rejected by the editor.
    closed: list[KeepRange] = []
    for r in kept:
        if (closed and r.start - closed[-1].end < min_gap
                and not any(b.start < r.start and b.end > closed[-1].end for b in blocked)):
            closed[-1].end = r.end
        else:
            closed.append(r)

    # Drop tiny islands: 20 s of "teaching" surrounded by chatter is usually a mislabel.
    solid = [r for r in closed if r.end - r.start >= min_island]

    # Pad edges so words are not clipped, then clamp and re-merge.
    padded = [KeepRange(start=max(0.0, r.start - padding),
                        end=min(duration, r.end + padding)) for r in solid]
    final = _merge(padded)

    kept_s = sum(r.end - r.start for r in final)
    logger.info("Keep list: %d ranges, %.1f min of %.1f min (%.0f%%)",
                len(final), kept_s / 60, duration / 60, 100 * kept_s / max(duration, 1))
    return final


def exclude_ranges(ranges: list[KeepRange], blocked: list[KeepRange]) -> list[KeepRange]:
    """Keep padding/speech snapping from pulling the opposite category back in."""
    result = []
    for r in ranges:
        fragments = [(r.start, r.end)]
        for b in blocked:
            next_fragments = []
            for start, end in fragments:
                if b.end <= start or b.start >= end:
                    next_fragments.append((start, end))
                else:
                    if start < b.start:
                        next_fragments.append((start, b.start))
                    if b.end < end:
                        next_fragments.append((b.end, end))
            fragments = next_fragments
        result.extend(KeepRange(start=s, end=e) for s, e in fragments if e > s)
    return _merge(result)
