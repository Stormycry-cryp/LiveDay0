"""Process-local, non-refundable reservations for one explicitly approved trial.

This is not an account spend cap. Share one instance across the trial; a new
process/instance has no knowledge of old or concurrent runs.
"""
from copy import deepcopy
from decimal import Decimal
from threading import Lock


MODEL = 'gpt-4.1-mini-2025-04-14'
MAX_INPUT = 8192
MAX_OUTPUT = 4096
# Snapshot from the official model page, 2026-10-09; recheck before paid use.
INPUT_USD_PER_MILLION = Decimal('0.40')
OUTPUT_USD_PER_MILLION = Decimal('1.60')
RESERVATION_USD = (MAX_INPUT * INPUT_USD_PER_MILLION +
                   MAX_OUTPUT * OUTPUT_USD_PER_MILLION) / 1_000_000


class ProviderFailure(ValueError):
    """Static reason codes only: no response, source, or credential in errors."""


class CallBudget:
    def __init__(self, *, max_calls=12, max_usd=Decimal('0.15')):
        if type(max_calls) is not int or not 1 <= max_calls <= 12:
            raise ValueError('trial allows 1..12 calls')
        if not isinstance(max_usd, Decimal) or not max_usd.is_finite() or not 0 < max_usd <= Decimal('0.15'):
            raise ValueError('trial budget must be a Decimal in (0, 0.15]')
        self._max_calls, self._max_usd = max_calls, max_usd
        self._lock = Lock()
        self._entries = []
        self._stopped = False

    def reserve(self):
        with self._lock:
            if (self._stopped or len(self._entries) >= self._max_calls or
                    (len(self._entries) + 1) * RESERVATION_USD > self._max_usd):
                raise ProviderFailure('budget_exhausted')
            self._entries.append({'status': 'reserved', 'input_tokens': None, 'output_tokens': None})
            return len(self._entries) - 1

    def finish(self, reservation, status, usage=None):
        with self._lock:
            item = self._entries[reservation]
            if item['status'] != 'reserved':
                raise ProviderFailure('reservation_already_finished')
            item['status'] = status
            if usage is not None:
                counts = [usage.get(k) for k in ('input_tokens', 'output_tokens')] if type(usage) is dict else []
                if (len(counts) != 2 or any(type(n) is not int or n < 0 for n in counts)
                        or counts[0] > MAX_INPUT or counts[1] > MAX_OUTPUT):
                    self._stopped = True
                    item['status'] = 'usage_outside_preflight'
                    raise ProviderFailure('usage_outside_preflight')
                item.update(input_tokens=counts[0], output_tokens=counts[1])

    def snapshot(self):
        with self._lock:
            return {'calls': len(self._entries), 'reserved_usd': str(len(self._entries) * RESERVATION_USD),
                    'stopped': self._stopped, 'entries': deepcopy(self._entries)}
