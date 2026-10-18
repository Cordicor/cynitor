"""Errors a CAN session raises that its callers answer in their own way."""


class SessionBusy(RuntimeError):
    """A connect refused because a session or a replay already runs: the caller's to resolve (409)."""
