"""Archived boards stay as published: every file in an archive with a manifest still hashes to
its manifest entry, every relative Markdown link resolves from the archive, and the v4.8
archive keeps its round-one preview tables."""
import hashlib
import json
from pathlib import Path
import re

import pytest

HISTORY = Path(__file__).resolve().parent.parent / "docs" / "history"
ARCHIVES = sorted(p.parent for p in HISTORY.glob("v4.*/manifest.json"))
LINK = re.compile(r"\]\(([^)\s]+)\)")
EXTERNAL = re.compile(r"^(?:[a-z]+:|#|mailto:)")


@pytest.mark.parametrize("archive", ARCHIVES, ids=lambda p: p.name)
def test_every_archived_file_matches_its_manifest(archive):
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest
    for rel, digest in manifest.items():
        assert hashlib.sha256((archive / rel).read_bytes()).hexdigest() == digest, rel


@pytest.mark.parametrize("archive", ARCHIVES, ids=lambda p: p.name)
def test_every_relative_link_resolves_from_the_archive(archive):
    for page in archive.glob("*.md"):
        for target in LINK.findall(page.read_text()):
            path = target.partition("#")[0]
            if EXTERNAL.match(target) or not path:
                continue
            assert (archive / path).resolve().exists(), f"{page.name}: {target}"


def test_the_v48_archive_keeps_its_preview_tracks():
    archive = HISTORY / "v4.8"
    assert "Archived: the v4.8 board" in (archive / "README.md").read_text()
    assert "## Preview tracks" in (archive / "FINDINGS.md").read_text()
    previews = json.loads((archive / "previews.json").read_text())
    assert previews and "Claim check" in (archive / "docs" / "index.html").read_text()
