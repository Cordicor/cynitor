"""Tests for the simulated bus: its wire, the transports its devices get, and their lifetime.

The devices are stand-ins; the demo devices themselves need the compiled DSDL,
and run in the integration test (tests/integration/hub_session.py).
"""

import asyncio

import can

from simulation import SimulatedBus


class TestWire:
    def test_each_bus_has_a_wire_of_its_own(self):
        assert SimulatedBus(1_000_000).channel != SimulatedBus(1_000_000).channel

    def test_the_hub_end_hears_what_is_sent_on_the_wire(self):
        sim = SimulatedBus(1_000_000)
        hub_end = sim.open_wire("sim:classic", 1_000_000, None)
        device = can.Bus(interface="virtual", channel=sim.channel)
        try:
            device.send(can.Message(arbitration_id=0x123, data=b"\x01"))
            got = hub_end.recv(timeout=1)
            assert got is not None and got.arbitration_id == 0x123
        finally:
            device.shutdown()
            hub_end.shutdown()


class TestTransports:
    """A frame's payload is the Cyphal MTU, less the tail byte: 7 on Classic CAN, 63 on CAN FD."""

    async def test_classic_bus(self):
        transport = SimulatedBus(1_000_000).make_transport(50)
        try:
            assert transport.local_node_id == 50 and transport.protocol_parameters.mtu == 7
        finally:
            transport.close()

    async def test_can_fd_bus(self):
        transport = SimulatedBus(1_000_000, 4_000_000).make_transport(None)  # an anonymous device
        try:
            assert transport.local_node_id is None and transport.protocol_parameters.mtu == 63
        finally:
            transport.close()


class TestDevices:
    async def test_run_on_its_wire_until_stopped(self):
        made, stopped = [], asyncio.Event()

        async def devices(make_transport):
            made.append(make_transport)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        sim = SimulatedBus(1_000_000, devices=devices)
        sim.start()
        await asyncio.sleep(0)
        assert sim.running and made == [sim.make_transport]
        await sim.stop()
        assert stopped.is_set() and not sim.running
        await sim.stop()  # idempotent

    async def test_a_failure_is_logged_not_raised(self, caplog):
        async def devices(make_transport):
            raise RuntimeError("no DSDL")

        sim = SimulatedBus(1_000_000, devices=devices)
        sim.start()
        await asyncio.sleep(0.01)
        assert not sim.running and "the demo devices stopped" in caplog.text
        await sim.stop()
