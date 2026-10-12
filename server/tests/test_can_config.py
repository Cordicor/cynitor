"""Tests for the CAN interface-spec and bitrate rules in can_config."""

import pytest

from can_config import (
    bitrate_env_value,
    bitrate_from_env,
    fd_data_bitrates,
    is_explicit_spec,
    is_simulated,
    is_socketcan,
    media_bitrate,
    media_mtu,
    normalize_can_iface,
    resolve_bitrate,
    resolve_data_bitrate,
    socketcan_supports_fd,
    supports_fd,
    validate_bitrate,
    validate_data_bitrate,
)


class TestNormalizeCanIface:
    def test_bare_name_means_socketcan(self):
        assert normalize_can_iface("vcan0") == "socketcan:vcan0"

    @pytest.mark.parametrize("spec", [
        "socketcan:can0",
        "gs_usb:0",
        "pcan:PCAN_USBBUS1",
        "slcan:COM5@115200",
        "kvaser:0",
    ])
    def test_explicit_spec_passes_through(self, spec):
        assert normalize_can_iface(spec) == spec

    def test_strips_whitespace(self):
        assert normalize_can_iface("  gs_usb:0 \n") == "gs_usb:0"

    def test_drops_legacy_pythoncan_prefix(self):
        # The old README spelling. pycyphal would read "pythoncan" as the
        # interface name and fail, so the prefix has to go.
        assert normalize_can_iface("pythoncan:pcan:PCAN_USBBUS1") == "pcan:PCAN_USBBUS1"

    def test_legacy_prefix_on_a_bare_name_still_means_socketcan(self):
        assert normalize_can_iface("pythoncan:vcan0") == "socketcan:vcan0"


class TestIsExplicitSpec:
    def test_bare_name(self):
        assert not is_explicit_spec("vcan0")

    def test_spec(self):
        assert is_explicit_spec("gs_usb:0")


class TestValidateBitrate:
    @pytest.mark.parametrize("bitrate", [10_000, 125_000, 500_000, 1_000_000])
    def test_accepts_classic_can_rates(self, bitrate):
        assert validate_bitrate(bitrate) == bitrate

    @pytest.mark.parametrize("bitrate", [0, -500_000, 2_000_000])
    def test_rejects_out_of_range(self, bitrate):
        with pytest.raises(ValueError):
            validate_bitrate(bitrate)

    @pytest.mark.parametrize("bitrate", ["500000", 500000.0, None, True])
    def test_rejects_non_integers(self, bitrate):
        # True is an int subclass and would otherwise pass as 1 bit/s.
        with pytest.raises(ValueError):
            validate_bitrate(bitrate)


class TestBitrateEnv:
    def test_env_value_repeats_the_rate(self):
        # A single number is read by pycyphal as [n, 0]: CAN FD with a
        # zero-rate data phase.
        assert bitrate_env_value(250_000) == "250000 250000"

    def test_reads_back_what_was_published(self, monkeypatch):
        monkeypatch.setenv("UAVCAN__CAN__BITRATE", bitrate_env_value(250_000))
        assert bitrate_from_env() == 250_000

    def test_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("UAVCAN__CAN__BITRATE", raising=False)
        assert bitrate_from_env() is None

    def test_none_when_unparseable(self, monkeypatch):
        monkeypatch.setenv("UAVCAN__CAN__BITRATE", "fast")
        assert bitrate_from_env() is None


class TestIsSocketcan:
    @pytest.mark.parametrize("iface", ["vcan0", "can0", "socketcan:can0", "pythoncan:vcan0"])
    def test_socketcan(self, iface):
        assert is_socketcan(iface)

    @pytest.mark.parametrize("iface", ["gs_usb:0", "pcan:PCAN_USBBUS1", "slcan:COM5@115200"])
    def test_not_socketcan(self, iface):
        assert not is_socketcan(iface)


class TestResolveBitrate:
    def test_required_for_adapters(self):
        # No default: a guessed bitrate floods a bus running at another speed.
        with pytest.raises(ValueError, match="bitrate is required for gs_usb:0"):
            resolve_bitrate("gs_usb:0", None)

    def test_optional_for_socketcan(self):
        assert resolve_bitrate("vcan0", None) is None

    def test_given_bitrate_is_validated(self):
        assert resolve_bitrate("gs_usb:0", 250_000) == 250_000
        with pytest.raises(ValueError):
            resolve_bitrate("vcan0", 0)


class TestMediaBitrate:
    def test_uses_published_bitrate(self, monkeypatch):
        monkeypatch.setenv("UAVCAN__CAN__BITRATE", "250000 250000")
        assert media_bitrate("gs_usb:0") == 250_000

    def test_socketcan_gets_a_placeholder(self, monkeypatch):
        # PythonCANMedia needs a number even though SocketCAN ignores it.
        monkeypatch.delenv("UAVCAN__CAN__BITRATE", raising=False)
        assert isinstance(media_bitrate("socketcan:vcan0"), int)

    def test_adapter_without_published_bitrate_refuses(self, monkeypatch):
        monkeypatch.delenv("UAVCAN__CAN__BITRATE", raising=False)
        with pytest.raises(RuntimeError):
            media_bitrate("gs_usb:0")


