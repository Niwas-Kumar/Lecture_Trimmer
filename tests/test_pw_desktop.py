"""Desktop OAuth and PW transport boundaries; no live identities or model calls."""
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from google.genai import types
from pydantic import BaseModel

import lecture_trimmer.auth.access_control as access
import lecture_trimmer.ui.app as app
import lecture_trimmer.analysis.classifier as classifier
import lecture_trimmer.config as config
import lecture_trimmer.analysis.lesson_classifier as lesson_classifier
from lecture_trimmer.models import ActivityEdit, ActivityKind, ActivityRole, LessonEdits, LessonMap, TeachingActivity, Unit
from lecture_trimmer.auth.pw_desktop_auth import DesktopAuthService, DesktopSettings, make_desktop_server
from lecture_trimmer.integrations.pw_proxy import PWSession, ProxyCallError, load_kit


class Clock:
    now = 1000
    def __call__(self):
        return self.now


class Kit:
    def __init__(self):
        self._session = {"token": "", "expiry": 0}
        self.status = "allowed"
        self.calls = []

    def check_allowed_status(self, token, app):
        self.calls.append(("access", token, app))
        return self.status

    def new_task_id(self, prefix):
        import uuid
        return prefix + "-" + uuid.uuid4().hex

    def gemini_generate(self, token, **kwargs):
        self.calls.append(("generate", token, kwargs))
        return {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": '{"ok":true}'}], "role": "model"}}]}


def settings():
    return DesktopSettings("http://127.0.0.1:7861", "desktop.apps.googleusercontent.com", "test-secret")


class DesktopAuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.kits = []
        self.claims = {}
        def factory(token, **kwargs):
            kit = Kit()
            self.kits.append(kit)
            return PWSession(token, kit=kit, **kwargs)
        self.auth = DesktopAuthService(settings(), clock=self.clock,
            token_verifier=lambda token: self.claims[token], proxy_factory=factory)

    def login(self, name="alice", *, email=None, overrides=None):
        flow_cookie, url = self.auth.begin_login()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["scope"], ["openid email"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        flow = self.auth.consume_flow(flow_cookie, query["state"][0])
        self.claims[name] = {"iss": "https://accounts.google.com", "aud": settings().client_id,
            "email_verified": True, "sub": name, "email": email or name + "@pw.live",
            "nonce": flow["nonce"], "exp": 5000, **(overrides or {})}
        with patch.object(self.auth, "exchange_tokens", return_value={"id_token": name}):
            return self.auth.complete_login("code", flow)

    def test_file_type_and_loopback_are_strict(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            path = Path(directory) / "client_secret_test.json"
            path.write_text(json.dumps({"web": {"client_id": settings().client_id, "client_secret": "secret"}}))
            with self.assertRaises(ValueError):
                DesktopSettings.from_file(directory, 7861)
            path.write_text(json.dumps({"installed": {"client_id": settings().client_id, "client_secret": "secret"}}))
            self.assertEqual(DesktopSettings.from_file(directory, 7861).base_url, settings().base_url)
            for origin in ("http://localhost:7861", "http://0.0.0.0:7861", "https://example.com", "http://127.0.0.1:7861/other"):
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    DesktopSettings(origin, settings().client_id, "secret").validate()

    def test_google_identity_checks_precede_proxy_access(self):
        for overrides in ({"aud": "another"}, {"nonce": "wrong"}, {"exp": 100},
                          {"email_verified": False}, {"iss": "https://fake.test"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.login(overrides=overrides)
        with self.assertRaises(PermissionError):
            self.login(email="alice@gmail.com")
        self.assertEqual(self.kits, [])

    def test_separate_accounts_logout_expiry_and_revocation(self):
        alice, bob = self.login("alice"), self.login("bob")
        alice_job, bob_job = self.auth.job_session(alice), self.auth.job_session(bob)
        self.assertIsNot(alice_job.session, bob_job.session)
        self.assertNotEqual(alice_job.task_id, self.auth.job_session(alice).task_id)
        self.auth.logout(alice)
        self.assertIsNone(self.auth.current_user(alice))
        with self.assertRaises(PermissionError):
            alice_job.ensure_allowed()
        self.assertEqual(self.auth.current_user(bob).email, "bob@pw.live")
        self.kits[1].status = "denied"
        self.clock.now += 61
        self.assertIsNone(self.auth.current_user(bob))
        third = self.login("third")
        self.clock.now += settings().session_seconds
        self.assertIsNone(self.auth.current_user(third))

    def test_proxy_outage_fails_closed_and_job_checks_without_waiting_for_cache(self):
        cookie = self.login()
        self.kits[0].status = "error"
        with self.assertRaises(access.AccessUnavailable):
            self.auth.job_session(cookie)
        self.clock.now += 61
        with self.assertRaises(access.AccessUnavailable):
            self.auth.current_user(cookie)

    def desktop_server(self, work):
        from lecture_trimmer.server.api import mount_web, signed_in_resolver
        from lecture_trimmer.server.jobs import JobStore, Scheduler
        server = make_desktop_server(settings(), service=self.auth)
        scheduler = Scheduler(JobStore(Path(work) / "jobs"), runner=lambda *a, **k: {})
        self.addCleanup(scheduler.shutdown)
        with patch.object(config, "DATA_ROOT", Path(work)), patch.object(config, "OUTPUT_DIR", Path(work) / "output"):
            mount_web(server, scheduler, resolve_user=signed_in_resolver(self.auth), base_url=settings().base_url,
                      mode="pw-desktop")
        return server

    def test_studio_and_api_are_served_behind_desktop_sign_in(self):
        with tempfile.TemporaryDirectory() as work:
            server = self.desktop_server(work)
            with TestClient(server, base_url=settings().base_url) as client:
                self.assertEqual(client.get("/api/me").status_code, 401)
                client.cookies.set(self.auth.cookie_name, self.login("studio"))
                home = client.get("/", follow_redirects=False)
                self.assertEqual(home.headers["location"], "/studio/")
                page = client.get("/studio/")
                self.assertEqual(page.status_code, 200)
                self.assertIn('<div id="root">', page.text)
                me = client.get("/api/me")
                self.assertEqual((me.status_code, me.json()["mode"]), (200, "pw-desktop"))
                # Cross-site requests cannot start uploads with the user's cookie.
                self.assertEqual(client.post("/api/uploads", json={"filename": "a.mp4", "size": 5}).status_code, 403)
                self.assertEqual(client.post("/api/uploads", json={"filename": "a.mp4", "size": 5},
                                             headers={"Origin": settings().base_url}).status_code, 200)

    def test_real_routes_auth_callback_and_browser_csrf(self):
        with tempfile.TemporaryDirectory() as work:
            server = self.desktop_server(work)
            with TestClient(server, base_url=settings().base_url) as client:
                self.assertEqual(client.get("/api/me").status_code, 401)
                home = client.get("/")
                self.assertIn("default browser", home.text)
                self.assertIn("Lecture Trimmer", home.text)  # the desktop window waits for this page
                with patch("lecture_trimmer.auth.pw_desktop_auth.webbrowser.open", return_value=True) as open_browser:
                    self.assertEqual(client.post("/auth/open-browser").status_code, 403)
                    open_browser.assert_not_called()
                    self.assertEqual(client.post("/auth/open-browser", headers={"Origin": settings().base_url}).status_code, 200)
                    open_browser.assert_called_once_with(settings().base_url + "/auth/login")
                login = client.get("/auth/login", follow_redirects=False)
                query = parse_qs(urlsplit(login.headers["location"]).query)
                self.assertIn("HttpOnly", login.headers["set-cookie"])
                self.assertIn("SameSite=lax", login.headers["set-cookie"])
                self.claims["callback"] = {"iss": "https://accounts.google.com", "aud": settings().client_id,
                    "email_verified": True, "sub": "callback", "email": "callback@pw.live",
                    "nonce": query["nonce"][0], "exp": 5000}
                with patch.object(self.auth, "exchange_tokens", return_value={"id_token": "callback"}):
                    callback = client.get("/auth/callback", params={"state": query["state"][0], "code": "test-code"}, follow_redirects=False)
                self.assertEqual((callback.status_code, callback.headers["location"]), (303, "/studio/"))
                self.assertNotIn("callback@pw.live", callback.headers["set-cookie"])
                self.assertEqual(client.get("/api/me").status_code, 200)
                old_cookie = client.cookies.get(self.auth.cookie_name)
                self.assertEqual(client.get("/api/me", headers={"Host": "evil.test"}).status_code, 400)
                self.assertEqual(client.post("/auth/logout").status_code, 403)
                self.assertEqual(client.post("/auth/logout", headers={"Origin": settings().base_url}, follow_redirects=False).status_code, 303)
                self.assertEqual(client.get("/api/me").status_code, 401)
                # The OAuth state is consumed once, even with the earlier session.
                client.cookies.set(self.auth.cookie_name, old_cookie)
                self.assertEqual(client.get("/auth/callback", params={"state": query["state"][0], "code": "test-code"}).status_code, 401)

    def test_remote_bind_rejected_before_startup(self):
        with patch("sys.argv", ["app.py", "--auth", "pw-desktop", "--host", "0.0.0.0"]), \
                patch("lecture_trimmer.auth.pw_desktop_auth.make_desktop_server") as build, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.main()
        build.assert_not_called()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.kit = Kit()
        self.session = PWSession("test-google-token", kit=self.kit, expires_at=9999, clock=Clock())
        self.job = self.session.job()
        self.job.filename = "test-lecture.mp4"

    def test_supplied_adapter_cache_is_isolated_without_modifying_it(self):
        first, second = load_kit(), load_kit()
        first._session["token"] = "first-account"
        self.assertEqual(second._session["token"], "")
        self.assertEqual(first.APP_NAME, "Lecture_Trimmer")

    def test_text_and_images_use_same_task_schema_and_proxy(self):
        class Result(BaseModel):
            ok: bool
        client = self.job.client("gemini")
        for prompt in ("test", ["board", types.Part.from_bytes(data=b"image", mime_type="image/jpeg")]):
            result = classifier._request(client, "gemini", prompt, "system", Result)
            self.assertTrue(result.ok)
        calls = [call[2] for call in self.kit.calls if call[0] == "generate"]
        self.assertEqual({call["task_id"] for call in calls}, {self.job.task_id})
        self.assertTrue(all(call["filename"] == "test-lecture.mp4" and call["count"] == 1 for call in calls))
        schema = calls[0]["request"]["generationConfig"]["responseSchema"]
        self.assertEqual(schema["properties"]["ok"]["type"].upper(), "BOOLEAN")
        self.assertNotIn("responseJsonSchema", calls[0]["request"]["generationConfig"])
        self.assertEqual(base64.b64decode(calls[1]["request"]["contents"][0]["parts"][1]["inlineData"]["data"]), b"image")

    def test_proxy_errors_are_redacted_and_no_key_fallback_occurs(self):
        with patch.object(self.kit, "gemini_generate", side_effect=Exception("gemini proxy error 403: SECRET_TOKEN private")), \
                self.assertRaises(ProxyCallError) as raised:
            self.session.generate(model="test", request={}, task_id="task", filename="file", count=1)
        self.assertEqual(raised.exception.code, 403)
        self.assertNotIn("SECRET", str(raised.exception))
        with patch.object(config, "ANTHROPIC_API_KEY", "local-key"):
            self.assertEqual(classifier.resolve_backend("auto", self.job), "gemini")
        with self.assertRaises(ValueError):
            classifier.resolve_backend("claude", self.job)

    def test_complete_classification_uses_proxy_client_without_google_key(self):
        source = [Unit(id=i, start=i*10, end=(i+1)*10, text="Theory explanation") for i in range(3)]
        mapped = LessonMap(activities=[TeachingActivity(first_id=0, last_id=2, kind=ActivityKind.THEORY, role=ActivityRole.CONCEPT,
            title="Physics", summary="Complete concept explanation")])
        edited = LessonEdits(activities=[ActivityEdit(activity_id=0, cuts=[], essential_ids=[0, 1, 2],
                                                     review_required=False, review_reason="")])
        def respond(token, **kwargs):
            self.kit.calls.append(("generate", token, kwargs))
            schema = kwargs["request"]["generationConfig"]["responseSchema"]
            fields = schema["properties"]["activities"]["items"]["properties"]
            result = mapped if "first_id" in fields else edited
            return {"candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"text": result.model_dump_json()}], "role": "model"}}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(config, "TEMP_DIR", Path(directory)), \
                patch.object(config, "GOOGLE_API_KEY", ""), patch.object(self.kit, "gemini_generate", side_effect=respond), \
                patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Direct Google network calls are forbidden")):
            labels = lesson_classifier.classify_activities(source, use_cache=False, provider_session=self.job)
        self.assertEqual(len(labels), 3)
        self.assertTrue(all(label.keep for label in labels.values()))
        self.assertGreaterEqual(len([call for call in self.kit.calls if call[0] == "generate"]), 2)


if __name__ == "__main__":
    unittest.main()
