"""Drop duplicate version keys that an upstream merge can leave in index/*.toml.

When this fork's updater and upstream both add the same version, they put the
line in different places (we add at the top of [versions], upstream usually at the
bottom), so git merges both without a conflict and the file no longer parses.
This keeps the first entry of each key per table and checks every file parses.

Usage: python autoupdate/fix_duplicate_versions.py   (prints the files it fixed)
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = re.compile(r'^\s*("(?:[^"\\]|\\.)*"|[A-Za-z0-9_.-]+)\s*=')


def dedupe(text: str) -> str:
    out, seen = [], set()
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            seen = set()
        elif m := KEY.match(line):
            key = m.group(1).strip('"')
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "".join(out)


def main() -> int:
    broken = []
    for path in sorted((ROOT / "index").glob("*.toml")):
        text = path.read_text(encoding="utf-8")
        try:
            tomllib.loads(text)
            continue
        except tomllib.TOMLDecodeError:
            pass
        fixed = dedupe(text)
        try:
            tomllib.loads(fixed)
        except tomllib.TOMLDecodeError as e:
            broken.append(f"{path.relative_to(ROOT)}: {e}")
            continue
        path.write_text(fixed, encoding="utf-8", newline="")
        print(f"fixed duplicate keys in {path.relative_to(ROOT).as_posix()}")
    for b in broken:
        print(f"::error::{b}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
