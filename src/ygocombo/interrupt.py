"""Interruption check: where can a hand trap stop a line, and what is left after it?

Steps:

1. Search the hand as usual (the opponent passes on everything) and keep the
   best few lines.
2. Replay each line. Every time the engine offers the opponent a chance to
   activate one of their cards, that is an interruption point. The opponent
   holds the cards given in Scenario.opponent_hand, so the engine decides
   what is legal (Ash Blossom only when a search or special summon is
   activated, and so on). Nothing is decided by us.
3. For each point, make the opponent activate there, then run the combo
   search again from that position to find your best recovery.
4. A line's worst case is its worst recovery. The line with the best worst
   case is the safest way to play the hand into those hand traps.

Shortcuts, in plain words:

* The opponent uses one card, once. After that they pass.
* When the opponent must pick something after activating (Effect Veiler's
  target, say), each pick is tried as its own interruption, up to a cap.
* Chances to interrupt between the same two of your moves are merged when
  the opponent would do the same thing, so the list stays readable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .decisions import Decision, parse_decision
from .engine import Environment
from .goldfish import Scenario, new_duel
from .search import BoardScorer, ComboSearch, Limits, Line, _step

MAX_POINTS_PER_LINE = 40
MAX_FOLLOW_UP_PICKS = 8  # opponent choices after activating (targets etc.)
MAX_LINES = 10
TOTAL_SECONDS_MAX = 6 * 3600.0
_DECLINE = ("pass", "no", "cancel")


@dataclass(frozen=True)
class Interruption:
    after_steps: int  # how many of your moves were made before it
    after_step: str  # your last move before it ("" if before any move)
    opponent: tuple[str, ...]  # what the opponent did, e.g. activation then target
    prefix: tuple[bytes, ...]  # engine answers that reach the position after it


@dataclass(frozen=True)
class Outcome:
    interruption: Interruption
    score: float
    reached_goal: bool
    steps: tuple[str, ...]  # your best play from the interruption on
    board: dict[str, list[str]]
    stopped_by: str


@dataclass
class LineReport:
    line: Line
    outcomes: list[Outcome] = field(default_factory=list)
    points_capped: bool = False

    @property
    def worst(self) -> Outcome | None:
        if not self.outcomes:
            return None
        return min(self.outcomes, key=lambda o: (o.reached_goal, o.score))

    def worst_key(self) -> tuple:
        """Sort key, best first: keep the goal, then score, in the worst case."""
        worst = self.worst
        if worst is None:  # nothing can interrupt this line
            return (True, self.line.score, self.line.score)
        return (worst.reached_goal, worst.score, self.line.score)


@dataclass
class InterruptResult:
    base_nodes: int
    base_stopped_by: str
    reports: list[LineReport]  # safest first
    seconds: float = 0.0
    stopped_by: str = "finished"  # finished | total_seconds


def find_interruptions(env: Environment, scenario: Scenario, responses: tuple[bytes, ...]) -> tuple[list[Interruption], bool]:
    """Every point on a line where the opponent could activate a card.

    Returns (points, capped). capped is True if MAX_POINTS_PER_LINE was hit.
    """
    db = env.db
    points: list[Interruption] = []
    seen: set[tuple[int, tuple[str, ...]]] = set()
    steps_done, last_step = 0, ""
    with new_duel(env, scenario) as duel:
        duel.advance()
        for index, response in enumerate(responses):
            if duel.pending is None:
                break
            decision = parse_decision(duel.pending, db)
            if decision.player == 1 and _can_decline(decision):
                for choice in decision.choices:
                    if choice.action in _DECLINE:
                        continue
                    for opponent, prefix in _expand(env, scenario, responses[:index] + (choice.response,),
                                                    (choice.label,)):
                        key = (steps_done, opponent)
                        if key in seen:
                            continue
                        seen.add(key)
                        points.append(Interruption(steps_done, last_step, opponent, prefix))
                        if len(points) >= MAX_POINTS_PER_LINE:
                            return points, True
            elif decision.player == 0:
                choice = next((c for c in decision.choices if c.response == response), None)
                text = _step(decision, choice) if choice else ()
                if text:
                    steps_done += 1
                    last_step = text[0]
            duel.respond(response)
            duel.advance()
    return points, False


def _can_decline(decision: Decision) -> bool:
    return any(c.action in _DECLINE for c in decision.choices) and any(
        c.action not in _DECLINE for c in decision.choices
    )


def _expand(env: Environment, scenario: Scenario, prefix: tuple[bytes, ...],
            labels: tuple[str, ...]) -> list[tuple[tuple[str, ...], tuple[bytes, ...]]]:
    """Follow the opponent's forced picks after they activate (targets etc.).

    Stops at the first prompt that is yours, or one the opponent may decline.
    Each different pick becomes its own variant, up to MAX_FOLLOW_UP_PICKS.
    """
    out: list[tuple[tuple[str, ...], tuple[bytes, ...]]] = []
    todo = [(prefix, labels)]
    while todo and len(out) < MAX_FOLLOW_UP_PICKS:
        answers, said = todo.pop()
        with new_duel(env, scenario) as duel:
            duel.advance()
            ok = True
            for response in answers:
                if duel.pending is None:
                    ok = False
                    break
                duel.respond(response)
                duel.advance()
            if not ok:
                continue
            if duel.pending is None:
                out.append((said, answers))
                continue
            decision = parse_decision(duel.pending, env.db)
            if decision.player != 1 or any(c.action in _DECLINE for c in decision.choices):
                out.append((said, answers))
                continue
            picks = [c for c in decision.choices if _step(decision, c)] or decision.choices[:1]
            for choice in reversed(picks[:MAX_FOLLOW_UP_PICKS]):
                text = _step(decision, choice)
                todo.append((answers + (choice.response,), said + text))
    return out


def analyse(
    env: Environment,
    scenario: Scenario,
    wanted: tuple[int, ...],
    *,
    lines: int = 3,
    limits: Limits | None = None,
    recovery_limits: Limits | None = None,
    total_seconds: float = 900.0,
    on_point=None,
) -> InterruptResult:
    """Find the best lines, then test each against every interruption point."""
    if not scenario.opponent_hand:
        raise ValueError("give the opponent at least one card to interrupt with")
    if not 1 <= lines <= MAX_LINES:
        raise ValueError(f"lines must be 1-{MAX_LINES}")
    if not 0 < total_seconds <= TOTAL_SECONDS_MAX:
        raise ValueError(f"total_seconds must be above 0 and at most {TOTAL_SECONDS_MAX:g}")
    limits = limits or Limits()
    recovery_limits = recovery_limits or Limits(max_nodes=300, max_seconds=20.0)
    started = time.monotonic()
    scorer = BoardScorer(env, wanted)

    base = ComboSearch(env, scenario, limits=limits, scorer=scorer, keep=lines).run()
    result = InterruptResult(base.nodes, base.stopped_by, [])
    for line in base.lines:
        points, capped = find_interruptions(env, scenario, line.responses)
        report = LineReport(line, points_capped=capped)
        result.reports.append(report)
        for point in points:
            left = total_seconds - (time.monotonic() - started)
            if left <= 0:
                result.stopped_by = "total_seconds"
                break
            search = ComboSearch(
                env, scenario,
                limits=Limits(max_nodes=recovery_limits.max_nodes, max_depth=recovery_limits.max_depth,
                              max_seconds=max(0.1, min(recovery_limits.max_seconds, left)),
                              include_sets=recovery_limits.include_sets),
                scorer=scorer, keep=1, stop_at_goal=bool(wanted), prefix=point.prefix,
            )
            found = search.run()
            best = found.lines[0] if found.lines else None
            outcome = Outcome(point, best.score if best else 0.0, found.reached_goal,
                              best.steps if best else (), best.board if best else {},
                              found.stopped_by)
            report.outcomes.append(outcome)
            if on_point:
                on_point(report, outcome)
        if result.stopped_by != "finished":
            break
    result.reports.sort(key=lambda r: r.worst_key(), reverse=True)
    result.seconds = time.monotonic() - started
    return result
