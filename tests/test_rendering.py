"""Parallel export must preserve chronological order, cache safety and audio coverage."""
import array
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import lecture_trimmer.config as config
import lecture_trimmer.media.cutter as cutter
import lecture_trimmer.processing.pipeline as pipeline
from lecture_trimmer.media.audio import run_ffmpeg
from lecture_trimmer.models import KeepRange, Label, Segment, Transcript, Unit, UnitLabel


class RenderingWorkerTests(unittest.TestCase):
    def test_hardware_workers_are_bounded_and_software_is_not_oversubscribed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cutter.render_worker_count("h264_nvenc"), 4)
            self.assertEqual(cutter.render_worker_count("h264_qsv"), 2)
            self.assertEqual(cutter.render_worker_count("libx264"), 1)
            self.assertEqual(cutter.render_worker_count("copy"), 1)
        with patch.dict(os.environ, {"TRIMMER_RENDER_WORKERS": "4"}):
            self.assertEqual(cutter.render_worker_count("h264_qsv"), 4)
            self.assertEqual(cutter.render_worker_count("libx264"), 1)
        for invalid in ["0", "5", "two"]:
            with patch.dict(os.environ, {"TRIMMER_RENDER_WORKERS": invalid}):
                with self.assertRaisesRegex(ValueError, "1 to 4"):
                    cutter.render_worker_count("h264_nvenc")

    def test_out_of_order_workers_publish_chronologically_and_reuse_cache(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as folder:
            root = Path(folder)
            source = root / "lecture.mp4"
            source.write_bytes(b"source")
            ranges = [KeepRange(start=0, end=3), KeepRange(start=10, end=17),
                      KeepRange(start=20, end=25)]
            active, peak, encodes = 0, 0, 0
            lock = threading.Lock()
            first_two_started = threading.Event()
            progress, completed, joined = [], [], []

            def fake_ffmpeg(args, *, progress=None, duration=None):
                nonlocal active, peak, encodes
                target = Path(args[-1])
                if "-ss" in args:
                    start = float(args[args.index("-ss") + 1])
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        encodes += 1
                        if active == 2:
                            first_two_started.set()
                    self.assertTrue(first_two_started.wait(2))
                    time.sleep(.09 if start == 0 else .01)
                    progress(.5, "half complete")
                    target.write_text(str(start))
                    with lock:
                        completed.append(start)
                        active -= 1
                else:
                    listing = Path(args[args.index("-i") + 1]).read_text()
                    # Generated test paths contain no apostrophes.
                    paths = [Path(line[6:-1]) for line in listing.splitlines()]
                    joined.append([float(path.read_text()) for path in paths])
                    target.write_bytes(b"finished")
                    progress(1, "complete")

            with (patch.dict(os.environ, {"TRIMMER_RENDER_WORKERS": "2"}),
                  patch.object(cutter, "video_fps", return_value=Fraction(25)),
                  patch.object(cutter, "run_ffmpeg", side_effect=fake_ffmpeg),
                  patch.object(cutter, "probe_duration", return_value=15),
                  patch.object(cutter, "validate_export", return_value={}) as validate):
                cutter.cut_and_join(source, ranges, root / "first.mp4", root,
                    encoder="h264_nvenc", cache_dir=root / "cache",
                    progress=lambda fraction, desc: progress.append(fraction))
                self.assertEqual(peak, 2)
                self.assertNotEqual(completed, [0, 10, 20])
                self.assertEqual(joined, [[0, 10, 20]])
                self.assertEqual(progress, sorted(progress))
                self.assertEqual(encodes, 3)
                cutter.cut_and_join(source, ranges, root / "retry.mp4", root,
                    encoder="h264_nvenc", cache_dir=root / "cache", progress=lambda *args: None)
                self.assertEqual(encodes, 3)
                self.assertEqual(joined, [[0, 10, 20], [0, 10, 20]])
                self.assertEqual(validate.call_count, 2)
            self.assertFalse(list(root.glob("cuts_*")))
            self.assertFalse(list(root.rglob("*.partial")))

    def test_hardware_failure_stops_siblings_before_consistent_software_retry(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as folder:
            root = Path(folder)
            source = root / "lecture.mp4"
            source.write_bytes(b"source")
            ranges = [KeepRange(start=0, end=2), KeepRange(start=10, end=12),
                      KeepRange(start=20, end=22)]
            active_hardware = 0
            lock = threading.Lock()
            siblings_started = threading.Event()
            cancelled = threading.Event()
            software_starts = []

            def fake_ffmpeg(args, *, progress=None, duration=None):
                nonlocal active_hardware
                target = Path(args[-1])
                if "-ss" not in args:
                    listing = Path(args[args.index("-i") + 1]).read_text()
                    paths = [Path(line[6:-1]) for line in listing.splitlines()]
                    self.assertTrue(all(path.read_text() == "libx264" for path in paths))
                    target.write_bytes(b"finished")
                    return
                encoder = args[args.index("-c:v") + 1]
                start = float(args[args.index("-ss") + 1])
                if encoder == "libx264":
                    with lock:
                        self.assertEqual(active_hardware, 0)
                        software_starts.append(start)
                    target.write_text(encoder)
                    return
                with lock:
                    active_hardware += 1
                    if active_hardware == 2:
                        siblings_started.set()
                try:
                    self.assertTrue(siblings_started.wait(2))
                    if start == 0:
                        raise RuntimeError("hardware session failure")
                    for _ in range(100):
                        time.sleep(.01)
                        progress(.2, "rendering")
                    self.fail("Sibling FFmpeg was not cancelled")
                except cutter._RenderAborted:
                    cancelled.set()
                    raise
                finally:
                    with lock:
                        active_hardware -= 1

            with (patch.dict(os.environ, {"TRIMMER_RENDER_WORKERS": "2"}),
                  patch.object(cutter, "video_fps", return_value=Fraction(25)),
                  patch.object(cutter, "run_ffmpeg", side_effect=fake_ffmpeg),
                  patch.object(cutter, "probe_duration", return_value=6),
                  patch.object(cutter, "validate_export", return_value={} )):
                cutter.cut_and_join(source, ranges, root / "recovered.mp4", root,
                    encoder="h264_nvenc", cache_dir=root / "cache", progress=lambda *args: None)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(software_starts, [0, 10, 20])
            self.assertFalse(list(root.glob("cuts_*")))
            self.assertFalse(list(root.rglob("*.partial")))

    def test_audio_validation_failure_cannot_replace_a_previous_output(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as folder:
            root = Path(folder)
            source, out = root / "lecture.mp4", root / "previous.mp4"
            source.write_bytes(b"source")
            out.write_bytes(b"previous valid output")

            def fake_ffmpeg(args, **kwargs):
                Path(args[-1]).write_bytes(b"invalid media")

            with (patch.object(cutter, "video_fps", return_value=Fraction(25)),
                  patch.object(cutter, "run_ffmpeg", side_effect=fake_ffmpeg),
                  patch.object(cutter, "probe_duration", return_value=2),
                  patch.object(cutter, "validate_export", side_effect=RuntimeError("audio has a gap"))):
                with self.assertRaisesRegex(RuntimeError, "audio has a gap"):
                    cutter.cut_and_join(source, [KeepRange(start=0, end=2)], out, root,
                                        encoder="libx264", progress=lambda *args: None)
            self.assertEqual(out.read_bytes(), b"previous valid output")
            self.assertFalse(list(root.glob("cuts_*")))
            self.assertFalse(list(root.rglob("*.partial")))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ParallelAudioTests(unittest.TestCase):
    def test_uneven_parallel_sections_keep_all_audio_and_match_sequential_render(self):
        encoder = cutter.select_encoder()
        if encoder not in {"h264_nvenc", "h264_qsv"}:
            self.skipTest("A working hardware encoder is required for parallel rendering")
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as folder:
            root = Path(folder)
            source = root / "uneven.mp4"
            run_ffmpeg(["-f", "lavfi", "-i", "color=size=320x180:rate=25:duration=180",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=180",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)])
            ranges = [KeepRange(start=.12, end=7.36), KeepRange(start=20.04, end=60.32),
                      KeepRange(start=76.48, end=179.08)]
            expected = sum(r.end-r.start for r in ranges)
            decoded_outputs = []
            for workers in [1, 2]:
                out = root / f"workers_{workers}.mp4"
                with patch.dict(os.environ, {"TRIMMER_RENDER_WORKERS": str(workers)}):
                    cutter.cut_and_join(source, ranges, out, root, encoder=encoder,
                                        cache_dir=root / f"cache_{workers}",
                                        progress=lambda *args: None)
                checked = cutter.validate_export(out, expected)
                self.assertAlmostEqual(checked["decoded_audio_seconds"], expected, delta=.05)
                decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(out),
                    "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                    capture_output=True, check=True)
                samples = array.array("h", decoded.stdout)
                self.assertAlmostEqual(len(samples)/8000, expected, delta=.05)
                for second in [1, 8, 30, 46, 50, 80, 140]:
                    block = samples[second*8000:(second+1)*8000]
                    self.assertGreater(sum(x*x for x in block)/len(block), 10000)
                decoded_outputs.append(decoded.stdout)
            self.assertEqual(decoded_outputs[0], decoded_outputs[1])


class CompleteExportIdentityTests(unittest.TestCase):
    def test_finished_export_key_includes_source_encoder_version_and_exact_ordered_plan(self):
        source = "a" * 64
        ranges = [KeepRange(start=0, end=2), KeepRange(start=5, end=8)]
        baseline = pipeline.complete_export_key(source, "libx264", "smooth", ranges)
        self.assertNotEqual(baseline, pipeline.complete_export_key("b" * 64, "libx264", "smooth", ranges))
        self.assertNotEqual(baseline, pipeline.complete_export_key(source, "h264_nvenc", "smooth", ranges))
        self.assertNotEqual(baseline, pipeline.complete_export_key(source, "libx264", "smooth", ranges[::-1]))
        changed = [KeepRange(start=.001, end=2), ranges[1]]
        self.assertNotEqual(baseline, pipeline.complete_export_key(source, "libx264", "smooth", changed))
        with patch.object(pipeline, "RENDER_VERSION", "future-render"):
            self.assertNotEqual(baseline, pipeline.complete_export_key(source, "libx264", "smooth", ranges))
        self.assertIsNone(pipeline.complete_export_key(source, "libx264", "fast", ranges))
        self.assertIsNone(pipeline.complete_export_key(source, None, "smooth", ranges))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CompleteExportCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=config.TEMP_DIR)
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "verified.mp4"
        run_ffmpeg(["-f", "lavfi", "-i", "color=size=320x180:rate=25:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(cls.video)])
        cutter.validate_export(cls.video, 4)
        cls.ranges = [KeepRange(start=0, end=4)]
        cls.key = pipeline.complete_export_key("a" * 64, "libx264", "smooth", cls.ranges)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_cache_reuses_verified_bytes_as_separate_owned_files(self):
        cache = self.root / "reuse_cache"
        pipeline.save_complete_export(cache, self.key, self.video, 4, "libx264")
        self.assertEqual(pipeline._finished_export_encoder(self.video), "libx264")
        owner_one, owner_two = self.root / "owner_one/video.mp4", self.root / "owner_two/video.mp4"
        for out in [owner_one, owner_two]:
            reused = pipeline.reuse_complete_export(cache, self.key, out)
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["duration"], 4)
            self.assertEqual(out.read_bytes(), self.video.read_bytes())
            cutter.validate_export(out, 4)
        owner_one.write_bytes(b"owner one replaced their file")
        self.assertEqual(owner_two.read_bytes(), self.video.read_bytes())
        fresh_copy = self.root / "owner_three/video.mp4"
        self.assertIsNotNone(pipeline.reuse_complete_export(cache, self.key, fresh_copy))
        self.assertEqual(fresh_copy.read_bytes(), self.video.read_bytes())
        self.assertFalse(list(cache.glob("*.partial")))

    def test_same_size_and_mtime_corruption_is_a_miss_and_preserves_existing_output(self):
        cache = self.root / "corruption_cache"
        pipeline.save_complete_export(cache, self.key, self.video, 4, "libx264")
        manifest = json.loads((cache / f"{self.key}.json").read_text())
        blob = cache / f"{manifest['sha256']}.mp4"
        stat = blob.stat()
        content = bytearray(blob.read_bytes())
        content[len(content)//2] ^= 0x01
        blob.write_bytes(content)
        os.utime(blob, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        out = self.root / "existing.mp4"
        out.write_bytes(b"previous valid output")
        self.assertIsNone(pipeline.reuse_complete_export(cache, self.key, out))
        self.assertEqual(out.read_bytes(), b"previous valid output")
        self.assertFalse(list(self.root.glob("*.partial")))

    def test_unfinished_or_unvalidated_cache_is_never_reused(self):
        cache = self.root / "unfinished_cache"
        cache.mkdir()
        digest = hashlib.sha256(self.video.read_bytes()).hexdigest()
        blob = cache / f"{digest}.mp4"
        shutil.copyfile(self.video, blob)
        out = self.root / "new_job/video.mp4"
        self.assertIsNone(pipeline.reuse_complete_export(cache, self.key, out))
        # Even a valid video must have a successful validation certificate.
        manifest = {"key": self.key, "sha256": digest, "render_version": pipeline.RENDER_VERSION,
                    "duration": 4, "size": blob.stat().st_size, "mtime_ns": blob.stat().st_mtime_ns,
                    "validation": "pending"}
        (cache / f"{self.key}.json").write_text(json.dumps(manifest))
        self.assertIsNone(pipeline.reuse_complete_export(cache, self.key, out))
        (cache / f"{self.key}.json").write_text("[]")
        self.assertIsNone(pipeline.reuse_complete_export(cache, self.key, out))
        self.assertFalse(out.exists())

    def test_pipeline_second_owner_gets_a_new_path_without_rejoining_completed_video(self):
        root = self.root / "pipeline_reuse"
        root.mkdir()
        transcript = Transcript(language="en", duration=4,
                                segments=[Segment(start=0, end=4, text="A complete concept explanation.")])
        unit = Unit(id=0, start=0, end=4, text=transcript.segments[0].text)
        labels = {0: UnitLabel(unit_id=0, label=Label.CORE_TEACHING, confidence=1, reason="complete lesson")}
        with (patch.object(config, "TEMP_DIR", root), patch.object(config, "OUTPUT_DIR", root),
              patch.object(pipeline, "get_transcript", return_value=transcript),
              patch.object(pipeline, "build_units", return_value=[unit]),
              patch.object(pipeline, "classify", return_value=labels),
              patch.object(pipeline, "select_encoder", return_value="libx264"),
              patch.object(pipeline, "cut_and_join", wraps=cutter.cut_and_join) as render):
            first = pipeline.process_video(self.video, outputs=["theory"], owner_id="owner-one")
            second = pipeline.process_video(self.video, outputs=["theory"], owner_id="owner-two")
        render.assert_called_once()
        self.assertEqual(first["owner_id"], "owner-one")
        self.assertEqual(second["owner_id"], "owner-two")
        self.assertEqual(second["outputs"]["theory"]["export_cache"], "reused")
        first_path = Path(first["outputs"]["theory"]["path"])
        second_path = Path(second["outputs"]["theory"]["path"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())
        self.assertEqual(second_path.parent, Path(second["report_path"]).parent)


if __name__ == "__main__":
    unittest.main()
