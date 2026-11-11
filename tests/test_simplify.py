"""Tests for dropping pointless moves. Run with:  PYTHONPATH=src python3 -m pytest -q"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MECHABA = 75286621
# Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters
HAND = (86120751, 37742478, 14087893, 97268402, 43898403)


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


@pytest.fixture(scope="module")
def search(env):
    from ygocombo.deck import load_ydk
    from ygocombo.goldfish import Scenario
    from ygocombo.search import BoardScorer, ComboSearch, Limits

    scenario = Scenario(load_ydk(ROOT / "decks" / "invoked_sample.ydk"), HAND)
    return ComboSearch(env, scenario, limits=Limits(max_nodes=400, max_seconds=120),
                       scorer=BoardScorer(env, (MECHABA,)), keep=3)


def test_best_line_has_no_wasted_cards(search):
    line = search.run().lines[0]
    assert line.meets_goal
    assert not any("Twin Twisters" in step or "Book of Moon" in step for step in line.steps)
    assert {"Twin Twisters", "Book of Moon"} <= set(line.board["hand"])


def test_simplify_drops_a_move_that_does_nothing(env, search):
    from ygocombo.search import ComboSearch

    # Without the clean-up, the search keeps a line that uses Book of Moon on
    # our own Invoked Mechaba, which gains nothing.
    raw = ComboSearch(env, search.scenario, limits=search.limits, scorer=search.scorer,
                      keep=10, simplify=False).run()
    padded = next(line for line in raw.lines if line.meets_goal
                  and any("Book of Moon" in step for step in line.steps))
    shorter = search.simplify(padded, [200])
    assert len(shorter.steps) < len(padded.steps)
    assert shorter.score >= padded.score
    assert not any("Book of Moon" in step for step in shorter.steps)


def test_simplify_keeps_a_line_that_is_already_clean(search):
    clean = search.run().lines[0]
    assert search.simplify(clean, [200]).steps == clean.steps
