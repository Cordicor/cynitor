"""Tests for CANSession.rescan_registrations — the post-compile rescan hook."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch


@pytest.fixture
def session():
    """A CANSession with scanner mocked to a populated state."""
    from main import CANSession
    s = CANSession()
    scanner = MagicMock()
    scanner.service_metadata = {
        (42, 100): {"namespace": "uavcan.node", "service_name": "GetInfo_1_0"},
        (42, 81): {"namespace": "dontpanic", "service_name": "SumService_1_0", "unavailable": True},
        (43, 81): {"namespace": "dontpanic", "service_name": "SumService_1_0", "unavailable": True},
    }
    s.scanner = scanner
    s.registered_nodes.update({42, 43, 77})
    return s


class TestRescanRegistrations:

    def test_clears_registered_nodes_set(self, session):
        assert session.registered_nodes == {42, 43, 77}
        session.rescan_registrations()
        assert session.registered_nodes == set()

    def test_drops_unavailable_service_metadata(self, session):
        session.rescan_registrations()
        meta = session.scanner.service_metadata
        assert (42, 81) not in meta, "unavailable entry should be evicted"
        assert (43, 81) not in meta, "unavailable entry should be evicted"
        assert (42, 100) in meta, "healthy entry should be preserved"

    def test_no_op_when_not_running(self):
        from main import CANSession
        s = CANSession()
        assert s.scanner is None
        s.registered_nodes.update({1, 2, 3})
        s.rescan_registrations()
        # Should not raise and should not touch the set (no scanner = no work to do)
        assert s.registered_nodes == {1, 2, 3}

    def test_no_op_when_scanner_missing(self):
        from main import CANSession
        s = CANSession()
        s.scanner = None
        s.registered_nodes.update({1})
        s.rescan_registrations()
        assert s.registered_nodes == {1}


class FakeHub:
    """Stands in for CANHub so that no test opens a real adapter."""

    def __init__(self, spec, bitrate, data_bitrate=None, open_bus=None):
        self.spec = spec
        self.bitrate = bitrate
        self.data_bitrate = data_bitrate
        self.open_bus = open_bus
        self.local_spec = "virtual:fake-hub"
        self.error = None
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def health(self):
        return self.error


@pytest.fixture
def hubs(monkeypatch):
    import main
    created = []

    def make_hub(spec, bitrate, data_bitrate=None, open_bus=None):
        created.append(FakeHub(spec, bitrate, data_bitrate, open_bus))
        return created[-1]

    monkeypatch.setattr(main, "CANHub", make_hub)
    monkeypatch.setattr(main, "pick_free_node_id", lambda *args, **kwargs: 42)
    monkeypatch.setattr(main, "ensure_libusb_on_path", lambda: None)
    monkeypatch.setenv("UAVCAN__NODE__ID", "7")  # skip picking unless a test clears it
    return created


@pytest.fixture
def captured(monkeypatch, hubs):
    import main
    calls = []

    def fake_prepare_runtime(*args):
        calls.append(args)
        # Stop before any component tries to open a real bus.
        raise RuntimeError("stop after prepare_runtime")

    monkeypatch.setattr(main, "prepare_runtime", fake_prepare_runtime)
    return calls


class TestConnectBitrate:
    """CANSession.connect hands one validated bitrate to prepare_runtime."""

    async def test_uses_session_default_when_none_given(self, captured, hubs):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0")
        assert (hubs[0].spec, hubs[0].bitrate) == ("gs_usb:0", 250_000)
        assert captured == [(False,)]  # compiling only: the rates stay with the session

    async def test_explicit_bitrate_wins(self, captured, hubs):
        from main import CANSession
        s = CANSession(default_bitrate=250_000)
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0", bitrate=125_000)
        assert hubs[0].bitrate == 125_000
        assert captured == [(False,)]

    async def test_invalid_bitrate_fails_before_prepare_runtime(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError):
            await CANSession().connect("gs_usb:0", bitrate=0)
        assert captured == [] and hubs == []

    async def test_adapter_without_any_bitrate_fails_before_prepare_runtime(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError, match="bitrate is required"):
            await CANSession().connect("gs_usb:0")
        assert captured == [] and hubs == []

    async def test_socketcan_needs_no_bitrate(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("vcan0")
        assert captured == [(False,)]
        assert hubs == []  # SocketCAN keeps its direct path

    async def test_socketcan_ignores_a_given_bitrate(self, captured, hubs):
        # The kernel's setting applies; passing one on would only mislead.
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession(default_bitrate=250_000).connect("vcan0")
        assert captured == [(False,)]

    def test_invalid_default_is_rejected_up_front(self):
        from main import CANSession
        with pytest.raises(ValueError):
            CANSession(default_bitrate=0)


class TestConnectSimulatedBus:
    """A simulated bus is opened through the hub, at its own rates, its wire as the adapter."""

    @pytest.mark.parametrize("spec, rates", [("sim:classic", (1_000_000, None)),
                                             ("sim:fd", (1_000_000, 4_000_000))])
    async def test_through_the_hub_at_its_own_rates(self, captured, hubs, spec, rates):
        from main import CANSession
        from simulation import SimulatedBus
        with pytest.raises(RuntimeError, match="stop"):
            # Given rates are ignored, as the kernel's are SocketCAN's.
            await CANSession().connect(spec, bitrate=250_000, data_bitrate=2_000_000)
        assert (hubs[0].spec, hubs[0].bitrate, hubs[0].data_bitrate) == (spec, *rates)
        wire = hubs[0].open_bus.__self__
        assert isinstance(wire, SimulatedBus) and (wire.bitrate, wire.data_bitrate) == rates
        assert captured == [(False,)]

    async def test_needs_no_bitrate(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("sim:classic")
        assert hubs[0].bitrate == 1_000_000

    async def test_unknown_simulated_bus_is_refused_before_opening(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError, match="no simulated bus 'warp'"):
            await CANSession().connect("sim:warp")
        assert captured == [] and hubs == []

    async def test_teardown_stops_in_reverse_and_runs_every_stop_though_one_fails(self):
        # F-030 Q-20: the order is the reverse of the starts, kept in one place; a
        # stop that raises no longer leaves the components after it running.
        from main import CANSession
        s = CANSession()
        stopped = []

        def broken():
            stopped.append("broken")
            raise OSError("adapter gone")
        s._stack.callback(stopped.append, "hub")
        s._stack.callback(broken)
        s._stack.callback(stopped.append, "test nodes")
        s.registered_nodes.add(5)
        with pytest.raises(OSError, match="adapter gone"):
            await s._teardown()
        assert stopped == ["test nodes", "broken", "hub"] and s.registered_nodes == set()
        await s._teardown()  # the stack is spent: a second teardown stops nothing again
        assert stopped == ["test nodes", "broken", "hub"]

    async def test_its_devices_stop_with_the_session(self):
        from main import CANSession
        stopped = []

        class FakeSimulation:
            async def stop(self):
                stopped.append(True)

        s = CANSession()
        s.simulation = FakeSimulation()
        s._stack.push_async_callback(s._stop_simulation)  # as connect registers it
        await s._teardown()
        assert stopped == [True] and s.simulation is None


class TestTestNodes:
    """The nodes made in the Test nodes tab run on the bus the session is connected to.

    Started on a connected bus in the integration test (tests/integration/hub_session.py).
    """

    async def test_they_stop_with_the_session(self, tmp_path):
        from main import CANSession
        from testnodes import TestNodeBus
        s = CANSession(data_dir=tmp_path)
        s.test_nodes.attach(TestNodeBus(lambda node_id: None, True, set))
        s._stack.push_async_callback(s.test_nodes.detach)  # as connect registers it
        await s._teardown()
        assert s.test_nodes.bus is None

    def test_node_ids_in_use_are_cynitors_own_and_those_online(self, tmp_path):
        from types import SimpleNamespace
        from main import CANSession
        s = CANSession(data_dir=tmp_path)

        def node(appeared, disappeared):
            return SimpleNamespace(has_appeared=appeared, has_disappeared=disappeared)
        s.scanner = SimpleNamespace(node=SimpleNamespace(id=10),
                                    all_nodes={20: node(True, False), 21: node(True, True), 22: node(False, False)})
        assert s._node_ids_in_use() == {10, 20}

    @pytest.mark.parametrize("bitrate, mtu, payload", [("1000000 1000000", "8", 7), ("1000000 4000000", "64", 63)])
    async def test_behind_the_hub_they_share_its_channel(self, tmp_path, monkeypatch, bitrate, mtu, payload):
        from types import SimpleNamespace
        from main import CANSession
        from can_config import media_settings
        s = CANSession(data_dir=tmp_path)
        s.hub = SimpleNamespace(local_spec="virtual:test-node-hub")
        rates = [int(r) for r in bitrate.split()]
        s._media = media_settings("virtual:test-node-hub", rates[0], None if mtu == "8" else rates[1])
        transport = s._transport(60)
        try:
            assert transport.local_node_id == 60 and transport.protocol_parameters.mtu == payload
        finally:
            transport.close()


class TestAllocator:
    """Cynitor gives plug-and-play nodes their node-IDs only when asked to (see allocator)."""

    @pytest.fixture
    def asked(self, monkeypatch, hubs, tmp_path):
        """connect() up to the allocator's turn; lists each time it was started."""
        import main
        import scanner_node
        import telemetry_manager
        starts = []

        def stop_after_the_scanner(scanner):
            raise RuntimeError("stop after the scanner")

        monkeypatch.setattr(main, "prepare_runtime", lambda *args: None)
        monkeypatch.setattr(scanner_node, "ScannerNode", lambda register_file, transport=None: MagicMock())
        monkeypatch.setattr(main.CANSession, "_transport", lambda self, node_id: None)
        monkeypatch.setattr(main, "FirmwareServer", MagicMock())  # the real one needs compiled DSDL
        monkeypatch.setattr(telemetry_manager, "TelemetryManager", stop_after_the_scanner)
        monkeypatch.setattr(main.CANSession, "_start_allocator", lambda self: starts.append(True))
        return starts

    @pytest.mark.parametrize("default, given, starts", [
        (False, None, False),   # off unless asked for
        (False, True, True),    # for this session
        (True, None, True),     # --allocator
        (True, False, False),   # this session without it
    ])
    async def test_it_starts_only_when_asked_for(self, asked, tmp_path, default, given, starts):
        from main import CANSession
        s = CANSession(data_dir=tmp_path, default_allocator=default)
        with pytest.raises(RuntimeError, match="stop after the scanner"):
            await s.connect("gs_usb:0", bitrate=500_000, allocator=given)
        assert bool(asked) == starts

    @pytest.mark.parametrize("default, given", [(True, None), (False, True)])
    async def test_never_while_taking_no_commands(self, asked, tmp_path, default, given):
        # Giving a node its node-ID changes it: a session that changes no node runs no allocator.
        from main import CANSession
        s = CANSession(data_dir=tmp_path, default_no_commands=default)
        with pytest.raises(RuntimeError, match="stop after the scanner"):
            await s.connect("gs_usb:0", bitrate=500_000, allocator=True, no_commands=given)
        assert asked == []

    async def test_never_while_a_raw_log_plays(self, asked, tmp_path):
        from main import CANSession
        s = CANSession(data_dir=tmp_path, default_allocator=True)
        with pytest.raises(RuntimeError, match="stop after the scanner"):
            await s.connect("rawlog:bus.log", bitrate=500_000, allocator=True, offline={})
        assert asked == []

    def test_it_answers_from_the_scanners_node_with_no_node_id_seen_on_the_bus(self, monkeypatch, tmp_path):
        from types import SimpleNamespace
        import allocator
        from main import CANSession
        made = []
        monkeypatch.setattr(allocator, "NodeIdAllocator",
                            lambda node, node_ids_taken: made.append((node, node_ids_taken)) or "the allocator")

        def node(appeared, disappeared):
            return SimpleNamespace(has_appeared=appeared, has_disappeared=disappeared)
        s = CANSession(data_dir=tmp_path)
        s.scanner = SimpleNamespace(node=SimpleNamespace(id=10),
                                    all_nodes={20: node(True, False), 21: node(True, True), 22: node(False, False)})
        s._start_allocator()
        (on_node, node_ids_taken), = made
        assert s.allocator == "the allocator" and on_node is s.scanner.node
        # Node 21 is off now, and comes back on the node-ID it had.
        assert node_ids_taken() == {10, 20, 21}

    def test_not_without_a_node_id_to_answer_from(self, monkeypatch, tmp_path):
        from types import SimpleNamespace
        import allocator
        from main import CANSession
        monkeypatch.setattr(allocator, "NodeIdAllocator", lambda *args: pytest.fail("should not start"))
        s = CANSession(data_dir=tmp_path)
        s.scanner = SimpleNamespace(node=SimpleNamespace(id=None), all_nodes={})
        s._start_allocator()
        assert s.allocator is None and s.allocator_status() is None

    async def test_it_stops_with_the_session(self, tmp_path):
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        s.allocator = running = MagicMock()
        running.status.return_value = {"other_allocator": None, "allocated": 3}
        assert s.allocator_status() == {"other_allocator": None, "allocated": 3}
        s._stack.callback(s._close_allocator)  # as connect registers it
        await s._teardown()
        running.close.assert_called_once_with()
        assert s.allocator is None and s.allocator_status() is None

    def test_cynitor_uses_one_node_id(self, tmp_path):
        from types import SimpleNamespace
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        assert s._own_node_ids() == set()  # not connected
        s.scanner = SimpleNamespace(node=SimpleNamespace(id=10))
        assert s._own_node_ids() == {10}


