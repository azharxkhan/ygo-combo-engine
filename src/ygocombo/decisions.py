"""Turn the engine's "please choose" messages into a plain list of choices.

Every decision becomes:  Decision(kind, player, choices=[Choice, ...])
Each Choice already carries the exact bytes to send back to the engine, so the
search code never has to know the wire format.

Wire formats are taken from vendor/ygopro-core/playerop.cpp.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from itertools import combinations, islice

from . import constants as C
from .carddb import CardDatabase
from .engine import Message

# Safety cap: a "pick 3 of 20" prompt has 1140 answers. We only keep this many.
MAX_CHOICES_PER_DECISION = 64


class UnsupportedDecision(NotImplementedError):
    """A prompt type this project does not handle yet."""


@dataclass(frozen=True)
class Choice:
    label: str
    response: bytes
    action: str = ""  # machine-readable tag, e.g. "summon", "activate", "end_phase"
    codes: tuple[int, ...] = ()  # card codes involved, if any


@dataclass
class Decision:
    kind: str
    player: int
    choices: list[Choice] = field(default_factory=list)
    truncated: bool = False  # True if some legal answers were dropped by the cap


class _Reader:
    """Sequential little-endian reader over a message payload."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def _take(self, fmt: str):
        (value,) = struct.unpack_from(fmt, self.data, self.offset)
        self.offset += struct.calcsize(fmt)
        return value

    def u8(self) -> int:
        return self._take("<B")

    def u16(self) -> int:
        return self._take("<H")

    def u32(self) -> int:
        return self._take("<I")

    def u64(self) -> int:
        return self._take("<Q")

    def loc_info(self) -> tuple[int, int, int, int]:
        """controller, location, sequence, position"""
        return self.u8(), self.u8(), self.u32(), self.u32()


def _i32(value: int) -> bytes:
    return struct.pack("<i", value)


def _card_list_response(indices: tuple[int, ...]) -> bytes:
    # type 0 = list of u32 indices: [i32 0][u32 count][u32 index]...
    return struct.pack(f"<iI{len(indices)}I", 0, len(indices), *indices)


def _where(location: int) -> str:
    return C.LOCATION_NAMES.get(location, f"location {location:#x}")


def parse_decision(message: Message, db: CardDatabase) -> Decision:
    parser = _PARSERS.get(message.type)
    if parser is None:
        raise UnsupportedDecision(f"no handler for engine message type {message.type}")
    return parser(_Reader(message.data), db)


# ------------------------------------------------------------ main phase


def _idle_command(r: _Reader, db: CardDatabase) -> Decision:
    decision = Decision("idle", r.u8())

    def simple_group(category: int, verb: str, action: str, narrow_seq: bool = False) -> None:
        for index in range(r.u32()):
            code, _con, loc = r.u32(), r.u8(), r.u8()
            _seq = r.u8() if narrow_seq else r.u32()
            decision.choices.append(
                Choice(
                    f"{verb} {db.name(code)} (from {_where(loc)})",
                    _i32((index << 16) | category),
                    action,
                    (code,),
                )
            )

    simple_group(0, "Normal Summon", "summon")
    simple_group(1, "Special Summon", "spsummon")
    simple_group(2, "Change position of", "reposition", narrow_seq=True)
    simple_group(3, "Set monster", "mset")
    simple_group(4, "Set spell/trap", "sset")
    for index in range(r.u32()):
        code, _con, loc, _seq = r.u32(), r.u8(), r.u8(), r.u32()
        _description, _mode = r.u64(), r.u8()
        decision.choices.append(
            Choice(
                f"Activate {db.name(code)} (in {_where(loc)})",
                _i32((index << 16) | 5),
                "activate",
                (code,),
            )
        )
    can_battle, can_end, _can_shuffle = r.u8(), r.u8(), r.u8()
    if can_battle:
        decision.choices.append(Choice("Go to Battle Phase", _i32(6), "battle_phase"))
    if can_end:
        decision.choices.append(Choice("Go to End Phase", _i32(7), "end_phase"))
    return decision


