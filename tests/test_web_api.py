"""Batch job engine and web API (no AI calls; a fake pipeline exercises the stages)."""
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import lecture_trimmer.config as config
from lecture_trimmer.processing.job_control import check_cancelled
from lecture_trimmer.server.api import mount_web
from lecture_trimmer.server.jobs import JobStore, Scheduler


class FakePipeline:
    """Walks through every stage like process_video and records stage overlap."""

    def __init__(self, video, seconds=0.15, fail=False):
        self.video, self.seconds, self.fail = video, seconds, fail
        self.active = {stage: 0 for stage in ("prepare", "transcribe", "analyse", "render")}
        self.peak = dict(self.active)
        self.lock = threading.Lock()
        self.calls = 0

    def __call__(self, path, *, stage_gate, progress, on_result, owner_id, **options):
        with self.lock:
            self.calls += 1
        for stage in self.active:
            with stage_gate(stage):
                with self.lock:
                    self.active[stage] += 1
                    self.peak[stage] = max(self.peak[stage], self.active[stage])
                try:
                    for _ in range(3):
                        check_cancelled()
                        time.sleep(self.seconds / 3)
                    progress(0.5, f"{stage} half way")
                finally:
                    with self.lock:
                        self.active[stage] -= 1
        if self.fail:
            raise RuntimeError("boom")
        report = {"status": "complete", "duration": 3600, "report_path": None, "source": None,
                  "outputs": {"questions": {"status": "ready", "duration": 60, "path": str(self.video)}},
                  "ai_usage": {"reported_cost_inr": "20.0"}}
        on_result(report)
        return report


