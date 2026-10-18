"""What Cynitor relies on below pycyphal's public surface (F-030, Q-23).

Each test names the code that relies on it. A pycyphal release that changes
one fails here, rather than leaving that code to stop working without a word.
requirements.txt keeps pycyphal to the minor version these pass on.
"""

import inspect
import sys

import pytest

pytest.importorskip("pycyphal")


def test_the_hub_replaces_pythoncan_media_acceptance_filters():
    # can_hub._no_filters_on_virtual_channels: a method of this signature, replaced at import.
    import can_hub  # noqa: F401  (replaces it)
    from pycyphal.transport.can.media.pythoncan import PythonCANMedia
    assert PythonCANMedia.configure_acceptance_filters.__name__ == "configure_unless_virtual"
    original = inspect.getsource(sys.modules[PythonCANMedia.__module__])
    assert "def configure_acceptance_filters(self, configuration" in original


def test_the_input_session_sheds_its_queue_and_counts_what_it_shed():
    # ScannerNode._skip_backlog_if_behind sets frame_queue_capacity; dropped_frames reads drops.
    from pycyphal.transport.can._session._input import CANInputSession
    capacity = inspect.getattr_static(CANInputSession, "frame_queue_capacity")
    assert isinstance(capacity, property) and capacity.fset is not None
    from pycyphal.transport.can import CANInputSessionStatistics
    assert "drops" in inspect.signature(CANInputSessionStatistics).parameters


def test_the_input_sessions_drop_lines_come_from_the_logger_quietened():
    # scanner_node quietens this logger by name: thousands of lines a second otherwise.
    from pycyphal.transport.can._session import _input
    assert _input._logger.name == "pycyphal.transport.can._session._input"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pycyphal's SocketCAN media is Linux's")
def test_the_error_frame_lines_filtered_are_socketcan_medias():
    # bus_errors filters these lines, by their text, from this logger.
    from pycyphal.transport.can.media.socketcan import _socketcan
    from bus_errors import _ERROR_FRAME_LINES
    assert _socketcan._logger.name == "pycyphal.transport.can.media.socketcan._socketcan"
    source = inspect.getsource(_socketcan)
    assert all(f'"{line}"' in source for line in _ERROR_FRAME_LINES)


def test_a_send_is_completed_from_the_transmit_thread_as_the_quiet_handler_expects():
    # main._quiet_completion_of_cancelled_futures recognises partial(future.set_result, ...).
    from pycyphal.transport.can.media.pythoncan import _pythoncan
    source = inspect.getsource(_pythoncan)
    assert "call_soon_threadsafe(partial(tx.future.set_result, None))" in source
    assert "call_soon_threadsafe(partial(tx.future.set_exception, ex))" in source
