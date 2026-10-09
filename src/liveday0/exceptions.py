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


class SnapshotInvalidated(LiveDay0Error):
    pass


class UnsafeOverlay(LiveDay0Error):
    pass
