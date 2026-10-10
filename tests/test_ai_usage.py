import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import lecture_trimmer.config as config
import lecture_trimmer.processing.pipeline as pipeline
from lecture_trimmer.integrations.ai_usage import AIUsage
from lecture_trimmer.models import Transcript, Segment, Unit, UnitLabel, Label
from lecture_trimmer.integrations.pw_proxy import PWJob, ProxyCallError


def billed(cost="0.125", text="OK"):
    return {"cost_inr": cost, "usage": {"tokens_in": 120, "tokens_out": 30},
            "result": {"candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"text": text}], "role": "model"}}]}}


class UsageTests(unittest.TestCase):
    def test_parallel_calls_are_counted_once_and_jobs_are_isolated(self):
        job, other = PWJob(Mock(), "one"), PWJob(Mock(), "two")
        job.session.generate.return_value = billed("0.01")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: job.generate(model="gemini-test", request={}), range(40)))
        usage = job.usage.snapshot()
        self.assertEqual(usage["requests"], 40)
        self.assertEqual(usage["reported_cost_inr"], "0.40")
        self.assertEqual(usage["input_tokens"], 4800)
        self.assertEqual(usage["output_tokens"], 1200)
        self.assertEqual(other.usage.snapshot()["requests"], 0)
        self.assertNotIn("OK", json.dumps(usage))

    def test_invalid_generation_and_failed_retry_do_not_hide_cost(self):
        job = PWJob(Mock(), "retry")
        incomplete = billed("0.2")
        incomplete["result"]["candidates"][0]["finishReason"] = "MAX_TOKENS"
        job.session.generate.side_effect = [incomplete, ProxyCallError(500), billed("0.3")]
        client = job.client("gemini")
        try:
            with self.assertRaises(RuntimeError):
                client.models.generate_content(model="gemini-test", contents="test")
            with self.assertRaises(ProxyCallError):
                client.models.generate_content(model="gemini-test", contents="test")
            self.assertEqual(client.models.generate_content(model="gemini-test", contents="test").text, "OK")
        finally:
            client.close()
        usage = job.usage.snapshot()
        self.assertEqual(usage["requests"], 3)
        self.assertEqual(usage["failed_requests"], 1)
        self.assertEqual(usage["reported_cost_inr"], "0.5")
        self.assertEqual(usage["priced_requests"], 2)  # a subtotal: the failed request's cost is unknown

    def test_absent_invalid_and_zero_billing_are_distinct(self):
        for invalid in (None, True, -1, "NaN", "Infinity", {}, "bad"):
            usage = AIUsage()
            usage.record("gemini", {"cost_inr": invalid, "usage": {"tokens_in": True, "tokens_out": -1}})
            snapshot = usage.snapshot()
            self.assertIsNone(snapshot["reported_cost_inr"])  # unknown, never shown as free
            self.assertEqual(snapshot["priced_requests"], 0)
            self.assertIsNone(snapshot["input_tokens"])
        usage = AIUsage()
        usage.record("gemini", billed(0))
        self.assertEqual((usage.snapshot()["reported_cost_inr"], usage.snapshot()["priced_requests"]), ("0", 1))
        tiny = AIUsage()
        tiny.record("gemini", billed("0.000001"))
        self.assertEqual(tiny.snapshot()["reported_cost_inr"], "0.000001")

    def test_report_retains_cost_after_failure_and_cache_reuse_is_new_run(self):
        with tempfile.TemporaryDirectory(dir=config.TEMP_DIR) as work:
            root = Path(work)
            source = root / "source.mp4"
            source.write_bytes(b"fixture")
            transcript = Transcript(language="en", duration=12,
                segments=[Segment(start=0, end=12, text="Theory explanation")])
            unit = Unit(id=0, start=0, end=12, text="Theory explanation")
            labels = {0: UnitLabel(unit_id=0, label=Label.CORE_TEACHING, confidence=1, reason="fixture")}
            job = PWJob(Mock(), "failed-job")
            job.session.generate.return_value = billed("0.125")
            def fail(*args, **kwargs):
                job.generate(model="gemini-test", request={})
                raise RuntimeError("Invalid classification")
            with (patch.object(config, "TEMP_DIR", root), patch.object(config, "OUTPUT_DIR", root),
                  patch.object(pipeline, "snapshot_source", return_value=source),
                  patch.object(pipeline, "probe_duration", return_value=12),
                  patch.object(pipeline, "get_transcript", return_value=transcript),
                  patch.object(pipeline, "build_units", return_value=[unit])):
                with patch.object(pipeline, "classify", side_effect=fail), self.assertRaises(RuntimeError):
                    pipeline.process_video(source, dry_run=True, provider_session=job)
                saved = json.loads(next(root.glob("*/analysis.json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["ai_usage"]["reported_cost_inr"], "0.125")
                self.assertTrue(saved['transcript_files'])
                self.assertTrue(all(Path(p).is_file() for p in saved['transcript_files']))
                pipeline.transcript_cache(source, None).write_text(transcript.model_dump_json())
                retry = PWJob(Mock(), "cached-job")
                with patch.object(pipeline, "classify", return_value=labels):
                    result = pipeline.process_video(source, dry_run=True, provider_session=retry)
                self.assertEqual(result["ai_usage"]["requests"], 0)
                self.assertEqual(result["ai_usage"]["reported_cost_inr"], "0")
                self.assertTrue(result["transcription"]["reused"])


if __name__ == "__main__":
    unittest.main()
