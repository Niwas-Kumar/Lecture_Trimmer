"""Lecture Trimmer server: the studio interface at /studio, its /api, and sign-in for shared modes.

  python app.py                      this computer only (http://127.0.0.1:7860/)
  python app.py --auth pw-desktop    Google desktop sign-in with PW access
  python app.py --auth google-sheet  private HTTPS hosting; see docs/AUTH_SETUP.md
"""
import argparse
import atexit
import base64
import binascii
import logging
import os
import secrets
import threading
import webbrowser
from pathlib import Path

import lecture_trimmer.config as config
from desktop_runtime.desktop_paths import DATA_ROOT
from lecture_trimmer.runtime.diagnostics import configure_logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trimmer")
configure_logging()
STUDIO = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _web_scheduler():
    """The batch job engine behind /api and the studio."""
    from lecture_trimmer.server.jobs import JobStore, Scheduler
    scheduler = Scheduler(JobStore(DATA_ROOT / "jobs"))
    atexit.register(scheduler.shutdown)
    return scheduler


def _speech_label():
    return "Groq Whisper Turbo" if config.GROQ_API_KEY else "Groq key missing"


def _basic_auth_middleware(user, password):
    """Optional password for local mode (TRIMMER_USER / TRIMMER_PASSWORD)."""
    from starlette.responses import Response
    expected = f"{user}:{password}".encode()

    async def check(request, call_next):
        header = request.headers.get("authorization", "")
        try:
            given = base64.b64decode(header[6:], validate=True) if header.startswith("Basic ") else b""
        except (ValueError, binascii.Error):
            given = b""
        if not secrets.compare_digest(given, expected):
            return Response("Sign in to continue.", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Lecture Trimmer"'})
        return await call_next(request)
    return check


def make_local_server(scheduler, *, user=None, password=None):
    """This computer only: the studio, its API and an optional password."""
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, RedirectResponse
    from lecture_trimmer.server.api import local_resolver, mount_web
    from lecture_trimmer.ui.branding import ICON_PATH
    server = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if user and password:
        server.middleware("http")(_basic_auth_middleware(user, password))

    @server.get("/")
    def home():
        return RedirectResponse("/studio/", status_code=303)

    @server.get("/favicon.ico")
    def favicon():
        return FileResponse(ICON_PATH, media_type="image/x-icon")

    mount_web(server, scheduler, resolve_user=local_resolver, mode="local", speech=_speech_label())
    return server


def _open_browser_later(url):
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true", help="start without opening a browser window")
    parser.add_argument("--auth", choices=["local", "google-sheet", "pw-desktop"], default=os.getenv("TRIMMER_AUTH", "local"),
                        help="pw-desktop uses Google desktop login and PW access; google-sheet is for HTTPS hosting")
    args = parser.parse_args()
    import uvicorn
    from lecture_trimmer.server.api import mount_web, signed_in_resolver
    if not STUDIO.is_file():
        logger.warning("The studio interface is not built. Run: cd frontend && npm install && npm run build")

    if args.auth == "pw-desktop":
        if args.host != "127.0.0.1":
            parser.error("PW desktop mode requires --host 127.0.0.1.")
        from lecture_trimmer.auth.pw_desktop_auth import DesktopAuthService, DesktopSettings, make_desktop_server
        try:
            settings = DesktopSettings.from_file(config.PROJECT_ROOT, args.port)
            auth = DesktopAuthService(settings)
            server = make_desktop_server(settings, service=auth)
            mount_web(server, _web_scheduler(), resolve_user=signed_in_resolver(auth), base_url=settings.base_url,
                      mode="pw-desktop", speech=_speech_label())
        except Exception as exc:
            parser.error(f"PW desktop sign-in could not start ({type(exc).__name__}). Check docs/PW_DESKTOP_SETUP.md.")
        if not args.no_browser:
            _open_browser_later(settings.base_url)
        # OAuth callback query strings contain a short-lived code. Do not log them.
        uvicorn.run(server, host="127.0.0.1", port=args.port, workers=1, proxy_headers=False, access_log=False)
        return

    if args.auth == "google-sheet":
        from lecture_trimmer.auth.access_control import AuthSettings, make_private_server
        try:
            settings = AuthSettings.from_env()
            server = make_private_server(settings)
            mount_web(server, _web_scheduler(), resolve_user=signed_in_resolver(server.state.trimmer_auth),
                      base_url=settings.base_url, mode="google-sheet", speech=_speech_label())
        except Exception as exc:
            # Invalid credentials fail startup, never launch an unprotected fallback.
            parser.error(f"Private sharing could not start ({type(exc).__name__}). Check docs/AUTH_SETUP.md and server configuration.")
        uvicorn.run(server, host=args.host, port=args.port, workers=1,
                    proxy_headers=True, forwarded_allow_ips=os.getenv("TRIMMER_TRUSTED_PROXY_IPS", "127.0.0.1"))
        return

    if args.host not in LOOPBACK:
        parser.error("Remote access requires --auth google-sheet with a stable HTTPS URL; see docs/AUTH_SETUP.md.")
    server = make_local_server(_web_scheduler(), user=os.getenv("TRIMMER_USER"), password=os.getenv("TRIMMER_PASSWORD"))
    if not args.no_browser:
        _open_browser_later(f"http://127.0.0.1:{args.port}/")
    uvicorn.run(server, host=args.host, port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
