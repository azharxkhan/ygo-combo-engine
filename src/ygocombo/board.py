"""Combo board: turn a found line into a diagram of what each card did.

The search finds a line as a list of engine answers. Here we replay that line
once more and read the engine's own messages about it:

* MSG_CHAINING      - a card's effect was activated
* MSG_CHAIN_SOLVING - that effect is now resolving
* MSG_MOVE          - a card moved, from where, to where, and why

From these we know, for example, that Invocation moved from the deck to the
hand while Aleister the Invoker's effect was resolving, so "Aleister searches
Invocation". Nothing is written by hand and no rules are guessed: every arrow
is a move the engine reported.

The result can be written as a self-contained HTML page (no scripts, no
outside files) and as JSON.
"""

from __future__ import annotations

import html
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from . import constants as C
from .decisions import parse_decision
from .engine import Environment, EngineError, Message
from .goldfish import Scenario, board_summary, new_duel
from .search import _step

BOARD_FORMAT = "ygocombo-board"
BOARD_VERSION = 1
MAX_TRACE_RESPONSES = 2_000

_LOC = struct.Struct("<BBII")  # controller, location, sequence, position
_MOVE = struct.Struct("<I" + "BBII" + "BBII" + "I")  # code, from, to, reason


@dataclass(frozen=True)
class Event:
    code: int
    verb: str  # "Add to hand", "Fusion Summon", "Used as material", ...
    source: str  # location name it came from ("" for activations)
    dest: str  # location name it went to ("" for activations)
    cause: int = 0  # card whose effect caused it, 0 if none


@dataclass(frozen=True)
class Edge:
    source: int
    target: int
    label: str
    step: int


@dataclass
class Step:
    number: int
    label: str
    events: list[Event] = field(default_factory=list)


@dataclass
class ComboBoard:
    hand: tuple[int, ...]
    steps: list[Step]
    edges: list[Edge]
    end_board: dict[str, list[str]]
    names: dict[int, str]
    title: str = "Combo board"

    def to_json(self) -> dict:
        def card(code: int) -> dict:
            return {"code": code, "name": self.names.get(code, f"#{code}")}

        return {
            "format": BOARD_FORMAT,
            "version": BOARD_VERSION,
            "title": self.title,
            "hand": [card(c) for c in self.hand],
            "steps": [
                {
                    "number": s.number,
                    "label": s.label,
                    "events": [
                        {**card(e.code), "verb": e.verb, "from": e.source, "to": e.dest,
                         "cause": card(e.cause) if e.cause else None}
                        for e in s.events
                    ],
                }
                for s in self.steps
            ],
            "edges": [
                {"from": card(e.source), "to": card(e.target), "label": e.label, "step": e.step}
                for e in self.edges
            ],
            "end_board": self.end_board,
        }


# ------------------------------------------------------------------ tracing


def trace_line(env: Environment, scenario: Scenario, responses: tuple[bytes, ...],
               title: str = "Combo board") -> ComboBoard:
    """Replay a line found by the search and record what every card did."""
    if len(responses) > MAX_TRACE_RESPONSES:
        raise ValueError(f"line is too long to trace (over {MAX_TRACE_RESPONSES} answers)")
    db = env.db
    tracker = _Tracker()
    steps: list[Step] = []
    current = Step(0, "Start of turn")
    with new_duel(env, scenario) as duel:
        tracker.feed(duel.advance(), current)
        for response in responses:
            if duel.pending is None:
                raise EngineError("the line could not be replayed (duel ended early)")
            decision = parse_decision(duel.pending, db)
            choice = next((c for c in decision.choices if c.response == response), None)
            if choice is None:
                raise EngineError("the line could not be replayed (an answer is no longer legal)")
            text = _step(decision, choice) if decision.player == 0 else ()
            if text:
                current = Step(len(steps) + 1, text[0])
                steps.append(current)
            duel.respond(response)
            tracker.feed(duel.advance(), current)
        end_board = board_summary(duel)

    codes = set(scenario.hand) | {e.code for s in steps for e in s.events}
    codes |= {e.cause for s in steps for e in s.events if e.cause}
    names = {code: db.name(code) for code in codes}
    return ComboBoard(tuple(scenario.hand), steps, tracker.edges, end_board, names, title)


