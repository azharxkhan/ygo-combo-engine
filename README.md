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

## Setup (Windows, native)

Needs: Git for Windows (for Git Bash), Python 3.10+, and Visual Studio 2019 or
later with the "Desktop development with C++" workload (or the Build Tools).

In Git Bash:

```bash
scripts/fetch_vendor.sh
powershell -ExecutionPolicy Bypass -File scripts/build_engine.ps1   # builds build/ocgcore.dll
python -m pip install pytest
PYTHONPATH=src python -m pytest -q
```

On Windows use `python` instead of `python3` in the commands below.

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
If a card name has a comma in it, separate the cards with `;` instead:
`--hand "Nibiru, the Primal Being; Ash Blossom & Joyous Spring; Terraforming"`.
Deck files are `.ydk` (export from EDOPro or YGOPRODeck).

## Combo board

Add `--board combo.html` to `solve` to draw the best line as a diagram:

```bash
PYTHONPATH=src python3 -m ygocombo solve \
  --deck decks/invoked_sample.ydk \
  --hand "Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters" \
  --want "Invoked Mechaba" --keep 1 --board combo.html
```

Each card is a box, and each arrow is something one card did to another:
"adds to hand", "fusion summons", "material for", "discards" and so on. Below
the diagram is every step with each card move (from where, to where, and
which card caused it), then the end board. Nothing is drawn by hand: the line
is replayed in the engine and every arrow is a move the engine reported. A
`combo.json` with the same data is written next to the page. The page is a
single file with no scripts, so it opens offline and can be shared as is.
Existing files are not replaced unless you add `--force`.

## Hand odds

How often does a random opening hand reach a board? This draws random hands
and runs the combo search on each one, so a hand only counts if the engine
actually plays it into the board. No tagging cards as starters or bricks.

```bash
PYTHONPATH=src python3 -m ygocombo odds \
  --deck decks/invoked_sample.ydk --want "Invoked Mechaba" --hands 100
```

Every hand is reported as **hit** (reached the board), **miss** (every line
within the limits was tried, none got there) or **unknown** (the search hit
its limit first; raise `--max-nodes` / `--max-seconds` to settle these). The
summary gives the hit rate and a 95% range for the true rate. 100 hands of
the sample deck take about 40 seconds. `--seed` picks a different set of
hands, `--hand-size 6` adds a sixth card, `--show-lines` prints each hit's
combo, and `--total-seconds` caps the whole run (default 30 minutes).

The odds are only as good as the search: a miss means "not found within the
limits", and the goldfish opponent never interrupts.

## Import decks

Found a deck online? Import it into `decks/`:

```bash
# a ydke:// string (EDOPro, Dueling Nexus, YGOPRODeck "Export Deck")
PYTHONPATH=src python3 -m ygocombo import "ydke://...!...!...!" --name tenpai

# a .ydk file or a plain text list
PYTHONPATH=src python3 -m ygocombo import ~/Downloads/list.txt --name snake_eye

# paste a list, then Ctrl+D (Ctrl+Z then Enter on Windows)
PYTHONPATH=src python3 -m ygocombo import - --name pasted
```

A text list has one card per line, with an optional count: `3 Ash Blossom & Joyous Spring`,
`2x Called by the Grave` or `Invoked Mechaba x2`. Lines `Main Deck`, `Extra Deck` and
`Side Deck` start each section. With no section lines, Extra Deck monsters are sorted into
the extra deck automatically. Names must match exactly, and every card is checked against
the database. The deck is saved as `decks/<name>.ydk`, and the command also prints a
`ydke://` string you can paste back into EDOPro.

Good places to find decks: tournament results on
[YGOPRODeck](https://ygoprodeck.com/tournaments/) (use "Export Deck" on a deck page).

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
