"""Drop duplicate version keys that an upstream merge can leave in index/*.toml.

When this fork's updater and upstream both add the same version, they put the
line in different places (we add at the top of [versions], upstream usually at the
bottom), so git merges both without a conflict and the file no longer parses.
This keeps the first entry of each key per table and checks every file parses.
index.lock can end up with the same [world] table twice the same way; those get
merged, keeping only hashes for versions the world's toml still lists.

Usage: python autoupdate/fix_duplicate_versions.py   (prints the files it fixed)
"""

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

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


def fix_lock() -> bool:
    """Merge [world] tables declared more than once. Returns True if it changed."""
    try:
        common.load_lock()
        return False
    except tomllib.TOMLDecodeError:
        pass
    tables: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for block in common.LOCK_PATH.read_text(encoding="utf-8").split("\n["):
        part = tomllib.loads(block if block.startswith("[") else "[" + block)
        for world, versions in part.items():
            tables.setdefault(world, {}).update(versions)
            counts[world] = counts.get(world, 0) + 1
    for world, n in counts.items():
        toml = ROOT / "index" / f"{world}.toml"
        if n > 1 and toml.exists():
            listed = common.all_versions(tomllib.loads(toml.read_text(encoding="utf-8")))
            tables[world] = {v: h for v, h in tables[world].items() if v in listed}
    common.save_lock(tables)
    print("merged duplicate tables in index.lock")
    return True


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
    fix_lock()
    for b in broken:
        print(f"::error::{b}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
