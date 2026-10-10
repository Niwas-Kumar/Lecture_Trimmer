"""Timing and local visual evidence regressions; no network calls or model loads."""
import hashlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

import lecture_trimmer.config as config
import lecture_trimmer.media.transcriber as transcriber
import lecture_trimmer.analysis.classifier as classifier
import lecture_trimmer.analysis.visual_evidence as visual_evidence
from lecture_trimmer.models import Segment, Transcript, Unit, Word


class UntranscribedGapTests(unittest.TestCase):
    def test_leading_internal_and_trailing_gaps_remain_visible_and_reindexed(self):
        from lecture_trimmer.analysis.classifier import add_untranscribed_gaps
        source = [Unit(id=9, start=10, end=20, text="Academic explanation"),
                  Unit(id=12, start=30, end=40, text="Its conclusion")]
        result = add_untranscribed_gaps(source, 50)
        self.assertEqual([(unit.start, unit.end) for unit in result],
                         [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)])
        self.assertEqual([unit.id for unit in result], list(range(5)))
        self.assertEqual([unit.id for unit in source], [9, 12])
        for unit in result[::2]:
            self.assertIn("possible silent board work", unit.text)
            self.assertIn("preserve if uncertain", unit.text)

    def test_brief_pauses_are_not_new_gaps_and_overlaps_do_not_create_false_gaps(self):
        from lecture_trimmer.analysis.classifier import add_untranscribed_gaps
        units = [Unit(id=0, start=1, end=15, text="Explanation"),
                 Unit(id=1, start=14, end=20, text="Conclusion")]
        result = add_untranscribed_gaps(units, 25)
        self.assertEqual(len(result), 2)

    def test_empty_transcript_fails_with_preserved_source_explanation(self):
        from lecture_trimmer.analysis.classifier import add_untranscribed_gaps
        with self.assertRaisesRegex(RuntimeError, "original video is preserved"):
            add_untranscribed_gaps([], 40)

    def test_out_of_source_timestamps_fail_before_planning(self):
        from lecture_trimmer.analysis.classifier import add_untranscribed_gaps
        with self.assertRaisesRegex(ValueError, "out-of-source"):
            add_untranscribed_gaps([Unit(id=0, start=5, end=50, text="invalid")], 40)


class WordBoundaryUnitTests(unittest.TestCase):
    @staticmethod
    def fixture():
        # Hindi sentence punctuation plus equation punctuation must survive.
        texts = ["वेग", "निकालते", "हैं।", "Now", "use", "s=ut+½gt².", "The", "answer", "is", "20", "m."]
        words = [Word(start=index * 2, end=index * 2 + 1.7, text=text)
                 for index, text in enumerate(texts)]
        segment = Segment(start=0, end=22, text=" ".join(texts))
        return Transcript(language="hi", duration=22, segments=[segment], words=words)

    def test_complete_word_timing_splits_at_real_boundaries_without_text_loss(self):
        from lecture_trimmer.analysis.classifier import build_units
        transcript = self.fixture()
        units = build_units(transcript, unit_seconds=8)
        self.assertGreater(len(units), 1)
        self.assertEqual(units[0].text, "वेग निकालते हैं।")
        self.assertEqual(" ".join(unit.text for unit in units), transcript.segments[0].text)
        self.assertEqual(units[0].start, 0)
        self.assertEqual(units[-1].end, 22)
        self.assertTrue(all(unit.end in {word.start for word in transcript.words} for unit in units[:-1]))
        self.assertTrue(all(left.end == right.start for left, right in zip(units, units[1:])))

    def test_missing_words_preserves_original_whole_segment(self):
        from lecture_trimmer.analysis.classifier import build_units
        transcript = self.fixture()
        transcript.words.pop(3)
        units = build_units(transcript, unit_seconds=8)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].text, transcript.segments[0].text)
        self.assertEqual((units[0].start, units[0].end), (0, 22))

    def test_changed_equation_words_cannot_claim_complete_coverage(self):
        from lecture_trimmer.analysis.classifier import build_units
        transcript = self.fixture()
        transcript.words[5].text = "s=ut-½gt²."
        units = build_units(transcript, unit_seconds=8)
        self.assertEqual(len(units), 1)

    def test_adjacent_segment_word_does_not_prevent_reliable_split(self):
        from lecture_trimmer.analysis.classifier import build_units
        transcript = self.fixture()
        transcript.duration = 23
        transcript.segments.append(Segment(start=22, end=23, text="Next"))
        transcript.words.append(Word(start=22, end=23, text="Next"))
        units = build_units(transcript, unit_seconds=8)
        self.assertGreater(len(units), 2)
        self.assertEqual(" ".join(unit.text for unit in units), transcript.segments[0].text + " Next")

    def test_overlapping_words_are_never_cut_through(self):
        from lecture_trimmer.analysis.classifier import build_units
        transcript = self.fixture()
        transcript.words[3].start = 5.5  # previous sentence's last word ends at 5.7.
        units = build_units(transcript, unit_seconds=8)
        self.assertNotIn(5.5, [unit.end for unit in units[:-1]])
        self.assertEqual(" ".join(unit.text for unit in units), transcript.segments[0].text)


