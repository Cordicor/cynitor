"""Tests for the server's own CPU reading (cpu_load.py), which the dashboard shows."""

from cpu_load import ProcessCpu


class Clocks:
    """Wall and CPU time the test sets."""

    def __init__(self):
        self.wall, self.used = 100.0, 5.0

    def pass_time(self, wall: float, used: float) -> None:
        self.wall += wall
        self.used += used


def make(interval=1.0):
    clocks = Clocks()
    return ProcessCpu(interval, cpu_clock=lambda: clocks.used, clock=lambda: clocks.wall), clocks


def test_nothing_to_say_in_the_first_interval():
    cpu, clocks = make()
    clocks.pass_time(0.5, 0.4)
    assert cpu.read() is None


def test_percent_of_one_core_over_the_interval():
    cpu, clocks = make()
    clocks.pass_time(1.0, 0.25)
    assert cpu.read() == 25.0
    clocks.pass_time(2.0, 1.8)   # busier since
    assert cpu.read() == 90.0


def test_several_threads_can_pass_a_hundred():
    cpu, clocks = make()
    clocks.pass_time(1.0, 1.3)
    assert cpu.read() == 130.0


def test_readers_within_an_interval_get_the_same_figure():
    # Every dashboard's metrics loop asks, and so does /api/status.
    cpu, clocks = make()
    clocks.pass_time(1.0, 0.5)
    assert cpu.read() == 50.0
    clocks.pass_time(0.4, 0.4)
    assert cpu.read() == 50.0
    clocks.pass_time(0.6, 0.4)   # a whole interval now: 0.8 s of CPU in it
    assert cpu.read() == 80.0
