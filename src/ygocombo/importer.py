"""Import decks from the formats people post online and save them as .ydk.

Supported inputs:

* ``ydke://`` strings (EDOPro, Dueling Nexus, YGOPRODeck export).
  Format: ydke://<main>!<extra>!<side>! where each part is standard base64
  of 32-bit little-endian passcodes.
* ``.ydk`` text (checked with the strict parser in deck.py).
* Plain text lists, one card per line::

      Main Deck
      3 Ash Blossom & Joyous Spring
      2x Infinite Impermanence
      Called by the Grave
      Extra Deck
      Invoked Mechaba x2

  A line is a card name or passcode, with an optional count (1-3) as
  "3 Name", "3x Name" or "Name x3". Headers "Main", "Extra", "Side" (with
  optional "Deck", ":" or a count) switch sections. Monster/Spell/Trap
  headers count as main deck. Without any header, Extra Deck monsters go to
  the extra deck and everything else to the main deck.

Every card must exist in the card database. Nothing is guessed or
auto-corrected: an unknown name is an error that names the line.
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from pathlib import Path

from .carddb import CardDatabase, validate_code
from .deck import MAX_COPIES, MAX_DECK_FILE_BYTES, Deck, DeckError, parse_ydk

YDKE_PREFIX = "ydke://"
MAX_SECTION_CARDS = 100  # far above any legal section; stops absurd input early
MAX_LINE_CHARS = 200
DECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}

_HEADER_RE = re.compile(
    r"^#?\s*(main|extra|side|monsters?|spells?|traps?)(\s+deck)?\s*:?\s*(\(\s*\d+\s*\)|\d+)?\s*:?$",
    re.IGNORECASE,
)
_COUNT_FIRST_RE = re.compile(r"^(\d+)\s*x?\s+(.+)$", re.IGNORECASE)
_COUNT_LAST_RE = re.compile(r"^(.+?)\s+x\s*(\d+)$", re.IGNORECASE)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


# ------------------------------------------------------------------ ydke://


def parse_ydke(text: str) -> Deck:
    """Decode a ydke:// string. Rejects anything that is not exactly that format."""
    text = text.strip()
    if not text.startswith(YDKE_PREFIX):
        raise DeckError("not a ydke:// string")
    body = text[len(YDKE_PREFIX):]
    if len(body) > MAX_DECK_FILE_BYTES:
        raise DeckError("ydke string is too long")
    parts = body.split("!")
    # Three sections, each followed by "!". Allow a missing final "!".
    if len(parts) == 4 and parts[3] == "":
        parts = parts[:3]
    if len(parts) != 3:
        raise DeckError("ydke string must have 3 sections: ydke://main!extra!side!")
    sections = [_decode_ydke_section(part, name) for part, name in zip(parts, ("main", "extra", "side"))]
    return Deck(main=sections[0], extra=sections[1], side=sections[2])


def _decode_ydke_section(part: str, name: str) -> list[int]:
    if not _BASE64_RE.match(part):
        raise DeckError(f"ydke {name} section is not valid base64")
    padded = part + "=" * (-len(part) % 4)  # some exporters drop the padding
    try:
        raw = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DeckError(f"ydke {name} section is not valid base64") from exc
    if len(raw) % 4:
        raise DeckError(f"ydke {name} section has a partial card code")
    count = len(raw) // 4
    if count > MAX_SECTION_CARDS:
        raise DeckError(f"ydke {name} section has too many cards ({count})")
    codes = []
    for (code,) in struct.iter_unpack("<I", raw):
        try:
            codes.append(validate_code(code))
        except ValueError as exc:
            raise DeckError(f"ydke {name} section: {exc}") from exc
    return codes


def to_ydke(deck: Deck) -> str:
    """Encode a deck as a ydke:// string (for sharing back to EDOPro etc.)."""
    def enc(codes: list[int]) -> str:
        return base64.b64encode(b"".join(struct.pack("<I", c) for c in codes)).decode("ascii")

    return f"{YDKE_PREFIX}{enc(deck.main)}!{enc(deck.extra)}!{enc(deck.side)}!"


# ------------------------------------------------------------------ text lists