class _Tracker:
    """Reads engine messages and works out which card caused each move."""

    def __init__(self) -> None:
        self.chain: list[int] = []  # codes of the chain links, link 1 first
        self.resolving: int = 0  # code of the effect resolving right now
        self.materials: list[tuple[int, int]] = []  # (code, reason) waiting for their summon
        self.edges: list[Edge] = []

    def feed(self, messages: list[Message], step: Step) -> None:
        for message in messages:
            kind, data = message.type, message.data
            if kind == C.MSG_CHAINING and len(data) >= 4:
                (code,) = struct.unpack_from("<I", data)
                self.chain.append(code)
                step.events.append(Event(code, "Activate effect", "", ""))
            elif kind == C.MSG_SUMMONING and len(data) >= 4:
                # A Normal Summon: relabel the move that put this card on the field.
                (code,) = struct.unpack_from("<I", data)
                for i in range(len(step.events) - 1, -1, -1):
                    event = step.events[i]
                    if event.code == code and event.dest == C.LOCATION_NAMES[C.LOCATION_MZONE]:
                        step.events[i] = Event(code, "Normal Summon", event.source, event.dest, event.cause)
                        break
            elif kind == C.MSG_CHAIN_SOLVING and len(data) >= 1:
                link = data[0]
                self.resolving = self.chain[link - 1] if 1 <= link <= len(self.chain) else 0
            elif kind == C.MSG_CHAIN_SOLVED:
                self.resolving = 0
            elif kind == C.MSG_CHAIN_END:
                self.chain.clear()
                self.resolving = 0
            elif kind == C.MSG_MOVE and len(data) >= _MOVE.size:
                self._move(_MOVE.unpack_from(data), step)

    def _move(self, fields: tuple, step: Step) -> None:
        code, _fc, floc, _fseq, _fpos, tc, tloc, _tseq, _tpos, reason = fields
        if not code or tc != 0:
            return  # face-down/unknown card, or moved to the opponent
        if floc == tloc and not (floc | tloc) & C.LOCATION_OVERLAY:
            return  # shuffles and position changes
        cause = self.resolving
        if not cause and reason & C.REASON_COST and self.chain:
            cause = self.chain[-1]  # costs are paid while activating
        if cause == code:
            cause = 0
        verb = _verb(floc, tloc, reason, self.materials)
        step.events.append(Event(code, verb, _loc_name(floc), _loc_name(tloc), cause))

        if reason & C.REASON_MATERIAL and tloc != C.LOCATION_MZONE:
            self.materials.append((code, reason))
        elif tloc & C.LOCATION_MZONE and not tloc & C.LOCATION_OVERLAY and self.materials:
            for material, _ in self.materials:
                self.edges.append(Edge(material, code, "material for", step.number))
            self.materials.clear()
        # Materials already get an arrow to the monster they made.
        if cause and verb not in ("Placed on field", "Resolved", "Used as material"):
            self.edges.append(Edge(cause, code, _EDGE_LABEL.get(verb, verb.lower()), step.number))


_EDGE_LABEL = {
    "Add to hand": "adds to hand",
    "Special Summon": "special summons",
    "Fusion Summon": "fusion summons",
    "Synchro Summon": "synchro summons",
    "Xyz Summon": "xyz summons",
    "Link Summon": "link summons",
    "Ritual Summon": "ritual summons",
    "Send to GY": "sends to GY",
    "Send to GY (cost)": "costs: send to GY",
    "Discard": "discards",
    "Banish": "banishes",
    "Banish (cost)": "costs: banish",
    "Destroyed": "destroys",
    "Return to hand": "returns to hand",
    "Return to deck": "returns to deck",
    "Attach as Xyz material": "attaches",
    "Draw": "draws",
}

_SUMMON_BY_REASON = (
    (C.REASON_FUSION, "Fusion Summon"),
    (C.REASON_SYNCHRO, "Synchro Summon"),
    (C.REASON_XYZ, "Xyz Summon"),
    (C.REASON_LINK, "Link Summon"),
    (C.REASON_RITUAL, "Ritual Summon"),
)


