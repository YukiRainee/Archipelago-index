"""
Shared helpers for the fork's auto-update tooling.

Lives in autoupdate/ (not scripts/) so it never collides with files the
upstream ionium-ap/Archipelago-index maintainers add when we sync.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = REPO_ROOT / "index"
LOCK_PATH = REPO_ROOT / "index.lock"
INDEX_META = REPO_ROOT / "index.toml"

# Strict semver, which the index requires for every version key.
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
# "/owner/repo/releases/download/<tag>/<file>" -- same shape on GitHub,
# Codeberg/Forgejo and other Gitea instances.
RELEASE_DOWNLOAD_RE = re.compile(
    r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
    r"(?P<tag>[^/]+)/(?P<filename>[^/]+)$"
)

session = requests.Session()
session.headers["User-Agent"] = "apworld-index-auto-updater"
_token = os.environ.get("GITHUB_TOKEN")

MAX_APWORLD_BYTES = 200 * 1024 * 1024


def is_semver(v: str) -> bool:
    return bool(SEMVER_RE.match(v))


def parse_version(v: str) -> Version | None:
    """Best-effort ordering key. None means 'can't order this, needs a human'."""
    try:
        return Version(v)
    except InvalidVersion:
        return None


def normalize_version(raw: str) -> str | None:
    """'v0.8' -> '0.8.0', 'V1.2.3' -> '1.2.3'. None if it isn't recognisably a version."""
    v = raw.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    if is_semver(v):
        return v
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", v)
    if m:
        return ".".join(str(int(p or 0)) for p in m.groups())
    return None


def version_from_tag(tag: str) -> str | None:
    """'v1.2' -> '1.2.0', 'DRG_1.1.2' -> '1.1.2'. Needs at least 'X.Y' somewhere in the tag,
    so 'apraiser_v134' or 'Archipelago' give None."""
    return normalize_version(tag) or normalize_version(re.sub(r"^.*?(?=\d+\.\d+)", "", tag))


def is_prerelease(v: str) -> bool:
    parsed = parse_version(v)
    return bool(parsed and (parsed.is_prerelease or parsed.is_devrelease))


# --------------------------------------------------------------------------
# Release hosts


@dataclass
class Release:
    tag: str
    prerelease: bool
    assets: dict[str, str] = field(default_factory=dict)  # filename -> download url
    name: str = ""


def _api_get(url: str, **kw) -> requests.Response:
    headers = {}
    if _token and urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {_token}"
        headers["Accept"] = "application/vnd.github+json"
    return session.get(url, headers=headers, timeout=30, **kw)


def list_releases(host: str, owner: str, repo: str, pages: int = 1) -> list[Release]:
    """Newest-first releases (drafts excluded). Raises RuntimeError on failure.
    `pages` > 1 digs further back, for monorepos with one release per world."""
    out = []
    for page in range(1, pages + 1):
        if host == "github.com":
            url = f"https://api.github.com/repos/{owner}/{repo}/releases"
            params = {"per_page": 100 if pages > 1 else 50, "page": page}
        else:
            # Gitea / Forgejo (Codeberg etc.) expose a near-identical API.
            url = f"https://{host}/api/v1/repos/{owner}/{repo}/releases"
            params = {"limit": 50, "page": page}
        resp = _api_get(url, params=params)
        if resp.status_code == 404:
            raise RuntimeError(f"{host}/{owner}/{repo} not found (404) - moved or deleted?")
        if resp.status_code in (403, 429):
            raise RuntimeError(f"rate limited / forbidden ({resp.status_code}) by {host}")
        if not resp.ok:
            raise RuntimeError(f"unexpected HTTP {resp.status_code} listing releases on {host}")
        batch = resp.json()
        for rel in batch:
            if rel.get("draft"):
                continue
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            out.append(Release(rel.get("tag_name", ""), bool(rel.get("prerelease")), assets,
                               rel.get("name") or ""))
        if len(batch) < params.get("per_page", params.get("limit")):
            break
    return out


def download(url: str) -> bytes:
    resp = session.get(url, timeout=120, stream=True)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} downloading {url}")
    buf = io.BytesIO()
    for chunk in resp.iter_content(1 << 16):
        buf.write(chunk)
        if buf.tell() > MAX_APWORLD_BYTES:
            raise RuntimeError(f"{url} is larger than {MAX_APWORLD_BYTES // 2**20} MB, refusing")
    return buf.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# .apworld inspection


@dataclass
class ApworldInfo:
    module: str               # top-level folder inside the zip == index file stem
    game: str | None          # world/game name (what goes in `name`)
    world_version: str | None
    min_ap: str | None
    max_ap: str | None


_GAME_RE = re.compile(r"""^\s+game\s*(?::\s*[^=]+)?=\s*["']([^"']+)["']""", re.M)