class TestConnectCanFd:
    """A data bitrate opens a hub adapter as CAN FD; SocketCAN decides for itself."""

    async def test_data_bitrate_reaches_hub_and_runtime(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("pcan:PCAN_USBBUS1", bitrate=500_000, data_bitrate=2_000_000)
        assert hubs[0].data_bitrate == 2_000_000
        assert captured == [(False,)]

    async def test_session_default_data_bitrate(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession(default_bitrate=500_000, default_data_bitrate=4_000_000).connect("kvaser:0")
        assert hubs[0].data_bitrate == 4_000_000

    async def test_candlelight_and_slcan_adapters_run_can_fd(self, captured, hubs):
        from main import CANSession
        for spec in ("gs_usb:0", "slcan:COM5@115200"):
            with pytest.raises(RuntimeError, match="stop"):
                await CANSession().connect(spec, bitrate=500_000, data_bitrate=2_000_000)
        assert [(hub.spec, hub.data_bitrate) for hub in hubs] == [
            ("gs_usb:0", 2_000_000), ("slcan:COM5@115200", 2_000_000),
        ]

    async def test_adapter_without_can_fd_is_refused_before_opening(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError, match="cannot run CAN FD"):
            await CANSession().connect("canalystii:0", bitrate=500_000, data_bitrate=2_000_000)
        assert captured == [] and hubs == []

    async def test_data_bitrate_outside_the_adapters_presets_is_refused_before_opening(self, captured, hubs):
        from main import CANSession
        with pytest.raises(ValueError, match="2000000 or 5000000 bit/s only"):
            await CANSession().connect("slcan:COM5", bitrate=500_000, data_bitrate=4_000_000)
        assert captured == [] and hubs == []

    async def test_socketcan_ignores_a_data_bitrate(self, captured, hubs):
        from main import CANSession
        with pytest.raises(RuntimeError, match="stop"):
            await CANSession().connect("vcan0", data_bitrate=2_000_000)
        assert captured == [(False,)]

    def test_invalid_default_data_bitrate_is_rejected_up_front(self):
        from main import CANSession
        with pytest.raises(ValueError):
            CANSession(default_data_bitrate=50_000_000)


class TestConnectThroughHub:
    """Non-SocketCAN adapters are opened once, by the hub, and shared."""

    async def test_hub_is_started_and_stopped_when_setup_fails(self, captured, hubs):
        from main import CANSession
        s = CANSession()
        with pytest.raises(RuntimeError, match="stop"):
            await s.connect("gs_usb:0", bitrate=500_000)
        assert hubs[0].started and hubs[0].stopped
        assert s.hub is None

    async def test_node_id_is_picked_on_the_hub_channel(self, captured, hubs, monkeypatch):
        # `yakut accommodate` runs in a child process, which cannot see the
        # hub's in-process channel, so the session picks the node-ID itself.
        import os
        import main
        picks = []

        def fake_pick(local_spec, exclude, rng=None, fd=False):
            picks.append((local_spec, exclude))
            return 42

        monkeypatch.setattr(main, "pick_free_node_id", fake_pick)
        monkeypatch.delenv("UAVCAN__NODE__ID")
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("gs_usb:0", bitrate=500_000)
        assert picks == [("virtual:fake-hub", frozenset())]
        assert "UAVCAN__NODE__ID" not in os.environ  # the session's own, not the process's

    async def test_preset_node_id_is_kept(self, captured, hubs, monkeypatch):
        import os
        import main
        monkeypatch.setattr(main, "pick_free_node_id", lambda *a: pytest.fail("should not pick"))
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("gs_usb:0", bitrate=500_000)
        assert os.environ["UAVCAN__NODE__ID"] == "7"  # read, never written


class TestNodeIdOnSocketcan:
    """SocketCAN picks Cynitor's node-ID too, without yakut, and hears CAN FD while doing so."""

    @pytest.mark.parametrize("fd", [False, True])
    async def test_picked_from_heartbeats(self, captured, hubs, monkeypatch, fd):
        import os
        import main
        picks = []
        monkeypatch.setattr(main, "pick_free_node_id", lambda *args: picks.append(args) or 42)
        import can_config
        monkeypatch.setattr(can_config, "socketcan_supports_fd", lambda device: fd)
        monkeypatch.delenv("UAVCAN__NODE__ID")
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("vcan0")
        assert picks == [("socketcan:vcan0", frozenset(), None, fd)]
        assert "UAVCAN__NODE__ID" not in os.environ

    async def test_preset_node_id_is_kept(self, captured, hubs, monkeypatch):
        import os
        import main
        monkeypatch.setattr(main, "pick_free_node_id", lambda *a: pytest.fail("should not pick"))
        with pytest.raises(RuntimeError, match="stop"):
            await main.CANSession().connect("vcan0")
        assert os.environ["UAVCAN__NODE__ID"] == "7"


class TestNodeIdIsPickedForEachBus:
    """A node-ID Cynitor picked is the free one of the bus it was picked on, not of the next.

    It is the session's own: no process-wide variable carries it to the next
    session, nor to the processes the server starts (F-023, F-030 Q-22)."""

    async def test_picked_for_the_bus_and_kept_by_the_session_only(self, hubs, monkeypatch):
        import os
        import main
        monkeypatch.delenv("UAVCAN__NODE__ID")
        session = main.CANSession()
        assert await session._pick_node_id("socketcan:vcan0") == 42
        assert "UAVCAN__NODE__ID" not in os.environ

    async def test_node_id_the_user_set_is_used(self, hubs, monkeypatch):
        import main
        monkeypatch.setattr(main, "pick_free_node_id", lambda *a: pytest.fail("should not pick"))
        session = main.CANSession()  # UAVCAN__NODE__ID=7, from the fixture
        assert session.fixed_node_id == 7 and await session._pick_node_id("socketcan:vcan0") == 7

    async def test_listening_only_runs_anonymously(self, hubs):
        # F-032: a session that sends nothing has no node-ID, even one the user set.
        import os
        import main
        session = main.CANSession()
        assert await session._pick_node_id("socketcan:vcan0", listen_only=True) is None
        assert os.environ["UAVCAN__NODE__ID"] == "7"

    @pytest.mark.parametrize("value", ["128", "abc", "-1"])
    def test_a_node_id_out_of_range_is_ignored(self, monkeypatch, caplog, value):
        import main
        monkeypatch.setenv("UAVCAN__NODE__ID", value)
        assert main.CANSession().fixed_node_id is None and "is no node-ID" in caplog.text

    def test_another_node_on_cynitors_node_id_is_said_in_last_error(self):
        import main
        session = main.CANSession()
        session._own_node_id_taken("Another node uses Cynitor's own node-ID 42")
        assert session.last_error == "Another node uses Cynitor's own node-ID 42"


class TestSessionHealth:
    """What the register loop's watchdog asks every few seconds."""

    async def test_hub_reports_its_own_error(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "_check_can_health", lambda iface: pytest.fail("not SocketCAN"))
        s = main.CANSession()
        s.can_interface = "gs_usb:0"
        s.hub = FakeHub("gs_usb:0", 500_000)
        assert await main._session_health_error(s) is None
        s.hub.error = "gs_usb:0: receive failed: device gone"
        assert await main._session_health_error(s) == "gs_usb:0: receive failed: device gone"

    async def test_socketcan_is_checked_by_device_name(self, monkeypatch):
        # sysfs has /sys/class/net/vcan0, not .../socketcan:vcan0; checking the
        # spec reported a healthy interface as gone.
        import main
        checked = []
        monkeypatch.setattr(main, "_check_can_health", lambda iface: checked.append(iface))
        monkeypatch.setattr(main, "socketcan_supports_fd", lambda device: False)
        s = main.CANSession()
        s.can_interface = "socketcan:vcan0"
        assert await main._session_health_error(s) is None
        assert checked == ["vcan0"]

    @pytest.mark.parametrize("connected_fd, now_fd, said", [
        (True, False, "Interface vcan0 was switched to Classic CAN; connect again to use it"),
        (False, True, "Interface vcan0 was switched to CAN FD; connect again to use it"),
        (True, True, None),
        (False, False, None),
    ])
    async def test_socketcan_switched_between_fd_and_classic_ends_the_session(
            self, monkeypatch, connected_fd, now_fd, said):
        # The session's MTU is fixed when it connects. An interface switched
        # under it (`ip link set can0 down; ... fd off; ... up`, quicker than
        # the watchdog's look at it being down) went unnoticed: the session
        # kept the other MTU and the dashboard said CAN FD.
        import main
        monkeypatch.setattr(main, "_check_can_health", lambda iface: None)
        monkeypatch.setattr(main, "socketcan_supports_fd", lambda device: now_fd)
        s = main.CANSession()
        s.can_interface = "vcan0"
        s.can_fd = connected_fd
        assert await main._session_health_error(s) == said


class TestControllerState:
    """The controller's state as `ip` prints it; only one off the bus ends the session."""

    @pytest.fixture
    def ip_says(self, monkeypatch, tmp_path):
        import types
        import can_link
        import main
        (tmp_path / "can0").mkdir()
        (tmp_path / "can0" / "operstate").write_text("up\n")
        real_path = main.Path
        # The health check is main's, the diagnostics can_link's: each reads its own.
        for module in (main, can_link):
            monkeypatch.setattr(module, "Path", lambda p: tmp_path if p == "/sys/class/net" else real_path(p))
            monkeypatch.setattr(module, "IS_LINUX", True)

        def say(line):
            out = ("141: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 72 qdisc pfifo_fast state UP mode DEFAULT\n"
                   f"    link/can  promiscuity 0 minmtu 0 maxmtu 0\n    {line}\n")
            monkeypatch.setattr(main.subprocess, "run",
                                lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout=out))
        return say

    @pytest.mark.parametrize("line, state", [
        ("can state ERROR-ACTIVE restart-ms 0", "ERROR-ACTIVE"),
        # A CAN FD interface: its modes come between, and the state went unread.
        ("can <FD> state ERROR-WARNING (berr-counter tx 0 rx 126) restart-ms 0", "ERROR-WARNING"),
        ("can <LISTEN-ONLY,FD> state ERROR-PASSIVE (berr-counter tx 0 rx 135) restart-ms 0", "ERROR-PASSIVE"),
    ])
    def test_state_is_read_with_the_controllers_modes_between(self, ip_says, line, state):
        import main
        ip_says(line)
        assert main.get_can_link_diagnostics("can0")["state"] == state

    @pytest.mark.parametrize("modes", ["", "<FD> "])
    @pytest.mark.parametrize("state, said", [
        ("ERROR-WARNING", None),
        ("ERROR-PASSIVE", None),  # still passes frames: the dashboard shows the errors instead
        ("BUS-OFF", "Interface can0: CAN state is BUS-OFF"),
    ])
    def test_only_a_controller_off_the_bus_ends_the_session(self, ip_says, modes, state, said):
        import main
        ip_says(f"can {modes}state {state} (berr-counter tx 0 rx 135) restart-ms 0")
        assert main._check_can_health("can0") == said


class TestBusErrorSamples:
    """What the register loop gives the session's BusErrors every few seconds."""

    async def test_hub_counters(self):
        import main
        from bus_errors import BusErrors
        s = main.CANSession()
        s.can_interface = "gs_usb:0"
        s.hub = FakeHub("gs_usb:0", 500_000)
        s.bus_errors = BusErrors("gs_usb:0")
        for frames in (0, 25):
            s.hub.link_diagnostics = lambda frames=frames: {"adapter_error_frames": frames}
            await main._sample_bus_errors(s)
        assert s.bus_errors.status() is not None

    async def test_socketcan_by_device_name(self, monkeypatch):
        import main
        from bus_errors import BusErrors
        asked = []
        monkeypatch.setattr(main, "get_can_link_diagnostics",
                            lambda device: asked.append(device) or {"state": "ERROR-PASSIVE"})
        s = main.CANSession()
        s.can_interface = "socketcan:can0"
        s.bus_errors = BusErrors("socketcan:can0")
        await main._sample_bus_errors(s)
        assert asked == ["can0"] and s.bus_errors.status()["state"] == "ERROR-PASSIVE"

    async def test_a_failed_sample_is_no_error(self, monkeypatch):
        import main
        from bus_errors import BusErrors
        monkeypatch.setattr(main, "get_can_link_diagnostics", lambda device: 1 / 0)
        s = main.CANSession()
        s.can_interface = "can0"
        s.bus_errors = BusErrors("can0")
        await main._sample_bus_errors(s)  # the register loop goes on
        assert s.bus_errors.status() is None


class TestQuietCompletionOfCancelledFutures:
    """pycyphal completing a send whose task was cancelled is not an error worth a traceback."""

    async def test_drops_completion_of_a_cancelled_future(self, caplog):
        import functools
        import logging
        import main
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(main._quiet_completion_of_cancelled_futures)
        try:
            future = loop.create_future()
            future.cancel()
            # What pycyphal's transmit thread schedules after the send finished.
            loop.call_soon_threadsafe(functools.partial(future.set_result, None))
            with caplog.at_level(logging.ERROR, logger="asyncio"):
                await asyncio.sleep(0.05)
            assert "InvalidStateError" not in caplog.text
        finally:
            loop.set_exception_handler(None)

    def test_passes_everything_else_on(self):
        import functools
        from unittest.mock import MagicMock
        import main
        loop = MagicMock()
        live = asyncio.Future(loop=MagicMock())  # not cancelled
        cases = [
            {"message": "boom", "exception": RuntimeError("boom")},
            {"message": "x", "exception": asyncio.InvalidStateError(),
             "handle": MagicMock(_callback=functools.partial(live.set_result, None))},
            {"message": "x", "exception": asyncio.InvalidStateError(),
             "handle": MagicMock(_callback=functools.partial(print, None))},
        ]
        for context in cases:
            main._quiet_completion_of_cancelled_futures(loop, context)
        assert loop.default_exception_handler.call_count == len(cases)


class TestRegisterNodesRetry:
    """A node whose registration fails is retried later, not given up on."""

    def make_scanner(self, update_reg_list):
        node = MagicMock(node_id=42, has_disappeared=False, has_appeared=True, has_registered_ports=True)
        scanner = MagicMock()
        scanner.nodes = {42: node}
        scanner.node_service_types = {}
        scanner.update_reg_list = update_reg_list
        scanner.add_subscriptions = AsyncMock()
        scanner.add_servers = AsyncMock()
        return scanner

    @pytest.mark.asyncio
    async def test_failed_node_is_not_marked_registered(self):
        from main import register_nodes
        scanner = self.make_scanner(AsyncMock(side_effect=TimeoutError("no answer")))
        registered, retry_at = set(), {}
        await register_nodes(scanner, registered, retry_at)
        assert registered == set()
        assert 42 in retry_at

    @pytest.mark.asyncio
    async def test_retry_waits_then_succeeds(self):
        from main import register_nodes
        update = AsyncMock(side_effect=[TimeoutError("no answer"), ({1620: "t.T_1_0"}, {})])
        scanner = self.make_scanner(update)
        registered, retry_at = set(), {}
        await register_nodes(scanner, registered, retry_at)
        await register_nodes(scanner, registered, retry_at)  # still backing off
        assert update.await_count == 1
        retry_at[42] = 0.0  # backoff elapsed
        await register_nodes(scanner, registered, retry_at)
        assert registered == {42}
        assert 42 not in retry_at
        scanner.add_subscriptions.assert_awaited_once_with(42, {1620: "t.T_1_0"})


class TestAdapterOverflows:
    """Frames an adapter lost, its buffer full, count as dropped (F-042)."""

    def test_behind_the_hub_its_overflow_notices(self, tmp_path):
        from types import SimpleNamespace
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        s.hub = SimpleNamespace(rx_overflows=3)
        assert s.adapter_overflows() == 3

    def test_on_socketcan_the_growth_of_rx_over_errors_since_connect(self, tmp_path, monkeypatch):
        import main
        s = main.CANSession(data_dir=tmp_path)
        s.can_interface, s._overflows_before = "can0", 40
        monkeypatch.setattr(main, "rx_overflows", lambda device: 45 if device == "can0" else None)
        assert s.adapter_overflows() == 5
        s._overflows_before = None  # no counter to read at connect
        assert s.adapter_overflows() == 0

    def test_the_counter_is_read_from_sysfs(self, tmp_path, monkeypatch):
        import can_link
        (tmp_path / "can0" / "statistics").mkdir(parents=True)
        (tmp_path / "can0" / "statistics" / "rx_over_errors").write_text("7\n")
        real_path = can_link.Path
        monkeypatch.setattr(can_link, "Path", lambda p: tmp_path if p == "/sys/class/net" else real_path(p))
        monkeypatch.setattr(can_link, "IS_LINUX", True)
        assert can_link.rx_overflows("can0") == 7 and can_link.rx_overflows("vcan9") is None


class TestDecodeOnDemand:
    """With the switch on, only what a dashboard watches or a recording takes is decoded; the rest is counted."""

    @pytest.fixture
    def session(self, tmp_path, monkeypatch):
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        s.scanner = MagicMock(subject_types={1620: "ns.T_1_0", 1621: "uavcan.si.unit.velocity.Scalar_1_0"},
                              active_publishers={1620: {42}, 1621: {43, 44}}, queue_rates=AsyncMock())
        s.taps = []   # (what the frames go to, the kernel's filter, whether it is stopped)

        def open_tap(on_frame, can_filters=None):
            tap = [on_frame, can_filters, False]
            s.taps.append(tap)
            return lambda: tap.__setitem__(2, True)

        monkeypatch.setattr(s, "_open_capture_tap", open_tap)
        return s

    def test_off_unless_asked_for(self, tmp_path):
        from main import CANSession
        assert CANSession(data_dir=tmp_path).decode_on_demand is False
        assert CANSession(data_dir=tmp_path, decode_on_demand=True).decode_on_demand is True

    async def test_switched_on_it_counts_message_frames_and_decodes_what_is_watched(self, session):
        session.set_watched("a dashboard", [1620])
        session.scanner.set_decoding.assert_not_called()   # off: every subject is decoded
        await session.set_decode_on_demand(True)
        try:
            (on_frame, can_filters, _), = session.taps
            assert on_frame == session.on_demand.rates.on_frame
            assert can_filters == [{"can_id": 0, "can_mask": 1 << 25, "extended": True}]   # no service frames
            session.scanner.set_decoding.assert_called_with({1620})
            session.set_watched("another", [1621, 1700])
            session.scanner.set_decoding.assert_called_with({1620, 1621, 1700})
            session.set_watched("a dashboard", None)    # it has gone
            session.scanner.set_decoding.assert_called_with({1621, 1700})
        finally:
            await session.set_decode_on_demand(False)
        session.scanner.set_decoding.assert_called_with(None)
        assert session.taps[0][2] and session.on_demand.rates is None and session.on_demand._task is None
        assert session.decode_on_demand is False

    async def test_a_recording_keeps_what_it_takes_decoded(self, session):
        asked = []

        def records_subject(subject_id, node_ids, message_type):
            asked.append((subject_id, set(node_ids), message_type))
            return subject_id == 1621
        session.event_logger = MagicMock(records_subject=records_subject)
        await session.set_decode_on_demand(True)
        try:
            session.scanner.set_decoding.assert_called_with({1621})
            assert (1621, {43, 44}, "Scalar_1_0") in asked and (1620, {42}, "T_1_0") in asked
        finally:
            session.on_demand.stop()

    async def test_every_second_the_counted_rates_go_to_the_scanner(self, session, monkeypatch):
        import main
        slept = []

        async def no_wait(seconds):
            slept.append(seconds)
            if len(slept) > 2:
                raise asyncio.CancelledError
        monkeypatch.setattr(main.asyncio, "sleep", no_wait)
        session.on_demand.rates = MagicMock(sample=MagicMock(return_value={(1621, 43): (250.0, 1000.0)}))
        with pytest.raises(asyncio.CancelledError):
            await session.on_demand._loop()
        assert session.scanner.queue_rates.await_count == 2
        session.scanner.queue_rates.assert_awaited_with({(1621, 43): (250.0, 1000.0)})
        session.on_demand.rates = None

    async def test_not_connected_the_choice_waits_for_the_next_session(self, tmp_path):
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        await s.set_decode_on_demand(True)
        assert s.decode_on_demand is True and s.on_demand.rates is None

    async def test_a_session_that_ends_does_not_start_decoding_everything_first(self, session):
        await session.set_decode_on_demand(True)
        scanner = session.scanner
        scanner.set_decoding.reset_mock()
        session._stack.callback(session.on_demand.stop, False)  # as connect registers it
        await session._teardown()
        scanner.set_decoding.assert_not_called()
        assert session.taps[0][2] and session.on_demand.rates is None
        assert session.decode_on_demand is True   # the next session decodes on demand too

    async def test_counted_rates_are_not_written_to_history(self):
        from main import _event_logger_loop
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"subject_id": 1621, "undecoded": True, "attributes": []})
        queue.put_nowait({"subject_id": 1620, "attributes": [{"attribute": "x", "value": 1}]})
        logger = MagicMock(log_event=AsyncMock())
        task = asyncio.create_task(_event_logger_loop(logger, queue))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        logger.log_event.assert_awaited_once_with({"subject_id": 1620, "attributes": [{"attribute": "x", "value": 1}]})


