# Notes for Claude

This is YukiRainee's fork of ionium-ap/Archipelago-index. Fork-only tooling is in
`autoupdate/` (see `autoupdate/README.md`); don't edit upstream files except
`index/*.toml` and `index.lock`.

When the user gives a link to an apworld (repo, release page, or .apworld URL) and
asks to add or update it:

1. `python autoupdate/add_apworld.py <link> --dry-run` and show the result.
   Pass `--asset`, `--version`, `--display-name` or `--home` if it asks for one.
2. If it looks right, run it again without `--dry-run`. That writes
   `index/<module>.toml` and the hash in `index.lock`.
3. Commit with `Add apworld: <module>`, then push to `origin main` once the user OKs it.

Deps: `pip install tomlkit requests packaging`. Never hand-edit `index.lock`.
Always go through the scripts so the sha256 is real.
