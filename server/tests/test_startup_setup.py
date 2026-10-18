"""Tests for startup_setup: project-root resolution and the CAN runtime environment."""

import os
import sys
import types
from pathlib import Path

import pytest

import startup_setup
from startup_setup import prepare_runtime, resolve_project_root


class TestResolveProjectRoot:
    def test_resolves_to_repository_root_from_source(self):
        root = resolve_project_root()
        # The root is the parent of server/, so it holds the DSDL definitions.
        assert (root / "dsdl_messages").is_dir()
        assert (root / "server" / "startup_setup.py").is_file()

    def test_uses_meipass_when_frozen(self, monkeypatch, tmp_path):
        # PyInstaller unpacks bundled data to sys._MEIPASS; the source tree is
        # absent in a frozen binary, so the root must follow the extraction dir.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() == Path(tmp_path)

    def test_ignores_meipass_when_not_frozen(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() != Path(tmp_path)


@pytest.fixture
def runtime_env(monkeypatch):
    """Run prepare_runtime without compiling DSDL, and undo its env changes."""
    for name in ("CYPHAL_PATH", "PYCYPHAL_PATH", "PYTHONPATH", "PATH"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(startup_setup, "_run_nnvg", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return monkeypatch


class TestPrepareRuntime:
    def test_makes_the_compiled_types_importable(self, runtime_env):
        prepare_runtime()
        compiled = str((resolve_project_root() / "python_compiled_messages").resolve())
        assert os.environ["PYCYPHAL_PATH"] == compiled and compiled in sys.path

    def test_publishes_no_session_settings(self, runtime_env):
        # Which bus, at which rates, as which node-ID is the session's own: none
        # of it may outlive a session or reach the processes it starts (F-030, Q-22).
        before = {k: v for k, v in os.environ.items() if k.startswith("UAVCAN__")}
        prepare_runtime()
        assert {k: v for k, v in os.environ.items() if k.startswith("UAVCAN__")} == before


class TestLibusbOnPath:
    @pytest.fixture
    def fake_libusb(self, monkeypatch, tmp_path):
        dll = tmp_path / "libusb-1.0.dll"
        module = types.SimpleNamespace(get_library_path=lambda: dll)
        monkeypatch.setitem(sys.modules, "libusb_package", module)
        monkeypatch.setenv("PATH", "existing")
        return dll

    def test_prepends_dll_directory_on_windows(self, monkeypatch, fake_libusb):
        monkeypatch.setattr(sys, "platform", "win32")
        startup_setup.ensure_libusb_on_path()
        first = os.environ["PATH"].split(os.pathsep)[0]
        assert first == str(fake_libusb.parent.resolve())

    def test_no_op_off_windows(self, monkeypatch, fake_libusb):
        monkeypatch.setattr(sys, "platform", "linux")
        startup_setup.ensure_libusb_on_path()
        assert os.environ["PATH"] == "existing"

    def test_no_op_without_the_package(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "libusb_package", None)  # import raises ImportError
        monkeypatch.setenv("PATH", "existing")
        startup_setup.ensure_libusb_on_path()
        assert os.environ["PATH"] == "existing"


def test_nnvg_is_found_beside_the_interpreter_when_not_on_path(monkeypatch, tmp_path):
    # A virtual environment's python run by its path, without activating it.
    name = "nnvg.exe" if os.name == "nt" else "nnvg"
    nnvg = tmp_path / name
    nnvg.write_text("")
    nnvg.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(startup_setup.sys, "executable", str(tmp_path / "python"))

    assert Path(startup_setup._find_nnvg()) == nnvg