def _verb(floc: int, tloc: int, reason: int, materials: list[tuple[int, int]]) -> str:
    if tloc & C.LOCATION_OVERLAY:
        return "Attach as Xyz material"
    if tloc == C.LOCATION_MZONE:
        if reason & C.REASON_SUMMON and not reason & C.REASON_SPSUMMON:
            return "Normal Summon"
        material_reasons = 0
        for _, r in materials:
            material_reasons |= r
        for flag, name in _SUMMON_BY_REASON:
            if material_reasons & flag:
                return name
        return "Special Summon"
    if tloc == C.LOCATION_HAND:
        if floc == C.LOCATION_DECK:
            return "Draw" if reason & C.REASON_DRAW else "Add to hand"
        if floc in (C.LOCATION_MZONE, C.LOCATION_SZONE):
            return "Return to hand"
        return "Add to hand"
    if tloc == C.LOCATION_GRAVE:
        if reason & C.REASON_MATERIAL:
            return "Used as material"
        if reason & C.REASON_DISCARD:
            return "Discard"
        if reason & C.REASON_COST:
            return "Send to GY (cost)"
        if reason & C.REASON_DESTROY:
            return "Destroyed"
        if reason & C.REASON_RELEASE:
            return "Tributed"
        if floc == C.LOCATION_SZONE and reason & C.REASON_RULE:
            return "Resolved"
        return "Send to GY"
    if tloc == C.LOCATION_REMOVED:
        if reason & C.REASON_MATERIAL:
            return "Used as material"
        return "Banish (cost)" if reason & C.REASON_COST else "Banish"
    if tloc in (C.LOCATION_DECK, C.LOCATION_EXTRA):
        return "Return to deck"
    if tloc == C.LOCATION_SZONE:
        return "Placed on field"
    return "Moved"


def _loc_name(location: int) -> str:
    if location & C.LOCATION_OVERLAY:
        return C.LOCATION_NAMES[C.LOCATION_OVERLAY]
    return C.LOCATION_NAMES.get(location, "unknown")


# ------------------------------------------------------------------ output


def check_board_path(raw: str, *, overwrite: bool = False) -> Path:
    """Where to write the board. Must be a .html file in an existing folder."""
    if not raw or len(raw) > 260:
        raise ValueError("board path must be 1-260 characters")
    path = Path(raw)
    if path.suffix.lower() != ".html":
        raise ValueError("board path must end in .html (a .json file is written next to it)")
    if not path.parent.is_dir():
        raise ValueError(f"folder not found: {path.parent}")
    for target in (path, path.with_suffix(".json")):
        if target.exists() and not overwrite:
            raise ValueError(f"{target} already exists (use --force to replace it)")
    return path


