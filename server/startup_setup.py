#!/usr/bin/env python3

import argparse
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _prepend_env_path(var_name: str, path: Path) -> None:
    resolved = str(path.resolve())
    current = os.environ.get(var_name, "")
    parts = [item for item in current.split(os.pathsep) if item]
    if resolved not in parts:
        os.environ[var_name] = os.pathsep.join([resolved, *parts]) if parts else resolved


def _ensure_sys_path(path: Path) -> None:
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def _find_nnvg() -> str | None:
    """nnvg on PATH, or beside the interpreter: a virtual environment's
    python run by its path, without activating it, has no Scripts/bin on PATH."""
    return shutil.which("nnvg") or shutil.which("nnvg", path=str(Path(sys.executable).parent))


def _run_nnvg(args: list[str], description: str) -> None:
    nnvg = _find_nnvg()
    if nnvg is None:
        logger.warning("nnvg is not available; skipping DSDL compilation step: %s", description)
        return

    try:
        result = subprocess.run(
            [nnvg, *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("Failed to run nnvg for %s: %s", description, exc)
        return

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        logger.warning(
            "nnvg failed for %s (exit=%s)%s",
            description,
            result.returncode,
            f": {stderr}" if stderr else "",
        )


def resolve_project_root() -> Path:
    """
    Directory that holds dsdl_messages/ and python_compiled_messages/.

    Under PyInstaller the source tree does not exist on disk: bundled data is
    unpacked to a temporary directory that the frozen binary reports through
    sys._MEIPASS. Everywhere else the root is the parent of server/.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def ensure_libusb_on_path() -> None:
    """Let pyusb find libusb on Windows, where it is not a system library.

    python-can's gs_usb interface (candleLight adapters such as the CANable)
    reaches the adapter through pyusb, which looks for libusb-1.0.dll on PATH
    and fails with NoBackendError when it is missing. The libusb-package wheel
    bundles the DLL; putting its directory on PATH covers this process and the
    `yakut accommodate` child alike.
    """
    if sys.platform != "win32":
        return
    try:
        import libusb_package
    except ImportError:
        return
    dll_path = libusb_package.get_library_path()
    if dll_path:
        _prepend_env_path("PATH", Path(dll_path).parent)


def prepare_runtime(force_compile: bool = False) -> None:
    """Compile the DSDL if it is not yet (or ``force_compile``), and make it importable.

    The process-wide part of a CAN session's preparation: where the compiled
    types are, for this process and the children it starts. What a session
    opens, at which bitrate and node-ID, is the session's own (main.CANSession).
    """
    project_root = resolve_project_root()
    dsdl_dir = project_root / "dsdl_messages"
    public_types_dir = dsdl_dir / "public_regulated_data_types"
    uavcan_dir = public_types_dir / "uavcan"
    reg_dir = public_types_dir / "reg"
    python_output_dir = project_root / "python_compiled_messages"

    already_compiled = all(
        (python_output_dir / name).is_dir() for name in ("uavcan", "reg")
    )

    should_compile = force_compile or not already_compiled
    if not should_compile:
        logger.info("DSDL already compiled in %s — skipping (use --recompile to force)", python_output_dir)

    if should_compile:
        _run_nnvg(
            [
                "--target-language", "py",
                str(reg_dir),
                "--lookup-dir", str(uavcan_dir),
                "--outdir", str(python_output_dir),
            ],
            "reg messages",
        )
        _run_nnvg(
            [
                "--target-language", "py",
                str(uavcan_dir),
                "--lookup-dir", str(reg_dir),
                "--outdir", str(python_output_dir),
            ],
            "uavcan messages",
        )

    _prepend_env_path("CYPHAL_PATH", public_types_dir)
    os.environ["PYCYPHAL_PATH"] = str(python_output_dir.resolve())
    _prepend_env_path("PYTHONPATH", python_output_dir)
    _ensure_sys_path(python_output_dir)

    ensure_libusb_on_path()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prepare runtime environment and compile DSDL messages")
    parser.add_argument(
        "--recompile",
        action="store_true",
        help="Force DSDL recompilation even if already compiled",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    # A build step: it compiles, and opens no bus.
    prepare_runtime(force_compile=args.recompile)
    compiled = resolve_project_root() / "python_compiled_messages"
    missing = [name for name in ("uavcan", "reg") if not (compiled / name).is_dir()]
    if missing:
        logger.error("DSDL not compiled: %s missing in %s (is nnvg installed? pip install nunavut)",
                     ", ".join(missing), compiled)
        sys.exit(1)
    logger.info("DSDL compiled in %s", compiled)
