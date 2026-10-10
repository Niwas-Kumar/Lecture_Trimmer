import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lecture_trimmer.runtime.messages import public_message
import lecture_trimmer.config as config
from lecture_trimmer.models import LessonMap, TeachingActivity, ActivityKind
from lecture_trimmer.integrations.pw_diagnostics import check_connection_events
from lecture_trimmer.integrations.pw_proxy import ProxyCallError, raise_safe_proxy_error, parse_gemini_response, response_structure, PWJob


class DiagnosticsTests(unittest.TestCase):
    def test_proxy_metadata_and_response_envelopes_preserve_only_final_text(self):
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "private reasoning", "thought": True}, {"text": '{"ok":true}'}]},
            "newProviderMetadata": 1}], "text": "convenience text", "usage": {"cost": 0}}
        for payload in (response, {"ok": True, "response": response, "cost": 0},
                        {"result": response}, {"data": response}):
            self.assertEqual(parse_gemini_response(payload).text, '{"ok":true}')
        self.assertNotIn("private reasoning", json.dumps(response_structure(response)))
        self.assertNotIn("convenience text", json.dumps(response_structure(response)))

    def test_dropped_connections_are_resent_but_timeouts_are_not(self):
        ok = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}], "cost_inr": "0.4"}
        session = Mock()
        session.generate.side_effect = [ProxyCallError(category="connection"), ProxyCallError(502), ok]
        job = PWJob(session, "retry-test")
        with patch("lecture_trimmer.processing.job_control.interruptible_wait") as wait:
            self.assertEqual(job.generate(model="m", request={}), ok)
        self.assertEqual([c.args[0] for c in wait.call_args_list], list(PWJob.RETRY_DELAYS))
        self.assertEqual(job.usage.snapshot()["failed_requests"], 2)
        session.generate.reset_mock(side_effect=True)
        session.generate.side_effect = ProxyCallError(category="timeout")
        with patch("lecture_trimmer.processing.job_control.interruptible_wait"):
            with self.assertRaises(ProxyCallError):
                job.generate(model="m", request={})
        self.assertEqual(session.generate.call_count, 1)  # may have run and billed; not resent here

    def test_blocked_incomplete_and_ambiguous_responses_are_not_accepted(self):
        candidate = {"finishReason": "STOP", "content": {"parts": [{"text": "OK"}]}}
        for payload in ({"candidates": [{**candidate, "finishReason": "MAX_TOKENS"}]},
                        {"candidates": [{**candidate, "finishReason": "SAFETY"}]},
                        {"candidates": [candidate], "promptFeedback": {"blockReason": "SAFETY"}},
                        {"candidates": [candidate], "error": "failed"},
                        {"ok": False, "candidates": [candidate]},
                        {"text": "OK"},
                        {"response": {"candidates": [candidate]}, "data": {"candidates": [candidate]}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_gemini_response(payload)

    def test_status_and_category_survive_without_provider_secrets(self):
        response = SimpleNamespace(status_code=500, headers={"x-vercel-id": "bom1::abc-123"},
            text='{"error":"Model not found. SECRET_TOKEN user@example.com https://private.invalid?token=SECRET"}')
        with self.assertRaises(ProxyCallError) as caught:
            raise_safe_proxy_error("gemini", response)
        error = caught.exception
        self.assertEqual(error.code, 500)
        self.assertEqual(error.category, "model_unavailable")
        self.assertEqual(error.request_id, "bom1::abc-123")
        text = json.dumps(error.diagnostic())
        for private in ("SECRET", "user@example", "private.invalid"):
            self.assertNotIn(private, text)
        self.assertIn("AI service is unavailable", public_message(error))
        self.assertEqual(error.diagnostic()["http_status"], 500)

    def test_bad_request_schema_is_distinguished_from_quota_and_credentials(self):
        for message, expected in [
            ('Unknown field responseJsonSchema', 'request_schema'),
            ('API_KEY_INVALID', 'provider_credentials'),
            ('RESOURCE_EXHAUSTED', 'quota'),
        ]:
            with self.subTest(message=message), self.assertRaises(ProxyCallError) as caught:
                raise_safe_proxy_error("gemini", SimpleNamespace(status_code=400, text=message, headers={}))
            self.assertEqual(caught.exception.category, expected)

    def test_failed_basic_check_stops_without_sending_lecture_or_second_request(self):
        job = SimpleNamespace(task_id="lecture-diagnostic-test", filename="", session=Mock())
        job.session.generate.side_effect = ProxyCallError(404, category="model_unavailable")
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "TEMP_DIR", Path(directory)):
            events = list(check_connection_events(job))
            report = json.loads((Path(directory)/"pw_diagnostics"/"lecture-diagnostic-test.json").read_text())
        self.assertIn("HTTP 404", events[-1])
        self.assertEqual(report["checks"][0]["http_status"], 404)
        self.assertEqual(job.session.generate.call_count, 1)
        request = job.session.generate.call_args.kwargs["request"]
        self.assertEqual(request["contents"][0]["parts"][0]["text"], "Reply with OK.")

    def test_both_checks_verify_actual_response_format(self):
        job = PWJob(Mock(), "lecture-diagnostic-test")
        lesson = LessonMap(activities=[TeachingActivity(first_id=0, last_id=0, kind=ActivityKind.THEORY,
                                                        title="Velocity", summary="Definition")])
        def result(text):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}], "role": "model"}}]}
        job.session.generate.side_effect = [result("OK"), result(lesson.model_dump_json())]
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "TEMP_DIR", Path(directory)):
            events = list(check_connection_events(job))
            report = json.loads((Path(directory)/"pw_diagnostics"/"lecture-diagnostic-test.json").read_text())
        self.assertIn("checks passed", events[-1])
        self.assertEqual([item["status"] for item in report["checks"]], ["passed", "passed"])
        self.assertEqual(job.session.generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