def write_board(board: ComboBoard, path: Path) -> tuple[Path, Path]:
    json_path = path.with_suffix(".json")
    path.write_text(render_html(board), encoding="utf-8")
    json_path.write_text(json.dumps(board.to_json(), indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    return path, json_path


_NODE_W, _NODE_H, _COL_GAP, _ROW_GAP, _PAD = 190, 48, 120, 26, 30
_EDGE_COLOURS = {
    "material for": "#b06cd8",
    "adds to hand": "#3f8fd8",
    "draws": "#3f8fd8",
}
_SUMMON_COLOUR = "#e0913a"
_OTHER_COLOUR = "#8a8f98"


def _edge_colour(label: str) -> str:
    if label in _EDGE_COLOURS:
        return _EDGE_COLOURS[label]
    return _SUMMON_COLOUR if "summons" in label else _OTHER_COLOUR


def _layout(board: ComboBoard) -> dict[int, tuple[float, float]]:
    """Layered layout: each card sits one column right of every card that acted on it.

    So arrows point right. A card that acts on an earlier card (a loop) keeps
    its column and that arrow is drawn as a curve under the boxes.
    """
    nodes = list(dict.fromkeys([*board.hand, *(c for e in board.edges for c in (e.source, e.target))]))
    column = {code: 0 for code in nodes}
    links = {(e.source, e.target) for e in board.edges if e.source != e.target}
    for _ in range(len(nodes)):  # longest path; capped so loops cannot run forever
        changed = False
        for source, target in links:
            if column[target] < column[source] + 1 <= len(nodes):
                column[target] = column[source] + 1
                changed = True
        if not changed:
            break
    order = {code: i for i, code in enumerate(nodes)}
    row_of: dict[int, float] = {}
    positions = {}
    for col in sorted(set(column.values())):
        members = [c for c in nodes if column[c] == col]

        def weight(code: int) -> tuple[float, int]:
            # Sit near the cards that point at you, to cut down on crossings.
            rows = [row_of[s] for s, t in links if t == code and s in row_of]
            return (sum(rows) / len(rows) if rows else order[code], order[code])

        members.sort(key=weight)
        for row, code in enumerate(members):
            row_of[code] = row
            positions[code] = (_PAD + col * (_NODE_W + _COL_GAP), _PAD + 20 + row * (_NODE_H + _ROW_GAP))
    return positions


def _svg(board: ComboBoard, end_names: set[str]) -> str:
    pos = _layout(board)
    if not pos:
        return ""
    # Arrows that skip a column go over the top in their own lane, so they
    # never run behind a box in between.
    step_x = _NODE_W + _COL_GAP
    unique = list(dict.fromkeys((e.source, e.target, e.label) for e in board.edges
                                if e.source in pos and e.target in pos))
    long_edges = [k for k in unique if pos[k[1]][0] - pos[k[0]][0] > step_x]
    lane_top = 24
    shift = lane_top + 18 * len(long_edges)
    pos = {code: (x, y + shift) for code, (x, y) in pos.items()}
    width = max(x for x, _ in pos.values()) + _NODE_W + _PAD
    height = max(y for _, y in pos.values()) + _NODE_H + _PAD + 30
    esc = html.escape
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
             'role="img" aria-label="Card flow">',
             '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
             'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
             'fill="context-stroke"/></marker></defs>']
    seen: set[tuple[int, int, str]] = set()
    for edge in board.edges:
        key = (edge.source, edge.target, edge.label)
        if key in seen or edge.source not in pos or edge.target not in pos:
            continue
        seen.add(key)
        (sx, sy), (tx, ty) = pos[edge.source], pos[edge.target]
        colour = _edge_colour(edge.label)
        if key in long_edges:
            lane = lane_top + 18 * long_edges.index(key)
            x1, y1, x2, y2 = sx + _NODE_W, sy + _NODE_H / 2, tx, ty + _NODE_H / 2
            g1, g2, r = x1 + _COL_GAP / 2, x2 - _COL_GAP / 2, 8
            d = (f"M{x1:.0f},{y1:.0f} H{g1 - r:.0f} Q{g1:.0f},{y1:.0f} {g1:.0f},{y1 - r:.0f} "
                 f"V{lane + r:.0f} Q{g1:.0f},{lane:.0f} {g1 + r:.0f},{lane:.0f} H{g2 - r:.0f} "
                 f"Q{g2:.0f},{lane:.0f} {g2:.0f},{lane + r:.0f} V{y2 - r:.0f} "
                 f"Q{g2:.0f},{y2:.0f} {g2 + r:.0f},{y2:.0f} H{x2:.0f}")
            lx, ly = (g1 + g2) / 2, lane - 4
        elif tx > sx:
            x1, y1, x2, y2 = sx + _NODE_W, sy + _NODE_H / 2, tx, ty + _NODE_H / 2
            bend = (x2 - x1) / 2
            d = f"M{x1:.0f},{y1:.0f} C{x1 + bend:.0f},{y1:.0f} {x2 - bend:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 2 - 4
        else:  # same column or backwards: loop under the boxes
            x1, y1, x2, y2 = sx + _NODE_W / 2, sy + _NODE_H, tx + _NODE_W / 2, ty + _NODE_H
            low = max(y1, y2) + 40
            d = f"M{x1:.0f},{y1:.0f} C{x1:.0f},{low:.0f} {x2:.0f},{low:.0f} {x2:.0f},{y2:.0f}"
            lx, ly = (x1 + x2) / 2, low - 8
        parts.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2" '
                     f'marker-end="url(#arrow)"><title>{esc(board.names.get(edge.source, ""))} '
                     f'{esc(edge.label)} {esc(board.names.get(edge.target, ""))} (step {edge.step})</title></path>')
        parts.append(f'<text x="{lx:.0f}" y="{ly:.0f}" class="edge" fill="{colour}">{esc(edge.label)}</text>')
    hand = set(board.hand)
    for code, (x, y) in pos.items():
        name = board.names.get(code, f"#{code}")
        short = name if len(name) <= 26 else name[:25] + "…"
        tags = []
        if code in hand:
            tags.append("hand")
        if name in end_names:
            tags.append("end board")
        cls = "node end" if name in end_names else "node hand" if code in hand else "node"
        parts.append(f'<g class="{cls}"><title>{esc(name)}</title>'
                     f'<rect x="{x:.0f}" y="{y:.0f}" width="{_NODE_W}" height="{_NODE_H}" rx="8"/>'
                     f'<text x="{x + 10:.0f}" y="{y + 20:.0f}" class="name">{esc(short)}</text>'
                     f'<text x="{x + 10:.0f}" y="{y + 38:.0f}" class="tag">{esc(", ".join(tags))}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(board: ComboBoard) -> str:
    esc = html.escape
    end_names = {n.split(" [")[0] for zone in ("monster zone", "spell/trap zone")
                 for n in board.end_board.get(zone, [])}
    rows = []
    for step in board.steps:
        events = "".join(
            f"<li><b>{esc(board.names.get(e.code, ''))}</b>: {esc(e.verb)}"
            + (f" ({esc(e.source)} → {esc(e.dest)})" if e.source else "")
            + (f" <span class=cause>by {esc(board.names.get(e.cause, ''))}</span>" if e.cause else "")
            + "</li>"
            for e in step.events
        )
        rows.append(f"<li><div class=label>{esc(step.label)}</div>"
                    + (f"<ul class=events>{events}</ul>" if events else "") + "</li>")
    end = "".join(f"<tr><th>{esc(zone)}</th><td>{esc(', '.join(names))}</td></tr>"
                  for zone, names in board.end_board.items() if names)
    hand = ", ".join(esc(board.names.get(c, f"#{c}")) for c in board.hand)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(board.title)}</title>
