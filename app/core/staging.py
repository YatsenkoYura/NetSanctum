"""Where plaintext exists on its way to becoming a sealed file.

Every write path has a moment where the bytes are not encrypted yet: a video the
worker just downloaded, a chunk envelope being built, a screenshot being staged.
Until now that moment lived in `tempfile`'s default directory, which is whatever
`TMPDIR` says and otherwise `/tmp` — a real disk on most deployments. On a real
disk that is the most sensitive thing on the machine sitting in the clear for as
long as the write takes, and a swap file or a backup can keep a copy after it is
gone.

So the staging directory is a setting, and three things are checked rather than
assumed:

* it is created `0700` and owned by this process, so another user on the box
  cannot read what lands there;
* it is verified to be a tmpfs when the deployment asks for that, because a
  tmpfs is the only version of "temporary" that means "never touches a disk";
* files a crash left behind are collected, because an interrupted four-gigabyte
  download should not sit there forever.

`require_staging()` is called by the writers rather than trusted at import: a
deployment that mounts the wrong thing should find out on the first write, with
the reason in the message, instead of discovering it in an incident.
"""

import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

STAGING_DIRNAME = "netsanctum-staging"


class StagingError(RuntimeError):
    """The staging directory cannot be used as configured."""


def staging_dir() -> Path:
    """The configured staging directory, created `0700` if it does not exist.

    `os.makedirs` with `mode=0o700` only applies the mode to the leaf, and only
    if it creates it — so the mode is set explicitly afterwards. A directory left
    behind by an earlier run keeps whatever it had, and `exist_ok` means this is
    the path where a misconfigured deployment is noticed.
    """
    configured = Path(get_settings().STAGING_DIR or tempfile.gettempdir())
    configured.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(configured, 0o700)
    except OSError as error:
        raise StagingError(f"Could not restrict the staging directory {configured}: {error}") from error
    return configured


def is_memory_backed(path: Path) -> bool:
    """Whether this filesystem is a tmpfs or ramfs rather than a disk.

    Read from `/proc/self/mounts` by mount point, longest prefix first: a staging
    directory nested inside a tmpfs mount is memory-backed too, and comparing the
    mount point with `startswith` alone would get `/tmpfoo` wrong for `/tmp`.
    """
    try:
        mounts = Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        # No procfs: a container without it is not going to tell us anything
        # useful, so the check answers "cannot tell" rather than "no".
        return False
    resolved = path.resolve()
    best = ""
    fstype = ""
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point, kind = parts[1], parts[2]
        if (resolved == Path(mount_point) or str(resolved).startswith(mount_point.rstrip("/") + "/")) and (
            len(mount_point) > len(best)
        ):
            best, fstype = mount_point, kind
    return fstype in {"tmpfs", "ramfs"}


def require_staging() -> Path:
    """The staging directory, after checking what the deployment asked for.

    Fails loudly rather than falling back to `/tmp`: a process that cannot keep
    its staging private has no business writing a plaintext file anywhere, and
    continuing quietly is how the file ends up somewhere worse.
    """
    settings = get_settings()
    directory = staging_dir()
    if settings.STAGING_REQUIRE_TMPFS and not is_memory_backed(directory):
        raise StagingError(
            f"{directory} is not a tmpfs and STAGING_REQUIRE_TMPFS is set. Mount a tmpfs there "
            "(docker: `--tmpfs {path}:mode=0700`, compose: a `tmpfs:` entry) or unset the check."
        )
    return directory


def staging_tempfile(prefix: str, suffix: str = "") -> Path:
    """A private temporary path inside the staging directory.

    `NamedTemporaryFile(delete=False)` so the caller can hand the path to another
    layer — yt-dlp writes to a directory it opens itself — and `0600` because the
    contents are plaintext until the encryption that follows.
    """
    directory = require_staging()
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(handle)
    path = Path(name)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def staging_workdir(prefix: str) -> Path:
    """A private `0700` directory for one job's intermediate files."""
    directory = require_staging()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    os.chmod(path, 0o700)
    return path


def collect_orphans(*, min_age_minutes: int | None = None) -> int:
    """Delete staging leftovers a crash left behind. Returns how many.

    Age rather than "everything": a second worker, or a second container sharing
    the directory, is mid-write right now, and collecting its file out from under
    it would turn a recoverable crash into a corrupt download.
    """
    settings = get_settings()
    age = min_age_minutes if min_age_minutes is not None else settings.STAGING_ORPHAN_MINUTES
    directory = staging_dir()
    removed = 0
    threshold = time.time() - max(age, 0) * 60
    for child in directory.iterdir():
        try:
            if child.stat().st_mtime >= threshold:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
            removed += 1
        except OSError:
            logger.debug("could not collect the staging leftover %s", child, exc_info=True)
    return removed
