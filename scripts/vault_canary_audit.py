"""Plant a token in a sealed vault and hunt for it everywhere it should not be.

    # 1. plant: needs an unlocked sealed collection
    python -m scripts.vault_canary_audit --plant 3

    # 2. exercise the system — capture, download, lock, unlock, share

    # 3. scan
    python -m scripts.vault_canary_audit --canary NSECANARY1-3-<hex>
    python -m scripts.vault_canary_audit --last          # the token from the last plant

Exit code is 1 when a token is found in the clear, 2 when the scan could not
reach a surface it was asked to check. Those are different failures and both are
worth a non-zero exit: a leak is a finding, and a scan that quietly skipped half
the system is not evidence of anything.

`--scan-all` re-reads every token this tool ever planted from its ledger instead
of one token, which is what you want after an upgrade rather than after a single
incident.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

LEDGER = Path("/tmp/netsanctum-staging/canary-ledger.json")


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    try:
        return json.loads(LEDGER.read_text())
    except json.JSONDecodeError:
        return []


def record(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entries = [e for e in load_ledger() if e.get("canary") != entry["canary"]]
    entries.append(entry)
    # The ledger is a list of tokens in the clear, in a tmpfs, on purpose: it is
    # itself a thing the scan must be able to read, since finding a token there
    # is the tool working rather than the tool leaking.
    LEDGER.write_text(json.dumps(entries[-200:], indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--plant", type=int, metavar="COLLECTION_ID", help="create the token card")
    parser.add_argument("--canary", action="append", default=[], help="a token to look for; repeatable")
    parser.add_argument("--last", action="store_true", help="use every token ever planted")
    parser.add_argument("--scan-all", action="store_true", help="scan every surface, not just the ones given")
    parser.add_argument("--staging", type=Path, help="the staging directory to read")
    parser.add_argument("--logs", type=Path, help="a log directory or file to read")
    parser.add_argument("--storage", type=Path, help="the storage root to read")
    parser.add_argument("--no-redis", action="store_true", help="skip the state store")
    parser.add_argument("--no-database", action="store_true", help="skip the database")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    tokens = list(args.canary)
    if args.last or args.scan_all:
        tokens += [entry["canary"] for entry in load_ledger()]
    if not tokens:
        print("nothing to look for: pass --canary, or --last after a --plant", file=sys.stderr)
        return 2

    wanted = set(tokens)

    async def run() -> int:
        from app.core.config import get_settings
        from app.modules.vault.canary import ScanReport, scan_database, scan_path, scan_state_store

        settings = get_settings()
        report = ScanReport()

        # The staging directory and the storage root first: they are on a disk,
        # they are the surfaces a stolen backup would contain, and they are the
        # ones a log-only audit never looks at.
        staging = args.staging or Path(settings.STAGING_DIR)
        scan_path("staging", staging, report)

        if args.storage:
            scan_path("storage", args.storage, report)

        if args.logs:
            scan_path("logs", args.logs, report)

        if not args.no_redis:
            await scan_state_store(report)

        if not args.no_database:
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                await scan_database(report, session)

        # Narrow the report to the tokens this run was asked about, so a stale
        # ledger entry cannot be reported against today's system.
        report.findings = [f for f in report.findings if wanted & set(f.canaries)]

        if args.json:
            print(
                json.dumps(
                    {
                        "clean": report.clean,
                        "scanned": report.scanned,
                        "unreachable": report.unreachable,
                        "findings": [asdict(f) for f in report.findings],
                    },
                    indent=2,
                )
            )
        else:
            print(f"tokens: {' '.join(sorted(wanted))}")
            print(report)
        if report.findings:
            return 1
        if report.unreachable:
            return 2
        return 0

    import asyncio

    if args.plant is not None:
        from app.core.database import AsyncSessionLocal

        async def plant() -> int:
            from app.modules.vault.canary import plant_canary

            async with AsyncSessionLocal() as session:
                planted = await plant_canary(session, args.plant)
            record(asdict(planted))
            print(f"planted {planted.canary} as item {planted.item_id} in collection {planted.collection_id}")
            print("now exercise the system, then re-run with --last")
            return 0

        return asyncio.run(plant())

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
