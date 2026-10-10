import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import lecture_trimmer.analysis.classifier as classifier
from lecture_trimmer.analysis.classifier import build_units, expand_spans, map_exercises, validate_episodes
from lecture_trimmer.models import (ClassificationSpan, EpisodeMap, Label, ProblemEpisode, Retention,
                    RetentionDecision, RetentionReview, Segment, SpanClassification, Transcript, Unit)


class ClassificationTests(unittest.TestCase):
    def test_prompt_does_not_present_timestamps_as_competing_identifiers(self):
        units = [Unit(id=17,start=2404,end=2410,text="Find the initial speed")]
        prompt = classifier._window_prompt(units,"Physics")
        self.assertIn('[unit_id=17]',prompt)
        self.assertNotIn('2404',prompt)
        self.assertNotIn('2410',prompt)

    def episode(self, start, end):
        return ProblemEpisode(first_id=start,last_id=end,task="Find rectangle area",evidence="length 7, width 3")

    def units(self, count=10):
        return [Unit(id=i,start=i*6,end=(i+1)*6,text="Fictitious test speech") for i in range(count)]

    def test_episode_order_and_exact_duplicates_are_normalized(self):
        mapped = EpisodeMap(episodes=[self.episode(6,8),self.episode(1,4),self.episode(1,4)])
        accepted, issues = validate_episodes(mapped,self.units())
        self.assertEqual([(e.first_id,e.last_id) for e in accepted],[(1,4),(6,8)])
        self.assertEqual(issues,[])

    def test_ambiguous_and_out_of_range_episodes_are_never_clamped_or_merged(self):
        mapped = EpisodeMap(episodes=[self.episode(0,2),self.episode(2,4),self.episode(6,7),
                                      self.episode(8,1000000000),self.episode(9,8)])
        accepted, issues = validate_episodes(mapped,self.units())
        self.assertEqual([(e.first_id,e.last_id) for e in accepted],[(6,7)])
        self.assertEqual(len(issues),3)

    def test_bad_map_requests_correction_then_uses_repaired_map(self):
        bad = EpisodeMap(episodes=[self.episode(0,4),self.episode(4,7)])
        fixed = EpisodeMap(episodes=[self.episode(0,7)])
        warnings = []
        with patch.object(classifier,'_request',side_effect=[bad,fixed]) as request:
            result = map_exercises(None,'gemini',self.units(),'',lambda *args: None,warnings)
        self.assertEqual(result,fixed.episodes)
        self.assertEqual(request.call_count,2)
        self.assertIn('overlap 0-4 / 4-7',request.call_args.args[2])
        self.assertIn('Previous response',request.call_args.args[2])
        self.assertEqual(warnings,[])

    def test_repeated_bad_map_falls_back_but_every_unit_still_requires_classification(self):
        units = self.units()
        bad = EpisodeMap(episodes=[self.episode(0,5),self.episode(5,9)])
        final = SpanClassification(spans=[ClassificationSpan(first_id=0,last_id=9,
                                    label=Label.CORE_TEACHING,confidence=.9,reason="Test theory")])
        review = RetentionReview(decisions=[RetentionDecision(unit_id=i,
                retention=Retention.ACADEMIC,reason="Necessary explanation",mixed_content=False) for i in range(10)])
        warnings = []
        with tempfile.TemporaryDirectory(dir=classifier.config.TEMP_DIR) as folder, \
             patch.object(classifier.config,'TEMP_DIR',Path(folder)), \
             patch('google.genai.Client',return_value=MagicMock()), \
             patch.object(classifier,'_request',side_effect=[bad,bad,final,review]) as request:
            labels = classifier.classify_legacy(units,backend='gemini',warnings=warnings)
            self.assertEqual(set(labels),set(range(10)))
            self.assertEqual(len(warnings),1)
            self.assertEqual(request.call_count,4)
            cached_warnings = []
            cached = classifier.classify_legacy(units,backend='gemini',warnings=cached_warnings)
            self.assertEqual(cached,labels)
            self.assertEqual(cached_warnings,warnings)
            self.assertEqual(request.call_count,4)

    def test_independent_review_can_remove_speech_from_inside_a_problem(self):
        units = self.units(3)
        labels = SpanClassification(spans=[ClassificationSpan(first_id=0,last_id=2,
            label=Label.WORKED_EXAMPLE,confidence=.99,reason="Exercise")])
        def decision(i, retention):
            return RetentionDecision(unit_id=i,retention=retention,reason="Evidence",mixed_content=False)
        review = RetentionReview(decisions=[decision(0,Retention.ACADEMIC),decision(1,Retention.NONACADEMIC),
                                      decision(2,Retention.ACADEMIC)])
        with tempfile.TemporaryDirectory(dir=classifier.config.TEMP_DIR) as folder, \
             patch.object(classifier.config,'TEMP_DIR',Path(folder)), \
             patch('google.genai.Client',return_value=MagicMock()), \
             patch.object(classifier,'_request',side_effect=[EpisodeMap(episodes=[]),labels,review]) as request:
            result = classifier.classify_legacy(units,backend='gemini')
        self.assertEqual([result[i].keep for i in range(3)],[True,False,True])
        self.assertEqual(result[1].label,Label.WORKED_EXAMPLE)
        self.assertNotIn('Lecture-wide exercise map',request.call_args.args[2])
        self.assertNotIn('worked_example',request.call_args.args[2])

    def test_retention_review_rejects_missing_or_overlapping_decisions(self):
        def decision(i):
            return RetentionDecision(unit_id=i,retention=Retention.ACADEMIC,
                                     reason="Academic",mixed_content=False)
        for ids in [[0,1],[0,1,2,2],[0,1,1000000000]]:
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                expand_spans(RetentionReview(decisions=[decision(i) for i in ids]),self.units(3))

    def test_retry_reuses_validated_category_window_after_editor_failure(self):
        units = self.units(3)
        category = SpanClassification(spans=[ClassificationSpan(first_id=0,last_id=2,
            label=Label.CORE_TEACHING,confidence=.9,reason="Definition")])
        review = RetentionReview(decisions=[RetentionDecision(unit_id=i,
            retention=Retention.ACADEMIC,reason="Definition",mixed_content=False) for i in range(3)])
        with tempfile.TemporaryDirectory(dir=classifier.config.TEMP_DIR) as folder, \
             patch.object(classifier.config,'TEMP_DIR',Path(folder)), \
             patch('google.genai.Client',return_value=MagicMock()), \
             patch.object(classifier,'_request',side_effect=[EpisodeMap(episodes=[]),category,
                        RuntimeError('provider temporarily unavailable'),EpisodeMap(episodes=[]),review]) as request:
            with self.assertRaisesRegex(RuntimeError,'provider temporarily unavailable'):
                classifier.classify_legacy(units,backend='gemini')
            result = classifier.classify_legacy(units,backend='gemini')
            self.assertEqual(set(result),{0,1,2})
            self.assertEqual(request.call_count,5)
            self.assertIs(request.call_args.args[-1],RetentionReview)

    def test_malformed_map_response_has_bounded_retries(self):
        warnings = []
        with patch.object(classifier,'_request',side_effect=ValueError('invalid JSON')) as request:
            result = map_exercises(None,'gemini',self.units(),'',lambda *args: None,warnings)
        self.assertEqual(result,[])
        self.assertEqual(request.call_count,2)
        self.assertEqual(len(warnings),1)

    def test_boundaries_use_real_speech_timestamps_without_twenty_second_buckets(self):
        transcript = Transcript(language="en", duration=40, segments=[
            Segment(start=0, end=2, text="Concept"),
            Segment(start=2, end=5, text="Explanation"),
            Segment(start=5, end=9, text="Now solve this question"),
            Segment(start=9, end=11, text="Given values"),
            Segment(start=11, end=35, text="One long ASR utterance")])
        units = build_units(transcript)
        self.assertEqual([(u.start,u.end) for u in units], [(0,5),(5,11),(11,35)])
        self.assertEqual(units[1].text, "Now solve this question Given values")

    def test_missing_overlapping_and_foreign_ids_fail_closed(self):
        units = [Unit(id=i,start=i,end=i+1,text="speech") for i in range(3)]
        def span(a,b):
            return ClassificationSpan(first_id=a,last_id=b,label=Label.CORE_TEACHING,confidence=.9,reason="test")
        for spans in [[span(0,1)], [span(0,2),span(2,2)], [span(0,3)], [span(2,0)]]:
            with self.subTest(spans=spans), self.assertRaises(ValueError):
                expand_spans(SpanClassification(spans=spans),units)
        labels = expand_spans(SpanClassification(spans=[span(0,2)]),units)
        self.assertEqual(set(labels),{0,1,2})
