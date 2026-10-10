import json
import shutil
import subprocess
import tempfile
import unittest
import copy
from pathlib import Path
from unittest.mock import patch

import lecture_trimmer.config as config
import lecture_trimmer.processing.pipeline as pipeline
from lecture_trimmer.media.audio import probe_duration, run_ffmpeg
from lecture_trimmer.media.cutter import cut_and_join, snap_to_keyframes
import lecture_trimmer.media.cutter as cutter
from lecture_trimmer.processing.editor import snap_to_speech
from lecture_trimmer.models import KeepRange, Label, Retention, Segment, Transcript, Unit, UnitLabel


def fixture():
    kinds = [Label.CORE_TEACHING, Label.WORKED_EXAMPLE, Label.CORE_TEACHING,
             Label.DOUBT_SOLVING, Label.CHATTER, Label.LOW_DENSITY]
    units = [Unit(id=i, start=i*2, end=(i+1)*2, text=f"Section {i}") for i in range(6)]
    labels = {i: UnitLabel(unit_id=i, label=k, confidence=1, reason="fixture") for i, k in enumerate(kinds)}
    transcript = Transcript(language="en", duration=12,
                            segments=[Segment(start=u.start, end=u.end, text=u.text) for u in units])
    return units, labels, transcript


class PlanningTests(unittest.TestCase):
    def test_rejected_academic_units_cannot_return_via_smoothing_or_custom_labels(self):
        units, labels, transcript = fixture()
        labels[1].retention = Retention.NONACADEMIC
        labels[2].retention = Retention.REPETITION
        plans = pipeline.plan_outputs(units,labels,transcript,12,min_gap=20,padding=3,
                                      combined_labels={'core_teaching','worked_example','doubt_solving'})
        for ranges in plans.values():
            self.assertFalse(any(r.start < 6 and r.end > 2 for r in ranges))
        self.assertTrue(any(r.start <= 6 and r.end >= 8 for r in plans['questions']))

    def test_nonacademic_speech_cannot_leak_back_through_padding(self):
        units, labels, transcript = fixture()
        plans = pipeline.plan_outputs(units, labels, transcript, 12, min_gap=20, padding=3)
        for ranges in plans.values():
            self.assertFalse(any(r.start < 10 and r.end > 8 for r in ranges))

    def test_source_snapshot_survives_reupload_and_reuses_identical_content(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as work:
            root = Path(work)
            a, b = root/'first.mp4', root/'renamed.mp4'
            a.write_bytes(b'original lecture')
            b.write_bytes(a.read_bytes())
            with patch.object(config, 'TEMP_DIR', root):
                first = pipeline.snapshot_source(a, lambda *args: None)
                second = pipeline.snapshot_source(b, lambda *args: None)
                self.assertEqual(first,second)
                a.write_bytes(b'replaced upload')
                self.assertEqual(first.read_bytes(),b'original lecture')
                self.assertNotEqual(first,pipeline.snapshot_source(a,lambda *args: None))
    def test_speech_snap_never_discards_selected_speech(self):
        transcript = Transcript(language="en", duration=10, segments=[
            Segment(start=0, end=1, text="Short introduction"),
            Segment(start=1, end=9, text="Rest of answer")])
        ranges = snap_to_speech([KeepRange(start=.8, end=8.8)], transcript)
        self.assertLessEqual(ranges[0].start, .8)
        self.assertGreaterEqual(ranges[0].end, 8.8)

    def test_specialized_outputs_do_not_bridge_opposite_category(self):
        units, labels, transcript = fixture()
        plans = pipeline.plan_outputs(units, labels, transcript, 12, min_gap=20, padding=2)
        for kind, blocked in [("questions", pipeline.THEORY_LABELS), ("theory", pipeline.QUESTION_LABELS)]:
            for r in plans[kind]:
                for u in units:
                    if labels[u.id].label.value in blocked:
                        self.assertFalse(r.start < u.end and r.end > u.start)
        # A two-second answer must survive; the old 30-second island rule dropped it.
        self.assertTrue(any(r.start <= 6 and r.end >= 8 for r in plans["questions"]))

    def test_categories_and_combined_in_order(self):
        units, labels, transcript = fixture()
        plans = pipeline.plan_outputs(units, labels, transcript, 12, min_gap=0, padding=0)
        self.assertEqual([(r.start, r.end) for r in plans["questions"]], [(2, 4), (6, 8)])
        self.assertEqual([(r.start, r.end) for r in plans["theory"]], [(0, 2), (4, 6), (10, 12)])
        self.assertEqual([(r.start, r.end) for r in plans["combined"]], [(0, 8), (10, 12)])

    def test_aggressive_still_preserves_question_answers(self):
        units, labels, transcript = fixture()
        plans = pipeline.plan_outputs(units, labels, transcript, 12, "aggressive", min_gap=0, padding=0)
        self.assertEqual([(r.start, r.end) for r in plans["questions"]], [(2, 4), (6, 8)])
        self.assertEqual([(r.start, r.end) for r in plans["theory"]], [(0, 2), (4, 6)])

    def test_empty_category(self):
        units, labels, transcript = fixture()
        for label in labels.values():
            label.label = Label.CORE_TEACHING
        plans = pipeline.plan_outputs(units, labels, transcript, 12)
        self.assertEqual(plans["questions"], [])

    def test_cache_identity_and_language(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as folder:
            root = Path(folder)
            (root / "other").mkdir()
            a, b = root / "same.mp4", root / "other/same.mp4"
            a.write_bytes(b"one")
            b.write_bytes(b"two")
            before = pipeline.transcript_cache(a, "en")
            self.assertNotEqual(before, pipeline.transcript_cache(b, "en"))
            self.assertNotEqual(before, pipeline.transcript_cache(a, "hi"))
            # Transcripts saved before the other speech engines were removed must still match.
            import hashlib, json, os
            stat = a.stat()
            legacy = [str(a.resolve()), stat.st_size, stat.st_mtime_ns, "groq", "en", "whisper-large-v3-turbo",
                      "saaras:v4", None, "unknown", "auto", "large-v3-turbo",
                      os.getenv("WHISPER_MODEL_CPU", "large-v3-turbo" if config.FROZEN else "small")]
            self.assertEqual(before.name, f"transcript_{hashlib.sha256(json.dumps(legacy).encode()).hexdigest()[:24]}.json")
            self.assertEqual(before, pipeline.transcript_cache(a, "English"))
            a.write_bytes(b"updated")
            self.assertNotEqual(before, pipeline.transcript_cache(a, "en"))

    def test_validation_before_api_calls(self):
        with patch.object(pipeline, "get_transcript") as transcribe:
            with self.assertRaises(ValueError):
                pipeline.process_video(Path("missing.mp4"), outputs=[])
            with self.assertRaises(ValueError):
                pipeline.process_video(Path("source.mp4"), combined_out=Path("source.mp4"))
            transcribe.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="test_media_", dir=config.TEMP_DIR)
        cls.root = Path(cls.temp.name)
        # Apostrophes/spaces exercise concat path handling.
        cls.video = cls.root / "teacher's lecture.mp4"
        run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac",
                    "-shortest", str(cls.video)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def assert_decodable(self, path):
        run_ffmpeg(["-i", str(path), "-f", "null", "-"])

    def test_smooth_precise_cuts_and_audio(self):
        out = self.root / "smooth.mp4"
        cut_and_join(self.video, [KeepRange(start=.32, end=2.52), KeepRange(start=5.16, end=8.36)],
                     out, temp_dir=self.root)
        self.assertAlmostEqual(probe_duration(out), 5.4, delta=.15)
        self.assert_decodable(out)
        raw = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
                             capture_output=True, text=True, check=True)
        streams = json.loads(raw.stdout)["streams"]
        self.assertEqual([s["codec_name"] for s in streams], ["h264", "aac"])
        self.assertLess(abs(float(streams[0]["duration"])-float(streams[1]["duration"])), .15)
        self.assertFalse(list(self.root.glob("cuts_*")))

    def test_uneven_long_cuts_preserve_all_decoded_audio_samples(self):
        import array
        source = self.root/'uneven_source.mp4'
        run_ffmpeg(['-f','lavfi','-i','color=size=160x90:rate=25:duration=400',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=44100:duration=400',
                    '-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',str(source)])
        out = self.root/'uneven.mp4'
        ranges = [KeepRange(start=3.12,end=38.32),KeepRange(start=89.24,end=205.92),
                  KeepRange(start=210.32,end=397.36)]
        cut_and_join(source,ranges,out,temp_dir=self.root)
        expected = sum(r.end-r.start for r in ranges)
        validation = cutter.validate_export(out,expected)
        self.assertAlmostEqual(validation['decoded_audio_seconds'],expected,delta=.05)
        decoded = subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(out),'-vn','-ac','1',
                                  '-ar','8000','-f','s16le','pipe:1'],capture_output=True,check=True)
        samples = array.array('h',decoded.stdout)
        self.assertAlmostEqual(len(samples)/8000,expected,delta=.05)
        # Inspect real samples throughout, including the middle clip previously lost by FLAC concat.
        for sec in [1,40,80,145,180,300]:
            block = samples[sec*8000:(sec+1)*8000]
            self.assertGreater(sum(x*x for x in block)/len(block),10000)

    def test_audio_gate_rejects_timestamp_holes_even_when_stream_endpoints_match(self):
        out = self.root/'audio_gap.mp4'
        run_ffmpeg(['-i',str(self.video),'-c:v','copy','-af',"aselect='not(between(t,3,8))'",
                    '-c:a','aac',str(out)])
        self.assertAlmostEqual(probe_duration(out),12,delta=.1)
        with self.assertRaisesRegex(RuntimeError,'gap|cover'):
            cutter.validate_export(out,12)

    def test_fast_export(self):
        out = self.root / "fast.mp4"
        ranges = snap_to_keyframes([KeepRange(start=1, end=3), KeepRange(start=7, end=9)], self.video)
        cut_and_join(self.video, ranges, out, temp_dir=self.root, style="fast")
        self.assert_decodable(out)

    def test_three_exports_share_one_analysis(self):
        units, labels, transcript = fixture()
        with (patch.object(config, "OUTPUT_DIR", self.root),
              patch.object(pipeline, "get_transcript", return_value=transcript) as speech,
              patch.object(pipeline, "build_units", return_value=units),
              patch.object(pipeline, "classify", return_value=labels) as classify,
              patch.object(cutter, "run_ffmpeg", wraps=cutter.run_ffmpeg) as ffmpeg):
            result = pipeline.process_video(self.video, min_gap=0, padding=0)
            speech.assert_called_once()
            classify.assert_called_once()
            self.assertEqual(set(result["outputs"]), {"questions", "theory", "combined"})
            for kind, expected in [("questions", 4), ("theory", 6), ("combined", 10)]:
                item = result["outputs"][kind]
                self.assertEqual(item["status"], "ready")
                self.assertAlmostEqual(item["duration"], expected, delta=.15)
                self.assert_decodable(Path(item["path"]))
            self.assertTrue(Path(result["report_path"]).is_file())
            # Five unique sections, not ten: combined output only remuxes saved parts.
            encodes = [c for c in ffmpeg.call_args_list if '-ss' in c.args[0]]
            self.assertLessEqual(len(encodes),5)

    def test_partial_failure_keeps_successes_and_reports_them_immediately(self):
        units, labels, transcript = fixture()
        observed = []
        def export(video, ranges, out, **kwargs):
            if out.stem.endswith('_theory'):
                raise RuntimeError('Simulated unavailable encoder')
            return cut_and_join(video,ranges,out,**kwargs)
        with (patch.object(config,'OUTPUT_DIR',self.root),
              patch.object(pipeline,'get_transcript',return_value=transcript),
              patch.object(pipeline,'build_units',return_value=units),
              patch.object(pipeline,'classify',return_value=labels),
              # Exercise a new export failure, not reuse of a previous valid fixture.
              patch.object(pipeline,'reuse_complete_export',return_value=None),
              patch.object(pipeline,'cut_and_join',side_effect=export)):
            result = pipeline.process_video(self.video,min_gap=0,padding=0,
                on_result=lambda r: observed.append(copy.deepcopy(r)))
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['outputs']['questions']['status'],'ready')
        self.assertEqual(result['outputs']['theory']['status'],'failed')
        self.assertEqual(result['outputs']['combined']['status'],'ready')
        self.assertTrue(any(r['outputs'].get('questions',{}).get('status')=='ready' and
                            r['outputs']['combined']['status']=='planned' for r in observed))
        self.assertTrue(Path(result['outputs']['questions']['path']).is_file())

    def test_many_precise_joins_do_not_accumulate_audio_drift(self):
        out = self.root/'many_joins.mp4'
        ranges = [KeepRange(start=i*.56,end=i*.56+.32) for i in range(20)]
        cut_and_join(self.video,ranges,out,temp_dir=self.root)
        raw = subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(out)],
                             capture_output=True,text=True,check=True)
        streams = json.loads(raw.stdout)['streams']
        self.assertAlmostEqual(probe_duration(out),6.4,delta=.15)
        self.assertLess(abs(float(streams[0]['duration'])-float(streams[1]['duration'])),.15)
        self.assert_decodable(out)

    def test_dry_run_empty_category_and_unique_jobs(self):
        units, labels, transcript = fixture()
        for label in labels.values():
            label.label = Label.CORE_TEACHING
        with (patch.object(config, "OUTPUT_DIR", self.root),
              patch.object(pipeline, "get_transcript", return_value=transcript),
              patch.object(pipeline, "build_units", return_value=units),
              patch.object(pipeline, "classify", return_value=labels),
              patch.object(pipeline, "cut_and_join") as cut):
            first = pipeline.process_video(self.video, dry_run=True)
            second = pipeline.process_video(self.video, outputs=["questions"])
            cut.assert_not_called()
            self.assertNotEqual(first["report_path"], second["report_path"])
            self.assertEqual(second["outputs"]["questions"]["status"], "no_matching_content")
            self.assertIsNone(second["outputs"]["questions"]["path"])

    def test_fast_mode_falls_back_instead_of_restoring_excluded_speech(self):
        units, labels, transcript = fixture()
        with (patch.object(config,'OUTPUT_DIR',self.root),
              patch.object(pipeline,'get_transcript',return_value=transcript),
              patch.object(pipeline,'build_units',return_value=units),
              patch.object(pipeline,'classify',return_value=labels),
              patch.object(pipeline,'keyframe_times',return_value=[0,12])):
            result = pipeline.process_video(self.video,cut_style='fast',dry_run=True)
        self.assertEqual(result['cut_style'],'smooth')
        self.assertTrue(any('restore excluded' in w for w in result['warnings']))
        self.assertFalse(any(r['start']<10 and r['end']>8 for r in result['outputs']['combined']['keep']))

    def test_export_failure_cleans_temporary_files(self):
        with patch("lecture_trimmer.media.cutter.run_ffmpeg", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                cut_and_join(self.video, [KeepRange(start=0, end=1)], self.root / "failed.mp4", self.root)
        self.assertFalse(list(self.root.glob("cuts_*")))
        self.assertFalse((self.root / "failed.mp4").exists())


if __name__ == "__main__":
    unittest.main()
