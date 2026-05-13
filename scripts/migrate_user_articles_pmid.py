#!/usr/bin/env python3
"""
Stage 1A — user_articles PMID backfill & reconciliation migration.

This script inspects, backfills, deduplicates, and reconciles the
``user_articles`` and ``library`` collections so that every user-article
record carries a canonical ``pmid`` field and no phantom duplicates exist.

By default it runs in **dry-run** mode — it reads data and prints a report
but writes nothing.  Pass ``--apply`` to actually modify documents.

Usage examples
--------------
    # Dry-run (safe — reads only)
    python scripts/migrate_user_articles_pmid.py

    # Dry-run for one specific user
    python scripts/migrate_user_articles_pmid.py --user-id abc123

    # Dry-run with a limit on documents scanned
    python scripts/migrate_user_articles_pmid.py --limit 500

    # Apply changes (writes to DB)
    python scripts/migrate_user_articles_pmid.py --apply

    # Apply changes for one user only
    python scripts/migrate_user_articles_pmid.py --apply --user-id abc123
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import motor.motor_asyncio

# ── paths ────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import shared phase logic from utils/migration_core.py
from utils.migration_core import phase_a, phase_b, phase_c, phase_d

# ── logging ──────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("migrate_pmid")


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="Stage 1A: Backfill pmid on user_articles, merge duplicates, reconcile library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually write changes (default is dry-run)")
    p.add_argument("--user-id", type=str, default=None,
                   help="Scope to a single user_id")
    p.add_argument("--limit", type=int, default=None,
                   help="Max documents to process per phase")
    p.add_argument("--mongo-url", type=str,
                   default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    p.add_argument("--db-name", type=str,
                   default=os.environ.get("DB_NAME", "litpulse_db"))
    p.add_argument("--phases", type=str, default="ABCD",
                   help="Which phases to run, e.g. 'AB' or 'CD' (default: ABCD)")
    return p.parse_args()


async def main():
    args = parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"

    print("=" * 60)
    print(f"Stage 1A Migration — {mode}")
    print(f"  DB: {args.db_name}")
    print(f"  User filter: {args.user_id or 'all users'}")
    print(f"  Limit: {args.limit or 'none'}")
    print(f"  Phases: {args.phases}")
    print("=" * 60)

    client = motor.motor_asyncio.AsyncIOMotorClient(args.mongo_url)
    db = client[args.db_name]

    user_filter: dict = {}
    if args.user_id:
        user_filter = {"user_id": args.user_id}

    all_stats: dict = {}

    # Phase A: Inspect
    if "A" in args.phases.upper():
        print("\n── Phase A: Inspect & Classify ──")
        stats_a = await phase_a(db, user_filter)
        all_stats["A"] = stats_a
        for k, v in stats_a.items():
            print(f"  {k}: {v}")

    # Phase B: Backfill pmid
    if "B" in args.phases.upper():
        print(f"\n── Phase B: Backfill pmid ({mode}) ──")
        stats_b = await phase_b(db, user_filter, apply=args.apply, limit=args.limit)
        all_stats["B"] = stats_b
        for k, v in stats_b.items():
            print(f"  {k}: {v}")

    # Phase C: Merge duplicates
    if "C" in args.phases.upper():
        print(f"\n── Phase C: Merge duplicate user_articles ({mode}) ──")
        stats_c = await phase_c(db, user_filter, apply=args.apply, limit=args.limit)
        all_stats["C"] = stats_c
        for k, v in stats_c.items():
            print(f"  {k}: {v}")

    # Phase D: Reconcile library
    if "D" in args.phases.upper():
        print(f"\n── Phase D: Reconcile library ({mode}) ──")
        stats_d = await phase_d(db, user_filter, apply=args.apply, limit=args.limit)
        all_stats["D"] = stats_d
        for k, v in stats_d.items():
            print(f"  {k}: {v}")

    # Summary
    print("\n" + "=" * 60)
    print(f"SUMMARY ({mode})")
    print("=" * 60)
    if not args.apply:
        print("  No changes written. Re-run with --apply to modify data.")
    else:
        total_writes = 0
        if "B" in all_stats:
            total_writes += all_stats["B"]["backfilled_from_pmid_aid"] + all_stats["B"]["backfilled_from_oid_lookup"]
        if "C" in all_stats:
            total_writes += all_stats["C"]["rows_merged"] + all_stats["C"]["rows_deleted"]
        if "D" in all_stats:
            total_writes += all_stats["D"]["ghost_library_deleted"] + all_stats["D"]["missing_library_created"] + all_stats["D"]["lib_dup_deleted"]
        print(f"  Total documents modified/created/deleted: {total_writes}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
