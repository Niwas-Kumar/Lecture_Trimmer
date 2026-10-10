"""Keep coherent activities and required context in each independently useful export."""
import lecture_trimmer.config as config
from lecture_trimmer.models import ActivityKind, ActivityRole, KeepRange
from lecture_trimmer.processing.editor import exclude_ranges


def _union(ranges):
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged = []
    for r in ordered:
        if merged and r.start <= merged[-1].end + .001:
            merged[-1].end = max(merged[-1].end, r.end)
        elif r.end > r.start:
            merged.append(KeepRange(start=r.start, end=r.end))
    return merged


def plan_activities(units, labels, duration, padding=.15, combined_labels=None):
    by_id = {u.id: u for u in units}
    activities = {a.first_id: a for a in labels.activities}
    edits = {e.activity_id: e for e in labels.edits}
    ordered = labels.activities
    from lecture_trimmer.analysis.lesson_classifier import StudyReviewRequired
    if any(a.kind == ActivityKind.UNCERTAIN or (
            a.kind == ActivityKind.NONACADEMIC and any(labels[uid].keep for uid in range(a.first_id, a.last_id + 1)))
           for a in ordered):
        raise StudyReviewRequired("Resolve uncertain or uncut nonacademic activities before planning study exports.")
    intervals = {}
    for i, a in enumerate(ordered):
        start, end = by_id[a.first_id].start, by_id[a.last_id].end
        # Own continuous board work BETWEEN utterances of the same activity.
        # At activity transitions split only the silence, never neighbouring speech.
        previous_end = by_id[ordered[i-1].last_id].end if i else 0
        next_start = by_id[ordered[i+1].first_id].start if i+1 < len(ordered) else duration
        start = max(0, min(start, max(previous_end, start-padding)))
        end = min(duration, max(end, min(next_start, end+padding)))
        intervals[a.first_id] = KeepRange(start=start, end=end)
    outputs, context_report = {}, {}
    for kind in ("questions", "theory", "combined"):
        desired = {ActivityKind.QUESTIONS} if kind == "questions" else {ActivityKind.THEORY}
        if kind == "combined":
            desired = {ActivityKind.THEORY, ActivityKind.QUESTIONS}
            if combined_labels is not None:
                desired = set()
                if set(combined_labels) & {"worked_example", "doubt_solving"}:
                    desired.add(ActivityKind.QUESTIONS)
                if set(combined_labels) & {"core_teaching", "low_density_teaching"}:
                    desired.add(ActivityKind.THEORY)
        selected = {a.first_id for a in ordered if a.kind in desired}
        if kind == "questions" and config.ILLUSTRATIONS_IN_QUESTIONS:
            selected |= {a.first_id for a in ordered if a.role == ActivityRole.ILLUSTRATION}
        initial = set(selected)
        pending = list(selected)
        skipped = set()
        while pending:
            activity = activities[pending.pop()]
            for dependency in activity.depends_on:
                required = activities[dependency]
                if required.kind == ActivityKind.NONACADEMIC:
                    raise ValueError("An output depends on a removed nonacademic activity")
                if dependency not in selected:
                    length = by_id[required.last_id].end - by_id[required.first_id].start
                    if required.kind not in desired and length > config.MAX_CONTEXT_SECONDS:
                        # Long background belongs to its own video, not copied in here.
                        skipped.add(dependency)
                        continue
                    selected.add(dependency)
                    pending.append(dependency)
        kept = []
        for aid in sorted(selected):
            activity = activities[aid]
            cuts = []
            for cut in edits[aid].cuts:
                start, end = by_id[cut.first_id].start, by_id[cut.last_id].end
                # Leading/trailing interruptions include the edge padding as well.
                if cut.first_id == activity.first_id:
                    start = intervals[aid].start
                if cut.last_id == activity.last_id:
                    end = intervals[aid].end
                cuts.append(KeepRange(start=start, end=end))
            kept.extend(exclude_ranges([intervals[aid]], cuts))
        outputs[kind] = _union(kept)
        context_report[kind] = {
            "required_context_activity_ids": sorted(selected-initial),
            "skipped_context_activity_ids": sorted(skipped-selected),
            "uncertain_activity_ids": [],
            "activity_ids": sorted(selected),
        }
    return outputs, context_report
