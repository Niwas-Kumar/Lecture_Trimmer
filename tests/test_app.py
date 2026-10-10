"""The server entry point (studio + API) and the plain messages shown for a failed lecture."""
import base64
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import lecture_trimmer.config as config
import lecture_trimmer.ui.app as app
from lecture_trimmer.runtime.messages import public_message
from lecture_trimmer.server.jobs import JobStore, Scheduler


class ServerTests(unittest.TestCase):
    def local_client(self, **auth):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        scheduler = Scheduler(JobStore(root / "jobs"), runner=lambda *a, **k: {})
        self.addCleanup(scheduler.shutdown)
        with patch.object(config, "DATA_ROOT", root), patch.object(config, "OUTPUT_DIR", root / "output"):
            server = app.make_local_server(scheduler, **auth)
        client = TestClient(server)
        self.addCleanup(client.close)
        return client

    def test_local_mode_opens_the_studio_with_its_api(self):
        client = self.local_client()
        home = client.get("/", follow_redirects=False)
        self.assertEqual((home.status_code, home.headers["location"]), (303, "/studio/"))
        studio = client.get("/studio/")
        self.assertEqual(studio.status_code, 200)
        self.assertIn('<div id="root">', studio.text)
        self.assertEqual(client.get("/api/me").json()["mode"], "local")
        self.assertEqual(client.get("/favicon.ico").status_code, 200)
        self.assertEqual(client.get("/app/").status_code, 404)  # the Classic page is gone

    def test_optional_local_password_protects_everything(self):
        client = self.local_client(user="teacher", password="long-private-password")
        for headers in ({}, {"Authorization": "Basic " + base64.b64encode(b"teacher:wrong").decode()},
                        {"Authorization": "Basic not-base64!"}):
            refused = client.get("/api/me", headers=headers)
            self.assertEqual(refused.status_code, 401)
            self.assertIn("Basic", refused.headers["www-authenticate"])
        good = {"Authorization": "Basic " + base64.b64encode(b"teacher:long-private-password").decode()}
        self.assertEqual(client.get("/api/me", headers=good).status_code, 200)

    def test_local_mode_binds_to_this_computer_and_remote_needs_google_sign_in(self):
        with patch.dict(app.os.environ, {"TRIMMER_AUTH": "local", "TRIMMER_USER": "u", "TRIMMER_PASSWORD": "p"}), \
                patch("sys.argv", ["app.py", "--no-browser"]), patch.object(app, "_web_scheduler") as scheduler, \
                patch.object(app, "make_local_server") as server, patch("uvicorn.run") as run:
            app.main()
        server.assert_called_once_with(scheduler.return_value, user="u", password="p")
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        with patch.dict(app.os.environ, {"TRIMMER_AUTH": "local"}), patch("sys.argv", ["app.py", "--host", "0.0.0.0"]), \
                patch.object(app, "make_local_server") as server, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            app.main()
        server.assert_not_called()


class MessageTests(unittest.TestCase):
    def test_study_review_error_explains_next_step_without_private_details(self):
        from lecture_trimmer.analysis.lesson_classifier import StudyReviewRequired
        message = public_message(StudyReviewRequired("private transcript and IDs"))
        self.assertIn("No new videos were exported", message)
        self.assertIn("selected-frame review", message)
        self.assertNotIn("private transcript", message)

    def test_audio_preparation_error_explains_failure_without_raw_log(self):
        from lecture_trimmer.media.audio import AudioExtractionError
        message = public_message(AudioExtractionError("Non-monotonic DTS at private-file.mp4"))
        self.assertIn("could not read this video's audio", message)
        self.assertNotIn("private-file.mp4", message)

    def test_provider_quota_message_is_actionable_without_provider_secrets(self):
        import lecture_trimmer.analysis.classifier as classifier

        class QuotaError(RuntimeError):
            public_message = "Classification provider daily quota is exhausted. Retry after the quota resets."
        with patch.object(classifier, "ProviderQuotaError", QuotaError, create=True):
            message = public_message(QuotaError("provider secret and internal metadata"))
        self.assertEqual(message, QuotaError.public_message)
        message = public_message(RuntimeError("private server path or key"))
        self.assertNotIn("private server path", message)
        self.assertIn("support report", message)

    def test_dropped_ai_connection_says_finished_steps_are_saved(self):
        from lecture_trimmer.integrations.pw_proxy import ProxyCallError
        self.assertIn("finished steps are saved", public_message(ProxyCallError(category="connection")))


if __name__ == "__main__":
    unittest.main()
