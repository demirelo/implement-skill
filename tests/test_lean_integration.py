import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "implement" / "scripts"))
from gate import detect_adapter, run_gate
from lean_support import preflight_lean


HAS_LEAN = all(shutil.which(x) for x in ("elan", "lake", "lean"))


@pytest.mark.skipif(not HAS_LEAN, reason="Lean toolchain is not installed")
def test_real_lake_gate_catches_oracle_outside_default_build_targets(tmp_path):
    installed = subprocess.run(["elan", "toolchain", "list"], capture_output=True,
                               text=True, check=True).stdout.splitlines()
    exact = next((line.split(" (", 1)[0].strip() for line in installed
                  if "leanprover/lean4:v" in line), None)
    if exact is None:
        pytest.skip("no exact leanprover/lean4:v* toolchain is installed")

    (tmp_path / "lean-toolchain").write_text(exact + "\n")
    (tmp_path / "lakefile.toml").write_text(
        'name = "HarnessLeanFixture"\nversion = "0.1.0"\n'
        'defaultTargets = ["HarnessLeanFixture"]\n\n'
        '[[lean_lib]]\nname = "HarnessLeanFixture"\n'
    )
    (tmp_path / "HarnessLeanFixture.lean").write_text("def fixtureValue : Nat := 4\n")
    (tmp_path / "Tests").mkdir()
    oracle = tmp_path / "Tests" / "Smoke.lean"
    oracle.write_text("import HarnessLeanFixture\n#check missingCertificate\n")

    preflight_lean(tmp_path)
    adapter = detect_adapter(tmp_path)
    red = run_gate(tmp_path, adapter)
    assert red.passed is False and red.failing_tests == ["Tests/Smoke.lean"]

    oracle.write_text("import HarnessLeanFixture\n#check fixtureValue\n")
    green = run_gate(tmp_path, adapter)
    assert green.passed is True and green.verified_count == 1
