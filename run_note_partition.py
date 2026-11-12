#!/usr/bin/env python3
"""Resume a frozen, non-overlapping range of the original local NOTE shard.

Operational adapter for the existing NOTE production runner. Range offsets are
zero-based, stop-exclusive over the original six CSVs in source order. No model,
prompt, validation, or request policy is changed.
"""
import argparse
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import sys


LOCAL_ROWS = 353653


def validate_partition(start, stop):
    if (
        type(start) is not int
        or type(stop) is not int
        or not 0 <= start < stop <= LOCAL_ROWS
    ):
        raise ValueError("Invalid original-local-shard range")


def select_range(cases, start, stop):
    validate_partition(start, stop)
    return islice(cases, start, stop)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--partition", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.partition.read_text())
    start, stop = spec["start"], spec["stop"]
    validate_partition(start, stop)
    if not start >= spec["completed_prefix"] or not 1 <= spec["workers"] <= 8:
        raise ValueError(
            "Partition overlaps already submitted records or invalid workers"
        )
    snapshot = Path(spec["snapshot"])
    digest = hashlib.sha256()
    with snapshot.open("rb") as stream:
        for block in iter(lambda: stream.read(2**20), b""):
            digest.update(block)
    if digest.hexdigest() != spec["snapshot_sha256"]:
        raise ValueError("Frozen local ledger checksum mismatch")
    import sqlite3

    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as db:
        count, first, last = db.execute(
            "SELECT COUNT(*),MIN(position),MAX(position) FROM notes"
        ).fetchone()
        inflight = db.execute(
            "SELECT COUNT(*) FROM notes WHERE status='inflight'"
        ).fetchone()[0]
        if (count, first, last) != (
            spec["completed_prefix"],
            1,
            spec["completed_prefix"],
        ) or inflight:
            raise ValueError("Local ledger is not a settled contiguous prefix")
    root = Path(spec["output_root"]).resolve()
    frozen_path = root / "results/audit/partition_contract.json"
    if frozen_path.exists() and json.loads(frozen_path.read_text()) != spec:
        raise ValueError("Partition changed; refusing mixed-run resume")
    if args.execute:
        os.umask(0o077)
        frozen_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        frozen_path.write_text(json.dumps(spec, indent=2) + "\n")
    import run_note_module as production

    production.ROOT = root
    production.SHARDS = {"local_range": (slice(0, 6), stop - start)}
    original_cases = production.read_cases
    production.read_cases = lambda files: select_range(
        original_cases(files), start, stop
    )
    sys.argv = [
        sys.argv[0],
        "--shard",
        "local_range",
        "--source-dir",
        spec["source_dir"],
        "--reference-contract",
        spec["reference_contract"],
        "--workers",
        str(spec["workers"]),
        "--budget-usd",
        str(spec["budget_usd"]),
    ]
    if args.execute:
        sys.argv.append("--execute")
    production.main()


if __name__ == "__main__":
    main()
