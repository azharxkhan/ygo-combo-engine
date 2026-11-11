"""Run with:  PYTHONPATH=src python3 -m pytest -q

Engine tests are skipped automatically if vendor/ and build/ are missing.
"""

from pathlib import Path

import pytest

from ygocombo.deck import DeckError, parse_ydk

ROOT = Path(__file__).resolve().parents[1]
ALEISTER, INVOCATION, MECHABA = 86120751, 74063034, 75286621
HAND = (ALEISTER, 37742478, 14087893, 97268402, 43898403)


def test_parse_ydk_sections():
    deck = parse_ydk("#created by x\n#main\n86120751\n#extra\n75286621\n!side\n")
    assert deck.main == [ALEISTER] and deck.extra == [MECHABA] and deck.side == []


@pytest.mark.parametrize("bad", ["#main\nabc", "#main\n12 34", "86120751", "#main\n-5", "#main\n0",
                                 "#main\n99999999999", "#main\n../../etc/passwd"])
def test_parse_ydk_rejects_garbage(bad):
    with pytest.raises(DeckError):
        parse_ydk(bad)


@pytest.fixture(scope="module")
def env():
    from ygocombo.engine import Environment, default_library_path

    if not default_library_path(ROOT).is_file() or not (ROOT / "vendor" / "CardScripts").is_dir():
        pytest.skip("engine not built (run scripts/fetch_vendor.sh and scripts/build_engine.sh)")
    return Environment.from_project_root(ROOT)


@pytest.fixture(scope="module")
def scenario(env):
    from ygocombo.deck import load_ydk, validate_deck
    from ygocombo.goldfish import Scenario

    deck = load_ydk(ROOT / "decks" / "invoked_sample.ydk")
    validate_deck(deck, env.db)
    return Scenario(deck, HAND)


def test_script_reader_refuses_path_tricks(env):
    assert env._load_script_bytes("../../etc/passwd") is None
    assert env._load_script_bytes("constant.lua") is not None


def test_opening_moves(env, scenario):
    from ygocombo.decisions import parse_decision
    from ygocombo.goldfish import new_duel

    with new_duel(env, scenario) as duel:
        duel.advance()
        decision = parse_decision(duel.pending, env.db)
    labels = [c.label for c in decision.choices]
    assert decision.kind == "idle" and decision.player == 0
    assert "Normal Summon Aleister the Invoker (from hand)" in labels
    assert "Go to End Phase" in labels


def test_hand_must_come_from_deck(scenario):
    from ygocombo.goldfish import Scenario

    with pytest.raises(ValueError):
        Scenario(scenario.deck, (MECHABA,))


def test_search_finds_mechaba(env, scenario):
    from ygocombo.search import BoardScorer, ComboSearch, Limits

    result = ComboSearch(
        env, scenario, limits=Limits(max_nodes=150, max_seconds=120),
        scorer=BoardScorer(env, (MECHABA,)), keep=1,
    ).run()
    assert result.lines and result.lines[0].score >= 100
    assert any("Invoked Mechaba" in name for name in result.lines[0].board["monster zone"])


def test_search_does_not_loop_on_cancel(env, scenario):
    """Regression: cancelling the fusion material prompt used to send the search
    back to the fusion monster prompt forever, so it never reached Mechaba."""
    from ygocombo.goldfish import Scenario
    from ygocombo.search import BoardScorer, ComboSearch, Limits

    # Terraforming, Ash Blossom, Effect Veiler, Nibiru, Infinite Impermanence
    hand = (73628505, 14558127, 97268402, 27204311, 10045474)
    result = ComboSearch(
        env, Scenario(scenario.deck, hand), limits=Limits(max_nodes=2000, max_seconds=120),
        scorer=BoardScorer(env, (MECHABA,)), keep=1,
    ).run()
    assert result.stopped_by == "finished"
    assert any("Invoked Mechaba" in name for name in result.lines[0].board["monster zone"])


def test_limits_are_validated():
    from ygocombo.search import Limits

    for bad in ({"max_nodes": 0}, {"max_nodes": 10**9}, {"max_depth": 0}, {"max_seconds": -1}):
        with pytest.raises(ValueError):
            Limits(**bad)
