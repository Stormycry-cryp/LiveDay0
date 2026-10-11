class LiveDay0Error(Exception):
    """Base exception for the memory core."""


class NotFound(LiveDay0Error):
    pass


class VersionConflict(LiveDay0Error):
    pass


class IdempotencyConflict(VersionConflict):
    """A source or delta key cannot claim equivalence to another/unknown request."""


class DeletedSource(VersionConflict):
    """A deleted source or consumed re-observation intent cannot be replayed."""


class InterpretationRevoked(VersionConflict):
    """Source remains stored, but automatic interpretation/replay was revoked."""


class ReceiptUnavailable(IdempotencyConflict):
    """The original receipt was never persisted; current links cannot reconstruct it."""


class AuthorizationRequired(VersionConflict):
    """Explicit recovery needs a trusted host's check of a real, bounded user intent."""


class SnapshotInvalidated(LiveDay0Error):
    pass


class UnsafeOverlay(LiveDay0Error):
    pass


class RecallLimitError(LiveDay0Error):
    """Static, content-free limit outcome; no usable memory context accompanies it."""
    code = 'recall_limit_error'

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class RecallBudgetExceeded(RecallLimitError):
    code = 'recall_budget_exceeded'


class RecallWorkLimitExceeded(RecallLimitError):
    code = 'recall_work_limit_exceeded'


class RecallDeadlineExceeded(RecallLimitError):
    code = 'recall_deadline_exceeded'


class RecallDeadlineUnavailable(RecallDeadlineExceeded):
    code = 'recall_deadline_unavailable'
