"""Lectures added from a pasted video link (no real network: a local test server)."""
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import lecture_trimmer.config as config
from lecture_trimmer.server import links
from lecture_trimmer.server.api import mount_web
from lecture_trimmer.server.jobs import JobStore, Scheduler
from tests.test_web_api import FakePipeline, make_video, wait_until


class VideoServer(BaseHTTPRequestHandler):
    """/video.mp4 resumable · /plain.mp4 no ranges · /flaky.mp4 drops each piece once ·
    /slow.mp4 trickles · /page HTML · /forbidden 403 · /moved redirects to /video.mp4"""
    data = b""
    dropped = set()
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        VideoServer.requests.append((path, self.headers.get("Range")))
        if path == "/page":
            return self._reply(200, b"<html>login</html>", "text/html")
        if path == "/forbidden":
            return self._reply(403, b"<Error>AccessDenied</Error>", "application/xml")
        if path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/video.mp4?Signature=SECRET")
            self.end_headers()
            return
        data, ranged = self.data, path != "/plain.mp4"
        header = self.headers.get("Range")
        if not (ranged and header):
            return self._reply(200, data, "video/mp4")
        start, end = header.removeprefix("bytes=").split("-")
        start, end = int(start), min(int(end or len(data) - 1), len(data) - 1)
        body = data[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if path == "/flaky.mp4" and start > 0 and start not in VideoServer.dropped:
            VideoServer.dropped.add(start)
            self.wfile.write(body[: len(body) // 2])  # connection drops half way through the piece
            self.close_connection = True
            return
        if path == "/slow.mp4":
            for i in range(0, len(body), 512):
                self.wfile.write(body[i:i + 512])
                time.sleep(0.05)
            return
        self.wfile.write(body)

    def _reply(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = tempfile.TemporaryDirectory()
        video = Path(cls.files.name) / "source.mp4"
        make_video(video, seconds=6)
        VideoServer.data = video.read_bytes()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), VideoServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.files.cleanup()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        # The test server runs on this computer, which real links may never reach.
        self.patches = [patch.object(config, "DATA_ROOT", self.root), patch.object(config, "OUTPUT_DIR", self.root),
                        patch.object(links, "check_public", lambda url: None),
                        patch.object(links, "PIECE_BYTES", 8 * 1024)]
        for p in self.patches:
            p.start()
        VideoServer.requests, VideoServer.dropped = [], set()
        self.scheduler = Scheduler(JobStore(self.root / "jobs"), runner=FakePipeline(Path(self.files.name) / "source.mp4", 0.01))
        server = FastAPI()
        mount_web(server, self.scheduler, resolve_user=lambda request: ("me", None), base_url="http://app.test")
        self.client = TestClient(server, base_url="http://app.test")
        self.origin = {"Origin": "http://app.test"}

    def tearDown(self):
        wait_until(lambda: all(j.status not in {"queued", "waiting", "running"} for j in self.scheduler.list("me")))
        self.scheduler.shutdown()
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def add(self, url):
        return self.client.post("/api/links", json={"url": url}, headers=self.origin)

    def finished(self, upload_id):
        status = {}
        def done():
            status.update(self.client.get(f"/api/uploads/{upload_id}").json())
            return status.get("complete") or status.get("error")
        self.assertTrue(wait_until(done, timeout=30), status)
        return status

    def test_secure_link_downloads_in_pieces_keeps_no_signature_and_starts_a_lecture(self):
        added = self.add(self.base + "/video.mp4?Expires=1999999999&Signature=SECRET&Key-Pair-Id=K")
        self.assertEqual(added.status_code, 200, added.text)
        body = added.json()
        self.assertEqual((body["filename"], body["size"]), ("video.mp4", len(VideoServer.data)))
        status = self.finished(body["upload_id"])
        self.assertTrue(status["complete"], status)
        self.assertAlmostEqual(status["duration"], 6, delta=0.4)
        saved = (self.root / "uploads" / body["upload_id"] / "video.mp4").read_bytes()
        self.assertEqual(saved, VideoServer.data)
        meta = (self.root / "uploads" / body["upload_id"] / "upload.json").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", meta)
        self.assertIn('"source": "http://127.0.0.1', meta)
        self.assertGreater(len([r for r in VideoServer.requests if r[1] and r[1] != "bytes=0-0"]), 2)
        job = self.client.post("/api/jobs", headers=self.origin, json={
            "lectures": [{"upload_id": body["upload_id"], "context": "Biology"}], "options": {"outputs": ["questions"]}})
        self.assertEqual(job.status_code, 200, job.text)
        self.assertEqual(job.json()["jobs"][0]["filename"], "video.mp4")

    def test_dropped_connections_resume_inside_the_piece(self):
        status = self.finished(self.add(self.base + "/flaky.mp4").json()["upload_id"])
        self.assertTrue(status["complete"], status)
        self.assertTrue(VideoServer.dropped)
        upload = next((self.root / "uploads").iterdir())
        self.assertEqual((upload / "flaky.mp4").read_bytes(), VideoServer.data)

    def test_servers_without_byte_ranges_and_redirects_still_work(self):
        for path, name in (("/plain.mp4", "plain.mp4"), ("/moved", "video.mp4")):
            added = self.add(self.base + path)
            self.assertEqual(added.status_code, 200, added.text)
            self.assertEqual(added.json()["filename"], name)
            self.assertTrue(self.finished(added.json()["upload_id"])["complete"])

    def test_pages_refusals_and_private_addresses_are_explained(self):
        page = self.add(self.base + "/page")
        self.assertEqual(page.status_code, 400)
        self.assertIn("web page, not a video", page.json()["detail"])
        refused = self.add(self.base + "/forbidden")
        self.assertIn("Secure links expire", refused.json()["detail"])
        self.assertEqual(self.add("not a link").status_code, 400)
        # Nothing is saved for a link that was refused.
        self.assertFalse((self.root / "uploads").exists() and any((self.root / "uploads").iterdir()))

    def test_removing_a_link_stops_the_download_and_deletes_it(self):
        upload_id = self.add(self.base + "/slow.mp4").json()["upload_id"]
        self.assertTrue(wait_until(lambda: self.client.get(f"/api/uploads/{upload_id}").json()["received"] > 0))
        self.assertEqual(self.client.delete(f"/api/uploads/{upload_id}", headers=self.origin).status_code, 200)
        self.assertTrue(wait_until(lambda: not (self.root / "uploads" / upload_id).exists(), timeout=15))

    def test_link_uploads_cannot_be_overwritten_by_browser_chunks(self):
        upload_id = self.add(self.base + "/video.mp4").json()["upload_id"]
        self.finished(upload_id)
        self.assertEqual(self.client.put(f"/api/uploads/{upload_id}?offset=0", content=b"x", headers=self.origin).status_code, 409)


class PublicAddressTests(unittest.TestCase):
    def test_only_public_internet_addresses_are_fetched(self):
        for url in ("http://127.0.0.1/a.mp4", "http://localhost/a.mp4", "http://10.0.0.5/a.mp4",
                    "http://192.168.1.10/a.mp4", "http://169.254.169.254/latest/meta-data", "http://[::1]/a.mp4"):
            with self.assertRaises(links.LinkError, msg=url):
                links.check_public(url)
        with self.assertRaises(links.LinkError):
            links.check_public("ftp://example.com/a.mp4")
        with self.assertRaises(links.LinkError):
            links.check_public("https://user:pass@example.com/a.mp4")

    def test_redacted_link_never_contains_the_signature(self):
        url = "https://d1.cloudfront.net/v/lecture_720p.mp4?Expires=1&Signature=abc&Key-Pair-Id=K#t=5"
        self.assertEqual(links.redacted(url), "https://d1.cloudfront.net/v/lecture_720p.mp4")


if __name__ == "__main__":
    unittest.main()
