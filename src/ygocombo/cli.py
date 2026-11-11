"""Command line interface.

  python -m ygocombo moves --deck decks/x.ydk --hand "Card A, Card B"
  python -m ygocombo solve --deck decks/x.ydk --hand "Card A, Card B" --want "Boss Monster"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .carddb import CardDatabase
from .decisions import parse_decision
from .deck import DeckError, load_ydk, validate_deck
from .engine import EngineError, Environment
from .goldfish import HAND_MAX, Scenario, board_summary, new_duel
from .search import BoardScorer, ComboSearch, Limits

MAX_LIST_ARG_CHARS = 2000
MAX_NAME_CHARS = 120
MAX_WANTED = 20


class InputError(ValueError):
    """Bad command line input. The message is safe to show to the user."""


def resolve_cards(raw: str, db: CardDatabase, *, limit: int, what: str) -> tuple[int, ...]:
    """Turn "Name One, 12345678, Name Two" into card codes. Fails loudly.

    Many card names contain a comma ("Nibiru, the Primal Being"). If the list
    has any ";" it is split on ";" only, so those names can be typed. No card
    name contains a ";".
    """
    if len(raw) > MAX_LIST_ARG_CHARS:
        raise InputError(f"{what} list is too long")
    separator = ";" if ";" in raw else ","
    codes: list[int] = []
    for part in raw.split(separator):
        entry = part.strip()
        if not entry:
            continue
        if len(entry) > MAX_NAME_CHARS:
            raise InputError(f"{what} entry is too long: {entry[:30]!r}...")
        if entry.isascii() and entry.isdigit():
            if len(entry) > 10 or int(entry) not in db:
                raise InputError(f"unknown card code in {what}: {entry}")
            codes.append(int(entry))
        else:
            record = db.find_by_name(entry)
            if record is None:
                hint = " (names must match exactly"
                if separator == ",":
                    hint += "; if a name has a comma, separate cards with ';' instead"
                raise InputError(f"unknown card name in {what}: {entry!r}{hint})")
            codes.append(record.code)
    if not codes:
        raise InputError(f"{what} is empty")
    if len(codes) > limit:
        raise InputError(f"{what} has {len(codes)} cards, the limit is {limit}")
    return tuple(codes)


def _print_board(board: dict[str, list[str]], indent: str = "  ") -> None:
    for zone, names in board.items():
        if names:
            print(f"{indent}{zone}: {', '.join(names)}")


def _setup(args) -> tuple[Environment, Scenario]:
    env = Environment.from_project_root(Path(args.root))
    deck = load_ydk(Path(args.deck))
    validate_deck(deck, env.db, enforce_size=not args.any_size)
    hand = resolve_cards(args.hand, env.db, limit=HAND_MAX, what="hand")
    try:
        scenario = Scenario(deck, hand, seed=args.seed)
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    return env, scenario


def cmd_moves(args) -> int:
    env, scenario = _setup(args)
    with new_duel(env, scenario) as duel:
        duel.advance()
        decision = parse_decision(duel.pending, env.db)
        print("Opening position:")
        _print_board(board_summary(duel))
        print("\nLegal moves:")
        for number, choice in enumerate(decision.choices, start=1):
            print(f"  {number}. {choice.label}")
    return 0


def cmd_solve(args) -> int:
    env, scenario = _setup(args)
    wanted = ()
    if args.want:
        wanted = resolve_cards(args.want, env.db, limit=MAX_WANTED, what="--want")
    try:
        limits = Limits(
            max_nodes=args.max_nodes,
            max_depth=args.max_depth,
            max_seconds=args.max_seconds,
            include_sets=args.include_sets,
        )
        search = ComboSearch(
            env, scenario, limits=limits, scorer=BoardScorer(env, wanted), keep=args.keep
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    result = search.run()

    print(
        f"Looked at {result.nodes} positions in {result.seconds:.1f}s "
        f"({result.replays} replays, {result.duplicates} repeats skipped, "
        f"{result.dead_ends} dead ends, {result.unsupported} unsupported prompts). "
        f"Stopped by: {result.stopped_by}."
    )
    if result.stopped_by != "finished":
        print("Search was cut short, so a better line may exist. Raise --max-nodes / --max-seconds.")
    if result.truncated_decisions:
        print(f"{result.truncated_decisions} prompts had too many answers and were capped.")
    for rank, line in enumerate(result.lines, start=1):
        print(f"\n#{rank}  score {line.score:g}  ({len(line.steps)} steps)")
        if not line.steps:
            print("  (do nothing)")
        for number, step in enumerate(line.steps, start=1):
            print(f"  {number}. {step}")
        print("  End board:")
        _print_board(line.board, indent="    ")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ygocombo", description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="project root (has build/ and vendor/)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--deck", required=True, help="path to a .ydk deck file")
        p.add_argument("--hand", required=True, help="card names or codes, separated by ',' (or ';' if a name has a comma)")
        p.add_argument("--seed", type=int, default=1, help="deck shuffle seed (default 1)")
        p.add_argument("--any-size", action="store_true", help="skip 40-60 card size check")

    moves = sub.add_parser("moves", help="list the legal first moves for a hand")
    common(moves)
    moves.set_defaults(func=cmd_moves)

    solve = sub.add_parser("solve", help="search for the best turn 1 board")
    common(solve)
    solve.add_argument("--want", default="", help="cards you want on the field (same format as --hand)")
    solve.add_argument("--max-nodes", type=int, default=2000)
    solve.add_argument("--max-depth", type=int, default=40)
    solve.add_argument("--max-seconds", type=float, default=60.0)
    solve.add_argument("--keep", type=int, default=3, help="how many lines to show")
    solve.add_argument("--include-sets", action="store_true", help="also try setting cards")
    solve.set_defaults(func=cmd_solve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.seed < 0 or args.seed > 2**32:
        print("error: --seed must be between 0 and 4294967296", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (InputError, DeckError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except EngineError as exc:
        print(f"engine error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
