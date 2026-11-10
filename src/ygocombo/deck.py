"""Strict parser for .ydk deck files (the format EDOPro and YGOPRODeck export)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .carddb import CardDatabase, validate_code

MAX_DECK_FILE_BYTES = 64 * 1024
MAIN_MIN, MAIN_MAX = 40, 60
EXTRA_MAX = 15
SIDE_MAX = 15
MAX_COPIES = 3


class DeckError(ValueError):
    """The deck file is malformed or breaks deck building rules."""


@dataclass
class Deck:
    main: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)
    side: list[int] = field(default_factory=list)


def parse_ydk(text: str) -> Deck:
    """Parse .ydk text. Rejects anything that is not a section marker or a card code."""
    deck = Deck()
    section: list[int] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line == "#main":
            section = deck.main
        elif line == "#extra":
            section = deck.extra
        elif line == "!side":
            section = deck.side
        elif line.startswith("#"):
            continue  # comment, e.g. "#created by ..."
        else:
            if not line.isascii() or not line.isdigit() or len(line) > 10:
                raise DeckError(f"line {lineno}: not a card code: {line[:40]!r}")
            if section is None:
                raise DeckError(f"line {lineno}: card code before any #main/#extra/!side")
            try:
                section.append(validate_code(int(line)))
            except ValueError as exc:
                raise DeckError(f"line {lineno}: {exc}") from exc
    return deck


def load_ydk(path: Path) -> Deck:
    path = Path(path)
    if path.suffix.lower() != ".ydk":
        raise DeckError(f"expected a .ydk file, got: {path.name}")
    if not path.is_file():
        raise DeckError(f"deck file not found: {path}")
    if path.stat().st_size > MAX_DECK_FILE_BYTES:
        raise DeckError(f"deck file too large (limit {MAX_DECK_FILE_BYTES} bytes)")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DeckError("deck file is not valid UTF-8 text") from exc
    return parse_ydk(text)


def validate_deck(deck: Deck, db: CardDatabase, *, enforce_size: bool = True) -> None:
    """Check every card exists and the deck follows basic construction rules.

    Does not check the Forbidden/Limited list yet (see docs/BUILD_PLAN.md).
    """
    for label, cards in (("main", deck.main), ("extra", deck.extra), ("side", deck.side)):
        for code in cards:
            if code not in db:
                raise DeckError(f"unknown card code in {label} deck: {code}")
    for code in deck.main:
        if db.get(code).is_extra_deck:
            raise DeckError(f"{db.name(code)} belongs in the extra deck, not the main deck")
    for code in deck.extra:
        if not db.get(code).is_extra_deck:
            raise DeckError(f"{db.name(code)} cannot go in the extra deck")
    if enforce_size:
        if not MAIN_MIN <= len(deck.main) <= MAIN_MAX:
            raise DeckError(f"main deck must be {MAIN_MIN}-{MAIN_MAX} cards, got {len(deck.main)}")
        if len(deck.extra) > EXTRA_MAX:
            raise DeckError(f"extra deck is over {EXTRA_MAX} cards")
        if len(deck.side) > SIDE_MAX:
            raise DeckError(f"side deck is over {SIDE_MAX} cards")
    counts: dict[int, int] = {}
    for code in deck.main + deck.extra + deck.side:
        record = db.get(code)
        key = record.alias or record.code  # alternate artworks count as the same card
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > MAX_COPIES:
            raise DeckError(f"more than {MAX_COPIES} copies of {db.name(key)}")
