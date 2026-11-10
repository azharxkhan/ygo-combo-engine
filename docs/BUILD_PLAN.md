# Build plan

Last updated: 2026-11-10

## Goal

Give the tool a deck and an opening hand. It returns the best turn 1 line of
play, step by step, with the end board.

Not the goal (yet): an AI that plays full duels against a person. That is
research-level work and needs serious hardware.

## The one design decision that matters

**We do not write the rules.** There are 14,000+ cards. The open source engine
`ygopro-core` plus the Project Ignis Lua scripts already implement all of them
and are what EDOPro runs on. This project is a search program sitting on top.

```
  deck.ydk + hand
        |
        v
  goldfish.py   builds the starting position (you go first, opponent empty)
        |
        v
  engine.py     talks to libocgcore (C++), via Python ctypes
        |               ^
        v               |  card data: vendor/BabelCDB (sqlite)
  decisions.py          |  card logic: vendor/CardScripts (Lua)
  turns each engine prompt into a list of choices
        |
        v
  search.py     tries the choices, scores each board, keeps the best lines
        |
        v
  cli.py        prints the result
```

Words used below:

- **Goldfish**: play your combo against an opponent who does nothing.
- **Replay**: the engine cannot copy a game, so we save "the list of answers
  that got us here" and rebuild the position by answering the same way in a
  fresh duel. Same seed + same answers = same game.
- **Node**: one position the search looked at.
- **Pruning**: skipping branches that cannot be better, to save time.

## What works today (checked by tests/test_basics.py)

- [x] Milestone 1: engine builds from source, Python can start a duel, place a
      chosen hand, and list every legal move.
- [x] Milestone 2: depth-first search over turn 1 with node, depth and time
      caps. Finds Aleister -> Invocation -> Invoked Mechaba -> recycle
      Invocation on the sample deck.
- [x] Strict input checks: deck files, card names and codes, search limits,
      script names requested by the engine (no path tricks).

## Known problems, most important first

1. **Wasted moves.** The search chains quick effects (Book of Moon, Twin
   Twisters) to its own plays at every chance, which bloats the tree. On the
   sample hand 3,244 of 3,253 positions were repeats.
2. **Slow.** About 75 ms per replay, so about 13 positions per second.
3. **Scoring is a placeholder.** It counts cards. It does not know that a
   negate is worth more than a beater, and it does not punish throwing cards away.
4. **Zones are ignored.** The first free zone is always picked. That is wrong
   for Link decks, where arrows matter.
5. **Not handled:** counter removal prompts, "declare a card name" prompts,
   End Phase effects, Forbidden/Limited list, Battle Phase.

## Next steps

### Milestone 3: make the search sane (start here)

- In `search.py::_options`, for `chain` prompts: read `special_count` from the
  message (`decisions.py::_select_chain` currently throws it away). If it is 0
  the prompt has only optional quick effects and no trigger effects. Default
  to "Do not chain" only. Add `Limits.explore_chains` to turn full exploring
  back on.
- Add a cost to the scorer for cards lost from hand/field with no gain, so
  "do less, same board" wins.
- Re-run the sample hand. Target: search finishes on its own (`stopped_by:
  finished`) in under 10 seconds.
- Add 2 more sample decks with a known best line and a test for each. Pick
  decks the owner knows well so he can check the output by eye.

### Milestone 4: speed

Pick one, measure before and after:

- **Fork snapshots (Linux/macOS/WSL only):** `os.fork()` copies the whole
  process, including the engine's memory, almost for free. A child process
  explores a branch and reports back over a pipe. Removes replays entirely.
- **Cheaper replays:** profile `Duel.__init__`. Most of the 28 ms start-up is
  Lua re-reading `utility.lua` and friends for every new duel.
- **Parallel search:** one worker process per top-level branch.

### Milestone 5: real scoring

- Let the user describe a good board in a small file: wanted cards, wanted
  count of negates, cards that must stay in hand (hand traps).
- Tag cards by role (negate, floodgate, removal, follow-up) from a hand-made
  list per deck. Do not try to guess roles from card text.

### Milestone 6: zones and Links

- Explore zone choices only when the card being placed is a Link monster, or
  when a Link monster on the field points at the zone.

### Milestone 7: interruption

- Give the opponent one named hand trap (Ash Blossom, Nibiru, Effect Veiler)
  and find the line that still ends on the best board if it is used at the
  worst moment. This needs the opponent to make choices, so `_opponent_choice`
  becomes a second search.

### Milestone 8: opening hand statistics

- Deal thousands of random hands, solve each, and report "X% of hands reach
  the target board". This is the feature deck builders want most.

### Later, optional

- Forbidden/Limited list check (`ProjectIgnis/LFLists`).
- Web page front end. If built: rate limit every endpoint, cap search limits
  per request, validate uploads the same way the CLI does, keys in env vars only.
- Explain each line in plain English with an LLM. The LLM explains; it never
  decides what is legal.

## Rules for whoever builds this

See `CLAUDE.md`. Short version: never hand-write card logic, never commit
`vendor/` or `build/`, cap every search, validate every input, no AI
attribution in commits.

## Facts worth knowing about the engine

- API: 13 C functions in `vendor/ygopro-core/ocgapi.h`. API version 11.0.
- Prompt formats and the exact answer bytes are in
  `vendor/ygopro-core/playerop.cpp`. `decisions.py` mirrors that file.
- The host must load `constant.lua` and `utility.lua` after creating a duel.
- The engine does not shuffle the starting deck. `goldfish.py` does, with a seed.
- A wrong answer makes the engine send `MSG_RETRY`. We raise `InvalidResponse`
  and drop that branch.
- Pinned versions are in `scripts/fetch_vendor.sh`. Bump on purpose, then test.
- Reference project doing something similar: YGO Judge (an MCP server over the
  same engine), https://glama.ai/mcp/servers/lukaznj/ygo-judge
