from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("target", ["live", "refresh"])
def test_live_shortcuts_cannot_infer(target):
    root = Path(__file__).resolve().parents[1]
    args = ["make", target, "MODELS=not-an-authorized-model"]
    preview = subprocess.run(
        args + ["--dry-run"], cwd=root, capture_output=True, text=True,
    )
    assert "with_env.sh" not in preview.stdout
    assert "src.run" not in preview.stdout
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0
    assert "native batch" in result.stderr