def parse_text_list(text: str, db: CardDatabase) -> Deck:
    """Parse a plain text decklist. See the module docstring for the format."""
    if len(text.encode("utf-8")) > MAX_DECK_FILE_BYTES:
        raise DeckError(f"decklist is too large (limit {MAX_DECK_FILE_BYTES} bytes)")
    deck = Deck()
    section: list[int] | None = None  # None = no header seen yet, sort by card type
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if len(line) > MAX_LINE_CHARS:
            raise DeckError(f"line {lineno}: line is too long")
        header = _HEADER_RE.match(line)
        if header:
            word = header.group(1).lower()
            section = deck.extra if word == "extra" else deck.side if word == "side" else deck.main
            continue
        if line.startswith("#"):
            continue  # other comment lines
        count, code = _parse_card_line(line, lineno, db)
        target = section
        if target is None:
            target = deck.extra if db.get(code).is_extra_deck else deck.main
        target.extend([code] * count)
        if len(target) > MAX_SECTION_CARDS:
            raise DeckError(f"line {lineno}: too many cards in one section")
    if not (deck.main or deck.extra or deck.side):
        raise DeckError("decklist has no cards")
    return deck


def _parse_card_line(line: str, lineno: int, db: CardDatabase) -> tuple[int, int]:
    # A whole-line match wins, so names like "7 Colored Fish" are not read as
    # 7 copies of "Colored Fish".
    code = _lookup(line, db)
    if code is not None:
        return 1, code
    for pattern, count_group, name_group in ((_COUNT_FIRST_RE, 1, 2), (_COUNT_LAST_RE, 2, 1)):
        match = pattern.match(line)
        if match:
            count = int(match.group(count_group)) if len(match.group(count_group)) <= 2 else 0
            if not 1 <= count <= MAX_COPIES:
                raise DeckError(f"line {lineno}: copy count must be 1-{MAX_COPIES}: {line[:60]!r}")
            code = _lookup(match.group(name_group).strip(), db)
            if code is None:
                raise DeckError(f"line {lineno}: unknown card {match.group(name_group).strip()[:60]!r} "
                                "(names must match exactly)")
            return count, code
    raise DeckError(f"line {lineno}: unknown card {line[:60]!r} (names must match exactly)")


def _lookup(entry: str, db: CardDatabase) -> int | None:
    if entry.isascii() and entry.isdigit():
        if len(entry) > 10:
            return None
        code = int(entry)
        return code if code in db else None
    record = db.find_by_name(entry)
    return record.code if record else None


# ------------------------------------------------------------------ dispatch and save


def read_deck(source: str, db: CardDatabase) -> tuple[Deck, str]:
    """Read a deck from a ydke:// string or a file path. Returns (deck, format name)."""
    if source.strip().startswith(YDKE_PREFIX):
        return parse_ydke(source), "ydke"
    path = Path(source)
    if not path.is_file():
        raise DeckError(f"not a ydke:// string and no such file: {source[:80]}")
    if path.stat().st_size > MAX_DECK_FILE_BYTES:
        raise DeckError(f"file too large (limit {MAX_DECK_FILE_BYTES} bytes)")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DeckError("file is not valid UTF-8 text") from exc
    return parse_text(text, db, ydk=path.suffix.lower() == ".ydk")


def parse_text(text: str, db: CardDatabase, *, ydk: bool = False) -> tuple[Deck, str]:
    """Parse pasted text: ydke string, .ydk content, or a plain list."""
    stripped = text.strip()
    if stripped.startswith(YDKE_PREFIX):
        return parse_ydke(stripped), "ydke"
    if ydk or re.search(r"^\s*(#main|#extra|!side)\s*$", text, re.MULTILINE):
        return parse_ydk(text), "ydk"
    return parse_text_list(text, db), "text list"


def check_deck_name(name: str) -> str:
    if not DECK_NAME_RE.match(name):
        raise DeckError("deck name must be 1-64 characters: letters, digits, '-' or '_', "
                        "starting with a letter or digit")
    if name.upper() in _WINDOWS_RESERVED:
        raise DeckError(f"{name!r} is a reserved file name on Windows, pick another")
    return name


def format_ydk(deck: Deck, comment: str = "imported by ygocombo") -> str:
    lines = [f"#{comment}", "#main", *map(str, deck.main), "#extra", *map(str, deck.extra),
             "!side", *map(str, deck.side)]
    return "\n".join(lines) + "\n"


def save_deck(deck: Deck, decks_dir: Path, name: str, *, overwrite: bool = False) -> Path:
    """Write decks_dir/<name>.ydk. The name is checked so it cannot escape decks_dir."""
    check_deck_name(name)
    decks_dir = Path(decks_dir)
    if not decks_dir.is_dir():
        raise DeckError(f"deck folder not found: {decks_dir}")
    path = decks_dir / f"{name}.ydk"
    if path.exists() and not overwrite:
        raise DeckError(f"{path} already exists (use --force to replace it)")
    path.write_text(format_ydk(deck), encoding="utf-8")
    return path
