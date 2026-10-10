import json
from pathlib import Path
import tempfile
import unittest

from lecture_trimmer.media.transcript_export import timestamp, write_transcript_files
from lecture_trimmer.models import Segment, Transcript, Word


class TranscriptExportTests(unittest.TestCase):
    def test_readable_hinglish_and_word_offsets_are_preserved(self):
        transcript = Transcript(language='hi', duration=3700,
            segments=[Segment(start=3600.125, end=3602.75, text='यह acceleration है।')],
            words=[Word(start=3600.125, end=3600.5, text='यह')])
        with tempfile.TemporaryDirectory() as work:
            files = write_transcript_files(transcript, Path(work), video_name='Physics.mp4', backend='groq',
                                           model='whisper-large-v3-turbo')
            self.assertEqual(len(files), 4)
            text = Path(files[0]).read_text(encoding='utf-8-sig')
            self.assertIn('[01:00:00.125 --> 01:00:02.750] यह acceleration है।', text)
            self.assertIn('ORIGINAL lecture', text)
            self.assertIn('1\n01:00:00,125 --> 01:00:02,750\nयह acceleration है।',
                          Path(files[1]).read_text(encoding='utf-8-sig'))
            self.assertEqual(json.loads(Path(files[2]).read_text(encoding='utf-8')), transcript.model_dump())
            self.assertIn('[01:00:00.125 --> 01:00:00.500] यह', Path(files[3]).read_text(encoding='utf-8-sig'))

    def test_missing_word_alignment_is_not_fabricated(self):
        transcript = Transcript(language='en', duration=2, segments=[Segment(start=0, end=2, text='Study')])
        with tempfile.TemporaryDirectory() as work:
            files = write_transcript_files(transcript, Path(work), video_name='Lesson.mp4', backend='groq')
            self.assertEqual(len(files), 3)
            self.assertEqual(json.loads(Path(files[2]).read_text())['words'], [])

    def test_timestamp_rounding_carries_into_next_minute(self):
        self.assertEqual(timestamp(59.9999), '00:01:00.000')


