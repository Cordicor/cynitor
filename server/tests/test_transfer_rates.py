"""Tests for the frame-level message rates (transfer_rates.py) of subjects that are not decoded."""

from types import SimpleNamespace

import pytest

from transfer_rates import TransferRates


def frame(subject_id, node_id, tail=0xE0, *, payload=b"\x01\x02", extended=True, error=False,
          service=False, anonymous=False, v11=False, timestamp=1000.0):
    """A CAN frame as python-can gives it: a Cyphal/CAN message frame unless said otherwise."""
    can_id = (4 << 26) | (3 << 21) | (subject_id << 8) | node_id
    can_id |= (service << 25) | (anonymous << 24) | (v11 << 7)
    return SimpleNamespace(arbitration_id=can_id, data=bytes(payload) + bytes([tail]), is_extended_id=extended,
                           is_error_frame=error, timestamp=timestamp)


class Clock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rates(clock):
    return TransferRates(clock)


def second(rates, clock, frames):
    """A second of bus traffic, then the sample taken at its end."""
    for f in frames:
        rates.on_frame(f)
    clock.now += 1.0
    return rates.sample()


class TestWhatCounts:
    def test_a_transfer_is_counted_at_its_last_frame(self, rates, clock):
        multi = [frame(1620, 42, 0xA0), frame(1620, 42, 0x00), frame(1620, 42, 0x60)]  # first, middle, last
        assert second(rates, clock, multi + [frame(1620, 42)]) == {(1620, 42): (2.0, 1000.0)}

    def test_by_subject_and_by_node(self, rates, clock):
        sample = second(rates, clock, [frame(1620, 42)] * 3 + [frame(1620, 43)] + [frame(7, 42)] * 2)
        assert {key: rate for key, (rate, _) in sample.items()} == {(1620, 42): 3.0, (1620, 43): 1.0, (7, 42): 2.0}

    @pytest.mark.parametrize("other", [
        frame(1620, 42, service=True),          # a service call
        frame(1620, 42, anonymous=True),        # no node-ID to count it under
        frame(1620, 42, v11=True),              # Cyphal v1.1: another frame format
        frame(1620, 42, 0xC0),                  # DroneCAN: its toggle starts at 0
        frame(1620, 42, extended=False),        # an 11-bit ID is no Cyphal frame
        frame(1620, 42, error=True),
        SimpleNamespace(arbitration_id=frame(1620, 42).arbitration_id, data=b"", is_extended_id=True,
                        is_error_frame=False, timestamp=0.0),  # no data at all, so no tail byte
    ])
    def test_nothing_else_is_counted(self, rates, clock, other):
        assert second(rates, clock, [other]) == {}

    def test_the_time_is_that_of_the_last_transfer(self, rates, clock):
        sample = second(rates, clock, [frame(1620, 42, timestamp=1000.5), frame(1620, 42, timestamp=1000.9)])
        assert sample[(1620, 42)][1] == 1000.9


class TestRates:
    def test_over_the_last_ten_seconds(self, rates, clock):
        for _ in range(12):
            sample = second(rates, clock, [frame(1620, 42)] * 100)
        assert sample[(1620, 42)][0] == pytest.approx(100.0)
        # Twice as fast from now on: the rate follows as the window fills with it.
        sample = second(rates, clock, [frame(1620, 42)] * 200)
        assert sample[(1620, 42)][0] == pytest.approx(110.0)
        for _ in range(10):
            sample = second(rates, clock, [frame(1620, 42)] * 200)
        assert sample[(1620, 42)][0] == pytest.approx(200.0)

    def test_a_publisher_that_stopped_is_gone_after_two_seconds(self, rates, clock):
        second(rates, clock, [frame(1620, 42)] * 100)
        assert (1620, 42) in second(rates, clock, [])       # one second quiet
        assert (1620, 42) in second(rates, clock, [])       # two
        assert second(rates, clock, []) == {}
        # Back again, it starts anew rather than average over the silence.
        assert second(rates, clock, [frame(1620, 42)] * 100)[(1620, 42)][0] == pytest.approx(100.0)

    def test_a_slow_publisher_is_not_lost_between_its_transfers(self, rates, clock):
        for _ in range(4):                                   # one transfer every four seconds
            assert (1620, 42) in second(rates, clock, [frame(1620, 42)])
            for _ in range(3):
                sample = second(rates, clock, [])
                assert (1620, 42) in sample
        assert sample[(1620, 42)][0] == pytest.approx(0.25, abs=0.06)  # two or three of them in the window
        # Stopped for good, it is gone once the window holds none of it.
        for _ in range(int(TransferRates.WINDOW_S)):
            sample = second(rates, clock, [])
        assert sample == {}