def _battle_command(r: _Reader, db: CardDatabase) -> Decision:
    decision = Decision("battle", r.u8())
    for index in range(r.u32()):
        code, _con, loc, _seq = r.u32(), r.u8(), r.u8(), r.u32()
        r.u64(), r.u8()
        decision.choices.append(
            Choice(
                f"Activate {db.name(code)} (in {_where(loc)})",
                _i32(index << 16),
                "activate",
                (code,),
            )
        )
    for index in range(r.u32()):
        code, _con, _loc, _seq, _direct = r.u32(), r.u8(), r.u8(), r.u8(), r.u8()
        decision.choices.append(
            Choice(f"Attack with {db.name(code)}", _i32((index << 16) | 1), "attack", (code,))
        )
    to_m2, to_ep = r.u8(), r.u8()
    if to_m2:
        decision.choices.append(Choice("Go to Main Phase 2", _i32(2), "main_phase_2"))
    if to_ep:
        decision.choices.append(Choice("Go to End Phase", _i32(3), "end_phase"))
    return decision


# ------------------------------------------------------------ yes / no


def _effect_yes_no(r: _Reader, db: CardDatabase) -> Decision:
    player, code = r.u8(), r.u32()
    return Decision(
        "effect_yes_no",
        player,
        [
            Choice(f"Use effect of {db.name(code)}", _i32(1), "yes", (code,)),
            Choice(f"Do not use effect of {db.name(code)}", _i32(0), "no", (code,)),
        ],
    )


def _yes_no(r: _Reader, _db: CardDatabase) -> Decision:
    return Decision(
        "yes_no", r.u8(), [Choice("Yes", _i32(1), "yes"), Choice("No", _i32(0), "no")]
    )


def _option(r: _Reader, _db: CardDatabase) -> Decision:
    player, count = r.u8(), r.u8()
    return Decision(
        "option",
        player,
        [Choice(f"Option {i + 1} of {count}", _i32(i), "option") for i in range(count)],
    )


# ------------------------------------------------------------ picking cards


def _combination_choices(
    decision: Decision, cards: list[tuple[int, int]], low: int, high: int, verb: str,
    db: CardDatabase,
) -> None:
    """Add one Choice per way of picking between `low` and `high` cards."""
    high = min(high, len(cards))
    low = max(low, 1)
    all_picks = (
        pick for size in range(low, high + 1) for pick in combinations(range(len(cards)), size)
    )
    room = MAX_CHOICES_PER_DECISION - len(decision.choices)
    picks = list(islice(all_picks, room + 1))
    if len(picks) > room:
        picks = picks[:room]
        decision.truncated = True
    for pick in picks:
        names = ", ".join(f"{db.name(cards[i][0])} ({_where(cards[i][1])})" for i in pick)
        decision.choices.append(
            Choice(
                f"{verb}: {names}",
                _card_list_response(pick),
                "select",
                tuple(cards[i][0] for i in pick),
            )
        )


def _select_card(r: _Reader, db: CardDatabase) -> Decision:
    player, cancelable, low, high = r.u8(), r.u8(), r.u32(), r.u32()
    cards = []
    for _ in range(r.u32()):
        code = r.u32()
        _con, loc, _seq, _pos = r.loc_info()
        cards.append((code, loc))
    decision = Decision("select_card", player)
    _combination_choices(decision, cards, low, high, "Select", db)
    if cancelable:
        decision.choices.append(Choice("Select nothing / cancel", _i32(-1), "cancel"))
    return decision


def _select_tribute(r: _Reader, db: CardDatabase) -> Decision:
    player, cancelable, low, high = r.u8(), r.u8(), r.u32(), r.u32()
    cards = []
    for _ in range(r.u32()):
        code, _con, loc, _seq, _weight = r.u32(), r.u8(), r.u8(), r.u32(), r.u8()
        cards.append((code, loc))
    decision = Decision("select_tribute", player)
    # Some monsters count as two tributes, so a pick can be invalid. The engine
    # answers MSG_RETRY for those and the caller skips that branch.
    _combination_choices(decision, cards, 1, high, "Tribute", db)
    if cancelable:
        decision.choices.append(Choice("Cancel", _i32(-1), "cancel"))
    return decision