class TestCanFd:
    @pytest.mark.parametrize("data_bitrate", [1_000_000, 2_000_000, 5_000_000, 8_000_000])
    def test_accepts_common_data_rates(self, data_bitrate):
        assert validate_data_bitrate(data_bitrate) == data_bitrate

    @pytest.mark.parametrize("data_bitrate", [0, -1, 50_000_000, "2000000", True, 2.0])
    def test_rejects_bad_data_rates(self, data_bitrate):
        with pytest.raises(ValueError):
            validate_data_bitrate(data_bitrate)

    def test_no_data_bitrate_means_classic(self):
        assert resolve_data_bitrate("pcan:PCAN_USBBUS1", None) is None

    @pytest.mark.parametrize("iface", ["pcan:PCAN_USBBUS1", "kvaser:0", "vector:0", "ixxat:0",
                                       "gs_usb:0", "slcan:COM5@115200"])
    def test_fd_capable_adapters(self, iface):
        assert resolve_data_bitrate(iface, 2_000_000) == 2_000_000
        assert supports_fd(iface)

    @pytest.mark.parametrize("iface", ["canalystii:0", "usb2can:ED000200"])
    def test_adapters_without_can_fd_are_refused(self, iface):
        with pytest.raises(ValueError, match="cannot run CAN FD"):
            resolve_data_bitrate(iface, 2_000_000)
        assert not supports_fd(iface)

    def test_slcan_runs_the_canable_firmwares_presets_only(self):
        assert fd_data_bitrates("slcan:COM5") == (2_000_000, 5_000_000)
        assert resolve_data_bitrate("slcan:COM5", 5_000_000) == 5_000_000
        with pytest.raises(ValueError, match="slcan adapters run the CAN FD data phase at "
                                             "2000000 or 5000000 bit/s only, not 4000000"):
            resolve_data_bitrate("slcan:COM5", 4_000_000)

    @pytest.mark.parametrize("iface", ["pcan:PCAN_USBBUS1", "gs_usb:0", "vcan0"])
    def test_other_interfaces_have_no_presets(self, iface):
        assert fd_data_bitrates(iface) is None

    def test_candlelight_takes_any_data_bitrate(self):
        # Its bit timing is worked out from the adapter's own clock (gs_usb_fd).
        assert resolve_data_bitrate("gs_usb:0", 8_000_000) == 8_000_000

    def test_socketcan_ignores_a_data_bitrate(self):
        # The kernel owns its rates; FD follows the interface's own setting.
        assert resolve_data_bitrate("vcan0", 2_000_000) is None

    def test_env_value_carries_both_phases(self):
        assert bitrate_env_value(500_000, 2_000_000) == "500000 2000000"

    def test_media_bitrate_is_a_pair_for_can_fd(self, monkeypatch):
        monkeypatch.setenv("UAVCAN__CAN__BITRATE", "500000 2000000")
        assert media_bitrate("virtual:hub") == (500_000, 2_000_000)
        assert bitrate_from_env() == 500_000

    def test_media_mtu_defaults_to_classic(self, monkeypatch):
        monkeypatch.delenv("UAVCAN__CAN__MTU", raising=False)
        assert media_mtu() == 8
        monkeypatch.setenv("UAVCAN__CAN__MTU", "64")
        assert media_mtu() == 64


class TestSimulatedBus:
    def test_named_by_its_interface(self):
        assert is_simulated("sim:classic") and is_simulated("sim:fd")
        assert not is_simulated("vcan0") and not is_simulated("virtual:sim")

    @pytest.mark.parametrize("iface, rates, fd", [("sim:classic", (1_000_000, None), False),
                                                  ("sim:fd", (1_000_000, 4_000_000), True)])
    def test_has_rates_of_its_own(self, iface, rates, fd):
        assert (resolve_bitrate(iface, None), resolve_data_bitrate(iface, None)) == rates
        # Given ones are checked, then ignored: only the bus-load estimate uses them.
        assert (resolve_bitrate(iface, 250_000), resolve_data_bitrate(iface, 2_000_000)) == rates
        assert supports_fd(iface) is fd

    def test_given_rates_must_still_be_valid(self):
        with pytest.raises(ValueError):
            resolve_bitrate("sim:classic", 0)
        with pytest.raises(ValueError):
            resolve_data_bitrate("sim:fd", 50_000_000)

    def test_unknown_bus(self):
        with pytest.raises(ValueError, match="no simulated bus 'warp'; there are sim:classic or sim:fd"):
            resolve_bitrate("sim:warp", None)


class TestSocketcanSupportsFd:
    @pytest.fixture
    def sysfs(self, tmp_path):
        def interface(name, mtu):
            (tmp_path / name).mkdir()
            (tmp_path / name / "mtu").write_text(f"{mtu}\n")
        interface("can0", 16)   # Classic CAN
        interface("can1", 72)   # `fd on`, or `mtu 72` for vcan
        return tmp_path

    def test_classic_interface(self, sysfs):
        assert not socketcan_supports_fd("can0", sysfs)

    def test_fd_interface(self, sysfs):
        assert socketcan_supports_fd("can1", sysfs)

    def test_missing_interface(self, sysfs):
        assert not socketcan_supports_fd("can9", sysfs)
