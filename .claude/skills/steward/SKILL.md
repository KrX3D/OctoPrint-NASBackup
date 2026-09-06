# Steward conventions - OctoPrint-NASBackup

Repo-specific conventions for an automated agent driving a pull request in
this repository to green and mergeable. This file only covers conventions
and how proactive to be here; it does not grant any additional access and
never overrides a rule the agent's own instructions state as "never".

## Merge strategy

- History on `main` is exclusively merge commits ("Merge pull request #N
  from ..."). Never rebase or force-push a branch you don't own.
- When bringing `main` into your own branch to resolve a conflict, use a
  merge commit, not a rebase.

## Versioning & changelog

- `octoprint_nasbackup/__init__.py`'s `__plugin_version__` string is the
  single source of truth for the plugin version. `setup.py` reads it via
  regex - never hardcode a version anywhere else.
- Any functional change (bug fix, behavior change, new option) should bump
  `__plugin_version__` and add a matching entry under `## Changelog` in
  `README.md`. Pure docs/CI/tooling changes don't need a bump.

## Translations

- `octoprint_nasbackup/translations/**/*.po` are the source files a
  contributor edits by hand. The compiled `**/*.mo` binaries are not meant
  to be hand-edited or committed as part of a feature/fix PR - they are
  regenerated and committed automatically by
  `.github/workflows/compile-translations.yml` once a change lands on
  `main`. Don't treat a PR that leaves `.mo` files unchanged/stale as an
  error; don't hand-edit `.mo` files.
- `.github/workflows/pr-checks.yml` validates that every `.po` file still
  compiles cleanly with `msgfmt --check`, alongside basic Python/JS syntax
  checks. That is the current get-green baseline for this repo - there is
  no unit test suite yet, so passing that workflow plus a clean manual
  read of the diff is what "CI green" means here.

## No Docker component

This repository is a plain OctoPrint pip plugin (installed via
`pip install -e .` or through OctoPrint's Plugin Manager, pointed at a
GitHub archive URL). There is no Dockerfile/docker-compose here, and one
shouldn't be added speculatively - don't treat its absence as a gap to fill.

## Line endings

All tracked text files are pinned to LF via `.gitattributes`
(`* text=auto eol=lf`). Don't reintroduce CRLF or bare-CR line endings.
