# Fork auto-updater

This fork (`YukiRainee/Archipelago-index`) of `ionium-ap/Archipelago-index`
keeps itself current and lets you add apworlds from just a link.
Everything fork-specific lives in `autoupdate/`, `.github/workflows/fork-*.yml`
and `.github/ISSUE_TEMPLATE/`, so syncing upstream never conflicts with it.

## What runs automatically

**`Fork: sync upstream + auto-update apworlds`** runs daily (and on demand
from the Actions tab). It does three things:
1. Turns off upstream's own workflows in this fork. They need ionium's
   GitLab secrets and would just fail.
2. Merges upstream. On a conflict, upstream wins.
3. For every world with a templated `default_url` (GitHub, Codeberg, other
   Gitea hosts), finds newer releases, downloads them, checks the apworld
   really is that world, and writes both the `.toml` entry **and** the sha256
   in `index.lock`. The lobby needs that hash, and upstream only fills it in
   from its own pipeline. It also fills in any missing lock hashes.

It never guesses. Worlds it can't update safely (no `default_url`, odd tag
names, a release whose apworld folder got renamed, ...) are listed in the
run's summary page.

## Adding an apworld

Any of these work. Give it a repo link, a release page, or a direct `.apworld` URL:

- **Issue form**: Issues → New issue → *Add an apworld* → paste the link →
  Submit. The bot replies with what it added and closes the issue. Only
  issues **you** open are acted on.
- **Actions form**: Actions → *Fork: add apworld from a link* → Run workflow.
- **Locally / ask Claude**: `python autoupdate/add_apworld.py <link>`
  (add `--dry-run` to preview), then commit and push. In a Claude Code
  session in this repo you can just say "add <link>".

- **Many at once**: put entries in `autoupdate/bulk/queue.json`
  (`{"game": ..., "link": ..., "home": ...}`) and push. *Fork: bulk add
  apworlds from queue* works through it and logs every outcome, including
  why a world couldn't be added, in `autoupdate/bulk/results.md`.

It reads the game name, folder name, version and supported AP range from the
apworld itself. When the release tag contains the version, it sets
`default_url`, so future releases get picked up automatically.

## One-time GitHub setup

1. **Settings → Actions → General**: allow actions, then under *Workflow
   permissions* pick **Read and write permissions**.
2. **Settings → General → Features**: tick **Issues** (forks have them off).
3. **Recommended: add a `SYNC_TOKEN` secret.** The default Actions token isn't
   allowed to push changes to `.github/workflows/`, so a sync where upstream
   edited its workflows fails without one. Create a fine-grained PAT at
   <https://github.com/settings/personal-access-tokens/new> scoped to only
   this repo, with *Contents: Read and write* and *Workflows: Read and write*.
   Save it as repo secret `SYNC_TOKEN`.
4. Actions → *Fork: sync upstream + auto-update apworlds* → Run workflow
   (tick *dry run* the first time if you like).

## Pointing a lobby at it

```
APWORLDS_INDEX_REPO_URL: https://github.com/YukiRainee/Archipelago-index.git
APWORLDS_INDEX_REPO_BRANCH: "main"
```

To make it refresh right after each change, set repo variable
`LOBBY_REFRESH_URL` and secret `LOBBY_ADMIN_TOKEN`. Check your lobby's docs
for the exact endpoint and auth header first.

## Caveat

Upstream fuzz-tests every release before merging it. Auto-added versions
skip that, so do a test generation before using a freshly bumped world in a
real multiworld.
