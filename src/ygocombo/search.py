"""Combo search: try the legal lines of play for turn 1 and keep the best boards.

How it works, in plain words:

* The engine cannot copy a game. So a position is stored as "the list of
  answers that got us here", and we rebuild it by replaying those answers
  in a fresh duel. The engine is deterministic (same seed + same answers =
  same game), so this is exact.
* Depth-first search: follow one line as far as it goes, then come back and
  try the next untried branch.
* Every time the engine asks "what do you want to do in your Main Phase?"
  we score the board. That is a point where you could stop and end your turn.

Known shortcuts (see docs/BUILD_PLAN.md for the plan to remove them):

* Zone and battle position choices take the first legal option.
* Setting cards, changing positions and the Battle Phase are not explored
  unless asked for.
* The opponent has no cards and always passes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import constants as C
from .decisions import Choice, Decision, UnsupportedDecision, parse_decision
from .engine import Duel, Environment, InvalidResponse
from .goldfish import Scenario, board_summary, new_duel

# Hard ceilings so a bad input cannot make the search run forever.
NODE_LIMIT_MAX = 200_000
DEPTH_LIMIT_MAX = 200
TIME_LIMIT_MAX = 3600.0
PENDING_BRANCH_MAX = 100_000
RESPONSES_PER_LINE_MAX = 2_000  # engine answers in one line, including forced ones

_IDLE_ORDER = {"activate": 0, "spsummon": 1, "summon": 2, "sset": 3, "mset": 4, "reposition": 5}
_FIRST_ONLY_KINDS = {"place", "position", "sort", "rps"}
_QUIET_KINDS = {"place", "position", "sort", "rps"}


@dataclass(frozen=True)
class Limits:
    max_nodes: int = 2000  # positions looked at
    max_depth: int = 40  # choices in one line
    max_seconds: float = 60.0
    include_sets: bool = False  # also try setting monsters / spells / traps

    def __post_init__(self) -> None:
        if not 1 <= self.max_nodes <= NODE_LIMIT_MAX:
            raise ValueError(f"max_nodes must be 1-{NODE_LIMIT_MAX}")
        if not 1 <= self.max_depth <= DEPTH_LIMIT_MAX:
            raise ValueError(f"max_depth must be 1-{DEPTH_LIMIT_MAX}")
        if not 0 < self.max_seconds <= TIME_LIMIT_MAX:
            raise ValueError(f"max_seconds must be above 0 and at most {TIME_LIMIT_MAX}")


@dataclass(frozen=True)
class Line:
    score: float
    steps: tuple[str, ...]
    board: dict[str, list[str]]


@dataclass
class SearchResult:
    lines: list[Line] = field(default_factory=list)  # best first
    nodes: int = 0
    replays: int = 0
    dead_ends: int = 0
    unsupported: int = 0
    duplicates: int = 0
    truncated_decisions: int = 0
    seconds: float = 0.0
    stopped_by: str = "finished"  # finished | max_nodes | max_seconds


class BoardScorer:
    """Placeholder scoring. Higher is better.

    +100 per wanted card face-up on the field
    +10  per Extra Deck monster on the field
    +3   per other monster on the field
    +2   per spell/trap on the field
    +1   per card in hand
    Ties are broken in favour of shorter lines by the search.
    """

    def __init__(self, env: Environment, wanted: tuple[int, ...] = ()):
        self.db = env.db
        self.wanted = set(wanted)

    def score(self, duel: Duel) -> float:
        total = 0.0
        for card in duel.cards_in(0, C.LOCATION_MZONE):
            record = self.db.get(card.code)
            total += 10 if record and record.is_extra_deck else 3
            if self._is_wanted(card.code) and card.position & C.POS_FACEUP:
                total += 100
        for card in duel.cards_in(0, C.LOCATION_SZONE):
            total += 2
            if self._is_wanted(card.code):
                total += 100
        total += duel.count(0, C.LOCATION_HAND)
        return total

    def _is_wanted(self, code: int) -> bool:
        if code in self.wanted:
            return True
        record = self.db.get(code)
        return bool(record and record.alias in self.wanted)


@dataclass
class _Branch:
    responses: tuple[bytes, ...]
    steps: tuple[str, ...]
    depth: int


class ComboSearch:
    def __init__(
        self,
        env: Environment,
        scenario: Scenario,
        *,
        limits: Limits | None = None,
        scorer: BoardScorer | None = None,
        keep: int = 5,
    ):
        if not 1 <= keep <= 100:
            raise ValueError("keep must be 1-100")
        self.env = env
        self.scenario = scenario
        self.limits = limits or Limits()
        self.scorer = scorer or BoardScorer(env)
        self.keep = keep

    # ---- public

    def run(self) -> SearchResult:
        result = SearchResult()
        started = time.monotonic()
        deadline = started + self.limits.max_seconds
        seen: set = set()
        best: dict[tuple, Line] = {}  # board fingerprint -> best line reaching it
        stack = [_Branch((), (), 0)]

        while stack:
            if result.nodes >= self.limits.max_nodes:
                result.stopped_by = "max_nodes"
                break
            if time.monotonic() >= deadline:
                result.stopped_by = "max_seconds"
                break
            self._follow(stack.pop(), stack, seen, best, result, deadline)

        result.lines = sorted(best.values(), key=lambda line: (-line.score, len(line.steps)))[
            : self.keep
        ]
        result.seconds = time.monotonic() - started
        return result

    # ---- internals

    def _follow(self, branch, stack, seen, best, result, deadline) -> None:
        """Rebuild one branch, then keep walking down its first option."""
        result.replays += 1
        responses = list(branch.responses)
        steps = list(branch.steps)
        depth = branch.depth
        turns_seen = 0
        duel = new_duel(self.env, self.scenario)
        try:
            for response in responses:
                turns_seen += self._advance(duel)
                if duel.ended or duel.pending is None:
                    result.dead_ends += 1
                    return
                duel.respond(response)

            while True:
                turns_seen += self._advance(duel)
                if duel.ended or turns_seen > 1:
                    return  # game over, or something moved us to the next turn
                if len(responses) >= RESPONSES_PER_LINE_MAX:
                    result.dead_ends += 1  # runaway line, most likely a prompt loop
                    return
                decision = parse_decision(duel.pending, self.env.db)
                if decision.truncated:
                    result.truncated_decisions += 1

                if decision.player != 0:
                    choice = _opponent_choice(decision)
                    responses.append(choice.response)
                    duel.respond(choice.response)
                    continue

                if decision.kind == "idle":
                    result.nodes += 1
                    fingerprint = _fingerprint(duel, decision)
                    self._record(duel, steps, fingerprint[0], best)
                    if fingerprint in seen:
                        result.duplicates += 1
                        return
                    seen.add(fingerprint)
                    if (
                        result.nodes >= self.limits.max_nodes
                        or time.monotonic() >= deadline
                        or depth >= self.limits.max_depth
                    ):
                        return

                options = self._options(decision)
                if not options:
                    if decision.kind != "idle":
                        result.dead_ends += 1
                    return
                if len(options) > 1 and depth >= self.limits.max_depth:
                    return  # depth cap applies inside effects too, not only at idle

                first, rest = options[0], options[1:]
                # Untried siblings go on the stack; reversed so they pop in order.
                if len(stack) + len(rest) <= PENDING_BRANCH_MAX:
                    for option in reversed(rest):
                        stack.append(
                            _Branch(
                                tuple(responses) + (option.response,),
                                tuple(steps) + _step(decision, option),
                                depth + 1,
                            )
                        )
                responses.append(first.response)
                steps.extend(_step(decision, first))
                if len(options) > 1:
                    depth += 1
                duel.respond(first.response)
        except InvalidResponse:
            result.dead_ends += 1  # the engine refused that pick (e.g. bad material set)
        except UnsupportedDecision:
            result.unsupported += 1
        finally:
            duel.close()

    @staticmethod
    def _advance(duel: Duel) -> int:
        """Run the engine; return how many new turns started."""
        return sum(1 for message in duel.advance() if message.type == C.MSG_NEW_TURN)

    def _options(self, decision: Decision) -> list[Choice]:
        choices = decision.choices
        if decision.kind == "idle":
            allowed = {"activate", "spsummon", "summon"}
            if self.limits.include_sets:
                allowed |= {"sset", "mset"}
            choices = [c for c in choices if c.action in allowed]
            choices.sort(key=lambda c: _IDLE_ORDER.get(c.action, 9))
            return _drop_identical(choices)
        if decision.kind == "battle":
            return [c for c in choices if c.action in ("main_phase_2", "end_phase")][:1]
        if decision.kind in _FIRST_ONLY_KINDS:
            return choices[:1]
        # Cancelling partway through an effect only rewinds to an earlier prompt,
        # which loops forever (e.g. pick fusion monster, cancel materials, pick
        # again). Not using the card at all is already tried at the idle prompt.
        # Keep cancel only when it is the sole legal answer.
        uncancelled = [c for c in choices if c.action != "cancel"]
        return _drop_identical(uncancelled or choices)

    def _record(self, duel: Duel, steps: list[str], board_key: tuple, best: dict) -> None:
        score = self.scorer.score(duel)
        current = best.get(board_key)
        if current is None or (score, -len(steps)) > (current.score, -len(current.steps)):
            best[board_key] = Line(score, tuple(steps), board_summary(duel))


def _opponent_choice(decision: Decision) -> Choice:
    """The goldfish opponent declines everything it is allowed to decline."""
    for choice in decision.choices:
        if choice.action in ("pass", "no", "cancel", "finish", "end_phase"):
            return choice
    if not decision.choices:
        raise UnsupportedDecision("opponent has a prompt with no legal answer")
    return decision.choices[0]


def _drop_identical(choices: list[Choice]) -> list[Choice]:
    """Two copies of the same card in the same place give the same result."""
    seen: set[str] = set()
    unique = []
    for choice in choices:
        if choice.label not in seen:
            seen.add(choice.label)
            unique.append(choice)
    return unique


def _step(decision: Decision, choice: Choice) -> tuple[str, ...]:
    """Text for the move list. Zone picks and "no" answers are left out."""
    if decision.kind in _QUIET_KINDS or choice.action in ("pass", "no", "cancel", "finish"):
        return ()
    return (choice.label,)


def _fingerprint(duel: Duel, decision: Decision) -> tuple:
    """Identity of a position: where every card is + which moves are open.

    The list of open moves stands in for hidden state such as "this effect was
    already used this turn". It is a heuristic (an educated shortcut), not a
    proof that two positions are identical.
    """
    board = tuple(
        tuple(
            sorted(
                (card.code, card.position if location == C.LOCATION_MZONE else 0)
                for card in duel.cards_in(0, location)
            )
        )
        for location in (
            C.LOCATION_MZONE,
            C.LOCATION_SZONE,
            C.LOCATION_HAND,
            C.LOCATION_GRAVE,
            C.LOCATION_REMOVED,
            C.LOCATION_EXTRA,
        )
    )
    moves = tuple(sorted(choice.label for choice in decision.choices))
    return (board, duel.count(0, C.LOCATION_DECK), moves)
