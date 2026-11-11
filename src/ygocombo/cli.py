"""Command line interface.

  python -m ygocombo moves --deck decks/x.ydk --hand "Card A, Card B"
  python -m ygocombo solve --deck decks/x.ydk --hand "Card A, Card B" --want "Boss Monster"
  python -m ygocombo odds --deck decks/x.ydk --want "Boss Monster" --hands 50
  python -m ygocombo interrupt --deck decks/x.ydk --hand "..." --want "Boss Monster" --opp "Ash Blossom & Joyous Spring"
  python -m ygocombo import "ydke://..." --name my_deck
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .board import check_board_path, trace_line, write_board
from .carddb import CardDatabase
from .decisions import parse_decision
from .deck import DeckError, load_ydk, validate_deck
from .engine import EngineError, Environment, project_databases
from .goldfish import HAND_MAX, OPPONENT_HAND_MAX, Scenario, board_summary, new_duel
from .interrupt import MAX_LINES as INTERRUPT_LINES_MAX
from .interrupt import TOTAL_SECONDS_MAX as INTERRUPT_SECONDS_MAX
from .interrupt import Outcome, analyse
from .importer import check_deck_name, parse_text, read_deck, save_deck, to_ydke
from .odds import TOTAL_SECONDS_MAX, HandResult, check_settings, run_odds, wilson_interval
from .search import BoardScorer, ComboSearch, Limits

MAX_STDIN_CHARS = 64 * 1024

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
    board_path = None
    if args.board:
        try:
            board_path = check_board_path(args.board, overwrite=args.force)
        except ValueError as exc:
            raise InputError(str(exc)) from exc
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
    if board_path:
        if not result.lines:
            print("\nNo line found, so no combo board was written.")
        else:
            hand = ", ".join(env.db.name(c) for c in scenario.hand)
            board = trace_line(env, scenario, result.lines[0].responses, title=f"Combo: {hand}")
            html_path, json_path = write_board(board, board_path)
            print(f"\nCombo board for line #1: {html_path} (data: {json_path})")
    return 0


def cmd_odds(args) -> int:
    env = Environment.from_project_root(Path(args.root))
    deck = load_ydk(Path(args.deck))
    validate_deck(deck, env.db, enforce_size=not args.any_size)
    wanted = resolve_cards(args.want, env.db, limit=MAX_WANTED, what="--want")
    db = env.db

    def show(number: int, hand: HandResult) -> None:
        names = ", ".join(db.name(code) for code in hand.hand)
        note = " (same cards as an earlier hand)" if hand.cached else f" ({hand.seconds:.1f}s)"
        print(f"  {number:>4}. {hand.outcome.upper():<7} {names}{note}", flush=True)
        if args.show_lines and hand.outcome == "hit" and not hand.cached:
            for step_number, step in enumerate(hand.steps, start=1):
                print(f"          {step_number}. {step}")

    try:
        check_settings(args.hands, args.hand_size, args.seed)
        limits = Limits(max_nodes=args.max_nodes, max_depth=args.max_depth,
                        max_seconds=args.max_seconds, include_sets=args.include_sets)
        if not 0 < args.total_seconds <= TOTAL_SECONDS_MAX:
            raise ValueError(f"--total-seconds must be above 0 and at most {TOTAL_SECONDS_MAX:g}")
        print(f"Drawing {args.hands} hands of {args.hand_size} cards (seed {args.seed}). "
              "Each one is searched for the wanted board.", flush=True)
        result = run_odds(env, deck, wanted, hands=args.hands, hand_size=args.hand_size,
                          seed=args.seed, limits=limits, total_seconds=args.total_seconds,
                          on_hand=show)
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    total = len(result.hands)
    hits, misses, unknown = result.count("hit"), result.count("miss"), result.count("unknown")
    want_names = ", ".join(db.name(code) for code in wanted)
    print(f"\nWanted on the field: {want_names}")
    print(f"Searched {total} hands in {result.seconds:.0f}s: "
          f"{hits} hit, {misses} miss, {unknown} unknown.")
    if result.stopped_by == "total_seconds":
        print(f"Stopped early: --total-seconds ran out before all {args.hands} hands were done.")
    if not total:
        return 0
    print(f"Hit rate: {result.low:.0%}", end="")
    if unknown:
        print(f" to {result.high:.0%} (unknown hands counted as miss / as hit)", end="")
    print()
    low, high = wilson_interval(hits, total)
    print(f"With {total} hands, the true rate is likely between {low:.0%} and {high:.0%} "
          "(95% range, unknown hands counted as miss). More hands make this range narrower.")
    if unknown:
        print("Unknown = the search hit its limit before finding the board. "
              "Raise --max-nodes / --max-seconds to settle them.")
    return 0


def _outcome_text(outcome: Outcome, wanted: bool) -> str:
    if outcome.stopped_by not in ("finished", "goal") and not outcome.reached_goal:
        verdict = "UNSURE (search limit hit)"
    elif wanted:
        verdict = "still makes the board" if outcome.reached_goal else "STOPS the board"
    else:
        verdict = f"score drops to {outcome.score:g}"
    monsters = [n.split(" [")[0] for n in outcome.board.get("monster zone", [])]
    left = f"; you end with: {', '.join(monsters) or 'no monsters'}" if not outcome.reached_goal else ""
    return f"{verdict}{left}"


def cmd_interrupt(args) -> int:
    env, plain = _setup(args)
    wanted = ()
    if args.want:
        wanted = resolve_cards(args.want, env.db, limit=MAX_WANTED, what="--want")
    opponent = resolve_cards(args.opp, env.db, limit=OPPONENT_HAND_MAX, what="--opp")
    db = env.db
    try:
        scenario = Scenario(plain.deck, plain.hand, seed=plain.seed, opponent_hand=opponent)
        limits = Limits(max_nodes=args.max_nodes, max_depth=args.max_depth,
                        max_seconds=args.max_seconds, include_sets=args.include_sets)
        recovery = Limits(max_nodes=args.recovery_nodes, max_depth=args.max_depth,
                          max_seconds=args.recovery_seconds, include_sets=args.include_sets)
        if not 1 <= args.lines <= INTERRUPT_LINES_MAX:
            raise ValueError(f"--lines must be 1-{INTERRUPT_LINES_MAX}")
        if not 0 < args.total_seconds <= INTERRUPT_SECONDS_MAX:
            raise ValueError(f"--total-seconds must be above 0 and at most {INTERRUPT_SECONDS_MAX:g}")
        print(f"Opponent holds: {', '.join(db.name(c) for c in opponent)} "
              "(they use one card, once). Searching...", flush=True)
        result = analyse(env, scenario, wanted, lines=args.lines, limits=limits,
                         recovery_limits=recovery, total_seconds=args.total_seconds)
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    checked = sum(len(r.outcomes) for r in result.reports)
    print(f"Found {len(result.reports)} lines, checked {checked} interruption points "
          f"in {result.seconds:.0f}s.")
    if result.base_stopped_by != "finished":
        print("The first search was cut short, so other lines may exist. Raise --max-nodes / --max-seconds.")
    if result.stopped_by != "finished":
        print("Stopped early: --total-seconds ran out, so some points were not checked.")
    for rank, report in enumerate(result.reports, start=1):
        line = report.line
        monsters = [n.split(" [")[0] for n in line.board.get("monster zone", [])]
        title = "safest line" if rank == 1 else f"line {rank}"
        print(f"\n#{rank} ({title}): {', '.join(monsters) or 'no monsters'} if nobody interrupts "
              f"(score {line.score:g}, {len(line.steps)} steps)")
        for number, step in enumerate(line.steps, start=1):
            print(f"  {number}. {step}")
        if not report.outcomes:
            print("  The opponent never gets a chance to use their cards on this line.")
            continue
        print(f"  Where it can be interrupted ({len(report.outcomes)}):")
        for outcome in report.outcomes:
            point = outcome.interruption
            where = f"after step {point.after_steps}" if point.after_steps else "before your first move"
            print(f"    - {where}: {' -> '.join(point.opponent)}: {_outcome_text(outcome, bool(wanted))}")
        if report.points_capped:
            print("    (more points exist, only the first ones were checked)")
        worst = report.worst
        print(f"  Worst case: {' -> '.join(worst.interruption.opponent)} "
              f"after step {worst.interruption.after_steps}. Best play from there:")
        for number, step in enumerate(worst.steps, start=1):
            print(f"    {number}. {step}")
        if not worst.steps:
            print("    (nothing useful left to do)")
        _print_board(worst.board, indent="    ")
    return 0


def cmd_import(args) -> int:
    check_deck_name(args.name)  # fail before doing any work
    try:
        db = CardDatabase(project_databases(Path(args.root)))
    except FileNotFoundError as exc:
        raise InputError(f"{exc} (run scripts/fetch_vendor.sh)") from exc
    if args.source == "-":
        text = sys.stdin.read(MAX_STDIN_CHARS + 1)
        if len(text) > MAX_STDIN_CHARS:
            raise InputError(f"pasted decklist is too long (limit {MAX_STDIN_CHARS} characters)")
        deck, kind = parse_text(text, db)
    else:
        deck, kind = read_deck(args.source, db)
    validate_deck(deck, db, enforce_size=not args.any_size)
    path = save_deck(deck, Path(args.root) / "decks", args.name, overwrite=args.force)

    print(f"Imported {kind}: {len(deck.main)} main, {len(deck.extra)} extra, {len(deck.side)} side")
    for label, cards in (("Main", deck.main), ("Extra", deck.extra), ("Side", deck.side)):
        if cards:
            counts: dict[int, int] = {}
            for code in cards:
                counts[code] = counts.get(code, 0) + 1
            print(f"  {label}: " + ", ".join(f"{n}x {db.name(c)}" for c, n in counts.items()))
    print(f"Saved {path}")
    print(f"Share as: {to_ydke(deck)}")
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
    solve.add_argument("--board", default="", help="write the best line as a combo board, e.g. combo.html")
    solve.add_argument("--force", action="store_true", help="replace an existing --board file")
    solve.set_defaults(func=cmd_solve)

    odds = sub.add_parser("odds", help="how often a random hand reaches the wanted board")
    odds.add_argument("--deck", required=True, help="path to a .ydk deck file")
    odds.add_argument("--want", required=True, help="cards that must all be on the field (same format as --hand)")
    odds.add_argument("--hands", type=int, default=50, help="random hands to try (default 50, max 1000)")
    odds.add_argument("--hand-size", type=int, default=5, help="5 (default) or 6")
    odds.add_argument("--seed", type=int, default=1, help="seed for drawing hands (default 1)")
    odds.add_argument("--max-nodes", type=int, default=300, help="search limit per hand (default 300)")
    odds.add_argument("--max-depth", type=int, default=40)
    odds.add_argument("--max-seconds", type=float, default=20.0, help="time limit per hand (default 20)")
    odds.add_argument("--total-seconds", type=float, default=1800.0, help="time limit for the whole run (default 1800)")
    odds.add_argument("--include-sets", action="store_true", help="also try setting cards")
    odds.add_argument("--show-lines", action="store_true", help="print the combo for each hit")
    odds.add_argument("--any-size", action="store_true", help="skip 40-60 card size check")
    odds.set_defaults(func=cmd_odds)

    inter = sub.add_parser("interrupt", help="where hand traps stop a line, and how to play around them")
    common(inter)
    inter.add_argument("--opp", required=True, help="opponent's hand traps (same format as --hand)")
    inter.add_argument("--want", default="", help="cards you want on the field (same format as --hand)")
    inter.add_argument("--lines", type=int, default=3, help="how many of the best lines to test (default 3)")
    inter.add_argument("--max-nodes", type=int, default=2000)
    inter.add_argument("--max-depth", type=int, default=40)
    inter.add_argument("--max-seconds", type=float, default=60.0)
    inter.add_argument("--recovery-nodes", type=int, default=300, help="search limit after each interruption")
    inter.add_argument("--recovery-seconds", type=float, default=20.0, help="time limit after each interruption")
    inter.add_argument("--total-seconds", type=float, default=900.0, help="time limit for the whole run")
    inter.add_argument("--include-sets", action="store_true", help="also try setting cards")
    inter.set_defaults(func=cmd_interrupt)

    imp = sub.add_parser("import", help="import a deck (ydke://, .ydk or text list) into decks/")
    imp.add_argument("source", help="a ydke:// string, a .ydk or .txt file, or - to paste text")
    imp.add_argument("--name", required=True, help="save as decks/NAME.ydk (letters, digits, - and _)")
    imp.add_argument("--force", action="store_true", help="replace an existing deck with that name")
    imp.add_argument("--any-size", action="store_true", help="skip 40-60 card size check")
    imp.set_defaults(func=cmd_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "seed") and not 0 <= args.seed <= 2**32:
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
