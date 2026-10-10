"""Persistent lecture queue with an assembly-line scheduler.

Each lecture runs in its own thread, but a lecture enters a stage only when that
stage has a free slot. So lecture B is transcribed and analysed while lecture A
renders, without overloading the GPU or the AI provider. Jobs are saved to disk
on every change: a refresh, a closed browser or an app restart never loses one,
and an interrupted job resumes from its saved work (snapshot, transcript, AI
replies and rendered sections are all cached by the pipeline).
"""
import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import lecture_trimmer.config as config
from lecture_trimmer.processing.job_control import ProcessingCancelled, cancellation_scope, check_cancelled

logger = logging.getLogger("trimmer")

ACTIVE = {"queued", "waiting", "running"}
FINISHED = {"complete", "partial", "failed", "cancelled"}
STAGES = ("prepare", "transcribe", "analyse", "render")
STAGE_NAMES = {"prepare": "Preparing", "transcribe": "Transcribing", "analyse": "AI analysis", "render": "Rendering"}


def stage_limits():
    return {"prepare": 2, "transcribe": 2, "analyse": config.PARALLEL_ANALYSES, "render": 1}


@dataclass
class Job:
    id: str
    owner: str
    filename: str
    input_path: str
    options: dict
    status: str = "queued"          # queued | waiting | running | complete | partial | failed | cancelled
    stage: str = ""                 # one of STAGES while running
    fraction: float = 0.0
    message: str = "Waiting in the queue"
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    report_path: str | None = None
    outputs: dict = field(default_factory=dict)
    cost_inr: str | None = None
    duration: float | None = None
    error: str | None = None
    error_reference: str | None = None
    batch: str | None = None

    def public(self):
        """What the owner's browser may see: no server paths."""
        data = asdict(self)
        for key in ("input_path", "report_path", "owner"):
            data.pop(key, None)
        data["outputs"] = {k: {x: v[x] for x in ("status", "duration") if x in v} for k, v in self.outputs.items()}
        return data


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, job: Job):
        path = self.root / f"{job.id}.json"
        staging = path.with_suffix(f".{uuid.uuid4().hex}.partial")
        staging.write_text(json.dumps(asdict(job), ensure_ascii=False), encoding="utf-8")
        staging.replace(path)

    def load_all(self):
        jobs = {}
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                jobs[data["id"]] = Job(**data)
            except (ValueError, KeyError, TypeError, OSError):
                logger.warning("Ignoring unreadable job record %s", path.name)
        return jobs

    def delete(self, job_id):
        (self.root / f"{job_id}.json").unlink(missing_ok=True)


