from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("target", ["refresh"])
def test_retired_refresh_cannot_infer(target):
    """`make refresh` re-ran published models at full price; it stays disabled.
    `make live` is the sanctioned lane for vendors without a batch route and is
    covered by the roster guard (`require-models`) instead."""
    root = Path(__file__).resolve().parents[1]
    args = ["make", target, "MODELS=not-an-authorized-model"]
    preview = subprocess.run(
        args + ["--dry-run"], cwd=root, capture_output=True, text=True,
    )
    assert "with_env.sh" not in preview.stdout
    assert "src.run" not in preview.stdout
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Disabled" in result.stderr and "resamples" in result.stderr
