#!/usr/bin/env python3
"""
add_apworld.py -- add an apworld to the index from just a link.

Accepts any of:
    https://github.com/owner/repo                               (latest release)
    https://github.com/owner/repo/releases/tag/<tag>            (that release)
    https://github.com/owner/repo/releases/download/<tag>/x.apworld
    the same shapes on Codeberg / other Gitea hosts
    any other direct https link to a .apworld file

It downloads the apworld, reads the folder name / game name / version / AP
version range out of the file itself, then writes index/<module>.toml
(or adds the version if the world is already listed) plus the sha256 in
index.lock. When the release tag contains the version it uses
`default_url`, so check_updates.py can keep it updated from then on.

Usage:
    python autoupdate/add_apworld.py <link> [--asset NAME] [--version X.Y.Z]
                                            [--display-name "..."] [--home URL] [--dry-run]
Writes add_result.md with a human-readable result.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

REPO_RE = re.compile(r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/#?]+?)(?:\.git)?/?$")
TAG_RE = re.compile(r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/tag/(?P<tag>[^/?#]+)")


class Fail(Exception):
    pass


def pick_asset(assets: dict[str, str], wanted: str | None) -> tuple[str, str]:
    apworlds = {n: u for n, u in assets.items() if n.lower().endswith(".apworld")}
    if wanted:
        if wanted not in apworlds:
            raise Fail(f"release has no asset named '{wanted}'. It has: {', '.join(apworlds) or 'none'}")
        return wanted, apworlds[wanted]
    if not apworlds:
        raise Fail("that release has no .apworld file attached")
    if len(apworlds) > 1:
        raise Fail("that release has several .apworld files - say which one with the asset field: "
                   + ", ".join(sorted(apworlds)))
    return next(iter(apworlds.items()))


def resolve(link: str, asset: str | None) -> dict:
    """-> {url, host, owner, repo, tag, filename, repo_home}"""
    link = link.strip().rstrip("/")
    if "discord.com" in link or "discordapp" in link:
        raise Fail("Discord links can't be downloaded reliably - the index needs a stable direct link "
                   "(e.g. a GitHub release). Ask the author or use a direct .apworld URL.")
    if not link.startswith("https://"):
        raise Fail("link must start with https://")

    if m := common.RELEASE_DOWNLOAD_RE.match(link):
        return {"url": link, **m.groupdict(), "filename": unquote(m["filename"]),
                "repo_home": f"https://{m['host']}/{m['owner']}/{m['repo']}"}

    if m := TAG_RE.match(link):
        releases = common.list_releases(m["host"], m["owner"], m["repo"])
        rel = next((r for r in releases if r.tag == unquote(m["tag"])), None)
        if rel is None:
            raise Fail(f"couldn't find release '{m['tag']}'")
        name, url = pick_asset(rel.assets, asset)
        return {"url": url, "host": m["host"], "owner": m["owner"], "repo": m["repo"],
                "tag": rel.tag, "filename": name,
                "repo_home": f"https://{m['host']}/{m['owner']}/{m['repo']}"}

    if urlparse(link).path.lower().endswith(".apworld"):
        return {"url": link, "host": urlparse(link).hostname, "owner": None, "repo": None,
                "tag": None, "filename": unquote(urlparse(link).path.rsplit("/", 1)[-1]),
                "repo_home": None}

    if m := REPO_RE.match(link):
        releases = common.list_releases(m["host"], m["owner"], m["repo"])
        with_apworld = [r for r in releases if any(n.lower().endswith(".apworld") for n in r.assets)]
        stable = [r for r in with_apworld if not r.prerelease] or with_apworld
        if not stable:
            raise Fail("no release in that repo has a .apworld file attached")
        rel = stable[0]
        name, url = pick_asset(rel.assets, asset)
        return {"url": url, "host": m["host"], "owner": m["owner"], "repo": m["repo"],
                "tag": rel.tag, "filename": name,
                "repo_home": f"https://{m['host']}/{m['owner']}/{m['repo']}"}

    raise Fail("didn't recognise that link - give a repo, release page, or direct .apworld URL")


def default_url_for(src: dict, version: str) -> str | None:
    """Templated URL if the tag carries the exact version string, else None."""
    tag = src.get("tag")
    if not tag or not src.get("owner") or tag.count(version) != 1:
        return None
    filename = src["filename"]
    if version in filename:
        if filename.count(version) != 1:
            return None
        filename = filename.replace(version, "{{version}}")
    templ_tag = tag.replace(version, "{{version}}")
    candidate = (f"https://{src['host']}/{src['owner']}/{src['repo']}/releases/download/"
                 f"{templ_tag}/{filename}")
    # Must reproduce the real download URL exactly, or it's not safe to use.
    if candidate.replace("{{version}}", version) != src["url"]:
        return None
    return candidate


def run(args) -> str:
    src = resolve(args.link, args.asset)
    data = common.download(src["url"])
    info = common.inspect_apworld(data)
    stem = info.module

    version = args.version
    if not version and src.get("tag"):
        version = common.normalize_version(src["tag"]) or common.normalize_version(
            re.sub(r"^.*?(?=\d+\.\d+)", "", src["tag"]))
    if not version and info.world_version:
        version = common.normalize_version(info.world_version)
    if not version:
        raise Fail("couldn't work out a version number - fill in the version field (e.g. 0.1.0)")
    if not common.is_semver(version):
        raise Fail(f"'{version}' isn't valid semver (the index needs X.Y.Z)")

    path = common.INDEX_DIR / f"{stem}.toml"
    lock = common.load_lock()
    existed = path.exists()

    if existed:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        known = common.all_versions(doc)
        if version in known:
            return f"**{stem}** already has version `{version}` listed - nothing to do."
        resolved = common.resolve_url(doc, version, {})
        url = None if resolved == src["url"] else src["url"]
        parsed = [(common.parse_version(v), v) for v in known if common.parse_version(v)]
        prev = known[max(parsed)[1]] if parsed else None
    else:
        game = args.name or info.game
        if not game:
            raise Fail("couldn't read the game name from the apworld - fill in the name field")
        for other in common.INDEX_DIR.glob("*.toml"):
            if tomlkit.parse(other.read_text(encoding="utf-8")).get("name") == game:
                raise Fail(f"game '{game}' is already in the index as {other.name} "
                           f"(but this apworld's folder is '{stem}') - not adding a duplicate")
        doc = tomlkit.document()
        doc["name"] = game
        if args.display_name:
            doc["display_name"] = args.display_name
        home = args.home or src.get("repo_home")
        if home:
            doc["home"] = home
        templ = default_url_for(src, version)
        if templ:
            doc["default_url"] = templ
        url = None if templ else src["url"]
        prev = None

    placement = common.placement_for(info, prev)
    newer_ap_only = not existed and not placement["in_versions"]
    if newer_ap_only:
        # Older lobbies only read [versions] / top-level `disabled`, so a world
        # that needs a newer AP is disabled there and re-enabled for that base
        # (README "Archipelago versions" section).
        doc["disabled"] = True
    elif not existed:
        doc.add(tomlkit.nl())
        doc["versions"] = tomlkit.table()
    common.add_release(doc, version, placement, url=url)
    if newer_ap_only:
        base = tomlkit.table()
        base["disabled"] = False
        doc.add(tomlkit.nl())
        doc["base"] = tomlkit.table(is_super_table=True)
        doc["base"][f">={placement['min']}"] = base

    where = "[versions]" if placement["in_versions"] else "[releases]"
    summary = [
        f"{'Added version to' if existed else 'Added new world'} **{stem}**" + (
            "" if existed else f" (game: *{doc['name']}*)"),
        f"- version `{version}` -> {where}"
        + "".join(f", {k} AP `{v}`" for k, v in (("min", placement["min"]), ("max", placement["max"])) if v),
        f"- source: {src['url']}",
        "- auto-updates: " + ("yes (default_url)" if doc.get("default_url") else
                              "**no** - tag doesn't contain the version, future versions must be added by link"),
        f"- sha256 `{common.sha256(data)}`",
    ]
    if args.dry_run:
        summary.insert(0, "*(dry run - nothing written)*")
        summary.append("\n```toml\n" + tomlkit.dumps(doc) + "```")
        return "\n".join(summary)

    path.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="\n")
    lock.setdefault(stem, {})[version] = common.sha256(data)
    common.save_lock(lock)
    return "\n".join(summary)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("link")
    ap.add_argument("--asset", help="which .apworld to use when a release has several")
    ap.add_argument("--version", help="override the detected version (must be X.Y.Z)")
    ap.add_argument("--name", help="override the detected game name")
    ap.add_argument("--display-name")
    ap.add_argument("--home", help="homepage (defaults to the repo URL)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    for k in ("asset", "version", "name", "display_name", "home"):
        if getattr(args, k) is not None and not getattr(args, k).strip():
            setattr(args, k, None)  # blank form fields

    try:
        result, code = run(args), 0
    except (Fail, RuntimeError) as e:
        result, code = f"Couldn't add it: {e}", 1
    print(result)
    Path("add_result.md").write_text(result + "\n", encoding="utf-8")
    sys.exit(code)


if __name__ == "__main__":
    main()
