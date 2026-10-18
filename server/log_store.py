#!/usr/bin/env python3

import logging
import re
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Any


_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class InMemoryLogStore:
    """Thread-safe in-memory log storage for API retrieval."""

    def __init__(self, max_entries: int = 5000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def add_record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{''.join(traceback.format_exception(*record.exc_info)).strip()}"

        entry = {
            # Always with microseconds, so that timestamps compare as text.
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="microseconds"),
            "logger": record.name,
            "level": record.levelname,
            "message": message,
        }

        with self._lock:
            self._entries.append(entry)

    def get_logs(
        self,
        *,
        limit: int = 200,
        level: Optional[str] = None,
        min_level: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` records, oldest first; ``since`` (a timestamp
        as the records carry) leaves out those from before that moment."""
        with self._lock:
            entries = list(self._entries)

        normalized_level = level.upper() if level else None
        normalized_min_level = min_level.upper() if min_level else None
        normalized_since = self._as_stored_timestamp(since) if since else None

        if normalized_level and normalized_level not in _LEVEL_ORDER:
            raise ValueError(f"Invalid level: {level}")
        if normalized_min_level and normalized_min_level not in _LEVEL_ORDER:
            raise ValueError(f"Invalid min_level: {min_level}")

        filtered: list[dict[str, Any]] = []
        for entry in entries:
            if normalized_level and entry["level"] != normalized_level:
                continue
            if normalized_min_level and _LEVEL_ORDER[entry["level"]] < _LEVEL_ORDER[normalized_min_level]:
                continue
            if normalized_since and entry["timestamp"] < normalized_since:
                continue
            filtered.append(entry)

        safe_limit = max(1, min(limit, 5000))
        return filtered[-safe_limit:]

    @staticmethod
    def _as_stored_timestamp(moment: str) -> str:
        """An ISO 8601 moment, written as the store writes its timestamps."""
        try:
            parsed = datetime.fromisoformat(moment)
        except ValueError:
            raise ValueError(f"Invalid since: {moment}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


# A token in a URL (the dashboard sends ?token= for its WebSocket and
# downloads), up to the end of its query parameter.
_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"']+")


class TokenRedactingFilter(logging.Filter):
    """Masks ``token=<value>`` in log lines, so the auth token stays out of the console and the journal."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "token=" in message:
            record.msg, record.args = _TOKEN_IN_URL.sub(r"\1***", message), None
        return True


# aiohttp logs every HTTP request under this name. A dashboard asks several
# times a second, so within minutes these lines would be all the store holds.
_HTTP_ACCESS_LOGGER = "aiohttp.access"


class APILogHandler(logging.Handler):
    """Logging handler that writes log records into InMemoryLogStore.

    HTTP access lines are left out: they still go to the console.
    """

    def __init__(self, store: InMemoryLogStore) -> None:
        super().__init__()
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == _HTTP_ACCESS_LOGGER:
            return
        try:
            self.store.add_record(record)
        except Exception:
            self.handleError(record)
