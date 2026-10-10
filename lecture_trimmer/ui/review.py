"""Show the AI's editing decisions: which part of the lecture went into which video, and why.

Everything is computed from the saved analysis report, so old and new jobs can be
reviewed without another AI request.
"""
import html
import re
import unicodedata
from pathlib import Path

COLORS = {
    "questions": ("#2563eb", "Questions"),
    "theory": ("#16a34a", "Theory"),
    "nonacademic": ("#9ca3af", "Removed"),
    "uncertain": ("#d97706", "Needs review"),
}
ROLE_NAMES = {
    "concept": "Concept explanation", "illustration": "Example explaining a concept",
    "task": "Posed question / exercise", "task_doubt": "Doubt about a question",
    "concept_doubt": "Concept doubt", "recap": "Recap", "off_topic": "Off-topic",
    "logistics": "Logistics", "technical": "Technical issue", "waiting": "Waiting",
    "unclear": "Unclear",
}
CUT_NAMES = {"nonacademic": "unrelated talk", "logistics": "logistics", "waiting": "waiting",
             "filler": "filler", "repetition": "repetition", "unintelligible": "unintelligible"}
LONG_CONCEPT_SECONDS = 12 * 60


def _clock(seconds):
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return [t for t in re.findall(r"[\wऀ-ॿ]+", normalized) if len(t) > 1]


def evidence_found(evidence, text):
    """True when most words of the quoted evidence occur in the passage (ASR-tolerant)."""
    quote = _tokens(evidence)
    if not quote:
        return False
    words = set(_tokens(text))
    return sum(t in words for t in quote) / len(quote) >= 0.6


def _overlap(start, end, ranges):
    return sum(max(0.0, min(end, r["end"]) - max(start, r["start"])) for r in ranges)


def build_sections(report):
    """One row per activity with its times, final video(s), role, reason and checks."""
    units = {u["id"]: u for u in report.get("units", [])}
    activities = report.get("activities") or []
    edits = {e["activity_id"]: e for e in report.get("passage_edits") or []}
    outputs = report.get("outputs", {})
    context = report.get("output_context", {})
    sections = []
    for a in activities:
        first, last = units.get(a["first_id"]), units.get(a["last_id"])
        if not first or not last:
            continue
        start, end = first["start"], last["end"]
        length = max(end - start, 1e-6)
        text = " ".join(units[i]["text"] for i in range(a["first_id"], a["last_id"] + 1) if i in units)
        videos = []
        for kind in ("questions", "theory", "combined"):
            share = _overlap(start, end, outputs.get(kind, {}).get("keep", [])) / length
            if share > 0.05:
                note = ""
                if a["first_id"] in context.get(kind, {}).get("required_context_activity_ids", []):
                    note = "context"
                videos.append({"kind": kind, "share": share, "note": note})
        checks = []
        role = a.get("role")
        if role in {"task", "task_doubt"}:
            if not evidence_found(a.get("evidence", ""), text):
                checks.append("The AI could not quote the question from the transcript; check this is really a question.")
        if a.get("kind") in {"theory", "questions"} and role not in {"task", "task_doubt"} and length > LONG_CONCEPT_SECONDS:
            checks.append(f"Long section ({_clock(length)}): it may contain several ideas or a question.")
        edit = edits.get(a["first_id"], {})
        notes = []
        titles = {x["first_id"]: x.get("title", "") for x in activities}
        for kind, other in (("questions", "Theory"), ("theory", "Questions")):
            skipped = set(context.get(kind, {}).get("skipped_context_activity_ids", []))
            needed = [titles.get(d, "") for d in a.get("depends_on", []) if d in skipped]
            if needed and any(v["kind"] == kind for v in videos):
                notes.append(f"Builds on “{'”, “'.join(needed)}” — long background kept in the {other} video, "
                             "not copied into this one.")
        if a.get("kind") == "uncertain":
            checks.append(edit.get("review_reason") or "The AI could not decide the category.")
        elif edit.get("review_required"):
            # Kept on purpose (e.g. silent board work); informative, not a judgement call.
            notes.append(edit.get("review_reason") or "Preserved for human review.")
        cuts = []
        for cut in edit.get("cuts", []):
            c0, c1 = units.get(cut["first_id"]), units.get(cut["last_id"])
            if c0 and c1:
                cuts.append({"start": c0["start"], "end": c1["end"],
                             "category": CUT_NAMES.get(cut.get("category"), cut.get("category")),
                             "reason": cut.get("reason", "")})
        sections.append({
            "id": a["first_id"], "start": start, "end": end, "kind": a.get("kind", "uncertain"),
            "role": role, "title": a.get("title", ""), "summary": a.get("summary", ""),
            "evidence": a.get("evidence", ""), "videos": videos, "checks": checks, "notes": notes, "cuts": cuts,
        })
    return sections


