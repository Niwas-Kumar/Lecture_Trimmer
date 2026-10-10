"""Real media regressions for transcription audio timing; no AI requests."""
import array
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import lecture_trimmer.media.audio as audio
import lecture_trimmer.config as config


def create_media(args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                   capture_output=True, text=True, check=True, timeout=25)


def inspect_wav(path, start=0, duration=.2):
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        length = wav.getnframes() / rate
        wav.setpos(int(start * rate))
        samples = array.array("h", wav.readframes(int(duration * rate)))
        energy = sum(abs(s) for s in samples) / max(1, len(samples))
        return length, energy, wav.getnchannels(), rate


class AudioExtractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=config.TEMP_DIR)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_overlapping_aac_start_reproduces_old_failure_and_now_extracts(self):
        source = self.root / "overlapping.m4a"
        create_media(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                      "-c:a", "aac", "-bsf:a", "setts=pts='max(PTS,0)':dts='max(DTS,0)'", str(source)])
        old = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-y",
            "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(self.root/"old.wav")],
            capture_output=True, text=True, timeout=20)
        self.assertNotEqual(old.returncode, 0)
        self.assertIn("Non-monotonic DTS", old.stderr)
        events = []
        result = audio.extract_audio(source, self.root, progress=lambda f, text: events.append((f, text)))
        length, energy, channels, rate = inspect_wav(result, .5)
        self.assertAlmostEqual(length, audio.probe_duration(source), delta=.04)
        self.assertGreater(energy, 100)
        self.assertEqual((channels, rate), (1, 16000))
        self.assertTrue(events)
        self.assertTrue(all("Preparing lecture audio" in text for _, text in events))

    def test_late_audio_preserves_initial_silence_in_video_timeline(self):
        source = self.root / "delayed.mp4"
        create_media(["-f", "lavfi", "-i", "color=s=160x90:r=10:d=4",
                      "-itsoffset", "2", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                      "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-threads", "1",
                      "-c:a", "aac", str(source)])
        result = audio.extract_audio(source, self.root)
        self.assertLess(inspect_wav(result, .5)[1], 1)
        self.assertGreater(inspect_wav(result, 2.5)[1], 100)
        self.assertAlmostEqual(inspect_wav(result)[0], 4, delta=.05)

    def test_internal_timestamp_gap_is_filled_without_shifting_later_speech(self):
        source = self.root / "gap.mka"
        create_media(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                      "-af", "aselect='not(between(t,1,2))'", "-c:a", "pcm_s16le", str(source)])
        result = audio.extract_audio(source, self.root)
        self.assertGreater(inspect_wav(result, .5)[1], 100)
        self.assertLess(inspect_wav(result, 1.5)[1], 1)
        self.assertGreater(inspect_wav(result, 2.5)[1], 100)
        self.assertAlmostEqual(inspect_wav(result)[0], 3, delta=.01)

    def test_failed_extraction_removes_partial_audio_and_keeps_source(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        partial = self.root / "source.wav"
        def fail(*args, **kwargs):
            partial.write_bytes(b"incomplete")
            raise RuntimeError("Decoder failed on private-source-path")
        with patch.object(audio, "probe_duration", return_value=10), patch.object(audio, "run_ffmpeg", side_effect=fail):
            with self.assertRaises(audio.AudioExtractionError) as caught:
                audio.extract_audio(source, self.root)
        self.assertFalse(partial.exists())
        self.assertEqual(source.read_bytes(), b"original")
        self.assertNotIn("private-source-path", caught.exception.public_message)

    def test_source_wav_cannot_be_overwritten_or_deleted(self):
        source = self.root / "source.wav"
        source.write_bytes(b"original")
        with self.assertRaises(audio.AudioExtractionError):
            audio.extract_audio(source, self.root)
        self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
