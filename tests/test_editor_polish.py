"""Pause-aware cuts, subject briefs and the AI decision review."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from lecture_trimmer.analysis.subjects import detect_subject, subject_brief
from lecture_trimmer.analysis.classifier import _window_prompt
from lecture_trimmer.media.pauses import Pause, detect_pauses, pause_markers, snap_to_pauses, tighten_pauses
from lecture_trimmer.models import KeepRange, Unit
from lecture_trimmer.ui.review import build_sections, evidence_found, review_html, write_review_page


def spans(ranges):
    return [(round(r.start, 2), round(r.end, 2)) for r in ranges]


class PauseTests(unittest.TestCase):
    def test_cut_points_move_into_silence_without_reaching_into_speech(self):
        pauses = [Pause(9.0, 12.0), Pause(20.3, 21.5)]
        # Start sits in dead air; end stops 0.1 s before a pause, so the word finishes.
        self.assertEqual(spans(snap_to_pauses([KeepRange(start=10, end=20.2)], pauses)), [(11.75, 20.55)])
        # Nothing nearby: unchanged. Far-away speech is never pulled in.
        self.assertEqual(spans(snap_to_pauses([KeepRange(start=30, end=40)], pauses)), [(30, 40)])

    def test_long_dead_air_is_shortened_but_short_pauses_stay(self):
        ranges = [KeepRange(start=0, end=60)]
        pauses = [Pause(10, 20), Pause(30, 31.5)]
        tightened, saved = tighten_pauses(ranges, pauses, min_pause=3.0, keep=1.2)
        self.assertEqual(spans(tightened), [(0, 10.6), (19.4, 60)])
        self.assertAlmostEqual(saved, 8.8)

    def test_markers_tell_the_editor_where_long_silences_are(self):
        units = [Unit(id=0, start=0, end=5, text="board work starts"), Unit(id=1, start=14, end=20, text="so")]
        self.assertEqual(pause_markers(units, [Pause(5, 13), Pause(15, 16)]), {0: 8.0})
        prompt = _window_prompt(units, "Physics", notes="Subject notes: x", pauses={0: 8.0})
        self.assertIn("[unit_id=0] board work starts <pause 8s>", prompt)
        self.assertTrue(prompt.startswith("Subject notes: x"))

    def test_detects_real_silence_relative_to_the_lecture_loudness(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "lecture.mp4"
            # 2 s tone, 4 s silence, 2 s tone.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=size=160x90:rate=25:duration=8",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-f", "lavfi", "-i",
                            "anullsrc=r=48000:cl=stereo:d=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-filter_complex", "[1][2][3]concat=n=3:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)],
                           check=True)
            pauses, threshold = detect_pauses(source, 8.0, cache_dir=Path(work))
            self.assertLess(threshold, -20)
            self.assertEqual(len(pauses), 1)
            self.assertAlmostEqual(pauses[0].start, 2.0, delta=0.3)
            self.assertAlmostEqual(pauses[0].end, 6.0, delta=0.3)
            # Cached second call returns the same measurement.
            self.assertEqual(detect_pauses(source, 8.0, cache_dir=Path(work))[0], pauses)


class SubjectTests(unittest.TestCase):
    def test_subject_comes_from_the_hint_then_the_transcript(self):
        self.assertEqual(detect_subject("Class 11 Chemistry, Mole concept", []), ("chemistry", None))
        self.assertEqual(detect_subject("NEET Biology one shot revision", [])[1], "revision")
        units = [Unit(id=i, start=i, end=i + 1, text="velocity and acceleration of the ball, force and momentum")
                 for i in range(5)]
        self.assertEqual(detect_subject("", units)[0], "physics")
        self.assertEqual(detect_subject("", [Unit(id=0, start=0, end=1, text="hello everyone")])[0], None)

    def test_brief_only_adds_notes_when_confident(self):
        subject, _, notes = subject_brief("Class 12 Physics", [])
        self.assertEqual(subject, "physics")
        self.assertIn("Subject notes: Physics", notes)
        self.assertEqual(subject_brief("", [])[2], "")


class BackgroundTests(unittest.TestCase):
    def test_only_short_background_is_copied_into_a_single_category_video(self):
        from lecture_trimmer.analysis.lesson_classifier import make_labels
        from lecture_trimmer.models import ActivityEdit, ActivityKind, ActivityRole, TeachingActivity
        from lecture_trimmer.processing.lesson_planner import plan_activities
        # Unit 0: 10-minute derivation; unit 1: 1-minute setup; unit 2: a question using both.
        units = [Unit(id=0, start=0, end=600, text="derivation"), Unit(id=1, start=600, end=660, text="setup"),
                 Unit(id=2, start=660, end=760, text="find the height")]
        activities = [TeachingActivity(first_id=0, last_id=0, role=ActivityRole.CONCEPT, title="Derivation", summary=""),
                      TeachingActivity(first_id=1, last_id=1, role=ActivityRole.CONCEPT, title="Setup", summary=""),
                      TeachingActivity(first_id=2, last_id=2, role=ActivityRole.TASK, title="Height", summary="",
                                       depends_on=[0, 1])]
        edits = [ActivityEdit(activity_id=i, cuts=[], essential_ids=[], review_required=False, review_reason="")
                 for i in range(3)]
        planned, context = plan_activities(units, make_labels(units, activities, edits), 760, padding=0)
        self.assertEqual(spans(planned["questions"]), [(600, 760)])
        self.assertEqual(context["questions"]["required_context_activity_ids"], [1])
        self.assertEqual(context["questions"]["skipped_context_activity_ids"], [0])
        self.assertEqual(spans(planned["combined"]), [(0, 760)])
        report = {"duration": 760, "units": [u.model_dump() for u in units],
                  "activities": [a.model_dump(mode="json") for a in activities],
                  "passage_edits": [e.model_dump(mode="json") for e in edits], "output_context": context,
                  "outputs": {k: {"keep": [r.model_dump() for r in v]} for k, v in planned.items()}}
        question = [s for s in build_sections(report) if s["id"] == 2][0]
        self.assertIn("“Derivation” — long background kept in the Theory video", question["notes"][0])


def report_fixture():
    units = [{"id": i, "start": i * 10.0, "end": i * 10.0 + 10, "text": t} for i, t in enumerate([
        "good morning everyone", "velocity is the rate of change of displacement",
        "a ball is thrown up at 20 m/s find the maximum height", "using v squared formula h is 20 m",
        "type in chat", "homework poll"])]
    return {
        "video": "lecture.mp4", "duration": 60.0, "source": __file__, "mode": "balanced",
        "classification_version": "test", "units": units,
        "activities": [
            {"first_id": 0, "last_id": 0, "kind": "nonacademic", "role": "off_topic", "title": "Greeting", "summary": "Hello", "evidence": ""},
            {"first_id": 1, "last_id": 1, "kind": "theory", "role": "concept", "title": "Velocity", "summary": "Defines velocity", "evidence": ""},
            {"first_id": 2, "last_id": 4, "kind": "questions", "role": "task", "title": "Ball height",
             "summary": "Finds height", "evidence": "a ball is thrown up at 20 m/s, find the maximum height"},
            {"first_id": 5, "last_id": 5, "kind": "questions", "role": "task", "title": "Invented", "summary": "", "evidence": "a train leaves the station"},
        ],
        "passage_edits": [
            {"activity_id": 0, "cuts": [{"first_id": 0, "last_id": 0, "category": "nonacademic", "reason": "greeting", "standalone": True}]},
            {"activity_id": 1, "cuts": []},
            {"activity_id": 2, "cuts": [{"first_id": 4, "last_id": 4, "category": "filler", "reason": "chat prompt", "standalone": True}]},
            {"activity_id": 5, "cuts": []},
        ],
        "outputs": {"theory": {"keep": [{"start": 10, "end": 20}]},
                    "questions": {"keep": [{"start": 20, "end": 40}, {"start": 50, "end": 60}]},
                    "combined": {"keep": [{"start": 10, "end": 40}, {"start": 50, "end": 60}]}},
        "output_context": {},
    }


class ReviewTests(unittest.TestCase):
    def test_evidence_check_tolerates_punctuation_but_not_invention(self):
        self.assertTrue(evidence_found("Ball thrown up at 20 m/s, find the maximum height!",
                                       "a ball is thrown up at 20 m/s find the maximum height"))
        self.assertFalse(evidence_found("a train leaves the station", "homework poll"))
        self.assertFalse(evidence_found("", "anything"))

    def test_sections_show_destination_reason_cuts_and_flags(self):
        sections = {s["id"]: s for s in build_sections(report_fixture())}
        self.assertEqual([v["kind"] for v in sections[1]["videos"]], ["theory", "combined"])
        self.assertEqual(sections[0]["videos"], [])
        self.assertEqual(sections[2]["cuts"][0]["category"], "filler")
        self.assertEqual(sections[2]["checks"], [])
        self.assertIn("could not quote", sections[5]["checks"][0])

    def test_review_page_links_times_to_the_original_lecture(self):
        report = report_fixture()
        page = review_html(report)
        self.assertIn("Questions", page)
        self.assertIn("1 section(s) are flagged", page)
        with tempfile.TemporaryDirectory() as work:
            path = write_review_page(report, Path(work) / "review.html")
            text = path.read_text(encoding="utf-8")
            self.assertIn("onclick=\"seek(20.00)", text)
            self.assertIn(Path(__file__).resolve().as_uri(), text)

    def test_legacy_reports_without_activities_explain_how_to_get_a_review(self):
        self.assertIn("Process the lecture again", review_html({"duration": 10, "units": []}))
        json.dumps(build_sections(report_fixture()))  # Serializable for the app state.


if __name__ == "__main__":
    unittest.main()