class SpeechEvidenceTests(unittest.TestCase):
    def test_legacy_transcript_has_empty_optional_words(self):
        transcript = Transcript.model_validate({"language": "hi", "duration": 4,
                                               "segments": [{"start": 0, "end": 4, "text": "गति"}]})
        self.assertEqual(transcript.words, [])

    def test_words_are_validated_sorted_deduplicated_and_offset(self):
        raw = [{"word": "velocity", "start": 1, "end": 2},
               {"word": "गति", "start": 0, "end": .8},
               {"word": "velocity", "start": 1, "end": 2},
               {"word": "invalid", "start": "nan", "end": 3},
               {"word": "reverse", "start": 3, "end": 2},
               {"word": "outside", "start": 11, "end": 12},
               {"word": "negative", "start": -1, "end": .1},
               {"word": None, "start": 3, "end": 4}]
        words = transcriber.normalise_word_timestamps(raw, offset=600, duration=610)
        self.assertEqual([(w.start, w.end, w.text) for w in words],
                         [(600, 600.8, "गति"), (601, 602, "velocity")])

    def test_segments_overhanging_the_final_short_section_fit_the_source(self):
        # Real failure: a 10s final section returned Whisper's 30s padded window.
        segments = [Segment(start=8100, end=8150, text="teaching"),
                    Segment(start=8152.62, end=8182.6, text="झाल झाल"),
                    Segment(start=8160, end=8189.98, text="Thank you."),
                    Segment(start=8171, end=8180, text="beyond the audio")]
        fitted = transcriber.fit_segments_to_source(segments, 8170.048)
        self.assertEqual([(s.start, s.end) for s in fitted],
                         [(8100, 8150), (8152.62, 8170.048), (8160, 8170.048)])
        units = classifier.add_untranscribed_gaps(
            classifier.build_units(Transcript(language="en", duration=8170.048, segments=fitted)), 8170.048)
        self.assertLessEqual(units[-1].end, 8170.048)

    def test_groq_requests_words_and_preserves_chunk_offset(self):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = SimpleNamespace(
            language="hi", segments=[{"start": .1, "end": 1.5, "text": " गति velocity "}],
            words=[{"start": .1, "end": .5, "word": "गति"},
                   {"start": .6, "end": 1.5, "word": "velocity"}])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory:
            audio = Path(directory) / "chunk.flac"
            audio.write_bytes(b"synthetic-test")
            language, segments, words = transcriber._groq_chunk(client, 600, audio, None)
        self.assertEqual(language, "hi")
        self.assertEqual(segments[0].start, 600.1)
        self.assertEqual(words[-1].end, 601.5)
        self.assertEqual(client.audio.transcriptions.create.call_args.kwargs["timestamp_granularities"],
                         ["word", "segment"])

    def test_groq_without_word_metadata_remains_usable(self):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = SimpleNamespace(
            language="en", segments=[SimpleNamespace(start=0, end=1, text="answer")])
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as directory:
            audio = Path(directory) / "chunk.flac"
            audio.write_bytes(b"synthetic-test")
            _, segments, words = transcriber._groq_chunk(client, 0, audio, "en")
        self.assertEqual(segments[0].text, "answer")
        self.assertEqual(words, [])


class VisualEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=config.TEMP_DIR)
        cls.root = Path(cls.directory.name)
        cls.video = cls.root / "evidence.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            "color=red:s=320x180:r=10:d=2", "-f", "lavfi", "-i", "color=blue:s=320x180:r=10:d=2",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(cls.video),
        ], capture_output=True, check=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_interval_sampling_is_bounded_and_prioritises_centres(self):
        positions = visual_evidence.plan_sample_times(100, [(0, 10), (50, 60), (80, 100)], 4)
        self.assertEqual(positions, [1, 5, 55, 90])
        self.assertEqual(visual_evidence.plan_sample_times(10, [], 24), [])
        self.assertTrue(all(0 <= t < 100 for t in visual_evidence.plan_sample_times(100)))
        with self.assertRaises(ValueError):
            visual_evidence.plan_sample_times(100, [(30, 20)])

    def test_real_frames_detect_change_and_reuse_verified_cache(self):
        cache = self.root / "frames"
        first = visual_evidence.sample_evidence(self.video, 4, intervals=[(0, 4)], cache_dir=cache,
                                                max_frames=3, budget_seconds=15)
        self.assertEqual(len(first.frames), 3, first.warnings)
        self.assertFalse(first.warnings)
        self.assertTrue(any((frame.change_score or 0) > .02 for frame in first.frames))
        self.assertTrue(first.frames[0].read_jpeg().startswith(b"\xff\xd8"))
        self.assertEqual(len(first.frames_for(0, 4, limit=2)), 2)
        with patch.object(visual_evidence, "_extract_frame", side_effect=AssertionError("Cache missed")):
            second = visual_evidence.sample_evidence(self.video, 4, intervals=[(0, 4)], cache_dir=cache,
                                                     max_frames=3, budget_seconds=15)
        self.assertEqual(second.cached_frames, 3)
        self.assertNotIn("path", second.to_report()["frames"][0])
        first.frames[0].path.write_bytes(b"broken-cache")
        with self.assertRaises(ValueError):
            first.frames[0].read_jpeg()
        repaired = visual_evidence.sample_evidence(self.video, 4, intervals=[(0, 4)], cache_dir=cache,
                                                   max_frames=3, budget_seconds=15)
        self.assertEqual(repaired.cached_frames, 2)
        self.assertFalse(repaired.warnings)

    def test_budget_timeout_returns_warning_not_deletion_advice(self):
        with patch.object(visual_evidence, "_extract_frame",
                          side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            evidence = visual_evidence.sample_evidence(self.video, 4, max_frames=1,
                cache_dir=self.root / "timeout", source_digest=hashlib.sha256(self.video.read_bytes()).hexdigest())
        self.assertEqual(evidence.frames, [])
        self.assertTrue(evidence.warnings)
        self.assertNotIn("remove", evidence.warnings[0])

    def test_changed_source_aborts_collection(self):
        video = self.root / "changing.mp4"
        video.write_bytes(b"first-content")
        def change_source(source, timestamp, target, timeout):
            Image.new("RGB", (32, 32)).save(target, "JPEG")
            source.write_bytes(b"replacement-content")
        with patch.object(visual_evidence, "_extract_frame", side_effect=change_source):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                visual_evidence.sample_evidence(video, 4, max_frames=1,
                                               cache_dir=self.root / "changed")

    def test_time_budget_stops_further_work(self):
        def delayed_frame(source, timestamp, target, timeout):
            time.sleep(.12)
            Image.new("RGB", (32, 32)).save(target, "JPEG")
        with patch.object(visual_evidence, "_extract_frame", side_effect=delayed_frame) as extract:
            evidence = visual_evidence.sample_evidence(self.video, 4, max_frames=3, budget_seconds=.1,
                                                       cache_dir=self.root / "limited")
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(evidence.frames), 1)
        self.assertTrue(any("budget" in warning for warning in evidence.warnings))


if __name__ == "__main__":
    unittest.main()
