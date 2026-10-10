"""Per-login PW kit instances and per-job Gemini requests; no provider keys."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, unquote

from lecture_trimmer.auth.access_control import AccessUnavailable
from lecture_trimmer.integrations.ai_usage import AIUsage
from lecture_trimmer.processing.job_control import check_cancelled, interruptible_wait
from lecture_trimmer.paths import APPLICATION_ROOT

logger = logging.getLogger("trimmer.pw")


@dataclass(frozen=True)
class PWGeminiResponse:
    text: str
    parsed: object = None


def response_structure(value, depth=0):
    """Diagnostic shape only: no text, tokens, account details, or field values."""
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        return {(key if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
                 else "unknown_field"): response_structure(part, depth+1)
                for key, part in list(value.items())[:25]}
    if isinstance(value, list):
        return [response_structure(value[0], depth+1)] if value else []
    return type(value).__name__


def parse_gemini_response(payload):
    """Read Gemini's completed text, allowing proxy accounting metadata.

    google-genai models forbid unknown fields, including harmless proxy usage
    and convenience-text fields. Validate the actual generation explicitly;
    the classifier separately validates every returned lesson against its schema.
    """
    if not isinstance(payload, dict) or payload.get("error") or payload.get("ok") is False:
        raise ValueError("PW did not return a successful Gemini response")
    data = payload
    if "candidates" not in data:
        envelopes = [payload[key] for key in ("response", "result", "data")
                     if isinstance(payload.get(key), dict) and "candidates" in payload[key]]
        if len(envelopes) != 1:
            raise ValueError("PW response does not contain unambiguous Gemini candidates")
        data = envelopes[0]
    if data.get("error"):
        raise ValueError("PW returned a provider error")
    feedback = data.get("promptFeedback", data.get("prompt_feedback")) or {}
    if not isinstance(feedback, dict) or feedback.get("blockReason", feedback.get("block_reason")):
        raise ValueError("Gemini blocked the test or classification request")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ValueError("PW Gemini returned no candidate")
    candidate = candidates[0]
    if candidate.get("finishReason", candidate.get("finish_reason")) != "STOP":
        raise ValueError("PW Gemini returned an incomplete or blocked response")
    content = candidate.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        raise ValueError("PW Gemini returned no text parts")
    texts = []
    for part in content["parts"]:
        if not isinstance(part, dict):
            raise ValueError("PW Gemini returned an invalid content part")
        if part.get("thought") is True:
            continue
        if "text" in part:
            if not isinstance(part["text"], str):
                raise ValueError("PW Gemini returned non-text content")
            texts.append(part["text"])
    if not texts or not "".join(texts).strip():
        raise ValueError("PW Gemini returned an empty answer")
    return PWGeminiResponse("".join(texts))


def error_category(text, code=None):
    """Map untrusted provider prose to fixed diagnostic categories only."""
    text = str(text).lower()
    if any(term in text for term in ("api_key_invalid", "api key not valid", "invalid api key", "default credentials", "could not load credentials")):
        return "provider_credentials"
    if "model" in text and any(term in text for term in ("not found", "not supported", "not allowed", "does not exist", "unsupported", "not available")):
        return "model_unavailable"
    if any(term in text for term in ("responsejsonschema", "response_json_schema", "responseschema", "response_schema", "generationconfig", "generation_config")):
        return "request_schema"
    if any(term in text for term in ("quota", "resource_exhausted", "rate limit")) or code == 429:
        return "quota"
    return {400: "invalid_request", 401: "access", 403: "access", 404: "model_or_route", 413: "request_size"}.get(code, "service")


class ProxyCallError(RuntimeError):
    """A fixed, public error without the proxy response body or tokens."""
    def __init__(self, code=None, *, category=None, request_id=""):
        self.code = code if isinstance(code, int) and 100 <= code <= 599 else None
        self.category = category if category in {
            "provider_credentials", "model_unavailable", "request_schema", "quota", "invalid_request",
            "access", "model_or_route", "request_size", "timeout", "connection", "service",
        } else error_category("", self.code)
        self.request_id = request_id if re.fullmatch(r"[A-Za-z0-9:_.-]{1,120}", request_id or "") else ""
        messages = {
            "provider_credentials": "The PW proxy's Google provider credentials need attention. Contact the PW proxy administrator.",
            "model_unavailable": "The configured Gemini model is unavailable through PW. Ask the proxy administrator to check model access and routing.",
            "request_schema": "PW or Gemini rejected the structured request format. The app/proxy request mapping needs correction.",
            "quota": "PW's AI service has reached a quota or rate limit. Ask the proxy administrator to check its project quota and billing.",
            "invalid_request": "PW rejected the Gemini request. Check the app/proxy request mapping before retrying the lecture.",
            "access": "PW rejected the AI request. Check app access and the proxy's provider permissions.",
            "model_or_route": "The Gemini model or proxy route was not found. Ask the proxy administrator to check the configured model and deployment.",
            "request_size": "The proxy rejected the request size. Check the PW storage/upload configuration.",
            "timeout": "The PW request timed out. Check proxy availability and try the short connection check.",
            "connection": "Could not connect to the PW AI service. Check the network and proxy availability.",
            "service": "The PW AI service could not complete this request. Run Check PW connection for a diagnostic report.",
        }
        prefix = f"PW Gemini error (HTTP {self.code})" if self.code else "PW Gemini connection error"
        self.public_message = f"{prefix}: {messages[self.category]} Saved analysis is preserved."
        super().__init__(self.public_message)

    def diagnostic(self):
        return {"http_status": self.code, "category": self.category,
                "proxy_request_id": self.request_id, "message": self.public_message}


def raise_safe_proxy_error(name, response):
    if response.status_code == 200:
        return
    # Inspect only to select fixed messages. Never retain the response body,
    # which might contain tokens, private URLs, or reflected lecture content.
    category = error_category(response.text[:16000], response.status_code)
    request_id = response.headers.get("x-vercel-id", "")
    raise ProxyCallError(response.status_code, category=category, request_id=request_id)


def load_kit():
    # The supplied kit caches one proxy token at module scope. A distinct module
    # per login prevents account switching/concurrent sessions sharing that token.
    path = APPLICATION_ROOT / "vendor" / "pw_access.py"
    spec = importlib.util.spec_from_file_location("_lecture_pw_session", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parsed = urlsplit(module.PROXY_BASE_URL)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("PW_PROXY_BASE_URL must be an HTTPS origin")
    if module.APP_NAME != "Lecture_Trimmer":
        raise ValueError("The PW adapter app name must be Lecture_Trimmer")
    module._raise_proxy_error = raise_safe_proxy_error
    # Keep the supplied kit intact. Its public status helper hides transport
    # failures; retain safe metadata locally so outages can be diagnosed.
    original_post = module._post
    module._access_diagnostic = {}
    def monitored_post(path, google_token, payload, timeout):
        if path != '/api/allowlist':
            return original_post(path, google_token, payload, timeout)
        started = time.monotonic()
        try:
            response = original_post(path, google_token, payload, timeout)
        except Exception as exc:
            import requests
            category = 'access_timeout' if isinstance(exc, requests.Timeout) else 'access_connection' if isinstance(exc, requests.ConnectionError) else 'access_error'
            module._access_diagnostic = {'category': category, 'http_status': None}
            logger.warning('PW access check failed: %s after %.1fs', category, time.monotonic()-started)
            raise
        status = response.status_code
        category = 'access_allowed' if status == 200 else 'access_expired' if status == 401 else 'access_denied' if status == 403 else 'access_http_error'
        try:
            retry_after = float(response.headers.get('retry-after', 0))
            import math
            if not math.isfinite(retry_after) or retry_after < 0:
                retry_after = 0
        except (TypeError, ValueError):
            retry_after = 0
        module._access_diagnostic = {'category': category, 'http_status': status, 'retry_after': retry_after}
        if status != 200:
            logger.warning('PW access check: HTTP %s after %.1fs', status, time.monotonic()-started)
        return response
    module._post = monitored_post
    return module


class PWSession:
    def __init__(self, google_token, *, expires_at, clock=time.time, kit=None):
        self._token = google_token
        self._kit = kit or load_kit()
        self._expires_at, self.clock = expires_at, clock
        self._lock = threading.RLock()
        self._active, self._checked_until = True, 0

    def close(self):
        with self._lock:
            self._active = False
            self._token = ""
            self._kit._session.update(token="", expiry=0)

    def ensure_allowed(self, *, force=False):
        with self._lock:
            if not self._active or self.clock() >= self._expires_at:
                raise PermissionError("Your sign-in expired. Sign in again before continuing.")
            if not force and self.clock() < self._checked_until:
                return
            metadata = {}
            for attempt in range(3):
                check_cancelled()
                status = self._kit.check_allowed_status(self._token, app="Lecture_Trimmer")
                metadata = getattr(self._kit, '_access_diagnostic', {})
                if status != 'error':
                    break
                code = metadata.get('http_status')
                if code == 401:
                    self.close()
                    raise PermissionError('Your sign-in expired. Sign in again before continuing.')
                if code is not None and code not in {408, 409, 429, 500, 502, 503, 504}:
                    break
                delay = max(attempt + 1, metadata.get('retry_after', 0))
                if attempt == 2 or delay > 5:
                    break
                logger.warning('PW access verification unavailable; retry %d/2', attempt + 1)
                interruptible_wait(delay)
            if status == "denied":
                self.close()
                raise PermissionError("This account is not approved in the Lecture_Trimmer column. Contact the tool owner.")
            if status != "allowed":
                self._checked_until = 0
                error = AccessUnavailable("PW access verification is unavailable. Please retry shortly.")
                error.category = metadata.get('category', 'access_unavailable')
                error.code = metadata.get('http_status')
                raise error
            self._checked_until = self.clock() + 60

    def job(self):
        self.ensure_allowed(force=True)
        return PWJob(self, self._kit.new_task_id("lecture"))

    def generate(self, *, model, request, task_id, filename, count):
        from lecture_trimmer.processing.job_control import check_cancelled
        check_cancelled()
        self.ensure_allowed()
        try:
            return self._kit.gemini_generate(
                self._token, model=model, request=request, app="Lecture_Trimmer",
                task_id=task_id, filename=filename, input_unit="No. of requests", count=count,
            )
        except Exception as exc:
            # The kit includes response bodies in errors. Never propagate them
            # into the browser, local logs, or saved analysis reports.
            if isinstance(exc, ProxyCallError):
                error = exc
            else:
                import requests
                match = re.search(r"(?:error|failed)\s+(\d{3})\b", str(exc))
                code = int(match.group(1)) if match else None
                category = ("timeout" if isinstance(exc, requests.Timeout) else
                            "connection" if isinstance(exc, requests.ConnectionError) else error_category(str(exc), code))
                error = ProxyCallError(code, category=category)
            logger.error("PW request failed: %s", json.dumps({**error.diagnostic(), "model": model, "task_id": task_id}))
            raise error from None


class PWJob:
    def __init__(self, session, task_id):
        self.session, self.task_id = session, task_id
        self.filename = ""
        self.usage = AIUsage()

    # A dropped connection or a gateway error never reached the model, so a short
    # wait and resend is safe and free. Timeouts are not resent here: the request
    # may still have run (and been billed); callers decide whether to repeat it.
    RETRY_DELAYS = (3, 10)

    def generate(self, *, model, request):
        from lecture_trimmer.processing.job_control import interruptible_wait
        for delay in (*self.RETRY_DELAYS, None):
            try:
                result = self.session.generate(model=model, request=request, task_id=self.task_id,
                                               filename=self.filename, count=1)
                break
            except Exception as exc:
                self.usage.record(model, failed=True)
                transient = isinstance(exc, ProxyCallError) and (
                    exc.category == "connection" or exc.code in {502, 503, 504})
                if delay is None or not transient:
                    raise
                logger.info("PW request did not connect; retrying in %s s", delay)
                interruptible_wait(delay)
        # Count a billable response even if classification/schema validation later fails.
        self.usage.record(model, result)
        return result

    def ensure_allowed(self):
        # job() already forced a fresh whitelist check. Reuse that check for
        # 60 seconds, while still enforcing logout, expiry and later revocation.
        self.session.ensure_allowed()

    def client(self, backend):
        if backend != "gemini":
            raise ValueError("PW desktop mode currently uses Gemini. Set CLASSIFIER_BACKEND=gemini or auto.")
        return PWGeminiClient(self)


class PWGeminiClient:
    """Use Google's SDK wire format, sending every request through PW.

    The HTTP transport is local to this client. It never contacts Google's API
    directly and never reads a provider key. SDK schema conversion preserves the
    request format that worked before adding PW authentication.
    """
    def __init__(self, job):
        import httpx
        from google import genai
        self.job = job
        self._client = genai.Client(api_key="pw-transport-no-provider-key", http_options={
            "client_args": {"transport": httpx.MockTransport(self._forward)},
            "timeout": 180000,
        })
        self.models = self._client.models

    def close(self):
        # Classification can close its client; browser logout owns the session.
        self._client.close()

    def _forward(self, request):
        import httpx
        match = re.fullmatch(r"/v1(?:beta)?/models/([^/]+):generateContent", request.url.path)
        if request.method != "POST" or not match:
            raise ValueError("Unsupported SDK operation for the PW transport")
        model = unquote(match.group(1))
        body = json.loads(request.content)
        result = self.job.generate(model=model, request=body)
        try:
            parsed = parse_gemini_response(result)
        except ValueError:
            logger.error("PW response format rejected: %s", json.dumps(response_structure(result)))
            raise RuntimeError("PW Gemini returned an incomplete or unexpected response. Run Check PW connection; saved analysis is preserved.") from None
        # Only generation content is handed to the SDK. PW already owns usage
        # logging; its envelope/cost metadata is not a Google SDK response field.
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {
            "role": "model", "parts": [{"text": parsed.text}],
        }}]})