def inspect_apworld(data: bytes) -> ApworldInfo:
    """Raises RuntimeError if this doesn't look like a real apworld."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise RuntimeError("downloaded file is not a zip / .apworld (wrong link?)")
    tops = {n.split("/", 1)[0] for n in zf.namelist() if "/" in n}
    tops.discard("__MACOSX")
    if len(tops) != 1:
        raise RuntimeError(f"expected one top-level folder in the apworld, found {sorted(tops)}")
    module = tops.pop()
    names = set(zf.namelist())

    manifest = {}
    if f"{module}/archipelago.json" in names:
        try:
            manifest = json.loads(zf.read(f"{module}/archipelago.json"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            manifest = {}

    game = manifest.get("game")
    if not game and f"{module}/__init__.py" in names:
        src = zf.read(f"{module}/__init__.py").decode("utf-8", "replace")
        m = _GAME_RE.search(src)
        if m:
            game = m.group(1)

    return ApworldInfo(
        module=module,
        game=game,
        world_version=manifest.get("world_version"),
        min_ap=manifest.get("minimum_ap_version"),
        max_ap=manifest.get("maximum_ap_version"),
    )


# --------------------------------------------------------------------------
# AP base versions (index.toml) and where a new release belongs


def ap_bases() -> list[Version]:
    meta = tomllib.loads(INDEX_META.read_text(encoding="utf-8"))
    bases = [Version(b) for b in meta.get("bases", {})] or [Version(meta["archipelago_version"])]
    return sorted(bases)


def placement_for(info: ApworldInfo, prev_constraint: dict | None) -> dict:
    """Decide where a new release goes, following the index README rules:
    returns {"in_versions": bool, "min": str|None, "max": str|None}.

    The apworld's own manifest wins; otherwise copy whatever constraint the
    previous newest release had (e.g. a world that already needs 0.6.8+)."""
    bases = ap_bases()
    oldest, newest = bases[0], bases[-1]
    min_ap = max_ap = None
    if info.min_ap and parse_version(info.min_ap) and Version(info.min_ap) > oldest:
        min_ap = info.min_ap
    if info.max_ap and parse_version(info.max_ap) and Version(info.max_ap) < newest:
        max_ap = info.max_ap
    if min_ap is None and max_ap is None and prev_constraint:
        min_ap = prev_constraint.get("min_ap_version")
        max_ap = prev_constraint.get("max_ap_version")
    return {"in_versions": min_ap is None, "min": min_ap, "max": max_ap}


def add_release(doc, version: str, placement: dict, url: str | None = None) -> None:
    """Write a new release into a tomlkit document per `placement_for`."""
    import tomlkit

    src = tomlkit.inline_table()
    if url:
        src["url"] = url
    if placement["in_versions"]:
        if "versions" not in doc:
            doc["versions"] = tomlkit.table()
        doc["versions"][version] = src
    constraint = {}
    if placement["min"]:
        constraint["min_ap_version"] = placement["min"]
    if placement["max"]:
        constraint["max_ap_version"] = placement["max"]
    if constraint:
        rel = tomlkit.inline_table()
        rel.update(constraint)
        # README: a [versions] entry MUST NOT carry a second key, so a url for a
        # releases-only entry goes here instead.
        if url and not placement["in_versions"]:
            rel["url"] = url
        if "releases" not in doc:
            doc.add(tomlkit.nl())
            doc["releases"] = tomlkit.table()
        doc["releases"][version] = rel


def all_versions(doc) -> dict[str, dict]:
    """version -> merged source/constraint info across [versions] and [releases]."""
    out: dict[str, dict] = {}
    for table in ("versions", "releases"):
        for k, v in (doc.get(table) or {}).items():
            out.setdefault(str(k), {}).update(dict(v))
    return out


def resolve_url(doc, version: str, info: dict) -> str | None:
    if "url" in info:
        return info["url"]
    if "local" in info:
        return None
    default = doc.get("default_url")
    return str(default).replace("{{version}}", version) if default else None


def fully_disabled(doc) -> bool:
    """disabled=true with no base override switching it back on."""
    if not doc.get("disabled"):
        return False
    for override in (doc.get("base") or {}).values():
        if override.get("disabled") is False:
            return False
    return True


# --------------------------------------------------------------------------
# index.lock  (world -> {version: sha256}, sorted the same way apwm writes it)

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def _ident_key(ident: str):
    # semver: numeric identifiers sort numerically and before alphanumeric ones
    return (0, int(ident), "") if ident.isdigit() else (1, 0, ident)


def _version_sort_key(v: str):
    """Strict semver precedence (what apwm/Rust's semver crate uses), with
    build metadata as a final tiebreak like that crate does."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+(.+))?$", v)
    if not m:
        return (1, (), (), (), v)
    major, minor, patch, pre, build = m.groups()
    pre_key = (1,) if pre is None else (0, *map(_ident_key, pre.split(".")))
    build_key = () if build is None else tuple(map(_ident_key, build.split(".")))
    return (0, (int(major), int(minor), int(patch)), pre_key, build_key, "")


def load_lock() -> dict[str, dict[str, str]]:
    return tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))


def save_lock(lock: dict[str, dict[str, str]]) -> None:
    blocks = []
    for world in sorted(lock):
        lines = [f"[{_key(world)}]"]
        for v in sorted(lock[world], key=_version_sort_key):
            lines.append(f'{json.dumps(v)} = "{lock[world][v]}"')
        blocks.append("\n".join(lines))
    LOCK_PATH.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="\n")
