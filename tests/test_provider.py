"""Quota failures must stop promptly without downgrading or exposing provider data."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from google import genai
from google.genai import errors

import lecture_trimmer.analysis.classifier as classifier
from lecture_trimmer.models import EpisodeMap


def quota_error(*, quota_id=None, quota_value=None, retry=None, message="private provider details: SECRET"):
    details = []
    if quota_id is not None or quota_value is not None:
        violation = {"quotaId": quota_id or "GenerateRequestsPerMinutePerProject"}
        if quota_value is not None:
            violation["quotaValue"] = quota_value
        details.append({"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [violation]})
    if retry is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                              "message": message, "details": details}})


class ProviderRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.settings = patch.multiple(classifier.config, GEMINI_MODEL="configured-quality-model",
                                       GEMINI_FALLBACK_MODELS=["configured-fallback"], GEMINI_MAX_RETRIES=5)
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def request(self):
        return classifier._request(self.client, "gemini", "test transcript", "test instructions", EpisodeMap)

    def assert_immediate_quota_failure(self, error, reason):
        self.client.models.generate_content.side_effect = error
        with patch.object(classifier, "interruptible_wait") as sleep:
            with self.assertRaises(classifier.ProviderQuotaError) as caught:
                self.request()
        sleep.assert_not_called()
        self.client.models.generate_content.assert_called_once()
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), caught.exception.public_message)
        self.assertNotIn("SECRET", caught.exception.public_message)
        self.assertIn("Saved analysis", caught.exception.public_message)
        return caught.exception

    def test_daily_quota_stops_on_first_response_and_shows_reset_guidance(self):
        error = self.assert_immediate_quota_failure(quota_error(
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier", quota_value="20", retry="64147s"),
            "daily_quota")
        self.assertEqual(error.retry_after, 64147)
        self.assertIn("20 requests", error.public_message)
        self.assertIn("17h 50m", error.public_message)
        self.assertIn("quota resets", error.public_message)

    def test_zero_quota_stops_without_waiting_or_model_rotation(self):
        self.assert_immediate_quota_failure(quota_error(quota_value="0", retry="5s"), "zero_quota")

    def test_long_retry_delay_stops_without_sleep(self):
        self.assert_immediate_quota_failure(quota_error(retry="61.1s"), "retry_later")

    def test_short_provider_delay_is_honoured_and_sdk_retries_are_disabled(self):
        result = EpisodeMap(episodes=[])
        self.client.models.generate_content.side_effect = [quota_error(retry="2.5s"),
                                                          SimpleNamespace(parsed=result)]
        with patch.object(classifier, "interruptible_wait") as sleep:
            self.assertEqual(self.request(), result)
        sleep.assert_called_once_with(2.5)
        calls = self.client.models.generate_content.call_args_list
        self.assertEqual([call.kwargs["model"] for call in calls], ["configured-quality-model"] * 2)
        for call in calls:
            self.assertEqual(call.kwargs["config"].http_options.retry_options.attempts, 1)

    def test_transient_retries_have_a_total_wait_budget_and_never_rotate_models(self):
        self.client.models.generate_content.side_effect = quota_error()
        with patch.object(classifier, "interruptible_wait") as sleep:
            with self.assertRaises(classifier.ProviderQuotaError) as caught:
                self.request()
        waits = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(waits, [5, 10, 20])
        self.assertLessEqual(sum(waits), 60)
        self.assertEqual(caught.exception.reason, "transient_exhausted")
        self.assertTrue(all(call.kwargs["model"] == "configured-quality-model"
                            for call in self.client.models.generate_content.call_args_list))

    def test_transport_without_structured_details_still_stops_daily_quota(self):
        error = RuntimeError("429 SECRET RequestsPerDay quotaValue: 20")
        error.code = 429
        self.assert_immediate_quota_failure(error, "daily_quota")

    def test_google_duration_object_and_retry_header(self):
        error = quota_error(retry={"seconds": "2", "nanos": 500000000})
        error.response = SimpleNamespace(headers={"Retry-After": "4"})
        self.assertEqual(classifier._quota_metadata(error)[2], 4)
        self.assertEqual(classifier._retry_delay_seconds({"seconds": "2", "nanos": 500000000}), 2.5)
        for invalid in ["nan", "-5s", "infinite", {"seconds": "bad"}]:
            self.assertIsNone(classifier._retry_delay_seconds(invalid))

    def test_configured_not_found_fallback_is_preserved(self):
        result = EpisodeMap(episodes=[])
        self.client.models.generate_content.side_effect = [errors.ClientError(404, {"error": {"message": "not found"}}),
                                                          SimpleNamespace(parsed=result)]
        with patch.object(classifier, "interruptible_wait") as sleep:
            self.assertEqual(self.request(), result)
        sleep.assert_not_called()
        self.assertEqual([call.kwargs["model"] for call in self.client.models.generate_content.call_args_list],
                         ["configured-quality-model", "configured-fallback"])

    def test_real_sdk_offline_transport_makes_exactly_one_daily_quota_request(self):
        requests = []
        error_body = quota_error(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                 quota_value="20", retry="64147s").details
        def respond(request):
            requests.append(request)
            return httpx.Response(429, json=error_body)

        # All HTTP is intercepted locally. This exercises actual SDK option
        # construction and its request/retry path without contacting any service.
        client = genai.Client(api_key="offline-test-key", vertexai=False, http_options={"timeout": 1000,
            "client_args": {"transport": httpx.MockTransport(respond)}})
        try:
            with patch.object(classifier, "interruptible_wait") as sleep:
                with self.assertRaises(classifier.ProviderQuotaError):
                    classifier._request(client, "gemini", "test", "test", EpisodeMap)
            sleep.assert_not_called()
            self.assertEqual(len(requests), 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
