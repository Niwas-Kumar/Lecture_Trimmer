"""HTTP API for the web interface: uploads, the lecture queue, live progress and media.

Every route resolves the signed-in owner first and only ever touches that owner's
uploads and jobs. Server paths are never sent to the browser; media is addressed by
job ID and kind. State-changing requests must come from the app's own origin.
"""
import json
import logging
import re
import shutil
import statistics
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

import lecture_trimmer.config as config
from lecture_trimmer.server import links

logger = logging.getLogger("trimmer")
VIDEO_TYPES = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
MAX_UPLOAD_BYTES = 20 * 1024**3
MAX_CHUNK_BYTES = 64 * 1024**2
MEDIA_KINDS = {"questions", "theory", "combined", "source"}
JOB_FILES = {"transcript.txt", "transcript.srt", "transcript.json", "analysis.json", "review.html"}
OUTPUT_KINDS = ("questions", "theory", "combined")


def _safe_name(name):
    stem = re.sub(r"[^\w\-. ()\[\]]+", "_", Path(name or "lecture").name).strip(" .") or "lecture"
    return stem[:150]


class UploadArea:
    """Resumable chunked uploads, one folder per upload, owned by one person."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, upload_id):
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
            raise HTTPException(404, "Upload not found")
        return self.root / upload_id / "upload.json"

    def meta(self, owner, upload_id):
        path = self._meta_path(upload_id)
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(404, "Upload not found") from None
        if meta.get("owner") != owner:
            raise HTTPException(404, "Upload not found")
        return meta

    def save_meta(self, upload_id, meta):
        path = self._meta_path(upload_id)
        staging = path.with_suffix(".partial")
        staging.write_text(json.dumps(meta), encoding="utf-8")
        staging.replace(path)

    def create(self, owner, filename, size):
        name = _safe_name(filename)
        if Path(name).suffix.lower() not in VIDEO_TYPES:
            raise HTTPException(400, "Choose a video file (MP4, MKV, MOV, WebM, AVI or M4V).")
        if not isinstance(size, int) or not 0 < size <= MAX_UPLOAD_BYTES:
            raise HTTPException(400, "The video is empty or larger than 20 GB.")
        free = shutil.disk_usage(self.root).free
        # The upload, its snapshot and render intermediates need about three times its size.
        if free < 3 * size + 2 * 1024**3:
            raise HTTPException(507, f"Not enough free disk space: about {round((3 * size) / 1024**3 + 2, 1)} GB is needed.")
        upload_id = uuid.uuid4().hex
        (self.root / upload_id).mkdir()
        meta = {"owner": owner, "filename": name, "size": size, "received": 0, "complete": False}
        self.save_meta(upload_id, meta)
        (self.root / upload_id / (name + ".part")).touch()
        return upload_id, meta

    def part_path(self, upload_id, meta):
        return self.root / upload_id / (meta["filename"] + ".part")

    def final_path(self, upload_id, meta):
        return self.root / upload_id / meta["filename"]


def create_api(scheduler, *, resolve_user, base_url=None, mode="local", speech="auto"):
    """Build the /api router. `resolve_user(request)` returns (owner, session_factory) or None."""
    router = APIRouter(prefix="/api")
    uploads = UploadArea(config.DATA_ROOT / "uploads")
    downloads = {}                                   # upload_id -> links.Download while saving
    link_slots = threading.BoundedSemaphore(2)       # links saved at once (each in 4 pieces)

    def user(request: Request):
        resolved = resolve_user(request)
        if not resolved or not resolved[0]:
            raise HTTPException(401, "Sign in to continue.")
        owner, factory = resolved
        scheduler.attach_owner(owner, factory)
        return owner

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        if base_url:
            ok = origin == base_url
        else:
            ok = origin is not None and origin.split("://", 1)[-1] == request.headers.get("host")
        if not ok:
            raise HTTPException(403, "Invalid request origin")

    def owned_job(owner, job_id):
        job = scheduler.get(owner, job_id)
        if not job:
            raise HTTPException(404, "Lecture not found")
        return job

    def report_of(job):
        if not job.report_path or not Path(job.report_path).is_file():
            raise HTTPException(404, "The analysis for this lecture is not ready yet.")
        return json.loads(Path(job.report_path).read_text(encoding="utf-8"))

    @router.get("/me")
    def me(request: Request):
        owner = user(request)
        from desktop_runtime.desktop_paths import VERSION
        from lecture_trimmer.analysis.prompts import VERSION as ANALYSIS
        return {"signed_in": True, "local": owner == "local", "mode": mode, "app_version": VERSION,
                "analysis_version": ANALYSIS, "speech": speech, "limits": dict(scheduler.limits),
                "video_types": sorted(VIDEO_TYPES)}

    # ---- uploads -------------------------------------------------------------
    @router.post("/uploads")
    async def start_upload(request: Request):
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        body = await request.json()
        upload_id, meta = uploads.create(owner, body.get("filename"), body.get("size"))
        return {"upload_id": upload_id, "received": 0, "size": meta["size"], "chunk_bytes": 16 * 1024**2}

    @router.get("/uploads/{upload_id}")
    def upload_status(upload_id: str, request: Request):
        meta = uploads.meta(user(request), upload_id)
        active = downloads.get(upload_id)
        return {"upload_id": upload_id, "received": active.received if active else meta["received"],
                "size": meta["size"], "complete": meta["complete"], "filename": meta["filename"],
                "duration": meta.get("duration"), "error": meta.get("error"), "downloading": active is not None}

    @router.put("/uploads/{upload_id}")
    async def upload_chunk(upload_id: str, offset: int, request: Request):
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        meta = uploads.meta(owner, upload_id)
        if meta["complete"] or meta.get("link"):
            raise HTTPException(409, "Upload already finished")
        if offset != meta["received"]:
            # The browser resumes from the server's count after an interruption.
            return JSONResponse({"received": meta["received"]}, status_code=409)
        part = uploads.part_path(upload_id, meta)
        written = 0
        with part.open("r+b") as handle:
            handle.seek(offset)
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_CHUNK_BYTES or offset + written > meta["size"]:
                    handle.truncate(offset)
                    raise HTTPException(413, "Chunk too large")
                await run_in_threadpool(handle.write, chunk)
            handle.truncate(offset + written)
        meta["received"] = offset + written
        uploads.save_meta(upload_id, meta)
        return {"received": meta["received"]}

    @router.post("/uploads/{upload_id}/complete")
    async def finish_upload(upload_id: str, request: Request):
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        meta = uploads.meta(owner, upload_id)
        if meta.get("link"):
            raise HTTPException(409, "This lecture is downloaded from its link by the server.")
        if meta["received"] != meta["size"]:
            raise HTTPException(409, f"Upload incomplete: {meta['received']} of {meta['size']} bytes")
        final = uploads.final_path(upload_id, meta)
        if not meta["complete"]:
            uploads.part_path(upload_id, meta).replace(final)
        from lecture_trimmer.media.audio import probe_duration
        try:
            duration = await run_in_threadpool(probe_duration, final)
        except Exception:
            shutil.rmtree(final.parent, ignore_errors=True)
            raise HTTPException(400, "This file is not a readable video.") from None
        meta.update(complete=True, duration=duration)
        uploads.save_meta(upload_id, meta)
        return {"upload_id": upload_id, "duration": duration, "filename": meta["filename"]}

    @router.delete("/uploads/{upload_id}")
    def discard_upload(upload_id: str, request: Request):
        same_origin(request)
        uploads.meta(user(request), upload_id)
        active = downloads.get(upload_id)
        if active:
            active.cancelled.set()  # the download thread removes its folder once it stops
        else:
            shutil.rmtree(uploads.root / upload_id, ignore_errors=True)
        return {"ok": True}

    # ---- video links -----------------------------------------------------------
    @router.post("/links")
    async def add_link(request: Request):
        """Save a lecture from a pasted video link; it then behaves like an upload."""
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        body = await request.json()
        try:
            url, filename, size, ranged = await run_in_threadpool(links.probe, str(body.get("url") or ""))
        except links.LinkError as exc:
            raise HTTPException(400, str(exc)) from None
        upload_id, meta = uploads.create(owner, filename, size)
        meta.update(link=True, source=links.redacted(url))
        uploads.save_meta(upload_id, meta)
        download = links.Download(url, uploads.part_path(upload_id, meta), size, ranged)
        downloads[upload_id] = download
        threading.Thread(target=fetch_link, args=(owner, upload_id, download), daemon=True,
                         name=f"link-{upload_id[:8]}").start()
        return {"upload_id": upload_id, "filename": meta["filename"], "size": size}

    def fetch_link(owner, upload_id, download):
        from lecture_trimmer.media.audio import probe_duration
        error = None
        try:
            with link_slots:
                download.run()
            meta = uploads.meta(owner, upload_id)
            final = uploads.final_path(upload_id, meta)
            uploads.part_path(upload_id, meta).replace(final)
            try:
                duration = probe_duration(final)
            except Exception:
                raise links.LinkError("The linked file is not a readable video.") from None
            meta.update(complete=True, received=meta["size"], duration=duration)
            uploads.save_meta(upload_id, meta)
        except links.DownloadCancelled:
            pass
        except links.LinkError as exc:
            error = str(exc)
        except Exception as exc:  # never log the exception text: it can contain the signed link
            logger.error("Link download failed: %s", type(exc).__name__)
            error = "The download failed unexpectedly. Try the link again."
        finally:
            downloads.pop(upload_id, None)
        if download.cancelled.is_set():
            shutil.rmtree(uploads.root / upload_id, ignore_errors=True)
        elif error:
            try:
                meta = uploads.meta(owner, upload_id)
                meta.update(error=error, received=download.received)
                uploads.save_meta(upload_id, meta)
            except (HTTPException, OSError):
                pass

    # ---- jobs ----------------------------------------------------------------
    @router.get("/jobs")
    def list_jobs(request: Request):
        owner = user(request)
        return {"jobs": [job.public() for job in scheduler.list(owner)]}

    @router.post("/jobs")
    async def create_jobs(request: Request):
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        body = await request.json()
        options = body.get("options") or {}
        outputs = [k for k in options.get("outputs") or OUTPUT_KINDS if k in OUTPUT_KINDS]
        if not outputs:
            raise HTTPException(400, "Choose at least one video to create.")
        clean = {"outputs": outputs,
                 "mode": options.get("mode") if options.get("mode") in config.MODES else "balanced",
                 "cut_style": options.get("cut_style") if options.get("cut_style") in {"smooth", "fast"} else "smooth",
                 "language": str(options.get("language") or "")[:40],
                 "visual_review": bool(options.get("visual_review")), "fresh": bool(options.get("fresh")),
                 "dry_run": bool(options.get("dry_run"))}
        items = body.get("lectures") or []
        if not items or len(items) > 50:
            raise HTTPException(400, "Add between 1 and 50 lectures.")
        batch = uuid.uuid4().hex
        created = []
        for item in items:
            meta = uploads.meta(owner, item.get("upload_id"))
            if not meta["complete"]:
                raise HTTPException(409, f"{meta['filename']} has not finished uploading.")
            path = uploads.final_path(item["upload_id"], meta)
            job = scheduler.submit(owner, path, meta["filename"],
                                   {**clean, "context": str(item.get("context") or body.get("context") or "")[:200]},
                                   batch=batch)
            created.append(job.public())
        return {"batch": batch, "jobs": created}

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str, request: Request):
        return owned_job(user(request), job_id).public()

    @router.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, request: Request):
        same_origin(request)
        job = scheduler.cancel(user(request), job_id)
        if not job:
            raise HTTPException(404, "Lecture not found")
        return job.public()

    @router.post("/jobs/{job_id}/retry")
    def retry_job(job_id: str, request: Request):
        same_origin(request)
        job = scheduler.retry(user(request), job_id)
        if not job:
            raise HTTPException(409, "Only a finished lecture whose source is still saved can be retried.")
        return job.public()

    @router.delete("/jobs/{job_id}")
    def remove_job(job_id: str, request: Request):
        same_origin(request)
        if not scheduler.remove(user(request), job_id):
            raise HTTPException(409, "Stop the lecture before removing it.")
        return {"ok": True}

    @router.get("/jobs/{job_id}/review")
    def job_review(job_id: str, request: Request):
        job = owned_job(user(request), job_id)
        report = report_of(job)
        from lecture_trimmer.ui.review import build_sections, summary_counts
        sections = build_sections(report) if report.get("activities") else []
        filtering = report.get("filtering", {})
        return {"duration": report.get("duration"), "analysis_version": report.get("classification_version"),
                "mode": report.get("mode"), "subject": report.get("subject"), "sections": sections,
                "totals": summary_counts(report, sections), "warnings": report.get("warnings", []),
                "removed": filtering.get("removed_seconds_by_reason", {}),
                "dead_air": filtering.get("dead_air_shortened_seconds", {}),
                "timings": report.get("timings", {}), "ai_usage": report.get("ai_usage"),
                "files": sorted(name for name in JOB_FILES if (Path(job.report_path).parent / name).is_file())}

    def media_path(job, kind):
        if kind == "source":
            path = Path(job.input_path)
        else:
            path = Path((job.outputs.get(kind) or {}).get("path") or "")
            if job.outputs.get(kind, {}).get("status") != "ready":
                raise HTTPException(404, "This video is not ready.")
        allowed = [config.OUTPUT_DIR.resolve(), (config.TEMP_DIR / "sources").resolve(),
                   (config.DATA_ROOT / "uploads").resolve()]
        resolved = path.resolve()
        if not path.is_file() or not any(resolved.is_relative_to(root) for root in allowed):
            raise HTTPException(404, "Video not found")
        return resolved

    @router.get("/jobs/{job_id}/media/{kind}")
    def job_media(job_id: str, kind: str, request: Request, download: bool = False):
        if kind not in MEDIA_KINDS:
            raise HTTPException(404, "Video not found")
        job = owned_job(user(request), job_id)
        path = media_path(job, kind)
        stem = Path(job.filename).stem
        name = f"{stem}.mp4" if kind == "source" else f"{stem}_{kind}.mp4"
        # FileResponse serves HTTP range requests, so players can seek large videos.
        return FileResponse(path, media_type="video/mp4", filename=name,
                            content_disposition_type="attachment" if download else "inline")

    @router.get("/jobs/{job_id}/files/{name}")
    def job_file(job_id: str, name: str, request: Request):
        if name not in JOB_FILES:
            raise HTTPException(404, "File not found")
        job = owned_job(user(request), job_id)
        if not job.report_path:
            raise HTTPException(404, "File not found")
        path = Path(job.report_path).parent / name
        if not path.is_file():
            raise HTTPException(404, "File not found")
        types = {".txt": "text/plain", ".srt": "text/plain", ".json": "application/json", ".html": "text/html"}
        return FileResponse(path, media_type=types[path.suffix] + "; charset=utf-8",
                            filename=f"{Path(job.filename).stem}_{name}", content_disposition_type="attachment")

    @router.get("/estimate")
    def estimate(seconds: float, request: Request):
        owner = user(request)
        rates = []
        for job in scheduler.list(owner):
            try:
                if job.cost_inr and job.duration and job.status == "complete":
                    rates.append(float(job.cost_inr) / (job.duration / 3600))
            except ValueError:
                continue
        rate = statistics.median(rates) if rates else 25.0
        return {"inr": round(rate * max(seconds, 0) / 3600, 1), "inr_per_hour": round(rate, 1),
                "basis": f"your last {len(rates)} lecture(s)" if rates else "a typical lecture"}

    # ---- desktop updates and support ---------------------------------------
    @router.get("/update")
    def update_status(request: Request):
        user(request)
        from desktop_runtime.desktop_paths import FROZEN, VERSION
        try:
            pending = json.loads((config.DATA_ROOT / "pending-update.json").read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            pending = None
        try:
            saved = json.loads((config.DATA_ROOT / "update-settings.json").read_text(encoding="utf-8"))
            link_saved = bool(saved.get("manifest_url"))
        except (OSError, ValueError, AttributeError):
            link_saved = False
        return {"installed": FROZEN, "version": VERSION, "pending": pending, "link_saved": link_saved}

    @router.post("/update/check")
    async def update_check(request: Request):
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        body = await request.json()
        if scheduler.busy():
            return {"message": "Finish or stop the lectures in progress before updating."}
        from desktop_runtime.desktop_update import UpdateError, check_and_stage
        try:
            message = await run_in_threadpool(check_and_stage, str(body.get("link") or "")[:2000])
        except UpdateError as exc:
            message = str(exc)
        except Exception as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = record_error(exc, stage="update", owner=owner)
            message = f"The update could not be downloaded. Check your connection and retry. Reference: {reference}."
        return {"message": message}

    @router.post("/update/restart")
    async def update_restart(request: Request):
        same_origin(request)
        await run_in_threadpool(user, request)
        if scheduler.busy():
            return {"message": "Finish or stop the lectures in progress before restarting."}
        from desktop_runtime.desktop_update import request_restart
        return {"message": await run_in_threadpool(request_restart)}

    @router.post("/support")
    async def support_report(request: Request):
        """Recent error records for this person only; no videos, transcripts or credentials."""
        same_origin(request)
        owner = await run_in_threadpool(user, request)
        from lecture_trimmer.runtime.diagnostics import export_support
        path = await run_in_threadpool(export_support, owner)
        return FileResponse(path, media_type="application/zip", filename=path.name,
                            headers={"Cache-Control": "no-store"})

    # ---- live progress ------------------------------------------------------
    @router.get("/events")
    async def events(request: Request, once: bool = False):
        owner = await run_in_threadpool(user, request)

        async def stream():
            # The browser's EventSource reconnects by itself, so each stream ends after
            # five minutes; abandoned connections can never pile up.
            import time
            version, deadline = -1, time.monotonic() + 300
            while time.monotonic() < deadline and not await request.is_disconnected():
                current = scheduler.version
                if current != version:
                    version = current
                    data = json.dumps({"jobs": [job.public() for job in scheduler.list(owner)]})
                    yield f"event: jobs\ndata: {data}\n\n"
                    if once:
                        return
                else:
                    yield ": keep-alive\n\n"
                await run_in_threadpool(scheduler.wait_for_change, version, 10.0)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return router


def signed_in_resolver(auth):
    """(owner, provider-session factory) from the private server's sign-in cookie."""
    def resolve(request):
        cookie = request.cookies.get(auth.cookie_name)
        identity = auth.current_user(cookie)
        if identity is None:
            return None
        factory = (lambda: auth.job_session(cookie)) if getattr(auth, "desktop", False) else None
        return identity.owner_id, factory
    return resolve


def local_resolver(request):
    """Local mode serves only this computer."""
    host = request.client.host if request.client else ""
    return ("local", None) if host in {"127.0.0.1", "::1", "localhost", "testclient"} else None


def mount_web(server, scheduler, *, resolve_user, base_url=None, mode="local", speech="auto"):
    """Attach the API and, when built, the web interface at /studio."""
    from starlette.staticfiles import StaticFiles
    server.include_router(create_api(scheduler, resolve_user=resolve_user, base_url=base_url,
                                     mode=mode, speech=speech))
    # Lectures finished on the old Classic page appear in the studio list too.
    try:
        scheduler.import_reports(config.OUTPUT_DIR, owner="local" if mode == "local" else None)
    except OSError:
        logger.warning("Earlier results could not be listed")
    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if (dist / "index.html").is_file():
        server.mount("/studio", StaticFiles(directory=dist, html=True), name="studio")
    return server
