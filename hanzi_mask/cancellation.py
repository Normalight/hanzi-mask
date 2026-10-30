"""Cooperative checkpoints scoped to one image-processing worker."""

from contextlib import contextmanager
from contextvars import ContextVar


class ProcessingCancelled(Exception):
    pass


_callback = ContextVar('image_cancellation', default=None)


def check_cancelled():
    cancel = _callback.get()
    if cancel is not None and cancel():
        raise ProcessingCancelled()


@contextmanager
def cancellation_scope(cancel):
    token = _callback.set(cancel)
    try:
        check_cancelled()
        yield
    finally:
        _callback.reset(token)
