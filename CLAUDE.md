# Project rules for Claude

## Commits and pull requests

- **Never tag yourself.** No `Co-Authored-By: Claude` line, no "Generated with
  Claude Code" line, no session links, no AI mention of any kind in commit
  messages, PR titles or PR descriptions.
- Commit as the repo owner's configured git identity. Do not change `user.name`
  or `user.email`.
- Short, plain commit messages that say what changed.
- `.claude/settings.json` already turns attribution off. Do not turn it back on.

## What this project is

A Yu-Gi-Oh! combo finder. It does **not** implement game rules. It drives the
open source EDOPro engine (`ygopro-core`) and searches the legal moves.
Read `docs/BUILD_PLAN.md` first: it has the design, what works, and what is next.

## Hard rules

- Never write rules logic or card effects by hand. The engine and the Project
  Ignis Lua scripts are the only source of truth for what is legal.
- Never commit anything under `vendor/` or `build/`. They are downloaded and
  built by `scripts/fetch_vendor.sh` and `scripts/build_engine.sh`.
- The engine and card scripts are AGPL-3.0. This repo is AGPL-3.0 too. Do not
  change the licence or add code under an incompatible licence.
- Only download code that is free to use (check the licence first) and record
  it in the "Third-party code" table in `README.md`.
- No card images or Konami artwork in the repo.

## Code standards

- Targeted edits only. Do not rewrite whole files to change a few lines.
- Verify every import exists before using it.
- Validate all outside input strictly (deck files, card names, CLI numbers).
  Reject bad input with a clear error. Never guess or auto-correct.
- Every search or loop that depends on user input needs a hard cap
  (nodes, depth, seconds). See `Limits` in `src/ygocombo/search.py`.
- If a web API is ever added: rate limit every endpoint, and read API keys
  from environment variables only. Never hard-code or log a key. `.env` files
  are git-ignored.
- `enableUnsafeLibraries` must stay `0` so card scripts get no file or OS access.
- Run `PYTHONPATH=src python3 -m pytest -q` before every commit.

## How to talk to the owner

- Plain English. Short. If a technical word is needed, add a short meaning.
- Challenge his assumptions and check facts before agreeing.
- Look up real card text before explaining any card interaction.
- No em dashes.