def _select_unselect(r: _Reader, db: CardDatabase) -> Decision:
    """Cards are picked one at a time; the engine re-asks until done."""
    player, finishable, cancelable, _low, _high = r.u8(), r.u8(), r.u8(), r.u32(), r.u32()
    decision = Decision("select_unselect", player)
    index = 0
    for _ in range(r.u32()):
        code = r.u32()
        _con, loc, _seq, _pos = r.loc_info()
        decision.choices.append(
            Choice(
                f"Select {db.name(code)} ({_where(loc)})",
                struct.pack("<ii", 1, index),
                "select",
                (code,),
            )
        )
        index += 1
    # Already-picked cards follow. Un-picking them only creates loops, so we
    # read past them and do not offer it.
    for _ in range(r.u32()):
        r.u32()
        r.loc_info()
    if finishable:
        decision.choices.append(Choice("Finish selection", _i32(-1), "finish"))
    elif cancelable:
        decision.choices.append(Choice("Cancel", _i32(-1), "cancel"))
    return decision


def _select_sum(r: _Reader, db: CardDatabase) -> Decision:
    """Pick cards whose levels/values add up to a target (e.g. Synchro, Ritual)."""
    player, _mode, _target, low, high = r.u8(), r.u8(), r.u32(), r.u32(), r.u32()
    for _ in range(r.u32()):  # cards that are forced into the pick
        r.u32()
        r.loc_info()
        r.u32()
    cards = []
    for _ in range(r.u32()):
        code = r.u32()
        _con, loc, _seq, _pos = r.loc_info()
        r.u32()
        cards.append((code, loc))
    decision = Decision("select_sum", player)
    # We do not solve the sum ourselves. Wrong picks get MSG_RETRY from the engine.
    _combination_choices(decision, cards, max(low, 1), high or len(cards), "Use", db)
    return decision


# ------------------------------------------------------------ chains


def _select_chain(r: _Reader, db: CardDatabase) -> Decision:
    player, _special_count, forced = r.u8(), r.u8(), r.u8()
    r.u32(), r.u32()  # timing hints
    decision = Decision("chain", player)
    for index in range(r.u32()):
        code = r.u32()
        _con, loc, _seq, _pos = r.loc_info()
        r.u64(), r.u8()
        decision.choices.append(
            Choice(
                f"Chain {db.name(code)} (in {_where(loc)})", _i32(index), "activate", (code,)
            )
        )
    if not forced:
        decision.choices.append(Choice("Do not chain", _i32(-1), "pass"))
    return decision


def _sort(r: _Reader, _db: CardDatabase) -> Decision:
    # -1 keeps the engine's default order. Ordering choices are not explored yet.
    return Decision("sort", r.u8(), [Choice("Keep default order", struct.pack("<b", -1), "sort")])


# ------------------------------------------------------------ zones


def _select_place(r: _Reader, _db: CardDatabase) -> Decision:
    player, count, blocked = r.u8(), r.u8(), r.u32()
    # `blocked` has one bit per zone; a set bit means the zone can NOT be used.
    # Low 16 bits = the choosing player's side, high 16 = the opponent's side.
    zones: list[tuple[int, int, int, str]] = []
    for side, shift in ((player, 0), (1 - player, 16)):
        whose = "own" if side == player else "opponent's"
        for seq in range(7):
            if not blocked & (1 << (shift + seq)):
                name = f"{whose} monster zone {seq}" if seq < 5 else f"extra monster zone {seq - 5}"
                zones.append((side, C.LOCATION_MZONE, seq, name))
        for seq in range(8):
            if not blocked & (1 << (shift + 8 + seq)):
                zones.append((side, C.LOCATION_SZONE, seq, f"{whose} spell/trap zone {seq}"))
    decision = Decision("place", player)
    all_picks = combinations(zones, max(count, 1))
    picks = list(islice(all_picks, MAX_CHOICES_PER_DECISION + 1))
    if len(picks) > MAX_CHOICES_PER_DECISION:
        picks = picks[:MAX_CHOICES_PER_DECISION]
        decision.truncated = True
    for pick in picks:
        response = b"".join(struct.pack("<BBB", side, loc, seq) for side, loc, seq, _ in pick)
        decision.choices.append(
            Choice("Use " + ", ".join(name for *_, name in pick), response, "place")
        )
    return decision


