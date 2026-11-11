"""Combo board tests. Run with:  PYTHONPATH=src python3 -m pytest -q"""

import json
from pathlib import Path

import pytest

from ygocombo.board import ComboBoard, Edge, Event, Step, check_board_path, render_html, write_board

ROOT = Path(__file__).resolve().parents[1]
ALEISTER, INVOCATION, MECHABA, VEILER = 86120751, 74063034, 75286621, 97268402
HAND = (ALEISTER, 37742478, 14087893, VEILER, 43898403)


def _tiny_board() -> ComboBoard:
    ash = 14558127
    names = {ash: "Ash Blossom & Joyous Spring", MECHABA: "<Mechaba>"}
    step = Step(1, "Do <things>", [Event(ash, "Discard", "hand", "graveyard", MECHABA)])
    return ComboBoard((ash,), [step], [Edge(MECHABA, ash, "discards", 1)],
                      {"monster zone": ["<Mechaba> [face-up attack]"]}, names, "T & <b>")


def test_html_escapes_card_text():
    page = render_html(_tiny_board())
    assert "<Mechaba>" not in page and "&lt;Mechaba&gt;" in page
    assert "Ash Blossom &amp; Joyous Spring" in page
    assert "<script" not in page.lower()


def test_board_path_checks(tmp_path):
    with pytest.raises(ValueError, match=".html"):
        check_board_path(str(tmp_path / "x.txt"))
    with pytest.raises(ValueError, match="folder not found"):
        check_board_path(str(tmp_path / "missing" / "x.html"))
    path = check_board_path(str(tmp_path / "x.html"))
    write_board(_tiny_board(), path)
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["format"] == "ygocombo-board" and data["edges"][0]["label"] == "discards"
    with pytest.raises(ValueError, match="already exists"):
        check_board_path(str(path))
    assert check_board_path(str(path), overwrite=True) == path


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


def test_trace_reads_what_each_card_did(env):
    from ygocombo.board import trace_line
    from ygocombo.deck import load_ydk
    from ygocombo.goldfish import Scenario
    from ygocombo.search import BoardScorer, ComboSearch, Limits

    scenario = Scenario(load_ydk(ROOT / "decks" / "invoked_sample.ydk"), HAND)
    result = ComboSearch(env, scenario, limits=Limits(max_nodes=400, max_seconds=120),
                         scorer=BoardScorer(env, (MECHABA,)), keep=1, stop_at_goal=True).run()
    assert result.reached_goal
    board = trace_line(env, scenario, result.lines[0].responses)

    edges = {(e.source, e.label, e.target) for e in board.edges}
    assert (ALEISTER, "adds to hand", INVOCATION) in edges
    assert (INVOCATION, "fusion summons", MECHABA) in edges
    assert (ALEISTER, "material for", MECHABA) in edges
    verbs = {(e.code, e.verb) for s in board.steps for e in s.events}
    assert (ALEISTER, "Normal Summon") in verbs
    assert any("Invoked Mechaba" in n for n in board.end_board["monster zone"])

    # A line that no longer fits the position is refused, not guessed at.
    from ygocombo.engine import EngineError

    with pytest.raises(EngineError):
        trace_line(env, scenario, (b"\xff\xff\xff\xff",))
