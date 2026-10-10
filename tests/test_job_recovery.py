import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from groq import APITimeoutError, RateLimitError
import lecture_trimmer.config as config
import lecture_trimmer.media.transcriber as transcriber
from lecture_trimmer.processing.job_control import (ProcessingCancelled, cancellation_scope,
                         interruptible_wait, submit_with_context)
from lecture_trimmer.models import Segment, Word


class RecoveryTests(unittest.TestCase):
    def test_timeout_retries_same_section_with_word_timestamps(self):
        client = MagicMock()
        client.audio.transcriptions.create.side_effect = [
            APITimeoutError(request=httpx.Request('POST', 'https://example.test')),
            SimpleNamespace(language='hi', segments=[dict(start=0, end=1, text='velocity')],
                            words=[dict(start=0, end=1, word='velocity')])]
        with tempfile.TemporaryDirectory() as work, patch.object(transcriber, 'interruptible_wait') as wait:
            path = Path(work)/'speech.flac'
            path.write_bytes(b'audio')
            result = transcriber._groq_chunk(client, 120, path, None)
        self.assertEqual(result[2][0].start, 120)
        self.assertEqual(client.audio.transcriptions.create.call_count, 2)
        wait.assert_called_once_with(5)

    def test_long_quota_wait_is_actionable_and_does_not_spin(self):
        client = MagicMock()
        client.audio.transcriptions.create.side_effect = RateLimitError('private provider data',
            response=httpx.Response(429, headers={'retry-after':'3600'},
                                    request=httpx.Request('POST', 'https://example.test')), body=None)
        with tempfile.TemporaryDirectory() as work, patch.object(transcriber, 'interruptible_wait') as wait:
            path = Path(work)/'speech.flac'
            path.write_bytes(b'audio')
            with self.assertRaises(transcriber.TranscriptionServiceError) as caught:
                transcriber._groq_chunk(client, 0, path, None)
        self.assertIn('usage limit', caught.exception.public_message)
        self.assertNotIn('private', caught.exception.public_message)
        wait.assert_not_called()

    def test_completed_chunks_survive_failure_and_are_not_sent_again(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            def chunks(*args, **kwargs):
                result = []
                for i in range(3):
                    path=root/f'{i}.flac'; path.write_bytes(bytes([i]))
                    result.append((i*120, path))
                return result
            calls=[]
            fail=True
            def request(client, offset, path, language, **kwargs):
                calls.append(offset)
                if fail and offset==120:
                    raise transcriber.TranscriptionServiceError('timeout')
                return 'hi', [Segment(start=offset,end=offset+1,text='velocity')], [Word(start=offset,end=offset+1,text='velocity')]
            with (patch('groq.Groq'), patch.object(config,'GROQ_API_KEY','test'),
                  patch.object(config,'GROQ_PARALLEL_REQUESTS',1),
                  patch.object(transcriber,'chunk_audio',side_effect=chunks),
                  patch.object(transcriber,'probe_duration',return_value=360),
                  patch.object(transcriber,'_groq_chunk',side_effect=request)):
                with self.assertRaises(transcriber.TranscriptionServiceError):
                    transcriber.transcribe_groq(root/'input.wav', cache_dir=root/'saved')
                fail=False
                transcript=transcriber.transcribe_groq(root/'input.wav', cache_dir=root/'saved')
            self.assertEqual(calls,[0,120,120,240])
            self.assertEqual([s.start for s in transcript.segments],[0,120,240])

    def test_stop_propagates_to_workers_and_interrupts_backoff(self):
        event=threading.Event()
        with cancellation_scope(event), ThreadPoolExecutor(1) as pool:
            event.set()
            future=submit_with_context(pool,interruptible_wait,60)
            with self.assertRaises(ProcessingCancelled): future.result(timeout=1)

    def test_cancelled_job_report_is_saved_without_error(self):
        import lecture_trimmer.processing.pipeline as pipeline
        import json
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); source=root/'video.mp4'; source.write_bytes(b'video')
            output=root/'output'; output.mkdir()
            with (patch.object(config,'OUTPUT_DIR',output), patch.object(config,'TEMP_DIR',root),
                  patch.object(pipeline,'snapshot_source',return_value=source),
                  patch.object(pipeline,'probe_duration',return_value=120),
                  patch.object(pipeline,'get_transcript',side_effect=ProcessingCancelled)):
                with self.assertRaises(ProcessingCancelled): pipeline.process_video(source)
            report=json.loads(next(output.glob('*/analysis.json')).read_text())
        self.assertEqual(report['status'],'cancelled')
        self.assertNotIn('error_reference',report)

    def test_stop_terminates_ffmpeg(self):
        import lecture_trimmer.media.audio as audio
        event=threading.Event()
        proc=MagicMock()
        proc.poll.return_value=None
        proc.stdout=iter([])
        proc.stdout=MagicMock(__iter__=lambda self: iter([]))
        def launch(*args,**kwargs):
            event.set()
            return proc
        with cancellation_scope(event), patch.object(audio.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(ProcessingCancelled): audio.run_ffmpeg(['-f','null','-'])
        proc.kill.assert_called_once()
        proc.wait.assert_called_once()


if __name__=='__main__':
    unittest.main()