<style>
body {{ font: 15px/1.45 system-ui, sans-serif; margin: 24px; color: #1d2026; background: #fafafa; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 17px; margin: 28px 0 8px; }}
.muted {{ color: #6b7079; }}
.flow {{ overflow-x: auto; background: #fff; border: 1px solid #e3e5e8; border-radius: 10px; }}
.node rect {{ fill: #fff; stroke: #c3c7cd; stroke-width: 1.5; }}
.node.hand rect {{ fill: #eef5ff; stroke: #3f8fd8; }}
.node.end rect {{ fill: #fff4e8; stroke: #e0913a; stroke-width: 2; }}
.name {{ font-size: 13px; font-weight: 600; fill: #1d2026; }}
.tag {{ font-size: 11px; fill: #6b7079; }}
.edge {{ font-size: 11px; text-anchor: middle; paint-order: stroke; stroke: #fff; stroke-width: 4px; }}
ol.steps > li {{ margin: 0 0 10px; }} .label {{ font-weight: 600; }}
ul.events {{ margin: 2px 0 0; padding-left: 18px; color: #3a3f47; font-size: 14px; }}
.cause {{ color: #6b7079; }}
table {{ border-collapse: collapse; }} th, td {{ text-align: left; padding: 4px 12px 4px 0; vertical-align: top; }}
.legend span {{ margin-right: 14px; }}
</style></head><body>
<h1>{esc(board.title)}</h1>
<div class=muted>Starting hand: {hand}</div>
<h2>Card flow</h2>
<div class="legend muted"><span style="color:{_SUMMON_COLOUR}">&#9632; summons</span><span style="color:#3f8fd8">&#9632; adds to hand</span><span style="color:#b06cd8">&#9632; material</span><span style="color:{_OTHER_COLOUR}">&#9632; other</span></div>
<div class=flow>{_svg(board, end_names)}</div>
<h2>Steps</h2>
<ol class=steps>{''.join(rows)}</ol>
<h2>End board</h2>
<table>{end}</table>
<p class=muted>Every arrow is a card move reported by the EDOPro engine (ygopro-core) while replaying this line. Goldfish: the opponent never responds.</p>
</body></html>
"""