def _select_position(r: _Reader, db: CardDatabase) -> Decision:
    player, code, positions = r.u8(), r.u32(), r.u8()
    decision = Decision("position", player)
    for bit, name in C.POSITION_NAMES.items():
        if positions & bit:
            decision.choices.append(
                Choice(f"Put {db.name(code)} in {name}", _i32(bit), "position", (code,))
            )
    return decision


# ------------------------------------------------------------ declarations


def _announce_number(r: _Reader, _db: CardDatabase) -> Decision:
    player, count = r.u8(), r.u8()
    values = [r.u64() for _ in range(count)]
    return Decision(
        "announce_number",
        player,
        [Choice(f"Declare {value}", _i32(i), "announce") for i, value in enumerate(values)],
    )


def _announce_bits(kind: str, fmt: str, wide: bool):
    def parse(r: _Reader, _db: CardDatabase) -> Decision:
        player, count = r.u8(), r.u8()
        available = r.u64() if wide else r.u32()
        bits = [1 << i for i in range(64) if available & (1 << i)]
        decision = Decision(kind, player)
        picks = list(islice(combinations(bits, count), MAX_CHOICES_PER_DECISION + 1))
        if len(picks) > MAX_CHOICES_PER_DECISION:
            picks = picks[:MAX_CHOICES_PER_DECISION]
            decision.truncated = True
        for pick in picks:
            mask = sum(pick)
            decision.choices.append(Choice(f"Declare {mask:#x}", struct.pack(fmt, mask), "announce"))
        return decision

    return parse


def _rock_paper_scissors(r: _Reader, _db: CardDatabase) -> Decision:
    return Decision("rps", r.u8(), [Choice("Rock", _i32(2), "rps")])


def _unsupported(name: str):
    def parse(_r: _Reader, _db: CardDatabase) -> Decision:
        raise UnsupportedDecision(f"{name} prompts are not supported yet")

    return parse


_PARSERS = {
    C.MSG_SELECT_IDLECMD: _idle_command,
    C.MSG_SELECT_BATTLECMD: _battle_command,
    C.MSG_SELECT_EFFECTYN: _effect_yes_no,
    C.MSG_SELECT_YESNO: _yes_no,
    C.MSG_SELECT_OPTION: _option,
    C.MSG_SELECT_CARD: _select_card,
    C.MSG_SELECT_TRIBUTE: _select_tribute,
    C.MSG_SELECT_UNSELECT_CARD: _select_unselect,
    C.MSG_SELECT_SUM: _select_sum,
    C.MSG_SELECT_CHAIN: _select_chain,
    C.MSG_SORT_CHAIN: _sort,
    C.MSG_SORT_CARD: _sort,
    C.MSG_SELECT_PLACE: _select_place,
    C.MSG_SELECT_DISFIELD: _select_place,
    C.MSG_SELECT_POSITION: _select_position,
    C.MSG_ANNOUNCE_NUMBER: _announce_number,
    C.MSG_ANNOUNCE_RACE: _announce_bits("announce_race", "<Q", wide=True),
    C.MSG_ANNOUNCE_ATTRIB: _announce_bits("announce_attribute", "<I", wide=False),
    C.MSG_ROCK_PAPER_SCISSORS: _rock_paper_scissors,
    C.MSG_SELECT_COUNTER: _unsupported("counter removal"),
    C.MSG_ANNOUNCE_CARD: _unsupported("declare-a-card-name"),
}
