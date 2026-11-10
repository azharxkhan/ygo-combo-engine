"""Read-only access to the Project Ignis card database (sqlite .cdb files)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .constants import TYPE_EXTRA, TYPE_LINK, TYPE_PENDULUM

MAX_CARD_CODE = 0xFFFFFFFF


@dataclass(frozen=True)
class CardRecord:
    code: int
    alias: int
    setcodes: tuple[int, ...]
    type: int
    level: int
    attribute: int
    race: int
    attack: int
    defense: int
    lscale: int
    rscale: int
    link_marker: int
    name: str
    text: str

    @property
    def is_extra_deck(self) -> bool:
        return bool(self.type & TYPE_EXTRA)


def validate_code(code: object) -> int:
    """Card codes (passcodes) must be plain positive 32-bit integers."""
    if isinstance(code, bool) or not isinstance(code, int):
        raise ValueError(f"card code must be an integer, got {type(code).__name__}")
    if not 0 < code <= MAX_CARD_CODE:
        raise ValueError(f"card code out of range: {code}")
    return code


class CardDatabase:
    """Loads one or more .cdb files. Later files override earlier ones."""

    def __init__(self, paths: list[Path]):
        if not paths:
            raise ValueError("at least one .cdb file is required")
        self._cards: dict[int, CardRecord] = {}
        for path in paths:
            self._load(Path(path))

    def _load(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"card database not found: {path}")
        # mode=ro: we never write to the database.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT d.id, d.alias, d.setcode, d.type, d.atk, d.def, d.level,"
                " d.race, d.attribute, t.name, t.desc"
                " FROM datas d LEFT JOIN texts t ON t.id = d.id"
            )
            for row in rows:
                record = self._to_record(row)
                self._cards[record.code] = record
        finally:
            conn.close()

    @staticmethod
    def _to_record(row: tuple) -> CardRecord:
        code, alias, setcode, ctype, atk, defense, level, race, attribute, name, desc = row
        setcode = (setcode or 0) & 0xFFFFFFFFFFFFFFFF
        # Up to four 16-bit archetype codes are packed into one 64-bit number.
        setcodes = tuple(
            sc for sc in ((setcode >> shift) & 0xFFFF for shift in (0, 16, 32, 48)) if sc
        )
        level = level or 0
        ctype = ctype or 0
        lscale = rscale = 0
        if ctype & TYPE_PENDULUM:
            lscale = (level >> 24) & 0xFF
            rscale = (level >> 16) & 0xFF
        link_marker = 0
        if ctype & TYPE_LINK:
            # For Link monsters the "def" column stores the arrow positions.
            link_marker = defense or 0
            defense = 0
        return CardRecord(
            code=code,
            alias=alias or 0,
            setcodes=setcodes,
            type=ctype,
            level=level & 0xFF,
            attribute=attribute or 0,
            race=race or 0,
            attack=atk or 0,
            defense=defense or 0,
            lscale=lscale,
            rscale=rscale,
            link_marker=link_marker,
            name=name or f"#{code}",
            text=desc or "",
        )

    def __len__(self) -> int:
        return len(self._cards)

    def __contains__(self, code: int) -> bool:
        return code in self._cards

    def get(self, code: int) -> CardRecord | None:
        return self._cards.get(code)

    def name(self, code: int) -> str:
        record = self._cards.get(code)
        return record.name if record else f"#{code}"

    def find_by_name(self, name: str) -> CardRecord | None:
        """Exact, case-insensitive name lookup. Returns None if not found."""
        wanted = name.strip().casefold()
        if not wanted:
            return None
        matches = [c for c in self._cards.values() if c.name.casefold() == wanted]
        if not matches:
            return None
        # Prefer the original print over alternate artworks (which carry an alias).
        matches.sort(key=lambda c: (c.alias != 0, c.code))
        return matches[0]
