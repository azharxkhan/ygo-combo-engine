"""Hand odds: how often does a random opening hand reach the wanted board?

Unlike a tag-based calculator, nothing here is declared by hand. Each sampled
hand is played out by the real engine with the combo search, so a hand only
counts as a hit if the search actually reaches a board with every wanted card.

Each hand ends as one of:

* hit      - the search reached the wanted board.
* miss     - the search tried every line it could within its limits and
             never reached it.
* unknown  - the search was cut short (node or time cap) before finding it,
             so a line may exist that it did not get to.

Hands are drawn with a fixed seed, so the same command gives the same hands.
Identical hands (same cards, any order) are only searched once.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .deck import Deck
from .engine import Environment
from .goldfish import Scenario
from .search import BoardScorer, ComboSearch, Limits

HANDS_MAX = 1000
HAND_SIZES = (5, 6)  # 5 going first, 6 to see what a sixth card adds
TOTAL_SECONDS_MAX = 6 * 3600.0
_SEED_MAX = 2**32


@dataclass(frozen=True)
class HandResult:
    hand: tuple[int, ...]
    outcome: str  # hit | miss | unknown
    seconds: float
    nodes: int
    steps: tuple[str, ...] = ()  # the line that reached the goal, for hits
    cached: bool = False  # same cards as an earlier hand, result reused


@dataclass
class OddsResult:
    hands: list[HandResult] = field(default_factory=list)
    seconds: float = 0.0
    stopped_by: str = "finished"  # finished | total_seconds

    def count(self, outcome: str) -> int:
        return sum(1 for h in self.hands if h.outcome == outcome)

    @property
    def low(self) -> float:
        """Hit rate if every unknown hand is a miss."""
        return self.count("hit") / len(self.hands) if self.hands else 0.0

    @property
    def high(self) -> float:
        """Hit rate if every unknown hand is a hit."""
        if not self.hands:
            return 0.0
        return (self.count("hit") + self.count("unknown")) / len(self.hands)


def wilson_interval(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% range for the true rate, given hits out of total samples.

    The Wilson interval behaves well for small samples and for rates near 0%
    or 100%, where the simple "p +/- 2 standard errors" formula breaks down.
    """
    if total <= 0:
        return 0.0, 1.0
    if not 0 <= hits <= total:
        raise ValueError("hits must be between 0 and total")
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def sample_hands(deck: Deck, count: int, hand_size: int, seed: int) -> Iterator[tuple[tuple[int, ...], int]]:
    """Yield (hand, shuffle seed) pairs, drawn from the main deck with a fixed seed."""
    check_settings(count, hand_size, seed)
    if len(deck.main) < hand_size:
        raise ValueError(f"main deck has fewer than {hand_size} cards")
    rng = random.Random(seed)
    for _ in range(count):
        cards = list(deck.main)
        rng.shuffle(cards)
        yield tuple(cards[:hand_size]), rng.randrange(_SEED_MAX)


def run_odds(
    env: Environment,
    deck: Deck,
    wanted: tuple[int, ...],
    *,
    hands: int = 50,
    hand_size: int = 5,
    seed: int = 1,
    limits: Limits | None = None,
    total_seconds: float = 1800.0,
    on_hand: Callable[[int, HandResult], None] | None = None,
    clean_lines: bool = False,
) -> OddsResult:
    """Sample opening hands and search each one for the wanted board."""
    if not wanted:
        raise ValueError("odds need at least one wanted card")
    if not 0 < total_seconds <= TOTAL_SECONDS_MAX:
        raise ValueError(f"total_seconds must be above 0 and at most {TOTAL_SECONDS_MAX:g}")
    limits = limits or Limits(max_nodes=300, max_seconds=20.0)
    scorer = BoardScorer(env, wanted)
    result = OddsResult()
    cache: dict[tuple[int, ...], HandResult] = {}
    started = time.monotonic()

    for number, (hand, shuffle_seed) in enumerate(sample_hands(deck, hands, hand_size, seed), start=1):
        if time.monotonic() - started >= total_seconds:
            result.stopped_by = "total_seconds"
            break
        key = tuple(sorted(hand))
        if key in cache:
            earlier = cache[key]
            hand_result = HandResult(hand, earlier.outcome, 0.0, 0, earlier.steps, cached=True)
        else:
            # Never let one hand run past the overall budget.
            left = total_seconds - (time.monotonic() - started)
            hand_limits = Limits(
                max_nodes=limits.max_nodes,
                max_depth=limits.max_depth,
                max_seconds=max(0.1, min(limits.max_seconds, left)),
                include_sets=limits.include_sets,
            )
            search = ComboSearch(
                env, Scenario(deck, hand, seed=shuffle_seed),
                limits=hand_limits, scorer=scorer, keep=1, stop_at_goal=True, simplify=clean_lines,
            )
            found = search.run()
            if found.reached_goal:
                outcome = "hit"
            elif found.stopped_by == "finished":
                outcome = "miss"
            else:
                outcome = "unknown"
            hand_result = HandResult(hand, outcome, found.seconds, found.nodes, found.goal_steps)
            cache[key] = hand_result
        result.hands.append(hand_result)
        if on_hand:
            on_hand(number, hand_result)

    result.seconds = time.monotonic() - started
    return result


def check_settings(count: int, hand_size: int, seed: int) -> None:
    """Reject a bad number of hands, hand size or seed."""
    if not 1 <= count <= HANDS_MAX:
        raise ValueError(f"number of hands must be 1-{HANDS_MAX}")
    if hand_size not in HAND_SIZES:
        raise ValueError(f"hand size must be one of {HAND_SIZES}")
    if not 0 <= seed < _SEED_MAX:
        raise ValueError(f"seed must be 0-{_SEED_MAX - 1}")
