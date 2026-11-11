"""Goldfish setup: you go first, the opponent has no cards and does nothing.

"Goldfishing" = practising your combo against an opponent who never interacts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from . import constants as C
from .deck import Deck
from .engine import Duel, Environment

HAND_MAX = 10
OPPONENT_HAND_MAX = 6


@dataclass(frozen=True)
class Scenario:
    """Everything needed to rebuild the exact same starting position."""

    deck: Deck
    hand: tuple[int, ...]
    seed: int = 1
    # Cards the opponent holds (hand traps). They only use them when told to:
    # the plain search still has the opponent pass on everything.
    opponent_hand: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= len(self.hand) <= HAND_MAX:
            raise ValueError(f"hand must have 1-{HAND_MAX} cards")
        if len(self.opponent_hand) > OPPONENT_HAND_MAX:
            raise ValueError(f"opponent hand can have at most {OPPONENT_HAND_MAX} cards")
        remaining = list(self.deck.main)
        for code in self.hand:
            if code not in remaining:
                raise ValueError(f"hand card {code} is not in the main deck (or too many copies)")
            remaining.remove(code)

    def library(self) -> list[int]:
        """Main deck minus the hand, shuffled the same way every time."""
        remaining = list(self.deck.main)
        for code in self.hand:
            remaining.remove(code)
        random.Random(self.seed).shuffle(remaining)
        return remaining


def new_duel(env: Environment, scenario: Scenario) -> Duel:
    """Create a duel at the start of turn 1 with the given opening hand."""
    # We place the hand ourselves, so nobody draws an opening hand.
    duel = Duel(env, scenario.seed, starting_draw=(0, 0))
    try:
        for code in scenario.library():
            duel.add_card(code, 0, C.LOCATION_DECK)
        for code in scenario.deck.extra:
            duel.add_card(code, 0, C.LOCATION_EXTRA)
        for code in scenario.hand:
            duel.add_card(code, 0, C.LOCATION_HAND)
        for code in scenario.opponent_hand:
            duel.add_card(code, 1, C.LOCATION_HAND)
        duel.start()
    except Exception:
        duel.close()
        raise
    return duel


def board_summary(duel: Duel, player: int = 0) -> dict[str, list[str]]:
    """Card names per zone, for printing."""
    db = duel.env.db
    summary: dict[str, list[str]] = {}
    for location in (
        C.LOCATION_MZONE,
        C.LOCATION_SZONE,
        C.LOCATION_HAND,
        C.LOCATION_GRAVE,
        C.LOCATION_REMOVED,
    ):
        names = []
        for card in duel.cards_in(player, location):
            name = db.name(card.code)
            if location in (C.LOCATION_MZONE, C.LOCATION_SZONE):
                name += f" [{C.POSITION_NAMES.get(card.position, 'unknown position')}]"
            names.append(name)
        summary[C.LOCATION_NAMES[location]] = names
    return summary
