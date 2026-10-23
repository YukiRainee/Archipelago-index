#!/usr/bin/env python3
"""
bulk_add.py -- run add_apworld over a queue of links.

Queue: autoupdate/bulk/queue.json, a list of
    {"game": "<name as you know it>", "link": "<repo/release/.apworld url>", "home": "<optional>"}

Each entry is removed from the queue once tried, and its outcome is appended
to autoupdate/bulk/results.md. If the host rate-limits us, the untried entries
stay in the queue so the next run picks them up.

Usage:
    python autoupdate/bulk_add.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_apworld  # noqa: E402
import common  # noqa: E402

BULK_DIR = common.REPO_ROOT / "autoupdate" / "bulk"
QUEUE = BULK_DIR / "queue.json"
RESULTS = BULK_DIR / "results.md"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N entries (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queue = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else []
    if not queue:
        print("Queue is empty.")
        Path("changes_made.flag").write_text("0")
        return

    added, already, failed = [], [], []
    remaining = list(queue)
    stopped = None
    for n, item in enumerate(queue):
        if args.limit and n >= args.limit:
            break
        game, link = item["game"], item["link"]
        ns = Namespace(link=link, asset=item.get("asset"), version=item.get("version"), name=None,
                       hint=game, display_name=item.get("display_name"), home=item.get("home"),
                       dry_run=args.dry_run)
        try:
            msg = add_apworld.run(ns)
        except (add_apworld.Fail, RuntimeError) as e:
            if "rate limited" in str(e):
                stopped = str(e)
                break
            failed.append((game, link, str(e)))
            print(f"  FAIL  {game}: {e}")
        except Exception as e:  # never let one weird apworld kill the batch
            failed.append((game, link, f"unexpected {type(e).__name__}: {e}"))
            print(f"  FAIL  {game}: {type(e).__name__}: {e}")
        else:
            first = next(l for l in msg.splitlines() if not l.startswith("*(dry run"))
            (already if "nothing to do" in msg else added).append((game, link, first))
            print(f"  OK    {game}: {first}")
        remaining.remove(item)
        time.sleep(0.2)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n## Run {stamp}" + (" (dry run)" if args.dry_run else ""), "",
             f"{len(added)} added, {len(already)} already present, {len(failed)} failed, "
             f"{len(remaining)} still queued.", ""]
    if stopped:
        lines += [f"Stopped early: {stopped}. The rest stay queued for the next run.", ""]
    if added:
        lines += ["### Added", ""] + [f"- **{g}**: {m}" for g, _, m in added] + [""]
    if failed:
        lines += ["### Couldn't add", "", "| Game | Link | Why |", "|---|---|---|"]
        lines += [f"| {g} | {l} | {w.replace('|', '/')} |" for g, l, w in failed] + [""]
    if already:
        lines += ["<details><summary>Already present</summary>", ""]
        lines += [f"- {g}" for g, _, _ in already] + ["", "</details>", ""]
    report = "\n".join(lines)
    print(report)
    Path("bulk_summary.md").write_text(report, encoding="utf-8")

    if not args.dry_run:
        BULK_DIR.mkdir(parents=True, exist_ok=True)
        if not RESULTS.exists():
            RESULTS.write_text("# Bulk add results\n", encoding="utf-8", newline="\n")
        with RESULTS.open("a", encoding="utf-8", newline="\n") as f:
            f.write(report)
        QUEUE.write_text(json.dumps(remaining, indent=1, ensure_ascii=False) + "\n",
                         encoding="utf-8", newline="\n")
    Path("changes_made.flag").write_text("0" if args.dry_run else "1")


if __name__ == "__main__":
    main()
