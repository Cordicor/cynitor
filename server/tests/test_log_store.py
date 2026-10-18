"""Tests for InMemoryLogStore and APILogHandler."""

import logging
import pytest
from log_store import InMemoryLogStore, APILogHandler, TokenRedactingFilter


@pytest.fixture
def store():
    return InMemoryLogStore(max_entries=100)


def _make_record(message: str, level: int = logging.INFO, name: str = "test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestInMemoryLogStore:

    def test_add_and_get(self, store):
        store.add_record(_make_record("hello"))
        logs = store.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "hello"
        assert logs[0]["level"] == "INFO"
        assert logs[0]["logger"] == "test"
        assert "timestamp" in logs[0]

    def test_level_filter_exact(self, store):
        store.add_record(_make_record("debug msg", logging.DEBUG))
        store.add_record(_make_record("info msg", logging.INFO))
        store.add_record(_make_record("error msg", logging.ERROR))

        logs = store.get_logs(level="ERROR")
        assert len(logs) == 1
        assert logs[0]["message"] == "error msg"

    def test_level_filter_case_insensitive(self, store):
        store.add_record(_make_record("warn", logging.WARNING))
        logs = store.get_logs(level="warning")
        assert len(logs) == 1

    def test_min_level_filter(self, store):
        store.add_record(_make_record("debug", logging.DEBUG))
        store.add_record(_make_record("info", logging.INFO))
        store.add_record(_make_record("warning", logging.WARNING))
        store.add_record(_make_record("error", logging.ERROR))

        logs = store.get_logs(min_level="WARNING")
        assert len(logs) == 2
        levels = {log["level"] for log in logs}
        assert levels == {"WARNING", "ERROR"}

    def test_limit(self, store):
        for i in range(10):
            store.add_record(_make_record(f"msg {i}"))
        logs = store.get_logs(limit=3)
        assert len(logs) == 3
        # Should return the last 3
        assert logs[0]["message"] == "msg 7"

    def test_limit_clamped(self, store):
        store.add_record(_make_record("msg"))
        logs = store.get_logs(limit=0)
        # limit is clamped to min 1
        assert len(logs) == 1

    def test_max_entries_enforced(self):
        store = InMemoryLogStore(max_entries=3)
        for i in range(5):
            store.add_record(_make_record(f"msg {i}"))
        logs = store.get_logs()
        assert len(logs) == 3
        assert logs[0]["message"] == "msg 2"

    def test_invalid_level_raises(self, store):
        with pytest.raises(ValueError, match="Invalid level"):
            store.get_logs(level="BOGUS")

    def test_invalid_min_level_raises(self, store):
        with pytest.raises(ValueError, match="Invalid min_level"):
            store.get_logs(min_level="BOGUS")

    def test_empty_store(self, store):
        assert store.get_logs() == []

    def test_since_gives_the_records_from_that_moment_on(self, store):
        for i, message in enumerate(["first", "second", "third"]):
            record = _make_record(message)
            record.created = 1_700_000_000.25 + i
            store.add_record(record)
        second = store.get_logs()[1]["timestamp"]
        assert [log["message"] for log in store.get_logs(since=second)] == ["second", "third"]
        assert [log["message"] for log in store.get_logs(since=second, limit=1)] == ["third"]

    def test_since_compares_moments_not_their_text(self, store):
        """A record on a whole second, and a moment given in another UTC offset."""
        for message, created in (("on the second", 1_700_000_000.0), ("half past it", 1_700_000_000.5)):
            record = _make_record(message)
            record.created = created
            store.add_record(record)
        assert [log["message"] for log in store.get_logs(since="2023-11-14T23:13:20.250000+01:00")] == ["half past it"]
        assert len(store.get_logs(since="2023-11-14T22:13:20+00:00")) == 2

    def test_invalid_since_raises(self, store):
        with pytest.raises(ValueError, match="Invalid since"):
            store.get_logs(since="yesterday")


class TestAPILogHandler:

    def test_handler_routes_to_store(self, store):
        handler = APILogHandler(store)
        record = _make_record("from handler")
        handler.emit(record)
        logs = store.get_logs()
        assert len(logs) == 1
        assert logs[0]["message"] == "from handler"

    def test_handler_keeps_http_access_lines_out(self, store):
        """aiohttp logs every request, and a dashboard asks several times a
        second: those lines would push every other record out of the store."""
        handler = APILogHandler(store)
        handler.handle(_make_record('127.0.0.1 "GET /api/nodes HTTP/1.1" 200 189', name="aiohttp.access"))
        handler.handle(_make_record("CAN connect failed", logging.ERROR, name="main"))
        assert [log["message"] for log in store.get_logs()] == ["CAN connect failed"]


class TestTokenRedactingFilter:
    """The auth token reached the console, the journal under systemd, in every ?token= request line (F-012)."""

    @pytest.mark.parametrize("line, shown", [
        ('"GET /ws?token=s3cr-et_7 HTTP/1.1" 101', '"GET /ws?token=*** HTTP/1.1" 101'),
        ('"GET /api/recordings/3/export?format=csv&token=abc HTTP/1.1" 200',
         '"GET /api/recordings/3/export?format=csv&token=*** HTTP/1.1" 200'),
        ('"GET /api/status HTTP/1.1" 200', '"GET /api/status HTTP/1.1" 200'),
    ])
    def test_a_token_in_a_url_is_masked(self, line, shown):
        record = logging.LogRecord("aiohttp.access", logging.INFO, "x.py", 1, "%s", (line,), None)
        assert TokenRedactingFilter().filter(record) and record.getMessage() == shown
