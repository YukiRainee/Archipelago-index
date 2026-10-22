#!/usr/bin/env python3
"""
check_updates.py

Two passes over index/*.toml:

1. Version bumps -- for entries using a templated `default_url`
   (".../releases/download/<tag with {{version}}>/file.apworld" on GitHub,
   Codeberg or another Gitea host), look for a newer release. If one exists,
   download it, check it really is this world's apworld, and add it to the
   toml *and* its sha256 to index.lock. (Upstream fills index.lock from its
   own GitLab pipeline, which this fork doesn't have -- a version without a
   lock hash is useless to the lobby.)

2. Lock repair -- any listed version that has no hash in index.lock gets one.
   Covers entries added by hand and anything a sync left half-done.

Never touches entries that are fully disabled, and never guesses: anything
it can't do safely is listed in the summary for a human.

Usage:
    python autoupdate/check_updates.py [--dry-run] [--only NAME ...] [--include-prereleases]

Env:
    GITHUB_TOKEN  strongly recommended (60 vs 1000 API requests/hour)
Writes:
    update_summary.md   human-readable report (used for the Actions summary)
    changes_made.flag   "1" if any file changed, else "0"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def tag_regex(tag_template: str) -> re.Pattern | None:
    if tag_template.count("{{version}}") != 1:
        return None
    prefix, suffix = tag_template.split("{{version}}")
    return re.compile(f"^{re.escape(prefix)}(.+){re.escape(suffix)}$")


def check_entry(path: Path, lock: dict, args) -> tuple[str, str]:
    """Returns (status, message). status in ADDED/WOULD-ADD/OK/SKIP/ERROR."""
    stem = path.stem
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    if common.fully_disabled(doc):
        return "SKIP", "disabled"

    default_url = doc.get("default_url")
    if not default_url:
        return "SKIP", "no default_url (explicit per-version URLs - needs manual updates)"
    m = common.RELEASE_DOWNLOAD_RE.match(str(default_url))
    if not m:
        return "SKIP", "default_url isn't a standard releases/download URL"
    tag_re = tag_regex(m["tag"])
    if tag_re is None:
        return "SKIP", f"tag template '{m['tag']}' can't be reversed safely"

    known = common.all_versions(doc)
    parsed_known = [(common.parse_version(v), v) for v in known]
    parsed_known = [p for p in parsed_known if p[0] is not None]
    current = max(parsed_known)[1] if parsed_known else None
    tracks_pre = bool(current and common.is_prerelease(current))

    try:
        releases = common.list_releases(m["host"], m["owner"], m["repo"])
    except RuntimeError as e:
        return "ERROR", str(e)

    # Some authors tick "pre-release" on every release; the flag only means
    # something if the repo also has normal releases.
    flag_meaningful = any(not r.prerelease for r in releases)
    candidates = []
    for rel in releases:
        tm = tag_re.match(rel.tag)
        if not tm:
            continue
        v = tm.group(1)
        pv = common.parse_version(v)
        if pv is None or not common.is_semver(v):
            continue
        pre = (rel.prerelease and flag_meaningful) or common.is_prerelease(v)
        if pre and not (args.include_prereleases or tracks_pre):
            continue
        candidates.append((pv, v))
    if not candidates:
        return "SKIP", f"no releases matched tag template '{m['tag']}'"

    best_pv, best = max(candidates)
    if best in known:
        return "OK", f"up to date ({best})"
    if current and best_pv <= common.parse_version(current):
        return "OK", f"newest tag {best} isn't newer than pinned {current}"

    url = str(default_url).replace("{{version}}", best)
    if args.dry_run:
        return "WOULD-ADD", f"{best} (currently {current})"

    try:
        data = common.download(url)
        info = common.inspect_apworld(data)
    except RuntimeError as e:
        return "ERROR", f"{best}: {e}"
    if info.module != stem:
        return "ERROR", f"{best}: apworld contains '{info.module}', expected '{stem}' - not adding"

    prev = known.get(current, {}) if current else None
    placement = common.placement_for(info, prev)
    common.add_release(doc, best, placement)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="\n")
    lock.setdefault(stem, {})[best] = common.sha256(data)

    where = "[versions]" if placement["in_versions"] else "[releases]"
    extra = ", ".join(f"{k} AP {v}" for k, v in (("min", placement["min"]), ("max", placement["max"])) if v)
    return "ADDED", f"{best} (was {current}) -> {where}" + (f" ({extra})" if extra else "")


def repair_lock(path: Path, lock: dict, dry_run: bool) -> list[str]:
    """Hash any listed version that index.lock doesn't know about."""
    stem = path.stem
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    if common.fully_disabled(doc):
        return []
    msgs = []
    for v, info in common.all_versions(doc).items():
        if v in lock.get(stem, {}):
            continue
        url = common.resolve_url(doc, v, info)
        if not url:
            continue  # local source - nothing we can fetch
        if dry_run:
            msgs.append(f"would hash {v}")
            continue
        try:
            data = common.download(url)
            common.inspect_apworld(data)
        except RuntimeError as e:
            msgs.append(f"couldn't hash {v}: {e}")
            continue
        lock.setdefault(stem, {})[v] = common.sha256(data)
        msgs.append(f"hashed {v}")
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't write files")
    ap.add_argument("--only", nargs="*", help="limit to these index file stems")
    ap.add_argument("--include-prereleases", action="store_true")
    ap.add_argument("--no-repair", action="store_true", help="skip the index.lock repair pass")
    args = ap.parse_args()

    files = sorted(common.INDEX_DIR.glob("*.toml"))
    if args.only:
        files = [f for f in files if f.stem in set(args.only)]
    lock = common.load_lock()
    print(f"Scanning {len(files)} entries ...\n")

    rows: dict[str, list[tuple[str, str]]] = {}
    for path in files:
        try:
            status, msg = check_entry(path, lock, args)
        except Exception as e:  # one broken entry must never stop the run
            status, msg = "ERROR", f"unexpected {type(e).__name__}: {e}"
        rows.setdefault(status, []).append((path.stem, msg))
        print(f"  {status:9} {path.stem}: {msg}")

    repaired = []
    if not args.no_repair:
        for path in files:
            for msg in repair_lock(path, lock, args.dry_run):
                repaired.append((path.stem, msg))
                print(f"  LOCK      {path.stem}: {msg}")

    changed = bool(rows.get("ADDED")) or any(m.startswith("hashed") for _, m in repaired)
    if changed and not args.dry_run:
        common.save_lock(lock)

    lines = ["# Apworld index update", ""]
    for status, title in (("ADDED", "New versions added"), ("WOULD-ADD", "Would add (dry run)"),
                          ("ERROR", "Errors - worth a look"), ("SKIP", "Skipped (need manual updates)")):
        items = rows.get(status, [])
        if not items:
            continue
        lines.append(f"## {title} ({len(items)})")
        if status == "SKIP":
            lines.append("<details><summary>show</summary>\n")
        lines += [f"- **{s}**: {m}" for s, m in items]
        if status == "SKIP":
            lines.append("\n</details>")
        lines.append("")
    if repaired:
        lines.append(f"## index.lock repairs ({len(repaired)})")
        lines += [f"- **{s}**: {m}" for s, m in repaired]
        lines.append("")
    lines.append(f"{len(rows.get('OK', []))} entries already up to date.")

    Path("update_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path("changes_made.flag").write_text("1" if changed and not args.dry_run else "0")
    print(f"\nDone: {len(rows.get('ADDED', []))} added, {len(rows.get('ERROR', []))} errors, "
          f"{len(repaired)} lock repairs.")


if __name__ == "__main__":
    main()
