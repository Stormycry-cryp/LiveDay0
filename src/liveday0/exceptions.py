class LiveDay0Error(Exception):
    """Base exception for the memory core."""


class NotFound(LiveDay0Error):
    pass


class VersionConflict(LiveDay0Error):
    pass


class SnapshotInvalidated(LiveDay0Error):
    pass


class UnsafeOverlay(LiveDay0Error):
    pass
