#!/usr/bin/env python3
"""
sync_sheet.py -- pull the community APWorld sheet's labels into sheet.json.

The sheet (https://docs.google.com/spreadsheets/d/1iuzDTOAvdoNe8Ne8i461qGNucg5OuEoF-Ikqs8aUQZw)
rates every world: Stability, PR Status, 18+ / Unrated (lives on the After Dark
Discord), AI-use Disclosures and Notes. This writes them to sheet.json at the index
root, keyed by index world (the toml file stem), for the lobby to show next to each
world. It's a fork-only file, so syncing upstream never touches it.

Sheet rows are matched to index worlds by name; autoupdate/bulk/results.md (what the
bulk add made of each sheet row) and autoupdate/sheet_overrides.json
({"<sheet game name>": "<world stem>"}) cover names that differ.

Usage:
    python autoupdate/sync_sheet.py [--html saved_sheet.html]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SHEET_ID = "1iuzDTOAvdoNe8Ne8i461qGNucg5OuEoF-Ikqs8aUQZw"
SHEET_GID = "58422002"  # the "Playable Worlds" tab
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/htmlview?gid={SHEET_GID}"
# The static rendering of the tab: unlike the CSV export it keeps links and checkboxes.
SHEET_HTML = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/htmlview/sheet"
              f"?headers=false&gid={SHEET_GID}")

OUT = common.REPO_ROOT / "sheet.json"
OVERRIDES = common.REPO_ROOT / "autoupdate" / "sheet_overrides.json"
BULK_RESULTS = common.REPO_ROOT / "autoupdate" / "bulk" / "results.md"


class _TableParser(HTMLParser):
    """Rows of cells: text, links, and checkbox state."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = {"text": "", "checked": None}
        elif tag == "use" and self.cell is not None and "checkbox-id" in a.get("href", ""):
            self.cell["checked"] = a["href"] == "#checked-checkbox-id"
        elif tag == "br" and self.cell is not None:
            self.cell["text"] += "\n"

    def handle_endtag(self, tag):
        if tag == "td" and self.cell is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data


def sheet_rows(html: str) -> list[dict]:
    p = _TableParser()
    p.feed(html)
    header_i = next(i for i, r in enumerate(p.rows) if r and r[0]["text"].strip() == "Game")
    header = [c["text"].strip() for c in p.rows[header_i]]
    rows = []
    for r in p.rows[header_i + 1:]:
        cells = {header[i]: c for i, c in enumerate(r) if i < len(header) and header[i]}
        game = cells.get("Game", {}).get("text", "").strip()
        if not game:
            continue
        text = lambda col: cells.get(col, {}).get("text", "").strip()
        rows.append({
            "game": game,
            "stability": text("Stability"),
            "pr_status": text("PR Status"),
            "after_dark": bool(cells.get("18+ / Unrated", {}).get("checked")),
            "ai_disclosure": text("Disclosures"),
            "notes": text("Notes"),
        })
    return rows


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s)


def index_names() -> dict[str, str]:
    """normalized name -> world stem, from each world's name, display_name and stem."""
    names: dict[str, str] = {}
    for f in sorted(common.INDEX_DIR.glob("*.toml")):
        d = tomllib.loads(f.read_text(encoding="utf-8"))
        for value in (d.get("display_name"), d.get("name"), f.stem):
            if value:
                names.setdefault(norm(value), f.stem)
                # "Manual: Foo" display names are listed as plain "Foo" on the sheet
                names.setdefault(norm(re.sub(r"^manual:\s*", "", value, flags=re.I)), f.stem)
    return names


def bulk_names() -> dict[str, str]:
    """sheet game name -> world stem, from what the bulk add made of each sheet row."""
    out = {}
    if BULK_RESULTS.exists():
        for m in re.finditer(r"^- \*\*(.+?)\*\*: Added (?:new world|version to) \*\*(.+?)\*\*",
                             BULK_RESULTS.read_text(encoding="utf-8"), re.M):
            out[m.group(1)] = m.group(2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="use a saved copy of the sheet instead of downloading it")
    args = ap.parse_args()

    html = (Path(args.html).read_text(encoding="utf-8") if args.html
            else common.download(SHEET_HTML).decode("utf-8"))
    rows = sheet_rows(html)
    if len(rows) < 100:  # a changed layout or an error page; don't wipe the labels
        sys.exit(f"Only {len(rows)} rows read from the sheet - not updating sheet.json")

    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    bulk, names = bulk_names(), index_names()
    stems = {f.stem for f in common.INDEX_DIR.glob("*.toml")}

    def exact(game):
        return (overrides.get(game) or bulk.get(game) or names.get(norm(game))
                or names.get(norm(re.sub(r"\(.*?\)", "", game))))

    def loose(game):
        """'The Legend of Zelda: Ocarina of Time' -> 'Ocarina of Time', 'Ender Lilies:
        Quietus of the Knights' -> 'Ender Lilies', 'The Messenger' -> 'Messenger'."""
        tries = [re.sub(r"^the\s+", "", game, flags=re.I)]
        if ":" in game:
            before, after = game.split(":", 1)
            tries += [after, before]
        return [names[norm(t)] for t in tries if norm(t) in names]

    worlds, pending = {}, []
    for row in rows:  # exact names first, so loose matches can't take their worlds
        stem = exact(row["game"])
        if stem in stems and stem not in worlds:  # two rows for one world: keep the first
            worlds[stem] = row
        elif not stem or stem not in stems:
            pending.append(row)
    unmatched = []
    for row in pending:
        found = [s for s in dict.fromkeys(loose(row["game"])) if s in stems and s not in worlds]
        if len(found) == 1:
            worlds[found[0]] = row
        else:
            unmatched.append(row["game"])
    worlds = {stem: {k: v for k, v in row.items() if v not in ("", "--", False)}
              for stem, row in worlds.items()}

    data = {"source": SHEET_URL, "worlds": dict(sorted(worlds.items()))}
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"{len(rows)} sheet rows, {len(worlds)} matched to index worlds, "
          f"{len(unmatched)} not in the index")
    Path("sheet_unmatched.txt").write_text("\n".join(unmatched) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
