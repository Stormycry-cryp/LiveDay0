"""Exact golden checks for two synthetic proposals, never a life-domain policy.

No transport, persistence, credentials or interpretation of arbitrary prose.
The adapter wrapper is opt-in and leaves the product's default path unchanged.
"""
from liveday0.extraction_contract import ProposalBatch, ProposalValidator
from liveday0.provider_budget import ProviderFailure


PENDING_SOURCE = '合成：爬山从2026-10-10改到2026-10-11，还在等朋友确认。'
CONFIRMED_SOURCE = '合成：爬山从2026-10-10改到2026-10-11，朋友已经确认。'
_FIELDS = (
    ('subject.kind', 'subject_mismatch'),
    ('subject.identifier', 'subject_mismatch'),
    ('semantic_input.card_type', 'event_mismatch'),
    ('semantic_input.body.goal_context', 'subject_mismatch'),
    ('semantic_input.body.development', 'date_mismatch'),
    ('semantic_input.body.current_result', 'date_mismatch'),
    ('semantic_input.body.unfinished_future', 'confirmation_mismatch'),
    ('semantic_input.body.uncertainty', 'confirmation_mismatch'),
)


def _expected(text):
    if text not in (PENDING_SOURCE, CONFIRMED_SOURCE):
        return None
    pending = text == PENDING_SOURCE
    return ('user', 'synthetic-person', 'event', '合成朋友爬山',
            '原定2026-10-10', '改到2026-10-11',
            '等待朋友确认' if pending else None,
            '朋友尚未确认' if pending else None)


def _report(code, fields=None):
    return {'diagnostic_version': 1, 'code': code,
            'status': 'validated_synthetic_semantics' if code == 'ok' else 'blocked',
            'field_results': fields or {}, 'raw_values_retained': False,
            'retry_authorized': False}


def diagnose_batch(request, batch):
    """Inspect a validated full batch; emit only predefined field paths/codes.

    Equal field values are exact golden-fixture equality, not a general semantic
    truth check. Optional nulls have already been removed by the wire binder.
    """
    expected = _expected(request.content)
    if expected is None:
        return _report('unsupported_synthetic_source')
    try:
        if (not isinstance(batch, ProposalBatch) or len(batch.proposals) != 1
                or any(not v.valid for v in ProposalValidator.validate(request, batch))):
            return _report('invalid_synthetic_batch')
        p = batch.proposals[0]
        semantic = p.semantic_input
        if semantic is None:
            return _report('invalid_synthetic_batch')
        body = semantic.body
        values = (p.subject.kind, p.subject.identifier, semantic.card_type,
                  body.get('goal_context'), body.get('development'), body.get('current_result'),
                  body.get('unfinished_future'), body.get('uncertainty'))
        fields = {path: 'match' if actual == wanted else code
                  for (path, code), actual, wanted in zip(_FIELDS, values, expected)}
        return _report('ok' if all(v == 'match' for v in fields.values())
                       else 'synthetic_semantic_mismatch', fields)
    except Exception:
        return _report('invalid_synthetic_batch')


class SyntheticSemanticFailure(ProviderFailure):
    def __init__(self, report):
        self.diagnostic = report
        super().__init__(report['code'])


class CheckedSyntheticAdapter:
    """One small opt-in seam before the existing validation/confirmation path."""
    production_ready = False

    def __init__(self, adapter):
        self._adapter = adapter

    def extract(self, request):
        if _expected(request.content) is None:
            raise SyntheticSemanticFailure(_report('unsupported_synthetic_source'))
        batch = self._adapter.extract(request)
        report = diagnose_batch(request, batch)
        if report['code'] != 'ok':
            raise SyntheticSemanticFailure(report)
        return batch
