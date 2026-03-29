"""Run enrichment batches until target accepted count is reached.

Usage: uv run python scripts/batch_validate.py --target 100 --batch-size 10
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def get_tested_handles(output_base: Path) -> dict[str, str]:
    """Scan all output dirs for handles and their outcomes."""
    results = {}
    for output_dir in output_base.iterdir():
        if not output_dir.is_dir() or not output_dir.name.startswith("validation"):
            continue
        decisions_path = output_dir / "match_decisions.jsonl"
        if decisions_path.exists():
            for line in decisions_path.read_text().strip().split("\n"):
                if not line:
                    continue
                d = json.loads(line)
                results[d["handle"]] = d["outcome"]
        # Also check for Shopify JSON path (no decision log entry)
        for sub in output_dir.iterdir():
            if sub.is_dir() and sub.name not in results:
                log_path = sub / "log.json"
                if log_path.exists():
                    log = json.load(open(log_path))
                    status = log.get("status")
                    if status == "UPDATED":
                        results[sub.name] = "accept"
                    elif status:
                        results[sub.name] = status.lower()
    return results


def pick_next_batch(
    tested: dict[str, str],
    batch_size: int,
    blocked_vendors: set[str],
) -> list[str]:
    """Pick next batch of handles to test."""
    from lookout.audit.auditor import ContentAuditor
    from lookout.enrich.utils import load_vendors_config
    from lookout.store import LookoutStore

    store = LookoutStore()
    vc = load_vendors_config(Path("vendors.yaml"))
    configured = set(vc.vendors.keys())

    auditor = ContentAuditor(store, exclude_house_brands=True)
    result = auditor.audit()

    picks = []
    seen_vendors: dict[str, int] = {}
    for s in result.priority_items:
        if s.handle in tested:
            continue
        if s.vendor in blocked_vendors:
            continue
        if s.vendor not in configured:
            continue
        # Limit 3 per vendor to keep diversity
        vendor_count = seen_vendors.get(s.vendor, 0)
        if vendor_count >= 3:
            continue
        seen_vendors[s.vendor] = vendor_count + 1
        picks.append(s.handle)
        if len(picks) >= batch_size:
            break

    return picks


def run_batch(handles: list[str], output_dir: Path) -> dict[str, str]:
    """Run enrichment on a batch of handles and return outcomes."""
    import subprocess

    cmd = [
        "uv", "run", "lookout", "enrich", "run",
        "--force",
        "-o", str(output_dir),
    ]
    for h in handles:
        cmd.extend(["-h", h])

    print(f"\n{'='*60}")
    print(f"Running batch: {len(handles)} products → {output_dir.name}")
    print(f"Handles: {', '.join(handles[:5])}{'...' if len(handles) > 5 else ''}")
    print(f"{'='*60}")

    result = subprocess.run(cmd, capture_output=False, timeout=600)

    # Read outcomes
    outcomes = {}
    decisions_path = output_dir / "match_decisions.jsonl"
    if decisions_path.exists():
        for line in decisions_path.read_text().strip().split("\n"):
            if not line:
                continue
            d = json.loads(line)
            outcomes[d["handle"]] = d["outcome"]

    # Check for Shopify JSON path products (no decision log)
    for h in handles:
        if h not in outcomes:
            log_path = output_dir / h / "log.json"
            if log_path.exists():
                log = json.load(open(log_path))
                status = log.get("status")
                if status == "UPDATED":
                    outcomes[h] = "accept"
                elif status:
                    outcomes[h] = status.lower()

    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--output-base", type=Path, default=Path("output"))
    args = parser.parse_args()

    # Load blocked vendors from config instead of hardcoding
    from lookout.enrich.utils import load_vendors_config
    vc = load_vendors_config(Path("vendors.yaml"))
    blocked_vendors = {v for v, c in vc.vendors.items() if c.blocked}

    # Get already-tested handles
    tested = get_tested_handles(args.output_base)
    accepted = sum(1 for v in tested.values() if v == "accept")
    total_tested = len(tested)

    print(f"Starting state: {accepted} accepted / {total_tested} tested")
    print(f"Target: {args.target} accepted")

    batch_num = 0
    while accepted < args.target:
        batch_num += 1
        remaining = args.target - accepted

        # Pick next batch
        handles = pick_next_batch(tested, args.batch_size, blocked_vendors)
        if not handles:
            print(f"\nNo more eligible products to test!")
            break

        # Run batch
        output_dir = args.output_base / f"validation-auto-{batch_num:02d}"
        outcomes = run_batch(handles, output_dir)

        # Update tracking
        batch_accepted = 0
        batch_failed = 0
        for h, outcome in outcomes.items():
            tested[h] = outcome
            if outcome == "accept":
                batch_accepted += 1
                accepted += 1
            else:
                batch_failed += 1

        # Also mark handles that didn't appear in outcomes (e.g. not found)
        for h in handles:
            if h not in tested:
                tested[h] = "unknown"

        total_tested = len(tested)
        print(f"\nBatch {batch_num}: {batch_accepted} accepted, {batch_failed} failed")
        print(f"Running total: {accepted} accepted / {total_tested} tested ({accepted/max(total_tested,1)*100:.0f}% success rate)")
        print(f"Progress: {accepted}/{args.target} ({remaining - batch_accepted} remaining)")

    print(f"\n{'='*60}")
    print(f"DONE: {accepted} accepted / {total_tested} tested")
    print(f"Success rate: {accepted/max(total_tested,1)*100:.0f}%")

    # Summary by outcome
    from collections import Counter
    counts = Counter(tested.values())
    for outcome, count in counts.most_common():
        print(f"  {outcome}: {count}")


if __name__ == "__main__":
    main()
