"""One cooperative recall deadline and the exact compact JSON wire contract."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time

from liveday0.exceptions import RecallDeadlineExceeded
from liveday0.serialization import canonical_json

_CURRENT = ContextVar('liveday0_recall_deadline', default=None)


class Deadline:
    def __init__(self, timeout_ms):
        self.end = time.monotonic() + timeout_ms / 1000

    def remaining_ms(self, stage='work'):
        left = math.floor((self.end - time.monotonic()) * 1000)
        if left < 1:
            raise RecallDeadlineExceeded(stage)
        return left

    def check(self, stage='work'):
        self.remaining_ms(stage)


def current_deadline():
    return _CURRENT.get()


def check_deadline(stage='compile'):
    if deadline := current_deadline():
        deadline.check(stage)


@contextmanager
def deadline_scope(timeout_ms):
    if existing := current_deadline():
        existing.check('entry')
        yield existing
        existing.check('return')
        return
    deadline = Deadline(timeout_ms)
    token = _CURRENT.set(deadline)
    try:
        yield deadline
        deadline.check('return')
    finally:
        _CURRENT.reset(token)


def recall_json(value):
    return canonical_json(value)


def wire_bytes(value):
    # Includes the newline printed by the CLI. External transport framing is separate.
    return len(recall_json(value).encode('utf-8')) + 1