def summary_counts(report, sections=None):
    sections = build_sections(report) if sections is None else sections
    totals = {}
    for s in sections:
        totals[s["kind"]] = totals.get(s["kind"], 0) + s["end"] - s["start"]
    return totals


def _bar(sections, duration):
    duration = max(duration, 1e-6)
    blocks = []
    for s in sections:
        color, label = COLORS.get(s["kind"], COLORS["uncertain"])
        left, width = 100 * s["start"] / duration, max(0.15, 100 * (s["end"] - s["start"]) / duration)
        tip = html.escape(f"{_clock(s['start'])}–{_clock(s['end'])} · {label} · {s['title']}")
        blocks.append(f'<span title="{tip}" data-start="{s["start"]:.2f}" style="position:absolute;top:0;bottom:0;'
                      f'left:{left:.3f}%;width:{width:.3f}%;background:{color}"></span>')
        for cut in s["cuts"]:
            cl, cw = 100 * cut["start"] / duration, max(0.1, 100 * (cut["end"] - cut["start"]) / duration)
            ctip = html.escape(f"Cut {_clock(cut['start'])}–{_clock(cut['end'])} · {cut['category']}")
            blocks.append(f'<span title="{ctip}" style="position:absolute;top:0;bottom:0;left:{cl:.3f}%;'
                          f'width:{cw:.3f}%;background:repeating-linear-gradient(45deg,#6b7280 0 3px,#d1d5db 3px 6px)"></span>')
    return ('<div style="position:relative;height:28px;border-radius:6px;overflow:hidden;'
            'background:#e5e7eb;margin:6px 0 4px">' + "".join(blocks) + "</div>")


def _legend(totals):
    items = []
    for kind, (color, label) in COLORS.items():
        seconds = totals.get(kind, 0)
        items.append(f'<span style="margin-right:14px;white-space:nowrap"><span style="display:inline-block;width:10px;'
                     f'height:10px;border-radius:2px;background:{color};margin-right:5px"></span>{label} {_clock(seconds)}</span>')
    items.append('<span style="white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;'
                 'margin-right:5px;background:repeating-linear-gradient(45deg,#6b7280 0 3px,#d1d5db 3px 6px)"></span>Cut inside a section</span>')
    return '<div style="font-size:13px;line-height:1.8">' + "".join(items) + "</div>"


