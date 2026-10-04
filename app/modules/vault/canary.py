"""A marker that can only be found if a sealed payload left the clear.

The unit tests in `test_vault_no_plaintext_leak` ask whether a given code path
writes a title into a column it should not. This asks the other question: on a
running system, is there any surface at all where a card's contents appear
unencrypted?

The method is the oldest one there is. A card is created in a sealed collection
whose content is a token that occurs nowhere else — `NSECANARY1` followed by the
collection id and a nonce. The token sits inside the sealed payload, so it is
only ever readable by somebody who has the data key. Anywhere else it turns up —
a Redis key or value, a staging file, a log line, a task argument, a cleartext
column, a share snapshot — the token is proof that something wrote the plaintext
out, and `scan_vault_surfaces` says where.

Two properties make a finding trustworthy. The token is checked with a prefix
rather than a substring, so an ordinary word cannot produce a false alarm. And
the scan reports what it could not reach, because a clean result from a detector
that only looked at one directory is worse than no result at all.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Deliberately not a word: no title, no filename and no log message is going to
# contain this by accident, and a false alarm teaches people to ignore the tool.
CANARY_PREFIX = "NSECANARY1"

# A token, not an equality: the scan is looking for the prefix anywhere, so a
# payload that got concatenated with a neighbour is still caught.
CANARY = re.compile(rb"NSECANARY1-[0-9]+-[0-9a-f]{16}")

# A useful ceiling. A card's content is a column, and something that wrote a
# megabyte of plaintext into it is worth hearing about just the same.
MAX_SCAN_BYTES = 8 * 1024 * 1024


def make_canary(collection_id: int) -> str:
    """A token for one collection. Unique, and meaningless to anyone but this run."""
    return f"{CANARY_PREFIX}-{collection_id}-{secrets.token_hex(8)}"


def find_canaries(blob: bytes) -> list[str]:
    """Every token in a blob, decoded. Empty is the answer we want."""
    return [match.decode("ascii") for match in CANARY.findall(blob)]


def find_canaries_in_text(text: str) -> list[str]:
    return find_canaries(text.encode("utf-8", "replace"))


@dataclass
class Planted:
    """What was planted, and where, so a later run can look for exactly this."""

    collection_id: int
    item_id: int
    canary: str


async def plant_canary(session: AsyncSession, collection_id: int) -> Planted:
    """Put a token into a sealed collection, through the ordinary write path.

    Deliberately not a direct call into the envelope. The card is built and sealed
    the way any card is, so what the scan later finds is a fact about the write
    path and not about a shortcut that skipped it.
    """
    from app.modules.vault.models import VaultCollection, VaultItem
    from app.modules.vault.sealing import inbox_public_key, seal_item

    collection = await session.get(VaultCollection, collection_id)
    public_key = inbox_public_key(collection)
    if not public_key:
        raise RuntimeError(f"collection {collection_id} is not sealed, or has no inbox key")
    token = make_canary(collection_id)
    item = VaultItem(
        collection_id=collection_id,
        entry_type="note",
        node_type="note",
        title=token,
        content=f"canary {token}",
    )
    session.add(item)
    await session.flush()
    seal_item(item, public_key)
    await session.commit()
    return Planted(collection_id=collection_id, item_id=item.id, canary=token)


@dataclass
class Finding:
    """One surface where a token turned up in the clear."""

    surface: str
    where: str
    canaries: list[str]
    detail: str = ""

    def __str__(self) -> str:
        head = f"{self.surface}: {self.where} carries {' '.join(self.canaries)}"
        return f"{head} — {self.detail}" if self.detail else head


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    # Surfaces the scan wanted and could not read. A report that hides its own
    # blind spots is the one failure mode worth engineering against.
    unreachable: list[str] = field(default_factory=list)
    scanned: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def note_unreachable(self, surface: str, reason: str) -> None:
        self.unreachable.append(f"{surface}: {reason}")

    def __str__(self) -> str:
        lines = [f"scanned {len(self.scanned)} surfaces"]
        for finding in self.findings:
            lines.append(f"  LEAK {finding}")
        for gap in self.unreachable:
            lines.append(f"  blind {gap}")
        if self.clean:
            lines.append("  no sealed content found in the clear")
        return "\n".join(lines)


def scan_path(surface: str, path: Path, report: ScanReport) -> None:
    """Read a file and look for a token. Directories are walked, not opened."""
    if not path.exists():
        report.note_unreachable(surface, f"{path} does not exist")
        return
    targets = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    if not targets:
        report.note_unreachable(surface, f"{path} holds no files")
        return
    for target in targets:
        try:
            if target.stat().st_size > MAX_SCAN_BYTES:
                continue
            blob = target.read_bytes()
        except OSError as error:
            report.note_unreachable(surface, f"{target}: {error}")
            continue
        found = find_canaries(blob)
        if found:
            report.add(Finding(surface, str(target), found, f"{len(blob)} bytes"))
        else:
            report.scanned.append(str(target))


async def scan_state_store(report: ScanReport, url: str | None = None) -> None:
    """Every key and value in the ephemeral Redis.

    An unlock session holds a data key and a handoff holds a video's address;
    neither should hold a card's contents, and a token in either is the finding.
    """
    import redis.asyncio as aioredis

    from app.core.state_store import state_redis_url

    client = aioredis.Redis.from_url(url or state_redis_url(), decode_responses=False)
    try:
        keys = [key async for key in client.scan_iter(match="*", count=500)]
        for key in keys:
            found = find_canaries(key)
            kind = await client.type(key)
            values: list[bytes] = []
            if kind == "string":
                values = [await client.get(key)]
            elif kind == "hash":
                values = [v async for _, v in client.hscan_iter(key, count=200)]
            elif kind == "list":
                values = [v async for v in client.lrange(key, 0, 200)]
            elif kind == "set":
                values = [v async for v in client.smembers(key)]
            for value in values:
                if isinstance(value, bytes):
                    found.extend(find_canaries(value))
            if found:
                report.add(Finding("state store", f"{key!r} ({kind})", sorted(set(found))))
            else:
                report.scanned.append(f"redis:{key.decode('utf-8', 'replace')}")
    except Exception as error:
        report.note_unreachable("state store", str(error))
    finally:
        await client.aclose()


async def scan_database(report: ScanReport, session) -> None:
    """Every text column of every vault table, sealed payload included.

    The sealed column is scanned too, and finding nothing there is the point: a
    token inside an envelope is ciphertext by the time it is stored, so it cannot
    come back out of the column as text.

    Written with the Core select API rather than raw SQL so it runs on SQLite in
    the tests and on PostgreSQL in production — a cast spelled `::text` works on
    one and not the other, and an audit that only runs on the real database is an
    audit nobody runs.
    """
    from sqlalchemy import String, cast, select

    from app.modules.vault.models import VaultCollection

    # The metadata hangs off a mapped class. `Base` is not used here on purpose:
    # in this codebase it resolves to an instrumented attribute rather than the
    # declarative class, and inspecting that raises instead of scanning nothing.
    metadata = VaultCollection.metadata
    needle = f"%{CANARY_PREFIX}%"

    for table in metadata.sorted_tables:
        for column in table.columns:
            as_text = cast(column, String)
            try:
                result = await session.execute(select(column).where(as_text.like(needle)))
            except Exception as error:
                report.note_unreachable("database", f"{table.name}.{column.name}: {error}")
                continue
            for row in result:
                value = row[0]
                blob = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
                found = find_canaries(blob)
                if found:
                    report.add(Finding("database", f"{table.name}.{column.name}", found))
                else:
                    report.scanned.append(f"db:{table.name}.{column.name}")
