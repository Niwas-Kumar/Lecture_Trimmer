"""Download a lecture from a pasted video link (for example a CloudFront or S3 URL).

The link is checked before anything is saved: it must be a public internet
address (never this computer or a private network), and the server must answer
with a video. Servers that support byte ranges are downloaded in parallel
pieces, each retried on its own after a dropped connection. A link's query
string often carries a signature, so only scheme, host and path are ever stored
or logged.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests

logger = logging.getLogger("trimmer")

PIECE_BYTES = 16 * 1024**2
WORKERS = 4
MAX_REDIRECTS = 5
PIECE_ATTEMPTS = 5
MAX_LINK_CHARS = 8000
TIMEOUT = (15, 60)  # connect, read between bytes
VIDEO_TYPES = {"video/mp4": ".mp4", "application/mp4": ".mp4", "video/quicktime": ".mov",
               "video/webm": ".webm", "video/x-matroska": ".mkv", "video/x-msvideo": ".avi",
               "video/x-m4v": ".m4v"}
GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}


class LinkError(ValueError):
    """A reason, in plain words, why the link cannot be used."""


class TemporaryLinkError(LinkError):
    """Unreachable server, timeout or 5xx/429: worth retrying."""


class DownloadCancelled(Exception):
    pass


def redacted(url: str) -> str:
    """The link without its query string or fragment, which may hold a signature."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{parts.path}"


