"""The video ceiling and the space available to reach it must agree.

A download is staged in the worker's `/tmp` before it is moved into storage. The
code refused anything over 2 GiB while that tmpfs was 2 GiB, so a file near the
limit could only ever die with `ENOSPC` — a much worse failure than a clear
refusal, and one that looks like a disk problem rather than a limit.

These tests hold the two numbers together, which is the check that was missing.
"""

import re
import unittest
from pathlib import Path

from app.core.config import get_settings
from app.modules.vault.tasks import _human

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
TASKS = Path(__file__).resolve().parents[1] / "app" / "modules" / "vault" / "tasks.py"

_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([kmg])\s*", value, re.IGNORECASE)
    assert match, f"unparseable size {value!r}"
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def worker_tmpfs() -> int:
    """The worker's /tmp size, following the compose default rather than the shell."""
    text = COMPOSE.read_text(encoding="utf-8")
    start = text.index("\n  worker:\n")
    # `worker` is the last service block, so the section that ends it is the
    # top-level volumes list. Looking for the next service fails: `migrate` is
    # declared earlier in the file, not after it.
    block = text[start : text.index("\nvolumes:", start)]
    match = re.search(r"- /tmp:size=([^\s]+)", block)
    assert match, "the worker has no /tmp size"
    raw = match.group(1)
    fallback = raw.split(":-", 1)[-1].rstrip("}") if ":-" in raw else raw
    return parse_size(fallback)


class VideoLimitTests(unittest.TestCase):
    def test_the_default_limit_is_four_gibibytes(self):
        self.assertEqual(4 * 1024**3, get_settings().VAULT_MAX_VIDEO_BYTES)

    def test_the_limit_is_configurable_rather_than_hardcoded(self):
        # Changing it should not require editing code and rebuilding the image.
        self.assertNotRegex(TASKS.read_text(encoding="utf-8"), r"^MAX_VIDEO_BYTES\s*=", re.M)

    def test_the_refusal_names_the_limit(self):
        # A number the user can act on beats a bare "too large".
        self.assertEqual("4 GiB", _human(4 * 1024**3))
        self.assertEqual("10 MiB", _human(10 * 1024**2))
        self.assertEqual("512 MiB", _human(512 * 1024**2))


class StagingSpaceTests(unittest.TestCase):
    def test_the_worker_can_actually_hold_a_video_at_the_limit(self):
        limit = get_settings().VAULT_MAX_VIDEO_BYTES
        self.assertGreater(
            worker_tmpfs(),
            limit,
            "a download at the limit must fit in the worker's /tmp, or it fails with ENOSPC",
        )

    def test_the_headroom_is_not_a_slippery_few_megabytes(self):
        # Merging, remuxing and a thumbnail all need room alongside the file.
        self.assertGreaterEqual(worker_tmpfs(), int(get_settings().VAULT_MAX_VIDEO_BYTES * 1.25))


if __name__ == "__main__":
    unittest.main()
