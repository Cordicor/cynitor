"""Tests for local_time: the server's local time with its UTC offset."""

import datetime

import local_time


def test_a_time_carries_its_offset():
    text = local_time.local_isoformat(1_790_000_000.0, timespec="seconds")
    parsed = datetime.datetime.fromisoformat(text)
    assert parsed.tzinfo is not None and parsed.timestamp() == 1_790_000_000.0


def test_a_time_windows_cannot_convert_still_gets_an_offset():
    # Windows raises OSError for local times near 1970, which tests use.
    class NearEpoch(datetime.datetime):
        def astimezone(self, tz=None):
            if tz is None:
                raise OSError(22, "Invalid argument")
            return super().astimezone(tz)
    text = local_time.local_isoformat(NearEpoch(1970, 1, 1, 0, 0, 5))
    assert datetime.datetime.fromisoformat(text).tzinfo is not None
