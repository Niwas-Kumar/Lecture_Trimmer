"""Measure editing quality instead of judging each version by eye.

  compare A.json B.json        Stability: do two analyses of one lecture agree?
  template RUN.json OUT.json   Start a hand-checked reference from an AI run.
  score RUN.json REF.json      Accuracy against a hand-checked reference.

A reference lists sections of the ORIGINAL lecture:
  {"video": "...", "sections": [{"start": "12:30", "end": "18:45",
    "label": "questions" | "theory" | "cut", "must_keep": true, "note": ""}]}
Times may be seconds or m:ss / h:mm:ss. Label what a good human editor would do;
mark must_keep for moments a student cannot lose (a formula, a question, an answer).
"""
import argparse
import json
import sys
from pathlib import Path

LABELS = ("questions", "theory", "cut")


def seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    total = 0.0
    for part in str(value).strip().split(":"):
        total = total * 60 + float(part)
    return total


def clock(value):
    value = int(round(value))
    h, rest = divmod(value, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def timeline(report):
    """Per-second final label from the exported ranges: questions, theory, both or cut."""
    duration = int(report["duration"]) + 1
    outputs = report.get("outputs", {})
    marks = []
    for kind in ("questions", "theory"):
        covered = [False] * duration
        for r in outputs.get(kind, {}).get("keep", []):
            for t in range(int(r["start"]), min(duration, int(r["end"]) + 1)):
                if t + 0.5 >= r["start"] and t + 0.5 <= r["end"]:
                    covered[t] = True
        marks.append(covered)
    result = []
    for q, th in zip(*marks):
        result.append("both" if q and th else "questions" if q else "theory" if th else "cut")
    return result


def minutes(line):
    counts = {}
    for label in line:
        counts[label] = counts.get(label, 0) + 1
    return {k: round(v / 60, 1) for k, v in sorted(counts.items())}


def compare(a_path, b_path):
    a, b = load(a_path), load(b_path)
    la, lb = timeline(a), timeline(b)
    n = min(len(la), len(lb))
    agree = sum(la[i] == lb[i] for i in range(n)) / max(n, 1)
    print(f"A: {a.get('classification_version')}  {minutes(la)}")
    print(f"B: {b.get('classification_version')}  {minutes(lb)}")
    print(f"Per-second agreement: {agree:.1%}")
    stretches, start = [], None
    for i in range(n + 1):
        differs = i < n and la[i] != lb[i]
        if differs and start is None:
            start = i
        elif not differs and start is not None:
            # Report the most common label on each side, not just the first second.
            x = max(set(la[start:i]), key=la[start:i].count)
            y = max(set(lb[start:i]), key=lb[start:i].count)
            stretches.append((i - start, start, i, x, y))
            start = None
    print("Largest disagreements:")
    for length, s, e, x, y in sorted(stretches, reverse=True)[:10]:
        print(f"  {clock(s)}-{clock(e)} ({clock(length)}): A={x} B={y}")
    return agree


def template(run_path, out_path):
    report = load(run_path)
    units = {u["id"]: u for u in report.get("units", [])}
    sections = []
    for a in report.get("activities", []):
        label = {"questions": "questions", "theory": "theory"}.get(a.get("kind"), "cut")
        sections.append({"start": clock(units[a["first_id"]]["start"]), "end": clock(units[a["last_id"]]["end"]),
                         "label": label, "must_keep": False, "note": a.get("title", "")})
    Path(out_path).write_text(json.dumps({"video": report.get("video"), "sections": sections}, indent=1,
                                         ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(sections)} sections to {out_path}. Correct each label and mark must_keep moments.")


def score(run_path, ref_path):
    report, reference = load(run_path), load(ref_path)
    line = timeline(report)
    total = agree = 0
    must = kept = 0
    misses = []
    reference_cut = 0
    for section in reference["sections"]:
        label = section["label"]
        if label not in LABELS:
            raise SystemExit(f"Unknown label {label!r}; use one of {LABELS}")
        start, end = int(seconds(section["start"])), int(seconds(section["end"]))
        for t in range(start, min(end, len(line))):
            got = line[t]
            total += 1
            agree += got == label or (got == "both" and label in {"questions", "theory"})
            if label == "cut":
                reference_cut += 1
            if section.get("must_keep") and label != "cut":
                must += 1
                ok = got in {label, "both"}
                kept += ok
                if not ok and (not misses or misses[-1][1] < t - 1):
                    misses.append([t, t, label, got])
                elif not ok:
                    misses[-1][1] = t
    print(f"Category agreement with reference: {agree / max(total, 1):.1%} of {clock(total)} reviewed")
    print(f"Must-keep recall: {kept / max(must, 1):.1%} of {clock(must)} (target 100%)")
    ai_cut = sum(label == "cut" for label in line)
    print(f"Removed: AI {clock(ai_cut)} vs reference {clock(reference_cut)}")
    for s, e, label, got in misses[:15]:
        print(f"  MISSED {clock(s)}-{clock(e + 1)}: should be in {label}, AI put it in {got}")
    return kept / max(must, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    t = sub.add_parser("template"); t.add_argument("run"); t.add_argument("out")
    s = sub.add_parser("score"); s.add_argument("run"); s.add_argument("reference")
    args = parser.parse_args(argv)
    if args.command == "compare":
        compare(args.a, args.b)
    elif args.command == "template":
        template(args.run, args.out)
    else:
        score(args.run, args.reference)


if __name__ == "__main__":
    sys.exit(main())
