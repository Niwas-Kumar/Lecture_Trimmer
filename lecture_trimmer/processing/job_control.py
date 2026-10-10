"""Cooperative cancellation shared by a lecture and its worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import threading


class ProcessingCancelled(BaseException):
    """Intentional stop; must not trigger provider retries or encoder fallback."""


_current = ContextVar('lecture_cancel', default=None)


@contextmanager
def cancellation_scope(event):
    token = _current.set(event)
    try:
        check_cancelled()
        yield
    finally:
        _current.reset(token)


def check_cancelled():
    event = _current.get()
    if event is not None and event.is_set():
        raise ProcessingCancelled()


def interruptible_wait(seconds):
    event = _current.get()
    if event is None:
        event = threading.Event()
    if event.wait(seconds):
        raise ProcessingCancelled()


def submit_with_context(pool, function, *args, **kwargs):
    context = copy_context()
    return pool.submit(context.run, function, *args, **kwargs)