class Scheduler:
    """Owns every job's lifecycle. Thread-safe; the API only calls its methods."""

    def __init__(self, store: JobStore, *, runner=None, limits=None, max_jobs=8):
        from lecture_trimmer.processing.pipeline import process_video
        self.store = store
        self.runner = runner or process_video
        self.limits = limits or stage_limits()
        self.gates = {name: threading.BoundedSemaphore(n) for name, n in self.limits.items()}
        self.pool = ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="lecture-job")
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.cancel_events = {}
        self.session_factories = {}
        self._last_saved = {}
        self.jobs = store.load_all()
        for job in self.jobs.values():
            if job.status in ACTIVE:
                # Interrupted by an app restart: resume when its owner can be served.
                job.status, job.stage, job.message = "waiting", "", "Resumes when you open the app"
                store.save(job)

    # ---- owner sessions -------------------------------------------------
    def attach_owner(self, owner, session_factory=None):
        """Called on each signed-in request; resumes that owner's interrupted jobs."""
        with self.lock:
            self.session_factories[owner] = session_factory
            waiting = [j for j in self.jobs.values() if j.owner == owner and j.status == "waiting"
                       and j.id not in self.cancel_events]
        for job in waiting:
            self._start(job)

    # ---- queries --------------------------------------------------------
    def list(self, owner):
        with self.lock:
            return sorted((j for j in self.jobs.values() if j.owner == owner), key=lambda j: j.created, reverse=True)

    def get(self, owner, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return job if job and job.owner == owner else None

    def wait_for_change(self, version, timeout=15.0):
        with self.changed:
            if self.version == version:
                self.changed.wait(timeout)
            return self.version

    # ---- commands -------------------------------------------------------
    def submit(self, owner, input_path, filename, options, batch=None):
        job = Job(id=uuid.uuid4().hex, owner=owner, filename=filename, input_path=str(input_path),
                  options=dict(options), batch=batch)
        with self.lock:
            self.jobs[job.id] = job
            self._touch(job, force=True)
        self._start(job)
        return job

    def cancel(self, owner, job_id):
        with self.lock:
            job = self.get(owner, job_id)
            if not job:
                return None
            event = self.cancel_events.get(job_id)
            if event:
                event.set()
                job.message = "Stopping safely…"
            elif job.status in ACTIVE:
                job.status, job.message, job.finished = "cancelled", "Stopped", time.time()
            self._touch(job, force=True)
            return job

    def retry(self, owner, job_id):
        with self.lock:
            job = self.get(owner, job_id)
            if not job or job.status not in FINISHED or not Path(job.input_path).is_file():
                return None
            job.status, job.stage, job.fraction, job.message = "queued", "", 0.0, "Waiting in the queue"
            job.error = job.error_reference = None
            job.finished = None
            self._touch(job, force=True)
        self._start(job)
        return job

    def remove(self, owner, job_id):
        with self.lock:
            job = self.get(owner, job_id)
            if not job or job.status in ACTIVE:
                return False
            del self.jobs[job_id]
            self.store.delete(job_id)
            self._touch(None)
            return True

    # ---- internals ------------------------------------------------------
    def _touch(self, job, force=False):
        """Notify listeners; persist at most every 2 s per job unless forced."""
        self.version += 1
        self.changed.notify_all()
        if job is None:
            return
        now = time.monotonic()
        if force or now - self._last_saved.get(job.id, 0) >= 2:
            self._last_saved[job.id] = now
            self.store.save(job)

    def _start(self, job):
        with self.lock:
            if job.id in self.cancel_events:
                return
            event = threading.Event()
            self.cancel_events[job.id] = event
            job.status, job.message = "queued", "Waiting in the queue"
            self._touch(job, force=True)
        self.pool.submit(self._run, job, event)

    @contextmanager
    def _stage(self, job, name):
        gate = self.gates[name]
        with self.lock:
            job.stage, job.status = name, "queued"
            job.message = f"Waiting for a free {STAGE_NAMES[name].lower()} slot"
            self._touch(job)
        while not gate.acquire(timeout=1):
            check_cancelled()
        try:
            with self.lock:
                job.status, job.message = "running", STAGE_NAMES[name] + "…"
                self._touch(job, force=True)
            yield
        finally:
            gate.release()

    def _run(self, job, event):
        try:
            with cancellation_scope(event):
                with self.lock:
                    job.started = job.started or time.time()
                    factory = self.session_factories.get(job.owner)
                provider_session = factory() if factory else None
                if provider_session is not None:
                    provider_session.ensure_allowed()
                    provider_session.filename = job.filename

                def progress(fraction, desc):
                    with self.lock:
                        if fraction is not None:
                            job.fraction = max(0.0, min(1.0, float(fraction)))
                        job.message = desc
                        self._touch(job)

                def on_result(report):
                    with self.lock:
                        self._absorb(job, report)
                        self._touch(job, force=True)

                options = job.options
                from desktop_runtime.desktop_update import processing_guard
                with processing_guard():
                    result = self.runner(
                        Path(job.input_path), outputs=options.get("outputs"), mode=options.get("mode", "balanced"),
                        context=options.get("context", ""), language=options.get("language") or None,
                        cut_style=options.get("cut_style", "smooth"), fresh=bool(options.get("fresh")),
                        dry_run=bool(options.get("dry_run")), visual_review=bool(options.get("visual_review")),
                        progress=progress, on_result=on_result, owner_id=job.owner,
                        stage_gate=lambda name: self._stage(job, name),
                        **({"provider_session": provider_session} if provider_session is not None else {}))
                with self.lock:
                    self._absorb(job, result)
                    job.status = result.get("status") if result.get("status") in FINISHED else "complete"
                    job.message = {"complete": "Done", "partial": "Some videos failed",
                                   "failed": "Failed"}.get(job.status, "Done")
        except ProcessingCancelled:
            with self.lock:
                job.status, job.message = "cancelled", "Stopped. Retry to resume from saved work."
        except Exception as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = getattr(exc, "diagnostic_reference", None) or record_error(exc, stage="job", owner=job.owner)
            from lecture_trimmer.runtime.messages import public_message
            with self.lock:
                job.status, job.error_reference = "failed", reference
                job.error = public_message(exc)
                job.message = "Failed"
        finally:
            with self.lock:
                job.finished = time.time()
                job.stage = ""
                self.cancel_events.pop(job.id, None)
                self._touch(job, force=True)

    def _absorb(self, job, report):
        job.report_path = report.get("report_path") or job.report_path
        job.duration = report.get("duration") or job.duration
        job.outputs = {k: {"status": v.get("status"), "duration": v.get("duration"), "path": v.get("path")}
                       for k, v in report.get("outputs", {}).items()}
        usage = report.get("ai_usage") or {}
        if usage.get("reported_cost_inr") is not None:
            job.cost_inr = usage["reported_cost_inr"]
        source = report.get("source")
        # After the immutable snapshot exists, retries use it and the upload copy is freed.
        if source and Path(source).is_file() and job.input_path != source:
            upload = Path(job.input_path)
            job.input_path = source
            uploads_root = (config.DATA_ROOT / "uploads").resolve()
            if upload.resolve().is_relative_to(uploads_root):
                shutil.rmtree(upload.parent, ignore_errors=True)

    def busy(self):
        with self.lock:
            return any(job.status in ACTIVE for job in self.jobs.values())

    def import_reports(self, output_dir, owner=None):
        """List lectures finished before the queue existed (the old Classic page).

        Each analysis report is imported once; removing an imported lecture keeps it
        removed. `owner` assigns all reports to one person (local mode); otherwise
        only reports that record their owner are imported.
        """
        ledger = self.store.root / "imported-reports.txt"  # not *.json: those are lecture records
        try:
            seen = set(json.loads(ledger.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seen = set()
        with self.lock:
            known = {str(Path(j.report_path).resolve()) for j in self.jobs.values() if j.report_path}
        added = 0
        for path in sorted(Path(output_dir).glob("*/analysis.json"), key=lambda p: p.stat().st_mtime):
            key = str(path.resolve())
            if key in seen or key in known:
                continue
            seen.add(key)
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                job_owner = owner or report.get("owner_id")
                if not job_owner or not report.get("outputs"):
                    continue
                outputs = {}
                for kind, item in (report.get("outputs") or {}).items():
                    status = item.get("status")
                    if status == "ready" and not (item.get("path") and Path(item["path"]).is_file()):
                        status = "failed"  # the video file was deleted since
                    outputs[kind] = {"status": status, "duration": item.get("duration"), "path": item.get("path")}
                usage = report.get("ai_usage") or {}
                finished = path.stat().st_mtime
                status = report.get("status") if report.get("status") in FINISHED else "complete"
                source = report.get("source") or ""
                job = Job(id=uuid.uuid4().hex, owner=job_owner, filename=str(report.get("video") or path.parent.name),
                          input_path=source if source and Path(source).is_file() else "",
                          options={"outputs": list(outputs), "mode": report.get("mode", "balanced"),
                                   "cut_style": report.get("cut_style", "smooth"), "context": report.get("context", "")},
                          status=status, message="Done" if status == "complete" else status.capitalize(),
                          fraction=1.0, created=finished, started=finished - float(report.get("elapsed") or 0),
                          finished=finished, report_path=key, outputs=outputs,
                          cost_inr=usage.get("reported_cost_inr"), duration=report.get("duration"))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            with self.lock:
                self.jobs[job.id] = job
                self._touch(job, force=True)
            added += 1
        staging = ledger.with_suffix(".partial")
        staging.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        staging.replace(ledger)
        if added:
            logger.info("Listed %d lecture(s) processed before the studio queue", added)
        return added

    def shutdown(self):
        with self.lock:
            for event in self.cancel_events.values():
                event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