def wait_until(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.video = self.root / "lecture.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self):
        self.dir.cleanup()

    def test_lectures_run_as_an_assembly_line_with_one_render_at_a_time(self):
        fake = FakePipeline(self.video)
        scheduler = Scheduler(JobStore(self.root / "jobs"), runner=fake,
                              limits={"prepare": 2, "transcribe": 2, "analyse": 3, "render": 1})
        jobs = [scheduler.submit("me", self.video, f"l{i}.mp4", {}) for i in range(4)]
        self.assertTrue(wait_until(lambda: all(j.status == "complete" for j in jobs)), [j.status for j in jobs])
        self.assertEqual(fake.peak["render"], 1)
        self.assertGreaterEqual(fake.peak["analyse"], 2)  # Analyses overlapped.
        self.assertLessEqual(fake.peak["transcribe"], 2)
        self.assertEqual(jobs[0].cost_inr, "20.0")
        saved = json.loads((self.root / "jobs" / f"{jobs[0].id}.json").read_text())
        self.assertEqual(saved["status"], "complete")
        scheduler.shutdown()

    def test_interrupted_jobs_resume_when_their_owner_returns(self):
        store = JobStore(self.root / "jobs")
        first = Scheduler(store, runner=FakePipeline(self.video, seconds=5))
        job = first.submit("me", self.video, "l.mp4", {})
        self.assertTrue(wait_until(lambda: job.status == "running"))
        first.shutdown()  # App closed mid-lecture.
        fake = FakePipeline(self.video)
        second = Scheduler(JobStore(self.root / "jobs"), runner=fake)
        restored = second.get("me", job.id)
        self.assertEqual(restored.status, "waiting")
        self.assertIsNone(second.get("other", job.id))
        second.attach_owner("me")
        self.assertTrue(wait_until(lambda: restored.status == "complete"))
        second.shutdown()

    def test_cancel_and_retry_and_failure_are_per_lecture(self):
        slow = FakePipeline(self.video, seconds=3)
        scheduler = Scheduler(JobStore(self.root / "jobs"), runner=slow)
        job = scheduler.submit("me", self.video, "l.mp4", {})
        self.assertTrue(wait_until(lambda: job.status == "running"))
        scheduler.cancel("me", job.id)
        self.assertTrue(wait_until(lambda: job.status == "cancelled"))
        scheduler.runner = FakePipeline(self.video, fail=True, seconds=0.01)
        scheduler.retry("me", job.id)
        self.assertTrue(wait_until(lambda: job.status == "failed"))
        self.assertTrue(job.error_reference)
        self.assertTrue(scheduler.remove("me", job.id))
        self.assertEqual(scheduler.list("me"), [])
        scheduler.shutdown()


def make_video(path, seconds=4):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=size=160x90:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.patches = [patch.object(config, "DATA_ROOT", self.root), patch.object(config, "OUTPUT_DIR", self.root)]
        for p in self.patches:
            p.start()
        self.video = self.root / "result.mp4"
        make_video(self.video)
        self.fake = FakePipeline(self.video, seconds=0.03)
        self.scheduler = Scheduler(JobStore(self.root / "jobs"), runner=self.fake)
        self.who = {"owner": "me"}
        server = FastAPI()
        mount_web(server, self.scheduler, resolve_user=lambda request: (self.who["owner"], None),
                  base_url="http://app.test")
        self.client = TestClient(server, base_url="http://app.test")
        self.origin = {"Origin": "http://app.test"}

    def tearDown(self):
        # Let fake lectures finish writing before the temporary folder is removed (Windows locks).
        wait_until(lambda: all(j.status not in {"queued", "waiting", "running"}
                               for j in self.scheduler.list(self.who["owner"] or "me")))
        self.scheduler.shutdown()
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def upload(self, data, name="lecture.mp4"):
        started = self.client.post("/api/uploads", json={"filename": name, "size": len(data)}, headers=self.origin)
        self.assertEqual(started.status_code, 200, started.text)
        upload_id = started.json()["upload_id"]
        half = len(data) // 2
        self.assertEqual(self.client.put(f"/api/uploads/{upload_id}?offset=0", content=data[:half],
                                         headers=self.origin).json()["received"], half)
        # A resumed browser that guesses the wrong offset is told where to continue.
        wrong = self.client.put(f"/api/uploads/{upload_id}?offset=0", content=data[half:], headers=self.origin)
        self.assertEqual((wrong.status_code, wrong.json()["received"]), (409, half))
        self.client.put(f"/api/uploads/{upload_id}?offset={half}", content=data[half:], headers=self.origin)
        done = self.client.post(f"/api/uploads/{upload_id}/complete", headers=self.origin)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertAlmostEqual(done.json()["duration"], 4, delta=0.3)
        return upload_id

    def test_batch_upload_process_stream_and_seek(self):
        data = self.video.read_bytes()
        ids = [self.upload(data, f"l{i}.mp4") for i in range(2)]
        created = self.client.post("/api/jobs", headers=self.origin, json={
            "lectures": [{"upload_id": i, "context": "Physics"} for i in ids], "options": {"outputs": ["questions"]}})
        self.assertEqual(created.status_code, 200, created.text)
        job_ids = [j["id"] for j in created.json()["jobs"]]
        self.assertTrue(wait_until(lambda: all(
            self.client.get(f"/api/jobs/{j}").json()["status"] == "complete" for j in job_ids)))
        listed = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual(len(listed), 2)
        self.assertNotIn("input_path", listed[0])  # No server paths reach the browser.
        media = self.client.get(f"/api/jobs/{job_ids[0]}/media/questions", headers={"Range": "bytes=0-99"})
        self.assertEqual((media.status_code, len(media.content)), (206, 100))
        download = self.client.get(f"/api/jobs/{job_ids[0]}/media/questions?download=1")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertIn("l0_questions.mp4", download.headers["content-disposition"])
        estimate = self.client.get("/api/estimate?seconds=7200").json()
        self.assertEqual(estimate["inr"], 40.0)  # Learned from the two finished lectures (₹20/hour).

    def test_transcript_files_are_listed_before_the_ai_analysis_finishes(self):
        from lecture_trimmer.server.jobs import Job
        folder = self.root / "output" / "early"
        folder.mkdir(parents=True)
        (folder / "analysis.json").write_text(json.dumps({"duration": 5, "outputs": {}, "status": "analysing"}), encoding="utf-8")
        (folder / "transcript.txt").write_text("00:00 hello", encoding="utf-8")
        job = Job(id="e" * 32, owner="me", filename="early.mp4", input_path="", options={}, status="running",
                  stage="analyse", report_path=str(folder / "analysis.json"))
        self.scheduler.jobs[job.id] = job
        review = self.client.get(f"/api/jobs/{job.id}/review").json()
        self.assertEqual((review["sections"], review["files"]), ([], ["analysis.json", "transcript.txt"]))
        self.assertEqual(self.client.get(f"/api/jobs/{job.id}/files/transcript.txt").text, "00:00 hello")

    def test_lectures_from_the_classic_page_are_listed_once_and_stay_removed(self):
        old = self.root / "output" / ("a" * 32)
        old.mkdir(parents=True)
        video = old / "old_questions.mp4"
        video.write_bytes(b"video")
        (old / "analysis.json").write_text(json.dumps({
            "video": "old.mp4", "owner_id": "me", "status": "complete", "duration": 3600, "elapsed": 600,
            "outputs": {"questions": {"status": "ready", "duration": 60, "path": str(video)},
                        "theory": {"status": "ready", "duration": 90, "path": str(old / "deleted.mp4")}},
            "ai_usage": {"reported_cost_inr": "41.5"}}), encoding="utf-8")
        foreign = self.root / "output" / ("b" * 32)
        foreign.mkdir()
        (foreign / "analysis.json").write_text(json.dumps({"video": "x.mp4", "owner_id": "someone-else",
                                                           "outputs": {"questions": {"status": "ready"}}}), encoding="utf-8")
        self.assertEqual(self.scheduler.import_reports(self.root / "output", owner=None), 2)
        jobs = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual([j["filename"] for j in jobs], ["old.mp4"])  # only this person's own lectures
        job = jobs[0]
        self.assertEqual((job["status"], job["cost_inr"], job["outputs"]["theory"]["status"]), ("complete", "41.5", "failed"))
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/media/questions").content, b"video")
        self.assertEqual(self.scheduler.import_reports(self.root / "output"), 0)  # never twice
        self.assertEqual(self.client.delete(f"/api/jobs/{job['id']}", headers=self.origin).status_code, 200)
        self.assertEqual(self.scheduler.import_reports(self.root / "output"), 0)  # removed stays removed
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])

    def test_updates_and_support_report(self):
        status = self.client.get("/api/update").json()
        self.assertEqual((status["installed"], status["pending"]), (False, None))  # tests run from source
        checked = self.client.post("/api/update/check", json={"link": ""}, headers=self.origin).json()
        self.assertIn("runs from source", checked["message"])
        self.assertEqual(self.client.post("/api/update/check", json={}).status_code, 403)  # other sites
        with patch.object(self.scheduler, "busy", return_value=True):
            self.assertIn("Finish or stop", self.client.post("/api/update/restart", headers=self.origin).json()["message"])
            self.assertIn("Finish or stop", self.client.post("/api/update/check", json={}, headers=self.origin).json()["message"])
        report = self.root / "support.zip"
        report.write_bytes(b"PK zip")
        with patch("lecture_trimmer.runtime.diagnostics.export_support", return_value=report) as export:
            saved = self.client.post("/api/support", headers=self.origin)
        self.assertEqual((saved.status_code, saved.content), (200, b"PK zip"))
        export.assert_called_once_with("me")  # only this person's own error records
        self.assertIn("attachment", saved.headers["content-disposition"])

    def test_other_people_and_other_sites_get_nothing(self):
        upload_id = self.upload(self.video.read_bytes())
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "x.mp4", "size": 10}).status_code, 403)
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "x.txt", "size": 10},
                                          headers=self.origin).status_code, 400)
        job = self.client.post("/api/jobs", headers=self.origin,
                               json={"lectures": [{"upload_id": upload_id}]}).json()["jobs"][0]
        self.who["owner"] = "someone-else"
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/media/questions").status_code, 404)
        self.assertEqual(self.client.get(f"/api/uploads/{upload_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        self.who["owner"] = None
        self.assertEqual(self.client.get("/api/jobs").status_code, 401)

    def test_live_events_stream_job_updates(self):
        upload_id = self.upload(self.video.read_bytes())
        self.client.post("/api/jobs", headers=self.origin, json={"lectures": [{"upload_id": upload_id}]})
        with self.client.stream("GET", "/api/events?once=1") as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    jobs = json.loads(line[5:])["jobs"]
                    self.assertEqual(len(jobs), 1)
                    break


if __name__ == "__main__":
    unittest.main()
