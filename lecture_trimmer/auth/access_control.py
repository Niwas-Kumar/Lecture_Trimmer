"""Server-side Google identity and private Sheets access rules.

No Google credentials or access rules are sent to the browser. Use one server
worker: sessions intentionally expire when the server restarts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

logger = logging.getLogger("trimmer.access")
COOKIE = "__Host-trimmer_session"
FLOW_COOKIE = "__Host-trimmer_login"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


class AccessUnavailable(Exception):
    """Invalid or currently unavailable access rules; never fall back to allow."""


@dataclass(frozen=True)
class AuthSettings:
    base_url: str
    client_id: str
    client_secret: str
    sheet_id: str
    sheet_range: str = "Access!A1:C10000"
    email_column: str = "email"
    enabled_column: str = "enabled"
    expires_column: str = "expires_at"
    refresh_seconds: int = 60
    session_seconds: int = 28800
    credentials_file: str = ""

    @classmethod
    def from_env(cls):
        try:
            settings = cls(
                base_url=os.environ.get("TRIMMER_BASE_URL", "").rstrip("/"),
                client_id=os.environ.get("TRIMMER_GOOGLE_CLIENT_ID", ""),
                client_secret=os.environ.get("TRIMMER_GOOGLE_CLIENT_SECRET", ""),
                sheet_id=os.environ.get("TRIMMER_ACCESS_SHEET_ID", ""),
                sheet_range=os.environ.get("TRIMMER_ACCESS_SHEET_RANGE", "Access!A1:C10000"),
                email_column=os.environ.get("TRIMMER_ACCESS_EMAIL_COLUMN", "email"),
                enabled_column=os.environ.get("TRIMMER_ACCESS_ENABLED_COLUMN", "enabled"),
                expires_column=os.environ.get("TRIMMER_ACCESS_EXPIRES_COLUMN", "expires_at"),
                refresh_seconds=int(os.environ.get("TRIMMER_ACCESS_REFRESH_SECONDS", "60")),
                session_seconds=int(os.environ.get("TRIMMER_SESSION_SECONDS", "28800")),
                credentials_file=os.environ.get("TRIMMER_GOOGLE_CREDENTIALS_FILE", ""),
            )
            settings.validate()
            return settings
        except (ValueError, TypeError) as exc:
            raise AccessUnavailable(f"Invalid authentication configuration: {exc}") from exc

    def validate(self):
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("TRIMMER_BASE_URL must be the HTTPS origin, without a path")
        if not self.client_id.endswith(".apps.googleusercontent.com") or not self.client_secret:
            raise ValueError("Google OAuth client ID and secret are required")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.sheet_id):
            raise ValueError("TRIMMER_ACCESS_SHEET_ID is required")
        if not self.sheet_range or not 15 <= self.refresh_seconds <= 300:
            raise ValueError("Set a Sheet range and a refresh interval of 15–300 seconds")
        if not 300 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be 300–86400 seconds")
        columns = [self.email_column, self.enabled_column, self.expires_column]
        if not all(columns) or len(set(columns)) != 3:
            raise ValueError("Three distinct email, enabled and expires_at column headers are required")


def normalize_email(value):
    email = str(value).strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise AccessUnavailable("The access Sheet contains an invalid email address")
    return email


def parse_access_rows(rows, settings: AuthSettings):
    """Validate the entire table before replacing a previously valid snapshot."""
    if not rows:
        raise AccessUnavailable("The access Sheet is empty")
    headers = [str(value).strip() for value in rows[0]]
    required = [settings.email_column, settings.enabled_column, settings.expires_column]
    if any(headers.count(name) != 1 for name in required):
        raise AccessUnavailable("The access Sheet must have unique configured column headers")
    indexes = [headers.index(name) for name in required]
    entries = {}
    for row in rows[1:]:
        if not any(str(cell).strip() for cell in row):
            continue
        values = [str(row[index]).strip() if index < len(row) else "" for index in indexes]
        email, enabled, expires = values
        email = normalize_email(email)
        if email in entries:
            raise AccessUnavailable("Duplicate emails in the access Sheet must be resolved")
        flag = enabled.casefold()
        if flag not in {"true", "false", "1", "0", "yes", "no", ""}:
            raise AccessUnavailable("Enabled values must be TRUE or FALSE")
        expiry = None
        if expires:
            try:
                date = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError("timezone required")
                expiry = date.timestamp()
            except (TypeError, ValueError) as exc:
                raise AccessUnavailable("Expiry values must be ISO timestamps with a timezone") from exc
        entries[email] = (flag in {"true", "1", "yes"}, expiry)
    return entries


class GoogleSheetReader:
    def __init__(self, settings):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        if settings.credentials_file:
            credentials = service_account.Credentials.from_service_account_file(
                settings.credentials_file, scopes=[SHEETS_SCOPE])
        else:
            credentials, _ = google.auth.default(scopes=[SHEETS_SCOPE])
        # The host service account is shared into this one private Sheet as Viewer.
        self.http = AuthorizedSession(credentials, refresh_timeout=10)
        self.url = (f"https://sheets.googleapis.com/v4/spreadsheets/{settings.sheet_id}/values/"
                    f"{quote(settings.sheet_range, safe='')}")

    def __call__(self):
        response = self.http.get(self.url, timeout=10,
                                 params={"valueRenderOption": "FORMATTED_VALUE"})
        response.raise_for_status()
        return response.json().get("values", [])


class SheetAllowlist:
    def __init__(self, settings, reader, clock=time.time):
        self.settings, self.reader, self.clock = settings, reader, clock
        self._entries, self._refresh_at, self._available = {}, 0, False
        self._lock = threading.Lock()

    def allowed(self, email):
        now = self.clock()
        with self._lock:
            if now >= self._refresh_at:
                try:
                    self._entries = parse_access_rows(self.reader(), self.settings)
                    self._available = True
                    self._refresh_at = self.clock() + self.settings.refresh_seconds
                except Exception as exc:
                    # Never use stale grants after a failed refresh; short backoff
                    # prevents a Sheet outage from triggering a request storm.
                    self._entries, self._available = {}, False
                    self._refresh_at = self.clock() + min(5, self.settings.refresh_seconds)
                    logger.warning("Access Sheet refresh failed (%s)", type(exc).__name__)
            if not self._available:
                raise AccessUnavailable("Access verification is temporarily unavailable. Please retry shortly.")
            enabled, expiry = self._entries.get(normalize_email(email), (False, None))
            return enabled and (expiry is None or now < expiry)


@dataclass(frozen=True)
class Identity:
    owner_id: str
    email: str


class AuthService:
    cookie_name = COOKIE
    flow_cookie_name = FLOW_COOKIE
    secure_cookies = True
    desktop = False

    def __init__(self, settings, allowlist, clock=time.time, token_verifier=None):
        self.settings, self.allowlist, self.clock = settings, allowlist, clock
        self.token_verifier = token_verifier or self.verify_google_token
        self._sessions, self._flows = {}, {}
        self._lock = threading.Lock()

    def _prune(self):
        now = self.clock()
        self._sessions = {key: value for key, value in self._sessions.items() if value[1] > now}
        self._flows = {key: value for key, value in self._flows.items() if value[1] > now}

    def begin_login(self):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        flow = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            if len(self._flows) >= 1000:
                raise AccessUnavailable("Too many pending sign-ins. Please retry shortly.")
            self._flows[flow] = ({"state": state, "nonce": nonce, "verifier": verifier}, self.clock() + 300)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.settings.client_id, "redirect_uri": self.settings.base_url + "/auth/callback",
            "response_type": "code", "scope": "openid email", "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
        })
        return flow, url

    def consume_flow(self, cookie, state):
        with self._lock:
            self._prune()
            data = self._flows.pop(cookie or "", None)
        if not data or not state or not hmac.compare_digest(data[0]["state"], state):
            raise ValueError("Invalid or expired sign-in state")
        return data[0]

    def verify_google_token(self, token):
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token
        request = GoogleRequest()
        def bounded_request(*args, **kwargs):
            kwargs.setdefault("timeout", 10)
            return request(*args, **kwargs)
        return id_token.verify_oauth2_token(token, bounded_request, audience=self.settings.client_id)

    def identity_from_token(self, token, nonce):
        claims = self.token_verifier(token)
        if (claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"} or
                claims.get("aud") != self.settings.client_id or claims.get("email_verified") is not True or
                not isinstance(claims.get("sub"), str) or not claims["sub"] or
                claims.get("azp", self.settings.client_id) != self.settings.client_id or
                not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce) or
                float(claims.get("exp", 0)) <= self.clock()):
            raise ValueError("Invalid Google identity")
        email = normalize_email(claims.get("email", ""))
        if not self.allowlist.allowed(email):
            raise PermissionError("This Google account does not have access. Contact the tool owner.")
        return Identity(hashlib.sha256(("google:" + claims["sub"]).encode()).hexdigest(), email)

    def create_session(self, identity):
        token = secrets.token_urlsafe(48)
        with self._lock:
            self._prune()
            self._sessions[token] = (identity, self.clock() + self.settings.session_seconds)
        return token

    def exchange_tokens(self, code, flow):
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "redirect_uri": self.settings.base_url + "/auth/callback",
                "grant_type": "authorization_code", "code_verifier": flow["verifier"],
            })
            response.raise_for_status()
            return response.json()

    def complete_login(self, code, flow):
        tokens = self.exchange_tokens(code, flow)
        identity = self.identity_from_token(tokens["id_token"], flow["nonce"])
        return self.create_session(identity)

    def current_user(self, cookie):
        with self._lock:
            value = self._sessions.get(cookie or "")
            if value and value[1] <= self.clock():
                self._sessions.pop(cookie, None)
                value = None
        if not value:
            return None
        if not self.allowlist.allowed(value[0].email):
            self.logout(cookie)
            return None
        return value[0]

    def logout(self, cookie):
        with self._lock:
            self._sessions.pop(cookie or "", None)


STUDIO_HOME = "/studio/"


def make_private_server(settings, *, service=None):
    """Verified Google sign-in in front of the studio; the caller mounts /studio and /api."""
    settings.validate()
    auth = service or AuthService(settings, SheetAllowlist(settings, GoogleSheetReader(settings)))
    cookie, flow_cookie = auth.cookie_name, auth.flow_cookie_name
    secure = auth.secure_cookies
    server = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    from lecture_trimmer.ui.branding import ICON_PATH
    @server.get('/favicon.ico')
    async def favicon():
        from fastapi.responses import FileResponse
        return FileResponse(ICON_PATH, media_type='image/x-icon')
    @server.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        from lecture_trimmer.runtime.diagnostics import record_error
        user = getattr(request.state, 'trimmer_user', None)
        reference = record_error(exc, stage='web-request', owner=getattr(user, 'owner_id', None))
        return JSONResponse({'detail': f'The request could not finish. Please retry. Reference: {reference}.'},
                            status_code=500, headers={'Cache-Control': 'no-store'})
    server.state.trimmer_auth = auth
    server.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.base_url).hostname])

    @server.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        try:
            user = await run_in_threadpool(auth.current_user, request.cookies.get(cookie))
        except AccessUnavailable:
            user = None
        if user:
            return RedirectResponse(STUDIO_HOME, status_code=303)
        login_control = ('<form action="/auth/open-browser" method="post">'
                         '<button type="submit">Sign in with Google in your browser</button></form>'
                         '<p>Use your approved @pw.live account. Google sign-in opens in your default browser.</p>'
                         if auth.desktop else '<p><a href="/auth/login">Continue with Google</a></p>')
        from lecture_trimmer.ui.ui_style import login_page
        return HTMLResponse(login_page(login_control),
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @server.get("/auth/login")
    async def login():
        try:
            flow, url = auth.begin_login()
        except AccessUnavailable as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = record_error(exc, stage='sign-in')
            raise HTTPException(503, f'Sign-in is temporarily unavailable. Please retry. Reference: {reference}.') from None
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(flow_cookie, flow, max_age=300, secure=secure, httponly=True,
                            samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @server.get("/auth/callback")
    async def callback(request: Request):
        try:
            flow = auth.consume_flow(request.cookies.get(flow_cookie), request.query_params.get("state"))
            code = request.query_params.get("code")
            if not code or request.query_params.get("error"):
                raise ValueError("Google sign-in was cancelled")
            session = await run_in_threadpool(auth.complete_login, code, flow)
            auth.logout(request.cookies.get(cookie))
            response = RedirectResponse(STUDIO_HOME, status_code=303)
            response.set_cookie(cookie, session, max_age=settings.session_seconds, secure=secure,
                                httponly=True, samesite="lax", path="/")
        except AccessUnavailable as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = record_error(exc, stage='sign-in')
            response = JSONResponse({"detail": f"Sign-in is temporarily unavailable. Please retry. Reference: {reference}."}, status_code=503)
        except PermissionError as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            reference = record_error(exc, stage='sign-in')
            response = JSONResponse({"detail": f"This account does not have access. Contact the tool owner. Reference: {reference}."}, status_code=403)
        except Exception as exc:
            from lecture_trimmer.runtime.diagnostics import record_error
            record_error(exc, stage='sign-in')
            response = JSONResponse({"detail": "Sign-in failed or expired. Return to the home page and retry."}, status_code=401)
        response.delete_cookie(flow_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @server.post("/auth/logout")
    async def logout(request: Request):
        if request.headers.get("origin") != settings.base_url:
            raise HTTPException(403, "Invalid request origin")
        auth.logout(request.cookies.get(cookie))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(cookie, path="/", secure=secure, httponly=True, samesite="lax")
        return response

    return server
