import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import lecture_trimmer.auth.access_control as access


class Clock:
    now = 1000
    def __call__(self):
        return self.now


def settings(**kwargs):
    return access.AuthSettings(base_url="https://lectures.test", client_id="example.apps.googleusercontent.com",
                               client_secret="test-secret", sheet_id="private-sheet", **kwargs)


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.settings = settings()
        self.rows = [["email", "enabled", "expires_at"], ["teacher@example.com", "TRUE", ""]]
        self.allowlist = access.SheetAllowlist(self.settings, lambda: self.rows, self.clock)
        self.service = access.AuthService(self.settings, self.allowlist, self.clock)

    def test_requires_https_and_complete_configuration(self):
        self.settings.validate()
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(access.AccessUnavailable):
            access.AuthSettings.from_env()
        with self.assertRaises(ValueError):
            access.AuthSettings("http://lectures.test", self.settings.client_id, "secret", "sheet").validate()

    def test_sheet_columns_expiry_disabled_and_invalid_rows(self):
        rows = [["enabled", "expires_at", "email"],
                ["TRUE", "2030-01-01T00:00:00Z", " Teacher@Example.com "],
                ["FALSE", "", "disabled@example.com"]]
        entries = access.parse_access_rows(rows, self.settings)
        self.assertTrue(entries["teacher@example.com"][0])
        self.assertFalse(entries["disabled@example.com"][0])
        for bad in ([['email', 'enabled']], self.rows + [["TEACHER@example.com", "FALSE", ""]],
                    [self.rows[0], ["teacher@example.com", "perhaps", ""]],
                    [self.rows[0], ["teacher@example.com", "TRUE", "2030-01-01"]]):
            with self.subTest(bad=bad), self.assertRaises(access.AccessUnavailable):
                access.parse_access_rows(bad, self.settings)

    def test_revocation_cache_is_bounded_and_never_stale_on_failure(self):
        self.assertTrue(self.allowlist.allowed("teacher@example.com"))
        self.rows[1][1] = "FALSE"
        self.assertTrue(self.allowlist.allowed("teacher@example.com"))
        self.clock.now += 61
        self.assertFalse(self.allowlist.allowed("teacher@example.com"))
        self.rows[1][1] = "TRUE"
        self.clock.now += 61
        self.assertTrue(self.allowlist.allowed("teacher@example.com"))
        self.allowlist.reader = lambda: (_ for _ in ()).throw(OSError("network unavailable"))
        self.clock.now += 61
        with self.assertRaises(access.AccessUnavailable):
            self.allowlist.allowed("teacher@example.com")
        self.assertEqual(self.allowlist._entries, {})

    def test_expiry_takes_effect_without_waiting_for_sheet_refresh(self):
        self.rows[1][2] = "1970-01-01T00:16:50+00:00"
        self.assertTrue(self.allowlist.allowed("teacher@example.com"))
        self.clock.now += 11
        self.assertFalse(self.allowlist.allowed("teacher@example.com"))

    def test_login_has_pkce_state_nonce_and_single_use_flow(self):
        cookie, url = self.service.begin_login()
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], ["openid email"])
        flow = self.service.consume_flow(cookie, query["state"][0])
        self.assertEqual(flow["nonce"], query["nonce"][0])
        with self.assertRaises(ValueError):
            self.service.consume_flow(cookie, query["state"][0])
        cookie, url = self.service.begin_login()
        with self.assertRaises(ValueError):
            self.service.consume_flow(cookie, "forged")
        cookie, url = self.service.begin_login()
        self.clock.now += 301
        with self.assertRaises(ValueError):
            self.service.consume_flow(cookie, parse_qs(urlsplit(url).query)["state"][0])

    def test_identity_rejects_forged_claims_and_unapproved_email(self):
        claims = {"iss": "https://accounts.google.com", "aud": self.settings.client_id,
                  "sub": "google-subject", "email_verified": True, "email": "teacher@example.com",
                  "nonce": "nonce", "exp": 2000}
        self.service.token_verifier = lambda token: claims
        identity = self.service.identity_from_token("verified-by-google-library", "nonce")
        self.assertEqual(len(identity.owner_id), 64)
        for field, value in (("iss", "evil.test"), ("aud", "other-client"), ("email_verified", False),
                             ("nonce", "wrong"), ("exp", 999), ("azp", "other-client")):
            with patch.dict(claims, {field: value}), self.assertRaises(ValueError):
                self.service.identity_from_token("token", "nonce")
        with patch.dict(claims, {"email": "stranger@example.com"}), self.assertRaises(PermissionError):
            self.service.identity_from_token("token", "nonce")

    def test_session_revokes_expires_and_logout_invalidates(self):
        identity = access.Identity("owner", "teacher@example.com")
        token = self.service.create_session(identity)
        self.assertEqual(self.service.current_user(token), identity)
        self.rows[1][1] = "FALSE"
        self.clock.now += 61
        self.assertIsNone(self.service.current_user(token))
        self.rows[1][1] = "TRUE"
        self.clock.now += 61
        token = self.service.create_session(identity)
        self.service.logout(token)
        self.assertIsNone(self.service.current_user(token))
        token = self.service.create_session(identity)
        self.clock.now += self.settings.session_seconds + 1
        self.assertIsNone(self.service.current_user(token))


class StudioSignInTests(unittest.TestCase):
    """Google sign-in in front of the studio and its API (shared HTTPS hosting)."""

    def setUp(self):
        from lecture_trimmer.server.api import mount_web, signed_in_resolver
        from lecture_trimmer.server.jobs import JobStore, Scheduler
        import lecture_trimmer.config as config
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.clock = Clock()
        rows = [["email", "enabled", "expires_at"], ["alice@example.com", "TRUE", ""]]
        self.auth = access.AuthService(settings(), access.SheetAllowlist(settings(), lambda: rows, self.clock), self.clock)
        server = access.make_private_server(settings(), service=self.auth)
        scheduler = Scheduler(JobStore(root / "jobs"), runner=lambda *a, **k: {})
        self.addCleanup(scheduler.shutdown)
        with patch.object(config, "DATA_ROOT", root), patch.object(config, "OUTPUT_DIR", root / "output"):
            mount_web(server, scheduler, resolve_user=signed_in_resolver(self.auth),
                      base_url=settings().base_url, mode="google-sheet")
        self.client = TestClient(server, base_url="https://lectures.test")
        self.addCleanup(self.client.close)

    def test_signed_out_people_see_sign_in_and_cannot_use_the_api(self):
        home = self.client.get("/", follow_redirects=False)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Make time for learning", home.text)  # the desktop window waits for this page
        self.assertIn("Lecture Trimmer", home.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)
        self.assertEqual(self.client.get("/app/").status_code, 404)  # the Classic page is gone

    def test_signed_in_people_go_to_the_studio_and_logout_needs_this_site(self):
        self.client.cookies.set(access.COOKIE, self.auth.create_session(access.Identity("alice", "alice@example.com")))
        home = self.client.get("/", follow_redirects=False)
        self.assertEqual((home.status_code, home.headers["location"]), (303, access.STUDIO_HOME))
        me = self.client.get("/api/me")
        self.assertEqual((me.status_code, me.json()["mode"]), (200, "google-sheet"))
        self.assertEqual(self.client.post("/auth/logout", headers={"Origin": "https://evil.test"}).status_code, 403)
        out = self.client.post("/auth/logout", headers={"Origin": settings().base_url}, follow_redirects=False)
        self.assertEqual(out.status_code, 303)
        self.assertEqual(self.client.get("/api/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
