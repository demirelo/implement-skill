import json
import subprocess
import sys
from pathlib import Path


def test_smoke_offline_runs_green():
    script = Path(__file__).parent.parent / "skills" / "implement" / "scripts" / "smoke.py"
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "offline"
    assert data["before_gate"] != 0
    assert data["after_gate"] == 0
    assert data["applied"] is True
