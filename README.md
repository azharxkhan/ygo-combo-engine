# ygo-combo-engine

A Yu-Gi-Oh! combo finder. Give it a deck and an opening hand, and it searches
the legal plays for the best turn 1 board.

It does not re-implement the rules. It drives the same open source engine that
EDOPro uses (`ygopro-core`) with the Project Ignis card scripts, so every move
it finds is one the simulator would allow.

Status: early. Milestones 1 and 2 of [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) work.

## Setup (Linux, macOS, or WSL on Windows)

Needs: `git`, `g++` (or `clang++`) with C++17, Python 3.10+.

```bash
scripts/fetch_vendor.sh     # download engine, card scripts, card database (~140 MB)
scripts/build_engine.sh     # compile the engine (~1 minute)
python3 -m pip install pytest
PYTHONPATH=src python3 -m pytest -q
```

## Use

List the legal first moves for a hand:

```bash
PYTHONPATH=src python3 -m ygocombo moves \
  --deck decks/invoked_sample.ydk \
  --hand "Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters"
```

Search for a combo:

```bash
PYTHONPATH=src python3 -m ygocombo solve \
  --deck decks/invoked_sample.ydk \
  --hand "Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters" \
  --want "Invoked Mechaba" --max-nodes 400
```

`--hand` and `--want` take exact card names or passcodes, separated by commas.
Deck files are `.ydk` (export from EDOPro or YGOPRODeck).

## Limits to know about

- Goldfish only: the opponent has no cards and never responds.
- Card scripts follow OCG rulings. TCG rulings sometimes differ, and new cards
  can have script bugs. Treat results as "the simulator allows this", not as
  a judge ruling.
- No Forbidden/Limited list check yet.
- Zone choice, battle position and card setting are simplified. See the plan.

## Third-party code

Nothing below is stored in this repo. `scripts/fetch_vendor.sh` downloads it.

| What | Source | Licence |
|---|---|---|
| Rules engine | [edo9300/ygopro-core](https://github.com/edo9300/ygopro-core) | AGPL-3.0-or-later |
| Lua (bundled by the engine) | [lua/lua](https://github.com/lua/lua) | MIT |
| Card scripts | [ProjectIgnis/CardScripts](https://github.com/ProjectIgnis/CardScripts) | AGPL-3.0-or-later |
| Card database | [ProjectIgnis/BabelCDB](https://github.com/ProjectIgnis/BabelCDB) | see that repo |

## Licence

AGPL-3.0-or-later, because this program links the AGPL engine. If you host a
modified version for other people, you must give them the source.

Not affiliated with or endorsed by Konami. Yu-Gi-Oh! is a trademark of
Shueisha and Konami.
