"""The real-stack scenarios under pytest: `pytest tests/integration` runs each as a test of its own.

Each scenario is a script that runs a whole CAN session in order (connect,
discover, record, update, reconnect, play back...), each step on what the
ones before left, and stops at the first check that fails; that is why
they stay scripts, which CI runs directly. Here each runs in a process of
its own, as there, and its output is shown when it fails.

Needs the server's requirements and the compiled DSDL (python
server/startup_setup.py --recompile); skipped without them.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

SCENARIOS = {
    "hub session, Classic CAN": ["hub_session.py"],
    "hub session, CAN FD": ["hub_session.py", "--fd"],
    "API walk of a running server": ["api_walk.py"],
}


def _compiled() -> bool:
    return (ROOT / "python_compiled_messages" / "uavcan").is_dir()


@pytest.mark.skipif(not _compiled(), reason="compiled DSDL missing: python server/startup_setup.py --recompile")
@pytest.mark.parametrize("args", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_scenario(args):
    result = subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]], cwd=ROOT,
                            capture_output=True, text=True, timeout=600)
    output = result.stdout + result.stderr
    failed = [line for line in output.splitlines() if line.startswith("FAIL")]
    assert result.returncode == 0, "\n".join(failed) or output[-4000:]
