"""Google desktop OAuth on loopback, with private PW Sheet authorization."""
from __future__ import annotations

import json
import os
import secrets
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse

from lecture_trimmer.auth.access_control import AuthService, make_private_server
from lecture_trimmer.integrations.pw_proxy import PWSession


@dataclass(frozen=True)
class DesktopSettings:
    base_url: str
    client_id: str
    client_secret: str = field(repr=False)
    credentials_file: str = ""
    session_seconds: int = 28800

    @classmethod
    def from_file(cls, root, port):
        explicit = os.getenv("TRIMMER_DESKTOP_OAUTH_FILE", "")
        files = [Path(explicit)] if explicit else list(Path(root).glob("client_secret*.json"))
        if len(files) != 1:
            raise ValueError("Set TRIMMER_DESKTOP_OAUTH_FILE to the Desktop OAuth JSON file; see docs/PW_DESKTOP_SETUP.md.")
        path = files[0] if files[0].is_absolute() else Path(root) / files[0]
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))["installed"]
            settings = cls(f"http://127.0.0.1:{port}", data["client_id"], data["client_secret"], str(path.resolve()))
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("Use the downloaded Desktop app OAuth JSON, not a Web client or service account.") from None
        settings.validate()
        return settings

    def validate(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("PW desktop authentication must bind to http://127.0.0.1:<port>")
        if not self.client_id.endswith(".apps.googleusercontent.com") or not self.client_secret:
            raise ValueError("Desktop OAuth client ID and secret are missing")
        if not 300 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be 300–86400 seconds")


class PWDomain:
    def allowed(self, email):
        return email.rsplit("@", 1)[-1] == "pw.live"


class DesktopAuthService(AuthService):
    secure_cookies = False  # Only this strictly loopback-bound desktop mode uses HTTP.
    desktop = True

    def __init__(self, settings, *, clock=time.time, token_verifier=None, proxy_factory=PWSession):
        settings.validate()
        super().__init__(settings, PWDomain(), clock=clock, token_verifier=token_verifier)
        port = urlsplit(settings.base_url).port
        self.cookie_name = f"trimmer_desktop_session_{port}"
        self.flow_cookie_name = f"trimmer_desktop_login_{port}"
        self.proxy_factory = proxy_factory
        self._desktop_sessions = {}

    def _prune(self):
        super()._prune()
        for key, (_, proxy, expiry) in list(self._desktop_sessions.items()):
            if expiry <= self.clock():
                proxy.close()
                del self._desktop_sessions[key]

    def complete_login(self, code, flow):
        tokens = self.exchange_tokens(code, flow)
        identity = self.identity_from_token(tokens["id_token"], flow["nonce"])
        expiry = self.clock() + self.settings.session_seconds
        proxy = self.proxy_factory(tokens["id_token"], expires_at=expiry, clock=self.clock)
        try:
            proxy.ensure_allowed(force=True)
        except Exception:
            proxy.close()
            raise
        cookie = secrets.token_urlsafe(48)
        with self._lock:
            self._prune()
            self._desktop_sessions[cookie] = (identity, proxy, expiry)
        return cookie

    def _session(self, cookie):
        with self._lock:
            self._prune()
            return self._desktop_sessions.get(cookie or "")

    def current_user(self, cookie):
        entry = self._session(cookie)
        if not entry:
            return None
        identity, proxy, _ = entry
        try:
            proxy.ensure_allowed()
        except PermissionError:
            self.logout(cookie)
            return None
        return identity

    def job_session(self, cookie):
        entry = self._session(cookie)
        if not entry:
            raise PermissionError("Sign in again before starting a lecture.")
        return entry[1].job()

    def logout(self, cookie):
        with self._lock:
            entry = self._desktop_sessions.pop(cookie or "", None)
        if entry:
            entry[1].close()


def make_desktop_server(settings, *, service=None):
    auth = service or DesktopAuthService(settings)
    server = make_private_server(settings, service=auth)

    @server.post("/auth/open-browser")
    async def open_browser(request: Request):
        if request.headers.get("origin") != settings.base_url:
            raise HTTPException(403, "Invalid request origin")
        opened = await run_in_threadpool(webbrowser.open, settings.base_url + "/auth/login")
        text = ("Your default browser has opened. Complete Google sign-in there; Lecture Trimmer will open after access is approved."
                if opened else "Open the address below in Chrome or Edge to sign in.")
        from lecture_trimmer.ui.ui_style import login_page
        return HTMLResponse(login_page(f'<h2>Continue in your browser</h2><p>{text}</p>'
                                      f'<a class="button" href="{settings.base_url}/auth/login">Continue to sign in</a>'),
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    return server
