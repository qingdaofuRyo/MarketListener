"""Cross-process personal-profile capture and writer coordination."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from market_monitor.profile_gate import ProfileGateBusy, profile_read_gate, profile_write_gate


@pytest.mark.parametrize("holder,contender", [("write", profile_read_gate), ("read", profile_write_gate)])
def test_profile_gate_blocks_conflicting_processes(
    tmp_path: Path, holder: str, contender,
) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    child_code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from market_monitor.profile_gate import profile_read_gate, profile_write_gate
gate = profile_write_gate if sys.argv[3] == 'write' else profile_read_gate
with gate(Path(sys.argv[2])):
    print('READY', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(source), str(tmp_path), holder],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(ProfileGateBusy):
            with contender(tmp_path, timeout=0.1):
                pytest.fail("a conflicting profile transaction entered the gate")
    finally:
        if child.stdin is not None:
            child.stdin.write("\n")
            child.stdin.flush()
        _out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
    with contender(tmp_path, timeout=0.1):
        pass


def test_profile_gate_allows_nested_read_under_write_but_not_upgrade(tmp_path: Path) -> None:
    with profile_write_gate(tmp_path):
        with profile_read_gate(tmp_path):
            pass
    with profile_read_gate(tmp_path):
        with pytest.raises(RuntimeError, match="不能升级"):
            with profile_write_gate(tmp_path):
                pass
