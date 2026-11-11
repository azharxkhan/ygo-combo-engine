"""Is a combo possible?

Two questions, both answered by the engine:

1. Can this hand make this board?  (can_make)
   Searches for a line that puts every wanted card on the field.
   possible  - a line was found (and is returned as proof).
   not found - the search tried every line it explores and none worked.
               "Every line it explores" has limits: zone and position
               choices take the first legal option, cards are only set
               with include_sets, and the opponent never responds.
   unknown   - the search hit its node or time limit first.

2. Does this written line work?  (check_line)
   Plays your steps one by one. Each step must match, word for word, a
   choice the engine offers at that moment (the wording `solve` prints).
   Prompts you did not write a step for are answered the simple way:
   "Do not chain" / "No" when that is allowed, the only choice when there
   is one, the first zone or position for zone and position picks.
   Anything else stops the check at that step and lists what was legal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .decisions import UnsupportedDecision, parse_decision
from .engine import Environment, InvalidResponse
from .goldfish import Scenario, board_summary, new_duel
from .search import (
    _FIRST_ONLY_KINDS, RESPONSES_PER_LINE_MAX, BoardScorer, ComboSearch, Limits, Line,
    SearchResult, _opponent_choice, _step,
)

MAX_LINE_STEPS = 300
MAX_STEP_CHARS = 200
MAX_LINE_FILE_BYTES = 64 * 1024
_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s*")
_AUTO_DECLINE = ("pass", "no")


# ------------------------------------------------------------------ can this hand make it?


@dataclass
class CanMake:
    verdict: str  # possible | not found | unknown
    line: Line | None  # proof when possible, else the best board reached
    missing: tuple[str, ...]  # wanted cards not on the field in that best board
    search: SearchResult


def can_make(env: Environment, scenario: Scenario, wanted: tuple[int, ...],
             limits: Limits | None = None) -> CanMake:
    if not wanted:
        raise ValueError("say which cards must be on the field")
    scorer = BoardScorer(env, wanted)
    found = ComboSearch(env, scenario, limits=limits or Limits(), scorer=scorer, keep=5,
                        stop_at_goal=True).run()
    if found.reached_goal:
        line = next((l for l in found.lines if l.meets_goal), found.lines[0] if found.lines else None)
        return CanMake("possible", line, (), found)
    best = found.lines[0] if found.lines else None
    on_field = set()
    if best:
        for zone in ("monster zone", "spell/trap zone"):
            on_field |= {name.split(" [")[0] for name in best.board.get(zone, [])}
    missing = tuple(env.db.name(code) for code in wanted if env.db.name(code) not in on_field)
    verdict = "not found" if found.stopped_by == "finished" else "unknown"
    return CanMake(verdict, best, missing, found)


# ------------------------------------------------------------------ does this line work?


@dataclass
class LineCheck:
    ok: bool
    steps_done: int  # how many of your steps were played
    error: str = ""
    legal: list[str] = field(default_factory=list)  # choices at the failing step
    board: dict[str, list[str]] = field(default_factory=dict)
    meets_goal: bool = False
    played: list[str] = field(default_factory=list)  # every step taken, yours and automatic
    declined: list[str] = field(default_factory=list)  # prompts you did not write, answered "no"


def read_line_file(path: Path) -> list[str]:
    """One step per line. "1." style numbers, blank lines and # comments are ignored."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"line file not found: {path}")
    if path.stat().st_size > MAX_LINE_FILE_BYTES:
        raise ValueError(f"line file is too large (limit {MAX_LINE_FILE_BYTES} bytes)")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("line file is not valid UTF-8 text") from exc
    return parse_steps(text)


def parse_steps(text: str) -> list[str]:
    steps = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_STEP_CHARS:
            raise ValueError(f"line {lineno}: step is too long")
        step = _NUMBER_RE.sub("", line)
        if step:
            steps.append(step)
        if len(steps) > MAX_LINE_STEPS:
            raise ValueError(f"too many steps (limit {MAX_LINE_STEPS})")
    if not steps:
        raise ValueError("the line has no steps")
    return steps


def check_line(env: Environment, scenario: Scenario, steps: list[str],
               wanted: tuple[int, ...] = ()) -> LineCheck:
    """Play written steps in the engine. Stops at the first one that is not legal."""
    if not steps or len(steps) > MAX_LINE_STEPS:
        raise ValueError(f"the line must have 1-{MAX_LINE_STEPS} steps")
    db = env.db
    scorer = BoardScorer(env, wanted)
    done = 0
    played: list[str] = []
    declined: list[str] = []
    answers = 0
    with new_duel(env, scenario) as duel:
        try:
            duel.advance()
            while True:
                if duel.ended or duel.pending is None:
                    return LineCheck(False, done, "the duel ended or moved to the next turn",
                                     played=played, declined=declined)
                answers += 1
                if answers > RESPONSES_PER_LINE_MAX:
                    return LineCheck(False, done, "the line runs too long (prompt loop?)", played=played, declined=declined)
                decision = parse_decision(duel.pending, db)
                if decision.player != 0:
                    choice = _opponent_choice(decision)
                elif decision.kind in _FIRST_ONLY_KINDS:
                    choice = decision.choices[0]
                else:
                    choice = None
                    if done < len(steps):
                        choice = next((c for c in decision.choices if c.label == steps[done]), None)
                        if choice is not None:
                            done += 1
                    if choice is None:
                        if done == len(steps) and decision.kind == "idle":
                            return LineCheck(True, done, board=board_summary(duel),
                                             meets_goal=scorer.meets_goal(duel), played=played, declined=declined)
                        choice = _auto(decision)
                        if choice is not None and choice.action in _AUTO_DECLINE and                                 decision.kind != "chain":
                            declined.append(f"{choice.label} (after step {done})")
                        if choice is None:
                            what = (f"step {done + 1} ({steps[done]!r}) is not a legal choice here"
                                    if done < len(steps)
                                    else "the line ends in the middle of a prompt")
                            return LineCheck(False, done, what,
                                             legal=[c.label for c in decision.choices],
                                             board=board_summary(duel), played=played, declined=declined)
                    played.extend(_step(decision, choice))
                duel.respond(choice.response)
                duel.advance()
        except InvalidResponse:
            return LineCheck(False, done, f"the engine refused step {done} ({steps[done - 1]!r})"
                             if done else "the engine refused an automatic answer", played=played, declined=declined)
        except UnsupportedDecision as exc:
            return LineCheck(False, done, f"a prompt this tool cannot answer yet: {exc}", played=played, declined=declined)


def _auto(decision):
    """The simple answer for a prompt you did not write a step for, if there is one."""
    if decision.kind == "idle":
        return None  # every main phase move must be written down
    decline = next((c for c in decision.choices if c.action in _AUTO_DECLINE), None)
    if decline is not None:
        return decline
    if len(decision.choices) == 1:
        return decision.choices[0]
    return None
