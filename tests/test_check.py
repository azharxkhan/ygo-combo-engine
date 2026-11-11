"""Tests for "is this combo possible?". Run with:  PYTHONPATH=src python3 -m pytest -q"""

from pathlib import Path

import pytest

from ygocombo.check import parse_steps

ROOT = Path(__file__).resolve().parents[1]
MECHABA = 75286621
# Aleister the Invoker, Honest, Book of Moon, Effect Veiler, Twin Twisters
HAND = (86120751, 37742478, 14087893, 97268402, 43898403)
# Ghost Ogre & Snow Rabbit, Nibiru, Honest, Effect Veiler x2: no way to reach Aleister
BRICK = (59438930, 27204311, 37742478, 97268402, 97268402)
GOOD_LINE = """
# the basic Mechaba line
1. Normal Summon Aleister the Invoker (from hand)
2. Use effect of Aleister the Invoker
3. Select: Invocation (deck)
4. Activate Invocation (in hand)
5. Select: Invoked Mechaba (extra deck)
6. Select Honest (hand)
7. Select Aleister the Invoker (monster zone)
"""


def test_parse_steps():
    steps = parse_steps(GOOD_LINE)
    assert len(steps) == 7 and steps[0] == "Normal Summon Aleister the Invoker (from hand)"
    assert parse_steps("2) Yes\n\n3.No") == ["Yes", "No"]
    for bad in ("", "# only a comment", "x" * 300):
        with pytest.raises(ValueError):
            parse_steps(bad)


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


def _scenario(hand):
    from ygocombo.deck import load_ydk
    from ygocombo.goldfish import Scenario

    return Scenario(load_ydk(ROOT / "decks" / "invoked_sample.ydk"), hand)


def test_can_make_possible(env):
    from ygocombo.check import can_make

    made = can_make(env, _scenario(HAND), (MECHABA,))
    assert made.verdict == "possible" and made.line.meets_goal


def test_can_make_not_possible(env):
    from ygocombo.check import can_make

    made = can_make(env, _scenario(BRICK), (MECHABA,))
    assert made.verdict == "not found" and made.missing == ("Invoked Mechaba",)


def test_written_line_that_works(env):
    from ygocombo.check import check_line

    result = check_line(env, _scenario(HAND), parse_steps(GOOD_LINE), (MECHABA,))
    assert result.ok and result.meets_goal and result.steps_done == 7


def test_written_line_that_breaks(env):
    from ygocombo.check import check_line

    steps = ["Normal Summon Aleister the Invoker (from hand)", "Activate Invocation (in hand)"]
    result = check_line(env, _scenario(HAND), steps, (MECHABA,))
    assert not result.ok and result.steps_done == 1
    assert "step 2" in result.error and "Go to End Phase" in result.legal
    assert any("Aleister" in d for d in result.declined)  # its search was skipped
    # A near-miss spelling is not guessed at.
    near = check_line(env, _scenario(HAND), ["Normal summon Aleister the Invoker (from hand)"])
    assert not near.ok and near.steps_done == 0
