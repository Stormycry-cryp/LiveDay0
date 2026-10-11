"""One cooperative recall deadline and the exact compact JSON wire contract."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time

from liveday0.exceptions import RecallDeadlineExceeded, RecallMaintenanceRequired, RecallWorkLimitExceeded
from liveday0.serialization import canonical_json

_CURRENT = ContextVar('liveday0_recall_deadline', default=None)


class Deadline:
    def __init__(self, timeout_ms, pending_delta_limit=None, catch_up_target_limit=8,
                 derived_input_limit=None):
        self.end = time.monotonic() + timeout_ms / 1000
        self.pending_delta_remaining = pending_delta_limit
        self.catch_up_target_limit = catch_up_target_limit
        self.derived_input_remaining = derived_input_limit
        self.work_exhaustion_reason = None
        self.work_exhaustion_type = RecallMaintenanceRequired

    def remaining_ms(self, stage='work'):
        self.check_work()
        left = math.floor((self.end - time.monotonic()) * 1000)
        if left < 1:
            raise RecallDeadlineExceeded(stage)
        return left

    def check(self, stage='work'):
        self.remaining_ms(stage)

    def check_work(self):
        if self.work_exhaustion_reason:
            raise self.work_exhaustion_type(self.work_exhaustion_reason)

    def exhaust_work(self, reason, error_type=RecallMaintenanceRequired):
        self.work_exhaustion_reason = reason
        self.work_exhaustion_type = error_type
        raise error_type(reason)


def current_deadline():
    return _CURRENT.get()


def check_deadline(stage='compile'):
    if deadline := current_deadline():
        deadline.check(stage)


@contextmanager
def deadline_scope(timeout_ms, *, pending_delta_limit=None, catch_up_target_limit=None,
                   derived_input_limit=None):
    if existing := current_deadline():
        existing.check('entry')
        existing.end = min(existing.end, time.monotonic() + timeout_ms / 1000)
        if pending_delta_limit is not None:
            existing.pending_delta_remaining = (pending_delta_limit
                if existing.pending_delta_remaining is None
                else min(existing.pending_delta_remaining, pending_delta_limit))
        if catch_up_target_limit is not None:
            existing.catch_up_target_limit = min(existing.catch_up_target_limit, catch_up_target_limit)
        if derived_input_limit is not None:
            existing.derived_input_remaining = (derived_input_limit
                if existing.derived_input_remaining is None
                else min(existing.derived_input_remaining, derived_input_limit))
        yield existing
        existing.check('return')
        return
    deadline = Deadline(timeout_ms, pending_delta_limit,
                        8 if catch_up_target_limit is None else catch_up_target_limit,
                        derived_input_limit)
    token = _CURRENT.set(deadline)
    try:
        yield deadline
        deadline.check('return')
    finally:
        _CURRENT.reset(token)


def pending_delta_fetch_limit():
    deadline = current_deadline()
    if deadline is None or deadline.pending_delta_remaining is None:
        return None  # Standalone maintenance is outside the query work contract.
    deadline.check_work()
    return deadline.pending_delta_remaining + 1


def consume_pending_deltas(count):
    deadline = current_deadline()
    if deadline is None or deadline.pending_delta_remaining is None:
        return
    deadline.check_work()
    if count > deadline.pending_delta_remaining:
        deadline.exhaust_work('pending_delta_limit')
    deadline.pending_delta_remaining -= count


def catch_up_target_limit():
    deadline = current_deadline()
    if deadline:
        deadline.check_work()
    return deadline.catch_up_target_limit if deadline else 8


def consume_catch_up_targets(count):
    if deadline := current_deadline():
        deadline.check_work()
        if count > deadline.catch_up_target_limit:
            deadline.exhaust_work('catch_up_target_limit')
        deadline.catch_up_target_limit -= count


def derived_input_fetch_limit():
    deadline = current_deadline()
    if deadline is None or deadline.derived_input_remaining is None:
        return None
    deadline.check_work()
    return deadline.derived_input_remaining + 1


def consume_derived_inputs(count):
    deadline = current_deadline()
    if deadline is None or deadline.derived_input_remaining is None:
        return
    deadline.check_work()
    if count > deadline.derived_input_remaining:
        deadline.exhaust_work('derived_input_limit', RecallWorkLimitExceeded)
    deadline.derived_input_remaining -= count


def recall_json(value):
    return canonical_json(value)


def wire_bytes(value):
    # Includes the newline printed by the CLI. External transport framing is separate.
    return len(recall_json(value).encode('utf-8')) + 1