def check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise LinkError("Paste the full video link, starting with https://")
    if parts.username or parts.password:
        raise LinkError("Links with a user name or password inside them are not supported.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)}
    except (socket.gaierror, ValueError):
        raise LinkError("That web address could not be found. Check the link.") from None
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise LinkError("Links to this computer or a private network are not allowed.")


def _refusal(status: int) -> LinkError:
    if status in {401, 403}:
        return LinkError(f"The link was refused (HTTP {status}). Secure links expire; copy a fresh link and try again.")
    if status == 404:
        return LinkError("There is no video at this link (HTTP 404). Check that the link is complete.")
    if status == 410:
        return LinkError("This link has expired (HTTP 410). Copy a fresh link and try again.")
    if status == 429 or status >= 500:
        return TemporaryLinkError(f"The video server is busy (HTTP {status}). Try again in a few minutes.")
    return LinkError(f"The video server answered with an error (HTTP {status}).")


def _get(session, url, headers=None, stream=True):
    """GET following redirects by hand, so every hop is checked before it is contacted."""
    for _ in range(MAX_REDIRECTS + 1):
        check_public(url)
        try:
            response = session.get(url, headers=headers or {}, stream=stream, timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException:
            raise TemporaryLinkError("The video server could not be reached. Check the connection and the link.") from None
        if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
            response.close()
            url = urljoin(url, response.headers["location"])
            continue
        return response, url
    raise LinkError("The link redirects too many times.")


def _filename(url: str, response, content_type: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
    name = unquote(match.group(1)) if match else unquote(Path(urlsplit(url).path).name)
    stem = name or "lecture"
    if Path(stem).suffix.lower() not in set(VIDEO_TYPES.values()):
        stem += VIDEO_TYPES.get(content_type, ".mp4")
    return stem


def probe(url: str, session=None):
    """Return (final_url, filename, size, supports_ranges) after checking it is a video."""
    url = (url or "").strip()
    if not url or len(url) > MAX_LINK_CHARS:
        raise LinkError("Paste the full video link, starting with https://")
    session = session or requests.Session()
    response, final = _get(session, url, headers={"Range": "bytes=0-0"})
    try:
        if response.status_code not in {200, 206}:
            raise _refusal(response.status_code)
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not (content_type.startswith("video/") or content_type in VIDEO_TYPES or content_type in GENERIC_TYPES):
            raise LinkError("This link opens a web page, not a video file. Copy the direct video link "
                            "(it usually ends in .mp4).")
        size = None
        ranged = response.status_code == 206
        total = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("content-range", ""))
        if ranged and total:
            size = int(total.group(1))
        elif response.status_code == 200 and response.headers.get("content-length", "").isdigit():
            size, ranged = int(response.headers["content-length"]), False
        if not size:
            raise LinkError("The video server did not report the file size, so it cannot be downloaded safely.")
        return final, _filename(final, response, content_type), size, ranged
    finally:
        response.close()


class Download:
    """One link being saved to disk; `received` is read by the API for progress."""

    def __init__(self, url, part: Path, size: int, ranged: bool, session=None):
        self.url, self.part, self.size, self.ranged = url, Path(part), size, ranged
        self.session = session or requests.Session()
        self.cancelled = threading.Event()   # the person removed the link
        self._abort = threading.Event()      # another piece failed for good
        self.received = 0
        self._lock = threading.Lock()

    def _count(self, n):
        with self._lock:
            self.received += n

    def _check(self):
        if self.cancelled.is_set() or self._abort.is_set():
            raise DownloadCancelled()

    def _wait(self, seconds):
        self.cancelled.wait(seconds)
        self._check()

    def _piece(self, start, end):
        """Write bytes start..end (inclusive), resuming inside the piece after an interruption."""
        position, delay = start, 2.0
        for attempt in range(PIECE_ATTEMPTS):
            self._check()
            try:
                response, _ = _get(self.session, self.url, headers={"Range": f"bytes={position}-{end}"})
                with response:
                    if response.status_code != 206:
                        raise _refusal(response.status_code) if response.status_code >= 400 else LinkError(
                            "The video server stopped supporting resumable downloads. Try the link again.")
                    if not response.headers.get("content-range", "").startswith(f"bytes {position}-"):
                        raise LinkError("The video server sent the wrong part of the file. Try the link again.")
                    with self.part.open("r+b") as handle:
                        handle.seek(position)
                        for chunk in response.iter_content(1024 * 1024):
                            self._check()
                            chunk = chunk[:end + 1 - position]
                            handle.write(chunk)
                            position += len(chunk)
                            self._count(len(chunk))
                if position > end:
                    return
            except (requests.RequestException, TemporaryLinkError):
                pass  # dropped connection, timeout or busy server: resume this piece
            logger.warning("Link download piece at byte %d interrupted; retry %d in %.0fs", position, attempt + 1, delay)
            self._wait(delay)
            delay = min(delay * 2, 30)
        raise LinkError("The download kept failing. Check the connection and try the link again.")

    def _single(self):
        """Servers without byte ranges: one stream, restarted from the beginning on failure."""
        for attempt in range(3):
            self._check()
            with self._lock:
                self.received = 0
            try:
                response, _ = _get(self.session, self.url)
                with response:
                    if response.status_code != 200:
                        raise _refusal(response.status_code)
                    with self.part.open("wb") as handle:
                        for chunk in response.iter_content(1024 * 1024):
                            self._check()
                            handle.write(chunk)
                            self._count(len(chunk))
                if self.received == self.size:
                    return
            except (requests.RequestException, TemporaryLinkError):
                pass
            if attempt < 2:
                self._wait(5)
        raise LinkError("The download kept failing. Check the connection and try the link again.")

    def run(self):
        started = time.monotonic()
        if not self.ranged:
            self._single()
        else:
            with self.part.open("r+b") as handle:
                handle.truncate(self.size)
            pieces = [(s, min(s + PIECE_BYTES, self.size) - 1) for s in range(0, self.size, PIECE_BYTES)]
            failure = None
            with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="link-download") as pool:
                for future in as_completed([pool.submit(self._piece, s, e) for s, e in pieces]):
                    try:
                        future.result()
                    except DownloadCancelled:
                        pass
                    except BaseException as exc:  # first real failure stops the other pieces
                        failure = failure or exc
                        self._abort.set()
            if self.cancelled.is_set():
                raise DownloadCancelled()
            if failure:
                raise failure
        if self.part.stat().st_size != self.size or self.received != self.size:
            raise LinkError("The downloaded file is incomplete. Try the link again.")
        logger.info("Downloaded %s (%.0f MB) in %.0fs", redacted(self.url), self.size / 1024**2, time.monotonic() - started)