def _rows(sections, clickable=False):
    rows = []
    for s in sections:
        color, label = COLORS.get(s["kind"], COLORS["uncertain"])
        videos = ", ".join(("Theory + questions" if v["kind"] == "combined" else v["kind"].title())
                           + (" (as needed context)" if v["note"] else "") for v in s["videos"]) or "Not in any video"
        role = ROLE_NAMES.get(s["role"], s["role"] or "—")
        evidence = (f'<div style="opacity:.8;font-style:italic">“{html.escape(s["evidence"][:240])}”</div>'
                    if s["evidence"] else "")
        cuts = "".join(f'<li>{_clock(c["start"])}–{_clock(c["end"])} removed as {html.escape(c["category"])}'
                       + (f': {html.escape(c["reason"])}' if c["reason"] else "") + "</li>" for c in s["cuts"])
        checks = "".join(f'<li style="color:#b45309">⚠ {html.escape(c)}</li>' for c in s["checks"])
        notes = "".join(f'<li style="opacity:.7">ℹ {html.escape(n)}</li>' for n in s.get("notes", []))
        detail = (f'<ul style="margin:4px 0 0 18px;padding:0">{cuts}{checks}{notes}</ul>'
                  if cuts or checks or notes else "")
        time_cell = f'{_clock(s["start"])}–{_clock(s["end"])}'
        if clickable:
            time_cell = f'<a href="#" onclick="seek({s["start"]:.2f});return false">{time_cell}</a>'
        rows.append(
            f'<tr style="border-top:1px solid rgba(127,127,127,.25);vertical-align:top">'
            f'<td style="padding:6px 8px;white-space:nowrap">{time_cell}</td>'
            f'<td style="padding:6px 8px"><span style="display:inline-block;padding:1px 8px;border-radius:10px;'
            f'background:{color};color:#fff;font-size:12px">{label}</span><div style="font-size:12px;opacity:.75;margin-top:3px">{html.escape(role)}</div></td>'
            f'<td style="padding:6px 8px"><b>{html.escape(s["title"])}</b><div>{html.escape(s["summary"])}</div>{evidence}{detail}</td>'
            f'<td style="padding:6px 8px;font-size:12px">{html.escape(videos)}</td></tr>')
    return ('<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<thead><tr style="text-align:left"><th style="padding:6px 8px">Time</th><th style="padding:6px 8px">AI decision</th>'
            '<th style="padding:6px 8px">What happens and why</th><th style="padding:6px 8px">Included in</th></tr></thead>'
            "<tbody>" + "".join(rows) + "</tbody></table>")


def review_html(report):
    """Timeline and decision table for the app (no scripts)."""
    sections = build_sections(report)
    if not sections:
        return ('<p>Section-level decisions are available for jobs analysed with complete-lesson editing. '
                'Process the lecture again to see them.</p>')
    totals = summary_counts(report, sections)
    flagged = sum(bool(s["checks"]) for s in sections)
    head = (f'<p style="margin:0 0 4px">Each block is a section of the original lecture, coloured by the video it went into. '
            f'Hover a block for its title. {flagged} section(s) are flagged ⚠ for your judgement.</p>')
    return head + _bar(sections, report.get("duration", 0)) + _legend(totals) + _rows(sections)


def write_review_page(report, path: Path):
    """A standalone page with the original lecture: click a time to watch that decision."""
    sections = build_sections(report)
    source = report.get("source", "")
    video_url = Path(source).resolve().as_uri() if source else ""
    body = review_html(report).replace(_rows(sections), _rows(sections, clickable=True)) if sections else review_html(report)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Editing Review</title>
<style>
:root{{color-scheme:light dark;--bg:#ffffff;--fg:#111827}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111827;--fg:#e5e7eb}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
video{{width:100%;max-height:52vh;background:#000;border-radius:8px}}
.sticky{{position:sticky;top:0;background:var(--bg);padding-bottom:8px;z-index:2}}
a{{color:#2563eb}}
</style></head><body>
<h1 style="font-size:20px;margin:0 0 6px">AI editing review · {html.escape(report.get('video', ''))}</h1>
<p style="margin:0 0 10px;opacity:.8">Analysis {html.escape(str(report.get('classification_version', '')))} ·
mode {html.escape(str(report.get('mode', '')))}. Click a time to watch that part of the original lecture.
The video plays only on the computer that processed the lecture.</p>
<div class="sticky"><video id="player" controls preload="metadata" src="{html.escape(video_url)}"></video></div>
{body}
<script>
function seek(t){{var v=document.getElementById('player');v.currentTime=t;v.play();window.scrollTo({{top:0,behavior:'smooth'}});}}
</script></body></html>"""
    staging = path.with_suffix(".partial")
    staging.write_text(page, encoding="utf-8")
    staging.replace(path)
    return path