class TestDroppedEvents:

    def test_none_when_not_connected(self):
        from main import CANSession
        assert CANSession().dropped_events() is None

    def test_sums_what_each_stage_dropped(self):
        from collections import defaultdict
        from main import CANSession
        s = CANSession()
        # The scanner: an event its queue had no room for, and ten frames never decoded.
        s.scanner = MagicMock(dropped_events=1, dropped_frames=MagicMock(return_value=10))
        s.telemetry = MagicMock(dropped=defaultdict(int, {"logger": 2, "client": 7}))
        s.event_logger = MagicMock(dropped_events=3)
        with patch.object(CANSession, "is_running", new_callable=PropertyMock, return_value=True):
            assert s.dropped_events() == {"scanner": 11, "logger": 5, "clients": 7}


class TestEventLogger:

    @pytest.mark.asyncio
    async def test_first_uses_at_once_start_one_logger(self, tmp_path):
        # The dashboard asks for the recordings and the buffer at once on opening the tab.
        from main import CANSession
        s = CANSession(data_dir=tmp_path)
        first, second = await asyncio.gather(s.ensure_event_logger(), s.ensure_event_logger())
        try:
            assert first is second is s.event_logger
        finally:
            await first.stop()


class TestKeepAttached:
    """--reconnect: the --can bus is attached again after it is lost (F-037)."""

    async def test_attaches_again_with_back_off_until_the_operator_disconnects(self, monkeypatch):
        import main
        waits = []

        async def no_wait(seconds):
            waits.append(seconds)
        monkeypatch.setattr(main.asyncio, "sleep", no_wait)
        session = main.CANSession()
        session.wanted_can = "can0"
        tries = []

        async def connect(iface, force_compile=False):
            tries.append(iface)
            if len(tries) < 3:
                raise OSError("No such device")
            session.wanted_can = None  # then the operator disconnects: the loop ends
        session.connect = connect
        await main._keep_attached(session, "can0")
        assert tries == ["can0"] * 3 and waits == [5, 10, 20]

